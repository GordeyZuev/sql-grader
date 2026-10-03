# ruff: noqa: I001
"""HTTP endpoints for this domain."""

from cabinet import api as core
from cabinet.domains.scoring import (
    parse_deadline,
)
from datetime import (
    datetime,
)
from decimal import (
    Decimal,
)
from fastapi.param_functions import (
    Depends,
)
import cabinet.infrastructure.sqlite_store as store
import json
from cabinet.api import (
    SqlInput,
    active_manifest,
    highest_raw_points,
    public_task,
    student,
    task_for,
)
from cabinet.routers.coursework import router as coursework_router

@coursework_router.get("/homeworks")
def homeworks(user=Depends(student)):
    _, manifest, _ = active_manifest()
    result = []
    with store.closing(store.connect()) as db:
        overrides = {
            row[0]
            for row in db.execute(
                "SELECT homework FROM deadline_overrides WHERE student_id=?", (user["id"],)
            ).fetchall()
        }
        for hw in manifest.homeworks:
            hard_deadline = hw.hard_deadline
            grading_is_open = core.grading_open(
                datetime.now().astimezone(), parse_deadline(hard_deadline, manifest.timezone)
            )
            tasks = []
            for task in hw.tasks:
                best = highest_raw_points(db, user["id"], hw.id, task.id)
                count = db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE student_id=? AND homework=? AND task=?",
                    (user["id"], hw.id, task.id),
                ).fetchone()[0]
                latest = db.execute(
                    "SELECT created_at FROM achievements WHERE student_id=? AND homework=? AND task=? ORDER BY id DESC LIMIT 1",
                    (user["id"], hw.id, task.id),
                ).fetchone()
                tasks.append(
                    {
                        "id": task.id,
                        "title": task.title,
                        "max_points": str(task.points),
                        "score": str(best),
                        "checks": count,
                        "last_improvement": latest[0] if latest else None,
                    }
                )
            result.append(
                {
                    "id": hw.id,
                    "title": hw.title,
                    "soft_deadline": hw.soft_deadline,
                    "hard_deadline": hard_deadline,
                    "grading_open": grading_is_open or hw.id in overrides,
                    "tasks": tasks,
                    "score": str(sum((Decimal(str(t["score"])) for t in tasks), Decimal(0))),
                    "max_points": str(sum((t.points for t in hw.tasks), Decimal(0))),
                }
            )
    return result


@coursework_router.get("/homeworks/{hw_id}/tasks/{task_id}")
def get_task(hw_id: str, task_id: str, user=Depends(student)):
    manifest, hw, task = task_for(hw_id, task_id)
    hard_deadline = hw.hard_deadline
    hard_at = parse_deadline(hard_deadline, manifest.timezone)
    with store.closing(store.connect()) as db:
        draft = db.execute(
            "SELECT sql,updated_at FROM drafts WHERE student_id=? AND homework=? AND task=?",
            (user["id"], hw_id, task_id),
        ).fetchone()
        attempts = db.execute(
            "SELECT a.id,a.sql,a.points,a.verdict,a.message,a.created_at,q.status AS query_status,q.elapsed_ms,q.row_count,q.result_json FROM attempts a LEFT JOIN query_runs q ON q.id=a.query_run_id WHERE a.student_id=? AND a.homework=? AND a.task=? ORDER BY a.id DESC LIMIT 50",
            (user["id"], hw_id, task_id),
        ).fetchall()
        runs = db.execute(
            "SELECT id,sql,status,elapsed_ms,row_count,message,result_json,created_at FROM query_runs WHERE student_id=? AND homework=? AND task=? AND kind='task' ORDER BY id DESC LIMIT 10",
            (user["id"], hw_id, task_id),
        ).fetchall()
        best = highest_raw_points(db, user["id"], hw_id, task_id)
        override = db.execute(
            "SELECT 1 FROM deadline_overrides WHERE student_id=? AND homework=?",
            (user["id"], hw_id),
        ).fetchone()
        manual_grade = db.execute(
            "SELECT points,comment,actor,created_at FROM manual_grades WHERE student_id=? AND homework=? AND task=? ORDER BY id DESC LIMIT 1",
            (user["id"], hw_id, task_id),
        ).fetchone()
    return {
        "homework": {
            "id": hw.id,
            "title": hw.title,
            "soft_deadline": hw.soft_deadline,
            "hard_deadline": hard_deadline,
            "grading_open": core.grading_open(datetime.now().astimezone(), hard_at) or bool(override),
        },
        "task": public_task(task),
        "draft": draft["sql"] if draft else "",
        "draft_saved_at": draft["updated_at"] if draft else None,
        "score": str(best),
        "manual_grade": dict(manual_grade) if manual_grade else None,
        "attempts": [
            {**dict(a), "result": json.loads(a["result_json"]) if a["result_json"] else None}
            for a in attempts
        ],
        "runs": [
            {**dict(run), "result": json.loads(run["result_json"]) if run["result_json"] else None}
            for run in runs
        ],
    }


@coursework_router.get("/homeworks/{hw_id}/tasks/{task_id}/history")
def task_query_history(
    hw_id: str,
    task_id: str,
    offset: int = 0,
    limit: int = 50,
    user=Depends(student),
):
    task_for(hw_id, task_id)
    offset = max(offset, 0)
    limit = min(max(limit, 1), 100)
    with store.closing(store.connect()) as db:
        total = db.execute(
            "SELECT COUNT(*) FROM query_runs WHERE student_id=? AND homework=? AND task=? AND kind IN ('task','check')",
            (user["id"], hw_id, task_id),
        ).fetchone()[0]
        rows = db.execute(
            "SELECT q.id,q.kind,q.sql,q.status,q.elapsed_ms,q.row_count,q.message,q.result_json,q.created_at,a.id AS attempt_id,a.points,a.verdict,a.message AS attempt_message "
            "FROM query_runs q LEFT JOIN attempts a ON a.query_run_id=q.id "
            "WHERE q.student_id=? AND q.homework=? AND q.task=? AND q.kind IN ('task','check') "
            "ORDER BY q.id DESC LIMIT ? OFFSET ?",
            (user["id"], hw_id, task_id, limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        result_json = item.pop("result_json")
        item["result"] = json.loads(result_json) if result_json else None
        items.append(item)
    return {"items": items, "total": total, "has_more": offset + len(items) < total}


@coursework_router.put("/homeworks/{hw_id}/tasks/{task_id}/draft")
def save_draft(hw_id: str, task_id: str, data: SqlInput, user=Depends(student)):
    task_for(hw_id, task_id)
    timestamp = store.now_iso()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO drafts VALUES(?,?,?,?,?) ON CONFLICT(student_id,homework,task) DO UPDATE SET sql=excluded.sql,updated_at=excluded.updated_at",
            (user["id"], hw_id, task_id, data.sql, timestamp),
        )
    return {"saved_at": timestamp}
