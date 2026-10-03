import os
import sqlite3
from threading import Thread

import pytest

from cabinet.backups import create_backup, restore_backup
from cabinet.infrastructure import sqlite_store as store


def test_password_hashing_and_session_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    store.initialize()
    encoded = store.hash_password("long-example-password")
    assert store.verify_password("long-example-password", encoded)
    assert not store.verify_password("incorrect-password", encoded)
    algorithm, rounds, salt, digest = encoded.split("$")
    assert not store.verify_password(
        "long-example-password", f"{algorithm}$999999999${salt}${digest}"
    )
    with store.transaction() as db:
        cur = db.execute(
            "INSERT INTO students(email,full_name,password_hash,created_at) VALUES(?,?,?,?)",
            ("student@example.edu", "Student Example", encoded, store.now_iso()),
        )
        token = store.issue_session(db, cur.lastrowid)
    assert store.find_session(token)["email"] == "student@example.edu"
    with store.closing(store.connect()) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    if os.name == "posix":
        assert (tmp_path / "cabinet.sqlite3").stat().st_mode & 0o777 == 0o600


def test_initialize_migrates_existing_email_to_login(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(store, "DB_PATH", path)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE students (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE query_runs (id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL, homework TEXT, task TEXT, kind TEXT NOT NULL, sql TEXT NOT NULL, status TEXT NOT NULL, elapsed_ms INTEGER, row_count INTEGER, message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO students(email,full_name,password_hash,created_at) VALUES(?,?,?,?)",
            ("legacy@example.edu", "Legacy Student", "hash", store.now_iso()),
        )
    store.initialize()
    with store.closing(store.connect()) as db:
        assert (
            db.execute("SELECT username FROM students WHERE id=1").fetchone()[0]
            == "legacy@example.edu"
        )
        assert "result_json" in {
            row[1] for row in db.execute("PRAGMA table_info(query_runs)")
        }


def test_attempts_and_audit_are_append_only(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    store.initialize()
    with store.transaction() as db:
        cur = db.execute(
            "INSERT INTO students(email,full_name,password_hash,created_at) VALUES(?,?,?,?)",
            ("student@example.edu", "Student Example", "x", store.now_iso()),
        )
        db.execute(
            "INSERT INTO attempts(student_id,homework,task,sql,points,verdict,message,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cur.lastrowid, "hw1", "Q01", "SELECT 1", "0", "WRONG", "wrong", store.now_iso()),
        )
        db.execute(
            "INSERT INTO audit(actor,student_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
            ("student@example.edu", cur.lastrowid, "login", "{}", store.now_iso()),
        )
    with pytest.raises(Exception, match="append-only"), store.transaction() as db:
        db.execute("DELETE FROM attempts")
    with pytest.raises(Exception, match="append-only"), store.transaction() as db:
        db.execute("UPDATE audit SET action='changed'")


def test_concurrent_transactions_all_commit(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    store.initialize()
    writers = 64
    errors: list[BaseException] = []

    def write(number: int) -> None:
        try:
            with store.transaction() as db:
                store.audit(db, "load", "burst", str(number))
        except BaseException as exc:
            errors.append(exc)

    threads = [Thread(target=write, args=(number,)) for number in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with store.closing(store.connect()) as db:
        saved = db.execute("SELECT COUNT(*) FROM audit WHERE action='burst'").fetchone()[0]
    assert saved == writers


def test_initialize_keeps_roles_and_closes_interrupted_rechecks(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    store.initialize()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO students(email,username,full_name,password_hash,created_at,is_assistant) VALUES(?,?,?,?,?,0)",
            ("student@example.edu", "student", "Student", "hash", store.now_iso()),
        )
        db.execute(
            "INSERT INTO admin_accounts(username,full_name,password_hash,active,created_at) VALUES(?,?,?,1,?)",
            ("student", "Old Staff", "hash", store.now_iso()),
        )
        db.execute(
            "INSERT INTO admin_accounts(username,full_name,password_hash,active,created_at) VALUES(?,?,?,1,?)",
            ("newstaff", "New Staff", "hash", store.now_iso()),
        )
        version = db.execute(
            "INSERT INTO manifest_versions(manifest_json,published_at,actor) VALUES('{}',?,?)",
            (store.now_iso(), "tester"),
        ).lastrowid
        db.execute(
            "INSERT INTO recheck_jobs(actor,homework,manifest_version,status,total,created_at,updated_at) VALUES(?,?,?,'running',1,?,?)",
            ("tester", "hw1", version, store.now_iso(), store.now_iso()),
        )
    store.initialize()
    with store.closing(store.connect()) as db:
        student = db.execute(
            "SELECT is_assistant FROM students WHERE username='student'"
        ).fetchone()
        staff = db.execute(
            "SELECT is_assistant FROM students WHERE username='newstaff'"
        ).fetchone()
        job = db.execute("SELECT status, error FROM recheck_jobs").fetchone()
    assert student["is_assistant"] == 0
    assert staff["is_assistant"] == 1
    assert job["status"] == "failed"
    assert job["error"] == "Прервано перезапуском"
    with store.transaction() as db:
        db.execute("UPDATE students SET is_assistant=0 WHERE username='newstaff'")
    store.initialize()
    with store.closing(store.connect()) as db:
        assert (
            db.execute("SELECT is_assistant FROM students WHERE username='newstaff'").fetchone()[
                "is_assistant"
            ]
            == 0
        )


def test_backup_restore_round_trip_and_integrity(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "source.sqlite3")
    store.initialize()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO students(email,full_name,password_hash,created_at) VALUES(?,?,?,?)",
            ("restore@example.edu", "Restore Test", "hash", store.now_iso()),
        )
        store.audit(db, "restore-test", "backup_test")
    backup = create_backup(tmp_path / "backups")
    restored = restore_backup(backup, tmp_path / "restored.sqlite3")
    import sqlite3

    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT email FROM students").fetchone()[0] == "restore@example.edu"
        assert db.execute("SELECT action FROM audit").fetchone()[0] == "backup_test"
    if os.name == "posix":
        assert backup.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700
        assert restored.stat().st_mode & 0o777 == 0o600
