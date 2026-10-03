"""Shared API services and application assembly."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated

import anyio
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cabinet.config import manifest_path
from cabinet.domains.manifests import validate_manifest
from cabinet.domains.psql_commands import (
    PsqlCommand,
    PsqlCommandError,
    matches_pattern,
    parse_psql_command,
    relation_kinds,
)
from cabinet.domains.scoring import (
    grading_open,
    parse_deadline,
)
from cabinet.domains.sql_safety import inspect_sql
from cabinet.infrastructure import sqlite_store as store
from cabinet.routers import admin, auth, coursework, queries, scoring, system

# The learning cluster accepts hundreds of sessions. This process uses a
# smaller slice: 32 queries run at once, and a request waits up to 20 seconds
# for a free slot. The request thread pool is larger, so pages and logins
# still have threads while those queries are running.
LEARNING_QUERY_SLOTS = 32
LEARNING_QUERY_WAIT_SECONDS = 20
REQUEST_THREADS = 80


@asynccontextmanager
async def lifespan(_app):
    store.initialize()
    anyio.to_thread.current_default_thread_limiter().total_tokens = REQUEST_THREADS
    yield


app = FastAPI(title="SQL Course Cabinet", version="0.2.0", lifespan=lifespan)
MANIFEST_FILE = manifest_path(os.getenv("CABINET_MANIFEST"))
MAX_REQUEST_BYTES = 25_000_000


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "Запрос превышает лимит 25 МБ"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Некорректный Content-Length"})
    return await call_next(request)

# Route groups live in cabinet.routers; shared dependencies and domain services are
# defined here so handlers use one consistent authentication and storage layer.
system_router = system.router
auth_router = auth.router
coursework_router = coursework.router
query_router = queries.router
admin_router = admin.router
score_router = scoring.router

STRUCTURE_PATTERNS = {
    "cte": r"\bwith\s+[\w]+\s+as\s*\(",
    "subquery": r"\(\s*select\b",
    "subquery_from": r"\bfrom\s*\(\s*select\b",
    "join": r"\bjoin\b",
    "inner_join": r"\binner\s+join\b",
    "left_join": r"\bleft\s+(?:outer\s+)?join\b",
    "right_join": r"\bright\s+(?:outer\s+)?join\b",
    "full_join": r"\bfull\s+(?:outer\s+)?join\b",
    "cross_join": r"\bcross\s+join\b",
    "exists": r"\bexists\s*\(",
    "is_null": r"\bis\s+null\b",
    "using": r"\busing\s*\(",
    "except": r"\bexcept(?:\s+all)?\b",
    "except_all": r"\bexcept\s+all\b",
    "except_distinct": r"\bexcept\b(?!\s+all\b)",
    "intersect_distinct": r"\bintersect\b(?!\s+all\b)",
    "intersect_all": r"\bintersect\s+all\b",
    "union_distinct": r"\bunion\b(?!\s+all\b)",
    "union_all": r"\bunion\s+all\b",
    "two_joins": r"(?:\bjoin\b[\s\S]*){2}",
    "two_left_joins": r"(?:\bleft\s+(?:outer\s+)?join\b[\s\S]*){2}",
    "three_inner_joins": r"(?:\binner\s+join\b[\s\S]*){3}",
    "over": r"\bover\s*(?:\(|[a-z_])",
    "partition_by": r"\bpartition\s+by\b",
    "rows_between": r"\brows\s+between\b",
    "row_number": r"\brow_number\s*\(",
    "rank": r"\brank\s*\(",
    "dense_rank": r"\bdense_rank\s*\(",
    "lag": r"\blag\s*\(",
    "first_value": r"\bfirst_value\s*\(",
    "last_value": r"\blast_value\s*\(",
    "ntile": r"\bntile\s*\(",
    "percent_rank": r"\bpercent_rank\s*\(",
    "cume_dist": r"\bcume_dist\s*\(",
    "avg_over": r"\bavg\s*\([^)]*\)\s*over\b",
}

STRUCTURE_LABELS = {
    "cte": "WITH ... AS (...)",
    "subquery": "подзапрос (SELECT ...) в скобках",
    "subquery_from": "подзапрос в FROM",
    "join": "JOIN",
    "inner_join": "INNER JOIN",
    "left_join": "LEFT JOIN",
    "right_join": "RIGHT JOIN",
    "full_join": "FULL JOIN",
    "cross_join": "CROSS JOIN",
    "exists": "EXISTS",
    "is_null": "IS NULL",
    "using": "USING (...)",
    "except": "EXCEPT",
    "except_all": "EXCEPT ALL",
    "except_distinct": "EXCEPT без ALL",
    "intersect_distinct": "INTERSECT без ALL",
    "intersect_all": "INTERSECT ALL",
    "union_distinct": "UNION без ALL",
    "union_all": "UNION ALL",
    "two_joins": "два JOIN",
    "two_left_joins": "два LEFT JOIN",
    "three_inner_joins": "три INNER JOIN",
    "over": "OVER",
    "partition_by": "PARTITION BY",
    "rows_between": "ROWS BETWEEN",
    "row_number": "ROW_NUMBER",
    "rank": "RANK",
    "dense_rank": "DENSE_RANK",
    "lag": "LAG",
    "first_value": "FIRST_VALUE",
    "last_value": "LAST_VALUE",
    "ntile": "NTILE",
    "percent_rank": "PERCENT_RANK",
    "cume_dist": "CUME_DIST",
    "avg_over": "AVG(...) OVER",
}
_REFERENCE_RESULTS: dict[str, dict] = {}
_REFERENCE_RESULTS_LOCK = threading.Lock()
_RECHECK_LOCK = threading.Lock()
_LEARNING_QUERY_SLOTS = threading.BoundedSemaphore(LEARNING_QUERY_SLOTS)


def get_reference_result(hw_id: str, task, version: int | None) -> dict:
    if version is not None:
        with store.closing(store.connect()) as db:
            row = db.execute(
                "SELECT result_json FROM reference_cache WHERE manifest_version=? AND homework=? AND task=?",
                (version, hw_id, task.id),
            ).fetchone()
        if not row:
            raise ValueError(
                "Для задачи нет снимка правильного результата. Попросите преподавателя перепубликовать ДЗ."
            )
        return json.loads(row[0])

    # The bundled course manifest is usable before a teacher publishes a version.
    # Its trusted reference query is run read-only and cached for this app process.
    key = hashlib.sha256(f"{hw_id}:{task.id}:{task.reference_sql}".encode()).hexdigest()
    with _REFERENCE_RESULTS_LOCK:
        cached = _REFERENCE_RESULTS.get(key)
    if cached is not None:
        return cached
    columns, rows, _, _ = execute_readonly(task.reference_sql, row_limit=100_000)
    result = {"columns": columns, "rows": rows}
    with _REFERENCE_RESULTS_LOCK:
        return _REFERENCE_RESULTS.setdefault(key, result)


class PreviewRequest(BaseModel):
    manifest: dict


class AuthInput(BaseModel):
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class RegisterInput(AuthInput):
    full_name: str = Field(min_length=2, max_length=200)


class ProfileInput(BaseModel):
    full_name: str = Field(min_length=2, max_length=200)


class PasswordChangeInput(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class ManualGradeInput(BaseModel):
    task: str
    points: Decimal
    comment: str = Field(min_length=3, max_length=2000)


class SqlInput(BaseModel):
    homework: str | None = None
    task: str | None = None
    sql: str = Field(max_length=100_000)


class PublishInput(BaseModel):
    manifest: dict


class LegacyZipOptions(BaseModel):
    timezone: str = "Europe/Moscow"
    hard_deadline: str
    soft_deadline: str


class AchievementInput(BaseModel):
    student_email: str = Field(max_length=320)
    homework: str = Field(max_length=100)
    task: str = Field(max_length=100)
    points: Decimal
    achieved_at: datetime
    soft_deadline: datetime


class ScorePreviewRequest(BaseModel):
    achievements: list[AchievementInput] = Field(max_length=10_000)
    penalty: dict = Field(default_factory=dict)


class AdminLoginInput(BaseModel):
    username: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class AdminCreateInput(BaseModel):
    username: str = Field(min_length=4, max_length=32)
    full_name: str = Field(min_length=2, max_length=200)


class AdminActiveInput(BaseModel):
    active: bool


class RoleInput(BaseModel):
    is_assistant: bool | None = None
    is_superadmin: bool | None = None


class SuperadminPhraseInput(BaseModel):
    phrase: str = Field(min_length=1, max_length=512)


class AdministratorLoginInput(AuthInput):
    phrase: str = Field(min_length=1, max_length=512)


def public_user(user) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "full_name": user["full_name"],
        "is_assistant": bool(user["is_assistant"]),
        "is_superadmin": bool(user["is_superadmin"]),
    }


def session_user(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return store.find_session(authorization[7:])


def student(authorization: Annotated[str | None, Header()] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход")
    row = store.find_session(authorization[7:])
    if not row:
        raise HTTPException(401, "Сессия истекла")
    return row


def _token_actor(x_admin_token: str | None) -> str | None:
    expected = os.getenv("CABINET_ADMIN_TOKEN", "")
    if expected and x_admin_token and hmac.compare_digest(expected, x_admin_token):
        return os.getenv("CABINET_ADMIN_NAME", "Администратор")
    return None


def admin(
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header()] = None,
    x_admin_session: Annotated[str | None, Header()] = None,
):
    actor = _token_actor(x_admin_token)
    if actor:
        return actor
    user = session_user(authorization)
    if user and (user["is_assistant"] or user["is_superadmin"]):
        return user["username"]
    if x_admin_session:
        account = store.find_admin_session(x_admin_session)
        if account:
            return account["username"]
    raise HTTPException(403, "Действие доступно преподавателю")


def super_admin(
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header()] = None,
):
    actor = _token_actor(x_admin_token)
    if actor:
        return actor
    user = session_user(authorization)
    if user and user["is_superadmin"]:
        return user["username"]
    raise HTTPException(403, "Ключ доступа не подошёл")


def record_audit(actor: str, action: str, details: dict, student_id: int | None = None) -> None:
    with store.transaction() as db:
        store.audit(
            db,
            actor,
            action,
            json.dumps(details, ensure_ascii=False),
            student_id,
        )


def active_manifest():
    with store.closing(store.connect()) as db:
        row = db.execute(
            "SELECT version,manifest_json FROM manifest_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    if row:
        raw, version = json.loads(row["manifest_json"]), row["version"]
    elif MANIFEST_FILE.is_file():
        raw, version = json.loads(MANIFEST_FILE.read_text(encoding="utf-8")), None
    else:
        raise HTTPException(503, "Преподаватель ещё не опубликовал манифест")
    try:
        return raw, validate_manifest(raw), version
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, f"Опубликованный манифест некорректен: {exc}") from exc


def task_for(hw_id: str, task_id: str):
    _, manifest, _ = active_manifest()
    hw = next((x for x in manifest.homeworks if x.id == hw_id), None)
    task = next((x for x in hw.tasks if x.id == task_id), None) if hw else None
    if not task:
        raise HTTPException(404, "Задача не найдена")
    return manifest, hw, task


def public_task(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "statement": task.statement,
        "points": str(task.points),
        "required": list(task.required),
    }


def manual_grade_locks(db, student_id: int, homework_id: str, task_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM manual_grades WHERE student_id=? AND homework=? AND task=? LIMIT 1",
            (student_id, homework_id, task_id),
        ).fetchone()
        is not None
    )


def score_from_records(
    manual: tuple[Decimal, str] | None,
    achievements: list[tuple[Decimal, str]],
) -> Decimal:
    """Latest manual grade replaces earlier results; a later higher achievement counts."""
    if manual is None:
        return max((points for points, _created_at in achievements), default=Decimal(0))
    manual_points, manual_at = manual
    later = [points for points, created_at in achievements if created_at > manual_at]
    return max([manual_points, *later])


def highest_raw_points(db, student_id: int, homework_id: str, task_id: str) -> Decimal:
    manual = db.execute(
        "SELECT points, created_at FROM manual_grades WHERE student_id=? AND homework=? AND task=? ORDER BY id DESC LIMIT 1",
        (student_id, homework_id, task_id),
    ).fetchone()
    achievements = db.execute(
        "SELECT points, created_at FROM achievements WHERE student_id=? AND homework=? AND task=?",
        (student_id, homework_id, task_id),
    ).fetchall()
    return score_from_records(
        (Decimal(manual["points"]), manual["created_at"]) if manual else None,
        [(Decimal(row["points"]), row["created_at"]) for row in achievements],
    )
















































def execute_readonly(sql: str, *, row_limit: int = 200, params=None):
    if not _LEARNING_QUERY_SLOTS.acquire(timeout=LEARNING_QUERY_WAIT_SECONDS):
        raise TimeoutError("Сейчас слишком много запросов. Подождите немного и нажмите ещё раз.")
    try:
        return _execute_readonly(sql, row_limit=row_limit, params=params)
    finally:
        _LEARNING_QUERY_SLOTS.release()


def _execute_readonly(sql: str, *, row_limit: int = 200, params=None):
    code, error = inspect_sql(sql)
    if error:
        raise ValueError(error)
    dsn = os.getenv("CABINET_LEARNING_DSN", "")
    if not dsn:
        raise RuntimeError("Не настроено учебное подключение CABINET_LEARNING_DSN")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Установите зависимости проекта (psycopg)") from exc
    started = time.monotonic()
    with psycopg.connect(
        dsn,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '15s'")
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute("SET LOCAL search_path TO bookings, public")
            cursor.execute("SET LOCAL TIME ZONE 'Europe/Moscow'")
            if params:
                cursor.execute(sql, params)
            else:
                # Student and reference SQL may contain literal % (LIKE 'My%').
                # psycopg treats a single % as a placeholder unless it is doubled.
                cursor.execute(sql.replace("%", "%%"))
            columns = [d.name for d in cursor.description or []]
            rows = []
            result_bytes = 0
            while columns and len(rows) <= row_limit:
                batch = cursor.fetchmany(min(1000, row_limit + 1 - len(rows)))
                if not batch:
                    break
                for row in batch:
                    values = [str(value) if value is not None else None for value in row]
                    result_bytes += sum(len(value.encode("utf-8")) for value in values if value is not None)
                    if result_bytes > 20_000_000:
                        raise ValueError("Результат превышает лимит передачи данных 20 МБ")
                    rows.append(values)
            if len(rows) > row_limit:
                raise ValueError(f"Результат превышает лимит {row_limit} строк")
            count = len(rows)
    return (
        columns,
        [[str(v) if v is not None else None for v in row] for row in rows],
        int((time.monotonic() - started) * 1000),
        count,
    )


def execution_error_message(exc: Exception) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "57014":
        return "Запрос остановлен: превышено время выполнения (15 секунд)."
    if sqlstate == "55P03":
        return "Запрос остановлен: не удалось получить блокировку за 3 секунды."
    return str(exc).splitlines()[0][:1000]


def execute_psql_command(command: PsqlCommand):
    if command.name == "dn":
        sql = (
            "SELECT schema_name AS schema, schema_owner AS owner "
            "FROM information_schema.schemata "
            "WHERE schema_name = ANY(current_schemas(false)) ORDER BY schema_name"
        )
        return execute_readonly(sql, row_limit=1000)

    kinds = relation_kinds(command)
    relation_sql = (
        "SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind, "
        "pg_catalog.pg_get_userbyid(c.relowner) AS owner, "
        "pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS size, "
        "pg_catalog.obj_description(c.oid, 'pg_class') AS description "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname = ANY(current_schemas(false)) AND c.relkind::text = ANY(%s) "
        "ORDER BY n.nspname,c.relname"
    )
    all_columns, all_rows, elapsed, _ = execute_readonly(
        relation_sql, row_limit=100_000, params=(list(kinds),)
    )
    records = [dict(zip(all_columns, row, strict=True)) for row in all_rows]

    if command.name in {"dt", "dv", "dm", "di", "ds"} or not command.pattern or "*" in command.pattern:
        filtered = [
            row
            for row in records
            if matches_pattern(row["name"], command.pattern)
            or matches_pattern(f"{row['schema']}.{row['name']}", command.pattern)
        ]
        columns = ["schema", "name", "type", "owner"]
        if command.detailed:
            columns.extend(["size", "description"])
        type_names = {"r": "table", "p": "partitioned table", "f": "foreign table", "v": "view", "m": "materialized view", "S": "sequence", "i": "index", "I": "partitioned index"}
        rows = [
            [row.get(column) if column not in {"type"} else type_names.get(row["kind"], row["kind"]) for column in columns]
            for row in filtered
        ]
        return columns, rows, elapsed, len(rows)

    pattern = command.pattern
    if "." in pattern:
        schema, name = pattern.split(".", 1)
        matches = [row for row in records if row["schema"] == schema and row["name"] == name]
    else:
        matches = [row for row in records if row["name"] == pattern]
    if not matches:
        raise PsqlCommandError(f"Таблица или представление «{pattern}» не найдены в доступных схемах")
    relation = matches[0]
    columns_sql = (
        "SELECT a.attnum AS position, a.attname AS column, "
        "pg_catalog.format_type(a.atttypid,a.atttypmod) AS type, "
        "CASE WHEN a.attnotnull THEN 'not null' ELSE '' END AS nullable, "
        "pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default, "
        "pg_catalog.col_description(c.oid,a.attnum) AS description "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
        "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
        "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
        "ORDER BY a.attnum"
    )
    column_names, rows, column_elapsed, count = execute_readonly(
        columns_sql, row_limit=1000, params=(relation["schema"], relation["name"])
    )
    if command.detailed:
        return column_names, rows, elapsed + column_elapsed, count
    simple_columns = ["column", "type", "nullable", "default"]
    indices = [column_names.index(name) for name in simple_columns]
    return simple_columns, [[row[index] for index in indices] for row in rows], elapsed + column_elapsed, count


def run_query(sql: str, user, kind: str, homework=None, task=None):
    start = time.monotonic()
    try:
        command = parse_psql_command(sql)
        if command:
            columns, rows, elapsed, count = execute_psql_command(command)
        else:
            columns, rows, elapsed, count = execute_readonly(sql, row_limit=100_000)
        truncated = len(rows) > 200
        rows = rows[:200]
        status, message = "ok", ""
    except Exception as exc:
        columns, rows, elapsed, count = [], [], int((time.monotonic() - start) * 1000), 0
        message = execution_error_message(exc)
        status = "error"
    with store.transaction() as db:
        cur = db.execute(
            "INSERT INTO query_runs(student_id,homework,task,kind,sql,status,elapsed_ms,row_count,message,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"],
                homework,
                task,
                kind,
                sql,
                status,
                elapsed,
                count,
                message,
                json.dumps({"columns": columns, "rows": rows, "row_count": count, "elapsed_ms": elapsed, "truncated": truncated} if status == "ok" else {"error": message}, ensure_ascii=False),
                store.now_iso(),
            ),
        )
        store.audit(
            db,
            user["username"],
            "query_run",
            json.dumps(
                {
                    "run_id": cur.lastrowid,
                    "kind": kind,
                    "status": status,
                    "elapsed_ms": elapsed,
                    "rows": count,
                    "sql": sql[:10000],
                },
                ensure_ascii=False,
            ),
            user["id"],
        )
    return {
        "id": cur.lastrowid,
        "status": status,
        "message": message,
        "columns": columns,
        "rows": rows,
        "row_count": count,
        "truncated": truncated if status == "ok" else False,
        "elapsed_ms": elapsed,
    }






def clean_sql(sql: str) -> str:
    return inspect_sql(sql)[0]


def evaluate_task(hw_id: str, task, version: int | None, sql: str):
    """Evaluate once without writing student attempts (also used by teacher rechecks)."""
    started = time.monotonic()
    code, safety_error = inspect_sql(sql)
    if not code:
        return "EMPTY", Decimal(0), "Пустой ответ", 0, 0, "not_run", None
    if safety_error:
        return "FORBID", Decimal(0), safety_error, 0, 0, "rejected", None
    structural = [
        name
        for name in task.required
        if name not in STRUCTURE_PATTERNS or not re.search(STRUCTURE_PATTERNS[name], code, re.I)
    ]
    try:
        expected = get_reference_result(hw_id, task, version)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        detail = execution_error_message(exc)
        return "ERROR", Decimal(0), "Проверка временно недоступна", elapsed, 0, "error", {"error": detail}
    try:
        columns, rows, _, row_count = execute_readonly(sql, row_limit=100_000)
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        detail = execution_error_message(exc)
        status = (
            "SYNTAX"
            if getattr(exc, "sqlstate", None) == "42601" or "syntax error" in detail.lower()
            else "ERROR"
        )
        return status, Decimal(0), detail, elapsed, 0, "error", {"error": detail, "columns": [], "rows": [], "row_count": 0, "elapsed_ms": elapsed}
    elapsed = int((time.monotonic() - started) * 1000)
    output = {"columns": columns, "rows": rows[:200], "row_count": row_count, "elapsed_ms": elapsed, "truncated": len(rows) > 200}
    if not columns:
        return "ERROR", Decimal(0), "Запрос не вернул таблицу", elapsed, row_count, "error", output
    available = list(range(len(columns)))
    order = []
    for expected_column in expected["columns"]:
        index = next((i for i in available if columns[i] == expected_column), None)
        if index is None:
            order = []
            break
        order.append(index)
        available.remove(index)
    rows_in_expected_order = [[row[i] for i in order] for row in rows] if order else []
    if not order or available:
        message = "Неверные столбцы"
        return (
            "WRONG",
            Decimal(0),
            message[:1000],
            elapsed,
            row_count,
            "ok",
            output,
        )
    if len(rows_in_expected_order) != len(expected["rows"]):
        return (
            "WRONG",
            Decimal(0),
            "Неверное количество строк",
            elapsed,
            row_count,
            "ok",
            output,
        )
    for index, (actual, reference) in enumerate(zip(rows_in_expected_order, expected["rows"]), 1):
        if actual != reference:
            return (
                "WRONG",
                Decimal(0),
                f"Не совпадает строка {index}",
                elapsed,
                row_count,
                "ok",
                output,
            )
    if structural:
        missing = [STRUCTURE_LABELS.get(name, name) for name in structural]
        return (
            "STRUCT",
            task.points / 2,
            "Ответ верный, но отсутствуют обязательные конструкции: " + ", ".join(missing),
            elapsed,
            row_count,
            "ok",
            output,
        )
    return "OK", task.points, "Верно", elapsed, row_count, "ok", output






















def run_recheck_job(job_id: int, actor: str, manifest, version: int, candidates: list[dict]):
    homework_index = {hw.id: hw for hw in manifest.homeworks}
    task_index = {hw.id: {task.id: task for task in hw.tasks} for hw in manifest.homeworks}
    with store.transaction() as db:
        db.execute(
            "UPDATE recheck_jobs SET status='running',updated_at=? WHERE id=?",
            (store.now_iso(), job_id),
        )

    def check(candidate):
        hw_id, task_id = candidate["homework"], candidate["task"]
        task = task_index.get(hw_id, {}).get(task_id)
        if task is None:
            return candidate, "MISSING_TASK", Decimal(0), "Задача отсутствует в активной версии", 0
        hard = parse_deadline(homework_index[hw_id].hard_deadline, manifest.timezone)
        submitted = datetime.fromisoformat(candidate["created_at"])
        with store.closing(store.connect()) as db:
            override = db.execute(
                "SELECT 1 FROM deadline_overrides WHERE student_id=? AND homework=?",
                (candidate["student_id"], hw_id),
            ).fetchone()
        if not grading_open(submitted, hard) and not override:
            return candidate, "HARD_LATE", Decimal(0), "Проверка заблокирована жёстким дедлайном", 0
        verdict, points, message, elapsed, _, _, _ = evaluate_task(
            hw_id, task, version, candidate["sql"]
        )
        return candidate, verdict, points, message, elapsed

    if not _RECHECK_LOCK.acquire(blocking=False):
        with store.transaction() as db:
            db.execute(
                "UPDATE recheck_jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                ("Другая перепроверка уже выполняется", store.now_iso(), job_id),
            )
        return
    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cabinet-recheck") as pool:
            futures = [pool.submit(check, row) for row in candidates]
            for future in as_completed(futures):
                candidate, verdict, points, message, elapsed = future.result()
                timestamp = store.now_iso()
                with store.transaction() as db:
                    db.execute(
                        "INSERT INTO recheck_results(job_id,student_id,homework,task,attempt_id,verdict,points,message,elapsed_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            candidate["student_id"],
                            candidate["homework"],
                            candidate["task"],
                            candidate["attempt_id"],
                            verdict,
                            str(points),
                            message,
                            elapsed,
                            timestamp,
                        ),
                    )
                    previous = highest_raw_points(
                        db, candidate["student_id"], candidate["homework"], candidate["task"]
                    )
                    improved = points > previous
                    if improved:
                        db.execute(
                            "INSERT INTO achievements(student_id,homework,task,points,attempt_id,manual_grade_id,created_at) VALUES(?,?,?,?,?,NULL,?)",
                            (
                                candidate["student_id"],
                                candidate["homework"],
                                candidate["task"],
                                str(points),
                                candidate["attempt_id"],
                                timestamp,
                            ),
                        )
                        store.audit(
                            db,
                            actor,
                            "achievement_created",
                            json.dumps(
                                {
                                    "job_id": job_id,
                                    "attempt_id": candidate["attempt_id"],
                                    "homework": candidate["homework"],
                                    "task": candidate["task"],
                                    "previous_points": str(previous),
                                    "points": str(points),
                                },
                                ensure_ascii=False,
                            ),
                            candidate["student_id"],
                        )
                    store.audit(
                        db,
                        actor,
                        "homework_rechecked",
                        json.dumps(
                            {
                                "job_id": job_id,
                                "homework": candidate["homework"],
                                "task": candidate["task"],
                                "attempt_id": candidate["attempt_id"],
                                "verdict": verdict,
                                "points": str(points),
                                "improved": improved,
                            },
                            ensure_ascii=False,
                        ),
                        candidate["student_id"],
                    )
                    completed += 1
                    db.execute(
                        "UPDATE recheck_jobs SET completed=?,updated_at=? WHERE id=?",
                        (completed, timestamp, job_id),
                    )
        with store.transaction() as db:
            db.execute(
                "UPDATE recheck_jobs SET status='completed',updated_at=? WHERE id=?",
                (store.now_iso(), job_id),
            )
            store.audit(
                db,
                actor,
                "recheck_completed",
                json.dumps({"job_id": job_id, "completed": completed}, ensure_ascii=False),
            )
    except Exception as exc:
        with store.transaction() as db:
            db.execute(
                "UPDATE recheck_jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                (str(exc)[:1000], store.now_iso(), job_id),
            )
            store.audit(
                db,
                actor,
                "recheck_failed",
                json.dumps({"job_id": job_id, "error": str(exc)[:1000]}, ensure_ascii=False),
            )
    finally:
        _RECHECK_LOCK.release()














# Import endpoint modules only after shared dependencies and services above are defined.
from cabinet.routers import (  # noqa: E402,F401
    admin_routes,
    auth_routes,
    coursework_routes,
    queries_routes,
    scoring_routes,
    system_routes,
)

for _router in (system_router, auth_router, coursework_router, query_router, admin_router, score_router):
    app.include_router(_router)
