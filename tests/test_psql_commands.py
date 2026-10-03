import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cabinet import api
from cabinet.domains.psql_commands import PsqlCommandError, parse_psql_command
from cabinet.infrastructure import sqlite_store as store


def test_psql_command_parser_allows_only_one_read_only_command():
    assert parse_psql_command("\\d+").detailed is True
    assert parse_psql_command("\\dt bookings.*").pattern == "bookings.*"
    assert parse_psql_command("\\dn").name == "dn"
    assert parse_psql_command("SELECT 1") is None
    for source in ("\\q", "\\d airplanes; SELECT 1", "\\d airplanes extra"):
        with pytest.raises(PsqlCommandError):
            parse_psql_command(source)


def test_psql_commands_run_read_only_and_are_saved_in_history(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    calls = []

    def fake_execute(sql, *, row_limit=200, params=None):
        calls.append((sql, params))
        if "pg_catalog.pg_attribute a" in sql:
            return (
                ["position", "column", "type", "nullable", "default", "description"],
                [["1", "airplane_code", "character varying(3)", "not null", None, None],
                 ["2", "range", "integer", "", None, "Flight range"]],
                3,
                2,
            )
        if "pg_catalog.pg_class c" in sql:
            return (
                ["schema", "name", "kind", "owner", "size", "description"],
                [["bookings", "airplanes", "r", "student", "8 kB", "Aircraft list"]],
                2,
                1,
            )
        raise AssertionError(f"Unexpected metadata query: {sql}")

    monkeypatch.setattr(api, "execute_readonly", fake_execute)
    with TestClient(api.app) as client:
        registration = client.post(
            "/api/auth/register",
            json={"username": "psql_user", "full_name": "Psql User", "password": "test-password-123"},
        )
        headers = {"Authorization": f"Bearer {registration.json()['token']}"}
        described = client.post("/api/run", headers=headers, json={"sql": "\\d+ airplanes"})
        assert described.status_code == 200
        assert described.json()["status"] == "ok"
        assert described.json()["columns"] == ["position", "column", "type", "nullable", "default", "description"]
        assert described.json()["rows"][1][1] == "range"
        assert calls[0][1] == (["r", "p", "v", "m", "f", "S", "i", "I"],)
        assert calls[1][1] == ("bookings", "airplanes")

        listed = client.post("/api/run", headers=headers, json={"sql": "\\dt"}).json()
        assert listed["status"] == "ok"
        assert listed["rows"] == [["bookings", "airplanes", "table", "student"]]

        formatted = client.post("/api/format", headers=headers, json={"sql": "\\d airplanes"})
        assert formatted.status_code == 200
        assert formatted.json()["sql"] == "\\d airplanes"
        check = client.post(
            "/api/homeworks/hw1/tasks/Q01/check",
            headers=headers,
            json={"sql": "\\d airplanes"},
        )
        assert check.status_code == 422
        assert "не создают попытку" in check.json()["detail"]
        task_state = client.get(
            "/api/homeworks/hw1/tasks/Q01", headers=headers
        )
        assert task_state.status_code == 200
        assert task_state.json()["attempts"] == []
        unsupported = client.post("/api/run", headers=headers, json={"sql": "\\q"}).json()
        assert unsupported["status"] == "error"
        history = client.get("/api/sandbox/history", headers=headers).json()
        assert [item["sql"] for item in history] == ["\\q", "\\dt", "\\d+ airplanes"]

    verdict, points, message, *_ = api.evaluate_task(
        "hw1", SimpleNamespace(id="Q01", required=[], points=1), None, "\\d airplanes"
    )
    assert verdict == "FORBID" and points == 0
    assert "SELECT" in message


def test_catalog_queries_keep_real_placeholders(monkeypatch):
    statements = []

    class Cursor:
        description = [SimpleNamespace(name="schema")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            statements.append((sql, params))

        def fetchmany(self, _count):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    def connect(_dsn, **_kwargs):
        return Connection()

    monkeypatch.setenv("CABINET_LEARNING_DSN", "postgresql://readonly@example.test/demo")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    api.execute_readonly(
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
        params=(["bookings"],),
    )
    assert statements[-1] == (
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)",
        (["bookings"],),
    )


def test_execution_uses_transaction_timeouts_without_startup_options(monkeypatch):
    statements = []
    connection_options = {}

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            statements.append((sql, params))

        def fetchmany(self, _count):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    def connect(_dsn, **kwargs):
        connection_options.update(kwargs)
        return Connection()

    monkeypatch.setenv("CABINET_LEARNING_DSN", "postgresql://readonly@example.test/demo")
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    api.execute_readonly("SELECT passenger_name FROM tickets WHERE passenger_name ILIKE 'My%'")
    assert statements[-1] == ("SELECT passenger_name FROM tickets WHERE passenger_name ILIKE 'My%%'", ())
    statements.clear()
    api.execute_readonly("SELECT 1")
    assert statements[:3] == [
        ("SET TRANSACTION READ ONLY", ()),
        ("SET LOCAL statement_timeout = '15s'", ()),
        ("SET LOCAL lock_timeout = '3s'", ()),
    ]
    assert connection_options["connect_timeout"] == 5
    assert "options" not in connection_options


def test_learning_query_admission_times_out_when_all_slots_are_busy(monkeypatch):
    class BusySlots:
        def acquire(self, timeout):
            assert timeout == api.LEARNING_QUERY_WAIT_SECONDS
            return False

        def release(self):
            raise AssertionError("the semaphore must not be released when it was not acquired")

    monkeypatch.setattr(api, "_LEARNING_QUERY_SLOTS", BusySlots())
    with pytest.raises(TimeoutError, match="Сейчас слишком много запросов"):
        api.execute_readonly("SELECT 1")


def test_long_running_query_timeout_is_a_saved_error(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")

    class QueryTimedOut(Exception):
        sqlstate = "57014"

    def timeout(_sql, *, row_limit=200, params=None):
        raise QueryTimedOut("canceling statement due to statement timeout")

    monkeypatch.setattr(api, "execute_readonly", timeout)
    with TestClient(api.app) as client:
        registration = client.post(
            "/api/auth/register",
            json={"username": "slow_query", "full_name": "Slow Query", "password": "test-password-123"},
        )
        headers = {"Authorization": f"Bearer {registration.json()['token']}"}
        response = client.post(
            "/api/run", headers=headers, json={"sql": "SELECT pg_sleep(30)"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "error"
        assert "15 секунд" in response.json()["message"]
        saved = client.get("/api/sandbox/history", headers=headers).json()
        assert saved[0]["sql"] == "SELECT pg_sleep(30)"
        assert saved[0]["status"] == "error"
