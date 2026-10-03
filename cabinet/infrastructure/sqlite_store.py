"""SQLite persistence for the standalone cabinet application."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta

from cabinet.config import app_path

DB_PATH = app_path(os.getenv("CABINET_DB_PATH"), "var/cabinet.sqlite3")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and not DB_PATH.exists():
        try:
            descriptor = os.open(DB_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            pass
    # Fail promptly on a stuck writer instead of holding an API worker for 20 seconds.
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    if os.name == "posix":
        os.chmod(DB_PATH, 0o600)
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize() -> None:
    with closing(connect()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            username TEXT COLLATE NOCASE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            student_id INTEGER NOT NULL REFERENCES students(id),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            admin_id INTEGER NOT NULL REFERENCES admin_accounts(id),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manifest_versions (
            version INTEGER PRIMARY KEY AUTOINCREMENT,
            manifest_json TEXT NOT NULL,
            published_at TEXT NOT NULL,
            actor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reference_cache (
            manifest_version INTEGER NOT NULL REFERENCES manifest_versions(version),
            homework TEXT NOT NULL, task TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY(manifest_version, homework, task)
        );
        CREATE TABLE IF NOT EXISTS drafts (
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT NOT NULL, task TEXT NOT NULL, sql TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(student_id, homework, task)
        );
        CREATE TABLE IF NOT EXISTS query_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT, task TEXT, kind TEXT NOT NULL,
            sql TEXT NOT NULL, status TEXT NOT NULL, elapsed_ms INTEGER,
            row_count INTEGER, message TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id),
            query_run_id INTEGER REFERENCES query_runs(id),
            homework TEXT NOT NULL, task TEXT NOT NULL, sql TEXT NOT NULL,
            points TEXT NOT NULL, verdict TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manual_grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT NOT NULL, task TEXT NOT NULL, points TEXT NOT NULL,
            comment TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT NOT NULL, task TEXT NOT NULL, points TEXT NOT NULL,
            attempt_id INTEGER REFERENCES attempts(id),
            manual_grade_id INTEGER REFERENCES manual_grades(id),
            created_at TEXT NOT NULL,
            CHECK ((attempt_id IS NULL) != (manual_grade_id IS NULL))
        );
        CREATE TABLE IF NOT EXISTS deadline_overrides (
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT NOT NULL, actor TEXT NOT NULL, comment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(student_id, homework)
        );
        CREATE TABLE IF NOT EXISTS recheck_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, homework TEXT,
            manifest_version INTEGER NOT NULL REFERENCES manifest_versions(version),
            status TEXT NOT NULL, total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
            summary_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recheck_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES recheck_jobs(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            homework TEXT NOT NULL, task TEXT NOT NULL,
            attempt_id INTEGER NOT NULL REFERENCES attempts(id),
            verdict TEXT NOT NULL, points TEXT NOT NULL, message TEXT NOT NULL,
            elapsed_ms INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL, student_id INTEGER REFERENCES students(id),
            action TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_attempts_lookup ON attempts(student_id, homework, task, id);
        CREATE INDEX IF NOT EXISTS ix_achievements_lookup ON achievements(student_id, homework, task);
        CREATE INDEX IF NOT EXISTS ix_runs_student ON query_runs(student_id, id DESC);
        CREATE INDEX IF NOT EXISTS ix_audit_student ON audit(student_id, id DESC);
        CREATE INDEX IF NOT EXISTS ix_audit_action ON audit(action, id DESC);
        CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit(actor, id DESC);
        CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS achievements_no_update BEFORE UPDATE ON achievements BEGIN SELECT RAISE(ABORT, 'achievements are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS achievements_no_delete BEFORE DELETE ON achievements BEGIN SELECT RAISE(ABORT, 'achievements are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS manual_grades_no_update BEFORE UPDATE ON manual_grades BEGIN SELECT RAISE(ABORT, 'manual_grades are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS manual_grades_no_delete BEFORE DELETE ON manual_grades BEGIN SELECT RAISE(ABORT, 'manual_grades are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS query_runs_no_update BEFORE UPDATE ON query_runs BEGIN SELECT RAISE(ABORT, 'query_runs are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS query_runs_no_delete BEFORE DELETE ON query_runs BEGIN SELECT RAISE(ABORT, 'query_runs are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS recheck_results_no_update BEFORE UPDATE ON recheck_results BEGIN SELECT RAISE(ABORT, 'recheck_results are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS recheck_results_no_delete BEFORE DELETE ON recheck_results BEGIN SELECT RAISE(ABORT, 'recheck_results are append-only'); END;
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(students)")}
        if "username" not in columns:
            db.execute("ALTER TABLE students ADD COLUMN username TEXT COLLATE NOCASE")
        if "is_assistant" not in columns:
            db.execute("ALTER TABLE students ADD COLUMN is_assistant INTEGER NOT NULL DEFAULT 0")
        if "is_superadmin" not in columns:
            db.execute("ALTER TABLE students ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE students SET username=email WHERE username IS NULL")
        for account in db.execute(
            "SELECT username,full_name,password_hash,active,created_at FROM admin_accounts"
        ):
            if not account["active"]:
                continue
            existing = db.execute(
                "SELECT id FROM students WHERE username=? COLLATE NOCASE",
                (account["username"],),
            ).fetchone()
            if existing:
                continue
            db.execute(
                "INSERT INTO students(email,username,full_name,password_hash,created_at,is_assistant,is_superadmin) VALUES(?,?,?,?,?,1,0)",
                (
                    f"{account['username'].lower()}@staff.cabinet.local",
                    account["username"],
                    account["full_name"],
                    account["password_hash"],
                    account["created_at"],
                ),
            )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_students_username ON students(username COLLATE NOCASE)"
        )
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(query_runs)")}
        if "result_json" not in run_columns:
            db.execute("ALTER TABLE query_runs ADD COLUMN result_json TEXT NOT NULL DEFAULT ''")
        db.execute(
            "UPDATE recheck_jobs SET status='failed', error=?, updated_at=? WHERE status IN ('queued', 'running')",
            ("Прервано перезапуском", now_iso()),
        )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        rounds_value = int(rounds)
        salt_bytes = bytes.fromhex(salt)
        expected_bytes = bytes.fromhex(expected)
        if (
            algorithm != "pbkdf2_sha256"
            or not 100_000 <= rounds_value <= 1_000_000
            or len(salt_bytes) != 16
            or len(expected_bytes) != hashlib.sha256().digest_size
        ):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_bytes, rounds_value)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def issue_session(conn: sqlite3.Connection, student_id: int) -> str:
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)", (token_hash, student_id, expires, now_iso())
    )
    return token


def find_session(token: str) -> sqlite3.Row | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with closing(connect()) as db:
        return db.execute(
            """SELECT s.id, s.email, COALESCE(s.username,s.email) AS username, s.full_name,
          s.is_assistant, s.is_superadmin FROM sessions x
          JOIN students s ON s.id=x.student_id WHERE x.token_hash=? AND x.expires_at>?""",
            (token_hash, now_iso()),
        ).fetchone()


def issue_admin_session(conn: sqlite3.Connection, admin_id: int) -> str:
    token = secrets.token_urlsafe(36)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    conn.execute(
        "INSERT INTO admin_sessions VALUES (?, ?, ?, ?)",
        (token_hash, admin_id, expires, now_iso()),
    )
    return token


def find_admin_session(token: str) -> sqlite3.Row | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with closing(connect()) as db:
        return db.execute(
            "SELECT a.id,a.username,a.full_name FROM admin_sessions x "
            "JOIN admin_accounts a ON a.id=x.admin_id "
            "WHERE x.token_hash=? AND x.expires_at>? AND a.active=1",
            (token_hash, now_iso()),
        ).fetchone()


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    details: str = "{}",
    student_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit(actor,student_id,action,details_json,created_at) VALUES(?,?,?,?,?)",
        (actor, student_id, action, details, now_iso()),
    )
