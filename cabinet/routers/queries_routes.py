# ruff: noqa: I001
"""HTTP endpoints for this domain."""

from cabinet import api as core
from cabinet.domains.psql_commands import (
    PsqlCommandError,
    parse_psql_command,
)
from cabinet.domains.scoring import (
    parse_deadline,
)
from cabinet.domains.sql_safety import (
    inspect_sql,
)
from datetime import (
    datetime,
)
from decimal import (
    Decimal,
)
from fastapi.exceptions import (
    HTTPException,
)
from fastapi.param_functions import (
    Depends,
)
import cabinet.infrastructure.sqlite_store as store
import json
from cabinet.api import (
    SqlInput,
    active_manifest,
    evaluate_task,
    highest_raw_points,
    record_audit,
    run_query,
    student,
    task_for,
)
from cabinet.routers.queries import router as query_router

@query_router.post("/format")
def format_sql(data: SqlInput, user=Depends(student)):
    try:
        command = parse_psql_command(data.sql)
    except PsqlCommandError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if command:
        return {"sql": data.sql.strip()}
    _, error = inspect_sql(data.sql)
    if error:
        raise HTTPException(422, detail=error)
    try:
        import sqlparse

        formatted = sqlparse.format(
            data.sql,
            reindent=True,
            indent_width=4,
            strip_comments=False,
            use_space_around_operators=True,
        )
    except Exception as exc:
        raise HTTPException(422, detail=f"Не удалось отформатировать SQL: {exc}") from exc
    return {"sql": formatted}


@query_router.post("/run")
def run(data: SqlInput, user=Depends(student)):
    return run_query(data.sql, user, "sandbox", data.homework, data.task)


@query_router.post("/homeworks/{hw_id}/tasks/{task_id}/run")
def run_task(hw_id: str, task_id: str, data: SqlInput, user=Depends(student)):
    task_for(hw_id, task_id)
    return run_query(data.sql, user, "task", hw_id, task_id)


@query_router.post("/homeworks/{hw_id}/tasks/{task_id}/check")
def check_task(hw_id: str, task_id: str, data: SqlInput, user=Depends(student)):
    manifest, hw, task = task_for(hw_id, task_id)
    if data.sql.lstrip().startswith("\\"):
        record_audit(
            user["username"],
            "task_check_rejected",
            {"homework": hw_id, "task": task_id, "reason": "psql_command"},
            user["id"],
        )
        raise HTTPException(422, "Команды psql запускаются отдельно и не создают попытку проверки.")
    _, _, manifest_version = active_manifest()
    now = datetime.now().astimezone()
    past_hard_deadline = not core.grading_open(now, parse_deadline(hw.hard_deadline, manifest.timezone))
    with store.closing(store.connect()) as db:
        override = db.execute(
            "SELECT 1 FROM deadline_overrides WHERE student_id=? AND homework=?",
            (user["id"], hw_id),
        ).fetchone()
    sql = data.sql
    if past_hard_deadline and not override:
        verdict, points, message = (
            "HARD_LATE",
            Decimal(0),
            "Жёсткий срок сдачи завершён. Обратитесь к преподавателю.",
        )
        elapsed, row_count, query_status, output = 0, 0, "blocked", None
    else:
        verdict, points, message, elapsed, row_count, query_status, output = evaluate_task(
            hw_id, task, manifest_version, sql
        )
    timestamp = store.now_iso()
    with store.transaction() as db:
        query = db.execute(
            "INSERT INTO query_runs(student_id,homework,task,kind,sql,status,elapsed_ms,row_count,message,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"],
                hw_id,
                task_id,
                "check",
                sql,
                query_status,
                elapsed,
                row_count,
                message,
                json.dumps(output, ensure_ascii=False) if output else "",
                timestamp,
            ),
        )
        cur = db.execute(
            "INSERT INTO attempts(student_id,query_run_id,homework,task,sql,points,verdict,message,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                user["id"],
                query.lastrowid,
                hw_id,
                task_id,
                sql,
                str(points),
                verdict,
                message,
                timestamp,
            ),
        )
        attempt_id = cur.lastrowid
        manual = db.execute(
            "SELECT points FROM manual_grades WHERE student_id=? AND homework=? AND task=? ORDER BY id DESC LIMIT 1",
            (user["id"], hw_id, task_id),
        ).fetchone()
        previous = highest_raw_points(db, user["id"], hw_id, task_id)
        improved = points > previous
        score_locked = (
            manual is not None and previous == Decimal(manual["points"]) and not improved
        )
        if improved:
            db.execute(
                "INSERT INTO achievements(student_id,homework,task,points,attempt_id,manual_grade_id,created_at) VALUES(?,?,?,?,?,NULL,?)",
                (user["id"], hw_id, task_id, str(points), attempt_id, timestamp),
            )
            store.audit(
                db,
                user["username"],
                "achievement_created",
                json.dumps(
                    {
                        "homework": hw_id,
                        "task": task_id,
                        "attempt_id": attempt_id,
                        "previous_points": str(previous),
                        "points": str(points),
                        "max_points": str(task.points),
                    },
                    ensure_ascii=False,
                ),
                user["id"],
            )
        store.audit(
            db,
            user["username"],
            "task_checked",
            json.dumps(
                {
                    "attempt_id": attempt_id,
                    "homework": hw_id,
                    "task": task_id,
                    "points": str(points),
                    "verdict": verdict,
                    "improved": improved,
                    "query_run_id": query.lastrowid,
                    "query_status": query_status,
                    "elapsed_ms": elapsed,
                    "rows": row_count,
                    "sql": sql[:10000],
                },
                ensure_ascii=False,
            ),
            user["id"],
        )
    return {
        "attempt_id": attempt_id,
        "verdict": verdict,
        "points": str(points),
        "max_points": str(task.points),
        "message": message,
        "elapsed_ms": elapsed,
        "row_count": row_count,
        "query_status": query_status,
        "result": output,
        "created_at": timestamp,
        "improved": improved,
        "score": str(points if improved else previous),
        "score_locked": score_locked,
    }


@query_router.get("/sandbox/history")
def sandbox_history(user=Depends(student)):
    with store.closing(store.connect()) as db:
        rows = db.execute(
            "SELECT id,sql,status,elapsed_ms,row_count,message,result_json,created_at FROM query_runs WHERE student_id=? AND kind='sandbox' ORDER BY id DESC LIMIT 10",
            (user["id"],),
        ).fetchall()
    return [
        {**dict(row), "result": json.loads(row["result_json"]) if row["result_json"] else None}
        for row in rows
    ]
