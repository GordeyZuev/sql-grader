# ruff: noqa: I001
"""HTTP endpoints for this domain."""

from cabinet import api as core
from cabinet.domains.manifests import (
    ManifestError,
    validate_manifest,
)
from cabinet.domains.scoring import (
    final_score,
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
from fastapi.background import (
    BackgroundTasks,
)
from fastapi.datastructures import (
    UploadFile,
)
from fastapi.exceptions import (
    HTTPException,
)
from fastapi.param_functions import (
    Depends,
    File,
    Header,
)
from starlette.responses import (
    Response,
)
from starlette.requests import (
    Request,
)
from typing import (
    Annotated,
)
import cabinet.infrastructure.sqlite_store as store
import csv
import hashlib
import io
import json
import os
import re
import secrets
from cabinet.security.login_limiter import (
    login_rate_limiter,
    request_client_ip,
)
from cabinet.api import (
    AdminActiveInput,
    AdminCreateInput,
    AdminLoginInput,
    MANIFEST_FILE,
    ManualGradeInput,
    PreviewRequest,
    PublishInput,
    RoleInput,
    STRUCTURE_PATTERNS,
    active_manifest,
    admin,
    highest_raw_points,
    score_from_records,
    public_task,
    record_audit,
    run_recheck_job,
    super_admin,
    task_for,
)
from cabinet.routers.admin import router as admin_router

@admin_router.post("/auth/login")
def admin_login(data: AdminLoginInput, request: Request):
    attempted_username = data.username.strip()
    client_ip = request_client_ip(request)
    login_rate_limiter.consume(
        scope="admin",
        client_ip=client_ip,
        username=attempted_username,
        account_limit=6,
        address_limit=20,
    )
    with store.closing(store.connect()) as db:
        account = db.execute(
            "SELECT * FROM admin_accounts WHERE username=? COLLATE NOCASE AND active=1",
            (data.username.strip(),),
        ).fetchone()
    if not account or not store.verify_password(data.password, account["password_hash"]):
        record_audit(
            attempted_username or "anonymous",
            "admin_login_failed",
            {"username": attempted_username},
        )
        raise HTTPException(401, "Неверный логин или пароль преподавателя")
    with store.transaction() as db:
        session = store.issue_admin_session(db, account["id"])
        store.audit(db, account["username"], "admin_login")
    login_rate_limiter.reset_account(
        scope="admin", client_ip=client_ip, username=attempted_username
    )
    return {"token": session, "username": account["username"], "full_name": account["full_name"]}


@admin_router.post("/auth/logout")
def admin_logout(
    x_admin_session: Annotated[str | None, Header()] = None,
    actor: str = Depends(admin),
):
    token_hash = None
    if x_admin_session:
        token_hash = hashlib.sha256(x_admin_session.encode()).hexdigest()
    with store.transaction() as db:
        if token_hash:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash,))
        store.audit(db, actor, "admin_logout")
    return {"status": "ok"}


@admin_router.post("/auth/token-login")
def super_admin_login(actor: str = Depends(super_admin)):
    record_audit(actor, "super_admin_login", {})
    return {"status": "ok", "actor": actor}


@admin_router.post("/import/preview")
def import_preview(request: PreviewRequest, actor: str = Depends(super_admin)):
    try:
        manifest = validate_manifest(request.manifest)
    except (ManifestError, ValueError, TypeError) as exc:
        record_audit(actor, "manifest_preview_rejected", {"error": str(exc)[:1000]})
        raise HTTPException(422, detail=str(exc)) from exc
    record_audit(
        actor,
        "manifest_previewed",
        {"homework_count": len(manifest.homeworks), "schema_version": manifest.schema_version},
    )
    return {
        "schema_version": manifest.schema_version,
        "timezone": manifest.timezone,
        "hard_deadline": manifest.hard_deadline,
        "penalty": {
            "formula": manifest.policy.formula,
            "period_days": manifest.policy.period_days,
            "period_multiplier": str(manifest.policy.period_multiplier),
            "max_penalty_fraction": str(manifest.policy.max_penalty_fraction),
            "minimum_score_fraction": str(manifest.policy.minimum_score_fraction),
            "round_digits": manifest.policy.round_digits,
            "rounding_mode": manifest.policy.rounding_mode,
        },
        "homeworks": [
            {
                "id": hw.id,
                "title": hw.title,
                "order": hw.order,
                "soft_deadline": hw.soft_deadline,
                "hard_deadline": hw.hard_deadline,
                "task_count": len(hw.tasks),
                "max_points": str(sum((t.points for t in hw.tasks), Decimal(0))),
                "tasks": [public_task(t) for t in hw.tasks],
            }
            for hw in manifest.homeworks
        ],
    }


@admin_router.get("/manifest")
def admin_manifest(_: str = Depends(super_admin)):
    try:
        raw, _, version = active_manifest()
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        return {"manifest": None, "version": None, "source": "empty"}
    return {"manifest": raw, "version": version, "source": "published" if version else "file"}


@admin_router.get("/settings")
def admin_settings(_: str = Depends(super_admin)):
    return {
        "learning_database_configured": bool(os.getenv("CABINET_LEARNING_DSN")),
        "manifest_file": MANIFEST_FILE.name,
        "storage": "SQLite",
        "timezone": "Europe/Moscow",
    }


@admin_router.post("/import/legacy-zip")
async def import_legacy_zip(
    file: UploadFile = File(...),
    timezone: str = "Europe/Moscow",
    hard_deadline: str = "2026-10-10T23:59:00+03:00",
    soft_deadline: str = "2026-09-25T23:59:00+03:00",
    actor: str = Depends(super_admin),
):
    from cabinet.domains.manifests import convert_legacy_zip

    if not file.filename or not file.filename.lower().endswith(".zip"):
        record_audit(actor, "legacy_zip_preview_rejected", {"filename": file.filename or ""})
        raise HTTPException(422, "Загрузите ZIP-архив")
    try:
        contents = await file.read(20_000_001)
        if len(contents) > 20_000_000:
            raise ValueError("Архив превышает лимит 20 МБ")
        manifest = convert_legacy_zip(contents, timezone, hard_deadline, soft_deadline)
        validated = validate_manifest(manifest)
    except (ManifestError, ValueError, TypeError) as exc:
        record_audit(
            actor,
            "legacy_zip_preview_rejected",
            {"filename": file.filename, "error": str(exc)[:1000]},
        )
        raise HTTPException(422, detail=str(exc)) from exc
    record_audit(
        actor,
        "legacy_zip_previewed",
        {"filename": file.filename, "homework_count": len(validated.homeworks)},
    )
    return {
        "manifest": manifest,
        "preview": {
            "homeworks": [
                {
                    "id": hw.id,
                    "title": hw.title,
                    "task_count": len(hw.tasks),
                    "max_points": str(sum((t.points for t in hw.tasks), Decimal(0))),
                }
                for hw in validated.homeworks
            ]
        },
    }


@admin_router.post("/publish")
def publish(data: PublishInput, actor: str = Depends(super_admin)):
    try:
        manifest = validate_manifest(data.manifest)
    except (ManifestError, ValueError, TypeError) as exc:
        record_audit(actor, "manifest_publish_rejected", {"error": str(exc)[:1000]})
        raise HTTPException(422, detail=str(exc)) from exc
    # Validate every golden query against the configured read-only PostgreSQL role
    # before making an immutable manifest version visible to students.
    reference_data = {}
    for hw in manifest.homeworks:
        for task in hw.tasks:
            reference_code, _ = inspect_sql(task.reference_sql)
            missing = [
                key
                for key in task.required
                if key not in STRUCTURE_PATTERNS
                or not re.search(STRUCTURE_PATTERNS[key], reference_code, re.IGNORECASE)
            ]
            if missing:
                record_audit(
                    actor,
                    "manifest_publish_rejected",
                    {"homework": hw.id, "task": task.id, "reason": "missing_required_constructs", "items": missing},
                )
                raise HTTPException(
                    422,
                    f"Правильный ответ {hw.id}/{task.id} не содержит обязательные конструкции: {', '.join(missing)}",
                )
            try:
                columns, rows, _, _ = core.execute_readonly(task.reference_sql, row_limit=100_000)
            except Exception as exc:
                record_audit(
                    actor,
                    "manifest_publish_rejected",
                    {"homework": hw.id, "task": task.id, "reason": str(exc)[:1000]},
                )
                raise HTTPException(
                    422, f"Правильный ответ {hw.id}/{task.id} не прошёл проверку: {exc}"
                ) from exc
            reference_data[(hw.id, task.id)] = {"columns": columns, "rows": rows}
    encoded = json.dumps(data.manifest, ensure_ascii=False, separators=(",", ":"))
    with store.transaction() as db:
        cur = db.execute(
            "INSERT INTO manifest_versions(manifest_json,published_at,actor) VALUES(?,?,?)",
            (encoded, store.now_iso(), actor),
        )
        for (hw_id, task_id), result in reference_data.items():
            db.execute(
                "INSERT INTO reference_cache(manifest_version,homework,task,result_json) VALUES(?,?,?,?)",
                (cur.lastrowid, hw_id, task_id, json.dumps(result, ensure_ascii=False)),
            )
        store.audit(db, actor, "manifest_published", json.dumps({"version": cur.lastrowid}), None)
    return {"version": cur.lastrowid, "homework_count": len(manifest.homeworks)}


@admin_router.get("/students")
def admin_students(
    _: str = Depends(admin),
    homework: str | None = None,
    task: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    status: str | None = None,
    search: str | None = None,
    sort: str = "name",
):
    valid_statuses = {None, "not_started", "complete", "partial", "zero"}
    if status not in valid_statuses:
        raise HTTPException(422, "status must be not_started, complete, partial or zero")
    if sort not in {"name", "username", "score", "status"}:
        raise HTTPException(422, "Unsupported sort field")
    target_tasks = []
    try:
        _, manifest, _ = active_manifest()
        for hw in manifest.homeworks:
            if homework and hw.id != homework:
                continue
            for item in hw.tasks:
                if task and item.id != task:
                    continue
                target_tasks.append((hw.id, item.id, item.points))
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
    with store.closing(store.connect()) as db:
        students = db.execute(
            "SELECT id,COALESCE(username,email) AS username,full_name FROM students ORDER BY full_name COLLATE NOCASE"
        ).fetchall()
        achievements = db.execute(
            "SELECT student_id,homework,task,points,created_at FROM achievements"
        ).fetchall()
        manual_grades = db.execute(
            "SELECT student_id,homework,task,points,created_at FROM manual_grades ORDER BY id"
        ).fetchall()
        activity = db.execute(
            "SELECT student_id,homework,task FROM attempts UNION SELECT student_id,homework,task FROM manual_grades"
        ).fetchall()
        result = []
        scores: dict[tuple[int, str, str], Decimal] = {}
        manual_by_task: dict[tuple[int, str, str], tuple[Decimal, str]] = {}
        achievements_by_task: dict[tuple[int, str, str], list[tuple[Decimal, str]]] = {}
        for row in manual_grades:
            manual_by_task[(row[0], row[1], row[2])] = (Decimal(row[3]), row[4])
        for row in achievements:
            achievements_by_task.setdefault((row[0], row[1], row[2]), []).append(
                (Decimal(row[3]), row[4])
            )
        for key in set(manual_by_task) | set(achievements_by_task):
            scores[key] = score_from_records(
                manual_by_task.get(key), achievements_by_task.get(key, [])
            )
        active = {(row[0], row[1], row[2]) for row in activity}
        for s in students:
            if search and search.strip().casefold() not in f"{s['full_name']} {s['username']}".casefold():
                continue
            assigned = target_tasks
            if not assigned and not homework and not task:
                assigned = sorted({(h, t, Decimal(0)) for sid, h, t in scores if sid == s["id"]})
            score = sum(
                (scores.get((s["id"], h, t), Decimal(0)) for h, t, _ in assigned), Decimal(0)
            )
            has_activity = any((s["id"], h, t) in active for h, t, _ in assigned)
            if not has_activity:
                student_status = "not_started"
            elif assigned and all(
                scores.get((s["id"], h, t), Decimal(0)) >= maximum for h, t, maximum in assigned
            ):
                student_status = "complete"
            elif score > 0:
                student_status = "partial"
            else:
                student_status = "zero"
            if status and student_status != status:
                continue
            if (min_score is not None and score < Decimal(str(min_score))) or (
                max_score is not None and score > Decimal(str(max_score))
            ):
                continue
            result.append(
                {
                    "id": s["id"],
                    "username": s["username"],
                    "full_name": s["full_name"],
                    "score": str(score),
                    "status": student_status,
                }
            )
    if sort == "score":
        result.sort(key=lambda item: Decimal(item["score"]), reverse=True)
    else:
        key = "username" if sort == "username" else "status" if sort == "status" else "full_name"
        result.sort(key=lambda item: item[key].casefold())
    return result


@admin_router.get("/students/{student_id}/history")
def admin_history(
    student_id: int,
    offset: int = 0,
    limit: int = 100,
    _: str = Depends(admin),
):
    offset = max(0, offset)
    limit = min(max(1, limit), 1000)
    with store.closing(store.connect()) as db:
        rows = db.execute(
            "SELECT actor,action,details_json,created_at FROM audit WHERE student_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (student_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


@admin_router.get("/students/{student_id}")
def admin_student_detail(student_id: int, _: str = Depends(admin)):
    try:
        _, manifest, _ = active_manifest()
    except HTTPException:
        manifest = None
    with store.closing(store.connect()) as db:
        student_row = db.execute(
            "SELECT id,COALESCE(username,email) AS username,full_name,created_at FROM students WHERE id=?",
            (student_id,),
        ).fetchone()
        if not student_row:
            raise HTTPException(404, "Студент не найден")
        attempts = db.execute(
            "SELECT a.id,a.homework,a.task,a.sql,a.points,a.verdict,a.message,a.created_at,q.status AS query_status,q.elapsed_ms,q.row_count,q.result_json "
            "FROM attempts a LEFT JOIN query_runs q ON q.id=a.query_run_id WHERE a.student_id=? ORDER BY a.id DESC LIMIT 2000",
            (student_id,),
        ).fetchall()
        runs = db.execute(
            "SELECT id,homework,task,kind,sql,status,elapsed_ms,row_count,message,result_json,created_at FROM query_runs "
            "WHERE student_id=? AND kind IN ('task','sandbox') ORDER BY id DESC LIMIT 5000",
            (student_id,),
        ).fetchall()
        grades = db.execute(
            "SELECT id,homework,task,points,comment,actor,created_at FROM manual_grades WHERE student_id=? ORDER BY id DESC",
            (student_id,),
        ).fetchall()
        achievements = db.execute(
            "SELECT homework,task,points,created_at FROM achievements WHERE student_id=?",
            (student_id,),
        ).fetchall()
        audit_rows = db.execute(
            "SELECT actor,action,details_json,created_at FROM audit WHERE student_id=? ORDER BY id DESC LIMIT 100",
            (student_id,),
        ).fetchall()
        audit_count = db.execute(
            "SELECT COUNT(*) FROM audit WHERE student_id=?", (student_id,)
        ).fetchone()[0]

    groups: dict[str, dict] = {}
    if manifest:
        for hw in manifest.homeworks:
            groups[hw.id] = {
                "id": hw.id,
                "title": hw.title,
                "tasks": {
                    task.id: {
                        "id": task.id,
                        "title": task.title,
                        "max_points": str(task.points),
                        "attempts": [],
                        "runs": [],
                        "manual_grades": [],
                    }
                    for task in hw.tasks
                },
            }

    def task_group(homework_id: str, task_id: str) -> dict:
        homework = groups.setdefault(
            homework_id, {"id": homework_id, "title": homework_id, "tasks": {}}
        )
        return homework["tasks"].setdefault(
            task_id,
            {"id": task_id, "title": task_id, "max_points": None, "attempts": [], "runs": [], "manual_grades": []},
        )

    for row in attempts:
        item = dict(row)
        result_json = item.pop("result_json", None)
        item["result"] = json.loads(result_json) if result_json else None
        task_group(item["homework"], item["task"])["attempts"].append(item)
    sandbox_runs = []
    for row in runs:
        item = dict(row)
        result_json = item.pop("result_json", None)
        item["result"] = json.loads(result_json) if result_json else None
        if item["kind"] == "sandbox":
            sandbox_runs.append(item)
        elif item["homework"] and item["task"]:
            task_group(item["homework"], item["task"])["runs"].append(item)
    for row in grades:
        item = dict(row)
        task_group(item["homework"], item["task"])["manual_grades"].append(item)
    grouped_achievements: dict[tuple[str, str], list] = {}
    for row in achievements:
        grouped_achievements.setdefault((row["homework"], row["task"]), []).append(row)
    latest_grades = {}
    for row in grades:
        latest_grades.setdefault((row["homework"], row["task"]), row)
    for homework_id, homework in groups.items():
        for task_id, task in homework["tasks"].items():
            key = (homework_id, task_id)
            milestones = grouped_achievements.get(key, [])
            manual = latest_grades.get(key)
            score = score_from_records(
                (Decimal(manual["points"]), manual["created_at"]) if manual else None,
                [(Decimal(row["points"]), row["created_at"]) for row in milestones],
            )
            task["score"] = str(score)
    return {
        "student": dict(student_row),
        "homeworks": list(groups.values()),
        "sandbox_runs": sandbox_runs,
        "audit": [dict(row) for row in audit_rows],
        "audit_count": audit_count,
        "audit_has_more": audit_count > len(audit_rows),
    }


@admin_router.post("/students/{student_id}/reset-password")
def reset_student_password(student_id: int, actor: str = Depends(admin)):
    password = secrets.token_urlsafe(12)
    with store.transaction() as db:
        target = db.execute(
            "SELECT username FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "Студент не найден")
        db.execute(
            "UPDATE students SET password_hash=? WHERE id=?",
            (store.hash_password(password), student_id),
        )
        db.execute("DELETE FROM sessions WHERE student_id=?", (student_id,))
        store.audit(
            db,
            actor,
            "student_password_reset",
            json.dumps({"username": target["username"]}, ensure_ascii=False),
            student_id,
        )
    return {"username": target["username"], "temporary_password": password}


@admin_router.get("/roles")
def list_roles(_: str = Depends(super_admin)):
    with store.closing(store.connect()) as db:
        rows = db.execute(
            "SELECT id,COALESCE(username,email) AS username,full_name,is_assistant,is_superadmin "
            "FROM students ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "full_name": row["full_name"],
            "is_assistant": bool(row["is_assistant"]),
            "is_superadmin": bool(row["is_superadmin"]),
        }
        for row in rows
    ]


@admin_router.patch("/students/{student_id}/role")
def update_role(student_id: int, data: RoleInput, actor: str = Depends(super_admin)):
    if data.is_assistant is None and data.is_superadmin is None:
        raise HTTPException(422, "Нечего менять")
    with store.transaction() as db:
        account = db.execute(
            "SELECT username,is_assistant,is_superadmin FROM students WHERE id=?",
            (student_id,),
        ).fetchone()
        if not account:
            raise HTTPException(404, "Студент не найден")
        is_assistant = (
            int(data.is_assistant)
            if data.is_assistant is not None
            else int(account["is_assistant"])
        )
        is_superadmin = (
            int(data.is_superadmin)
            if data.is_superadmin is not None
            else int(account["is_superadmin"])
        )
        db.execute(
            "UPDATE students SET is_assistant=?, is_superadmin=? WHERE id=?",
            (is_assistant, is_superadmin, student_id),
        )
        store.audit(
            db,
            actor,
            "role_changed",
            json.dumps(
                {
                    "username": account["username"],
                    "is_assistant": bool(is_assistant),
                    "is_superadmin": bool(is_superadmin),
                },
                ensure_ascii=False,
            ),
            student_id,
        )
    return {
        "id": student_id,
        "is_assistant": bool(is_assistant),
        "is_superadmin": bool(is_superadmin),
    }


@admin_router.get("/accounts")
def list_admin_accounts(_: str = Depends(super_admin)):
    with store.closing(store.connect()) as db:
        rows = db.execute(
            "SELECT id,username,full_name,active,created_at FROM admin_accounts ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@admin_router.post("/accounts")
def create_admin_account(data: AdminCreateInput, actor: str = Depends(super_admin)):
    username = data.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,32}", username):
        raise HTTPException(422, "Логин: 4–32 латинских буквы, цифры, точка, дефис или подчёркивание")
    password = secrets.token_urlsafe(12)
    try:
        with store.transaction() as db:
            cur = db.execute(
                "INSERT INTO admin_accounts(username,full_name,password_hash,active,created_at) VALUES(?,?,?,1,?)",
                (username, data.full_name.strip(), store.hash_password(password), store.now_iso()),
            )
            store.audit(
                db,
                actor,
                "admin_account_created",
                json.dumps({"account_id": cur.lastrowid, "username": username}, ensure_ascii=False),
            )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "Логин администратора уже занят") from exc
        raise
    return {"id": cur.lastrowid, "username": username, "temporary_password": password}


@admin_router.patch("/accounts/{account_id}")
def update_admin_account(account_id: int, data: AdminActiveInput, actor: str = Depends(super_admin)):
    with store.transaction() as db:
        account = db.execute(
            "SELECT username FROM admin_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account:
            raise HTTPException(404, "Учётная запись не найдена")
        db.execute("UPDATE admin_accounts SET active=? WHERE id=?", (int(data.active), account_id))
        if not data.active:
            db.execute("DELETE FROM admin_sessions WHERE admin_id=?", (account_id,))
        store.audit(
            db,
            actor,
            "admin_account_status_changed",
            json.dumps({"account_id": account_id, "active": data.active}, ensure_ascii=False),
        )
    return {"id": account_id, "active": data.active}


@admin_router.post("/accounts/{account_id}/reset-password")
def reset_admin_password(account_id: int, actor: str = Depends(super_admin)):
    password = secrets.token_urlsafe(12)
    with store.transaction() as db:
        account = db.execute(
            "SELECT username FROM admin_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not account:
            raise HTTPException(404, "Учётная запись не найдена")
        db.execute(
            "UPDATE admin_accounts SET password_hash=? WHERE id=?",
            (store.hash_password(password), account_id),
        )
        db.execute("DELETE FROM admin_sessions WHERE admin_id=?", (account_id,))
        store.audit(
            db,
            actor,
            "admin_password_reset",
            json.dumps(
                {"account_id": account_id, "username": account["username"]},
                ensure_ascii=False,
            ),
        )
    return {
        "id": account_id,
        "username": account["username"],
        "temporary_password": password,
    }


@admin_router.get("/audit")
def global_audit(
    actor_filter: str | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: str = Depends(super_admin),
):
    limit = min(max(limit, 1), 2000)
    offset = max(offset, 0)
    query = "SELECT a.actor,a.action,a.details_json,a.created_at,a.student_id,COALESCE(s.username,'') AS student_username FROM audit a LEFT JOIN students s ON s.id=a.student_id WHERE 1=1"
    params: list = []
    if actor_filter:
        query += " AND a.actor LIKE ?"
        params.append(f"%{actor_filter}%")
    if action:
        query += " AND a.action=?"
        params.append(action)
    query += " ORDER BY a.id DESC LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    with store.closing(store.connect()) as db:
        rows = db.execute(query, params).fetchall()
    return [dict(row) for row in rows]


@admin_router.post("/recheck", status_code=202)
def start_recheck(
    background: BackgroundTasks, homework: str | None = None, actor: str = Depends(super_admin)
):
    _, manifest, version = active_manifest()
    if version is None:
        raise HTTPException(
            409, "Сначала опубликуйте манифест, чтобы зафиксировать проверяемые ответы"
        )
    with store.closing(store.connect()) as db:
        query = """SELECT a.id AS attempt_id,a.student_id,a.homework,a.task,a.sql,a.created_at
          FROM attempts a JOIN (SELECT student_id,homework,task,MAX(id) AS latest_id FROM attempts
          GROUP BY student_id,homework,task) x ON x.latest_id=a.id"""
        params = ()
        if homework:
            query += " WHERE a.homework=?"
            params = (homework,)
        candidates = [dict(row) for row in db.execute(query, params).fetchall()]
    now = store.now_iso()
    with store.transaction() as db:
        active_job = db.execute(
            "SELECT id FROM recheck_jobs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone()
        if active_job:
            raise HTTPException(409, "Другая перепроверка уже выполняется")
        cur = db.execute(
            "INSERT INTO recheck_jobs(actor,homework,manifest_version,status,total,completed,created_at,updated_at) VALUES(?,?,?,'queued',?,0,?,?)",
            (actor, homework, version, len(candidates), now, now),
        )
        job_id = cur.lastrowid
        store.audit(
            db,
            actor,
            "recheck_started",
            json.dumps({"job_id": job_id, "homework": homework, "total": len(candidates)}),
        )
    background.add_task(run_recheck_job, job_id, actor, manifest, version, candidates)
    return {"job_id": job_id, "status": "queued", "total": len(candidates)}


@admin_router.get("/recheck/{job_id}")
def recheck_progress(job_id: int, _: str = Depends(super_admin)):
    with store.closing(store.connect()) as db:
        job = db.execute("SELECT * FROM recheck_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "Перепроверка не найдена")
        rows = db.execute(
            "SELECT COALESCE(s.username,s.email) AS username,r.homework,r.task,r.verdict,r.points,r.message,r.elapsed_ms,r.created_at FROM recheck_results r JOIN students s ON s.id=r.student_id WHERE r.job_id=? ORDER BY r.created_at",
            (job_id,),
        ).fetchall()
    return {
        "job_id": job_id,
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "error": job["error"],
        "results": [dict(r) for r in rows],
    }


@admin_router.post("/students/{student_id}/deadline-override/{hw_id}")
def grant_deadline_override(student_id: int, hw_id: str, comment: str, actor: str = Depends(admin)):
    if len(comment.strip()) < 3 or len(comment) > 2000:
        record_audit(
            actor,
            "deadline_override_rejected",
            {"student_id": student_id, "homework": hw_id, "reason": "invalid_comment"},
        )
        raise HTTPException(422, "Укажите причину снятия ограничения (3–2000 символов)")
    with store.transaction() as db:
        target = db.execute(
            "SELECT COALESCE(username,email) AS username FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "Студент не найден")
        db.execute(
            "INSERT INTO deadline_overrides(student_id,homework,actor,comment,created_at) VALUES(?,?,?,?,?) ON CONFLICT(student_id,homework) DO UPDATE SET actor=excluded.actor,comment=excluded.comment,created_at=excluded.created_at",
            (student_id, hw_id, actor, comment.strip(), store.now_iso()),
        )
        store.audit(
            db,
            actor,
            "deadline_override_granted",
            json.dumps({"homework": hw_id, "comment": comment.strip()}, ensure_ascii=False),
            student_id,
        )
    return {"status": "enabled", "username": target["username"], "homework": hw_id}


@admin_router.post("/students/{student_id}/manual-grade/{hw_id}")
def manual_grade(student_id: int, hw_id: str, data: ManualGradeInput, actor: str = Depends(admin)):
    _, hw, task = task_for(hw_id, data.task)
    if data.points < 0 or data.points > task.points:
        record_audit(
            actor,
            "manual_grade_rejected",
            {
                "student_id": student_id,
                "homework": hw_id,
                "task": data.task,
                "points": str(data.points),
                "reason": "points_out_of_range",
            },
        )
        raise HTTPException(422, f"Баллы должны быть от 0 до {task.points}")
    timestamp = store.now_iso()
    with store.transaction() as db:
        target = db.execute(
            "SELECT COALESCE(username,email) AS username FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if not target:
            raise HTTPException(404, "Студент не найден")
        previous = highest_raw_points(db, student_id, hw_id, data.task)
        cur = db.execute(
            "INSERT INTO manual_grades(student_id,homework,task,points,comment,actor,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                student_id,
                hw_id,
                data.task,
                str(data.points),
                data.comment.strip(),
                actor,
                timestamp,
            ),
        )
        if data.points > previous:
            db.execute(
                "INSERT INTO achievements(student_id,homework,task,points,attempt_id,manual_grade_id,created_at) VALUES(?,?,?,?,NULL,?,?)",
                (student_id, hw_id, data.task, str(data.points), cur.lastrowid, timestamp),
            )
            store.audit(
                db,
                actor,
                "achievement_created",
                json.dumps(
                    {
                        "homework": hw_id,
                        "task": data.task,
                        "manual_grade_id": cur.lastrowid,
                        "previous_points": str(previous),
                        "points": str(data.points),
                        "max_points": str(task.points),
                    },
                    ensure_ascii=False,
                ),
                student_id,
            )
        store.audit(
            db,
            actor,
            "manual_grade",
            json.dumps(
                {
                    "homework": hw_id,
                    "task": data.task,
                    "points": str(data.points),
                    "comment": data.comment.strip(),
                },
                ensure_ascii=False,
            ),
            student_id,
        )
    return {"points": str(data.points), "improved": data.points > previous}


def _csv_attachment(output: io.StringIO, filename: str) -> Response:
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_points(value) -> str:
    return str(value).replace(".", ",")


@admin_router.get("/course-export/{kind}.csv")
def export_course_csv(kind: str, actor: str = Depends(admin)):
    if kind not in {"gradebook", "journal"}:
        raise HTTPException(404, "Неизвестная выгрузка")
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    with store.closing(store.connect()) as db:
        if kind == "journal":
            writer.writerow(
                ["username", "homework", "task", "verdict", "points", "message", "created_at"]
            )
            writer.writerows(
                [
                    [row[0], row[1], row[2], row[3], _csv_points(row[4]), row[5], row[6]]
                    for row in db.execute(
                        "SELECT username,homework,task,verdict,points,message,created_at FROM ("
                        "SELECT COALESCE(s.username,s.email) AS username,a.homework AS homework,a.task AS task,a.verdict AS verdict,a.points AS points,a.message AS message,a.created_at AS created_at "
                        "FROM attempts a JOIN students s ON s.id=a.student_id "
                        "UNION ALL "
                        "SELECT COALESCE(s.username,s.email) AS username,m.homework AS homework,m.task AS task,'MANUAL' AS verdict,m.points AS points,m.comment AS message,m.created_at AS created_at "
                        "FROM manual_grades m JOIN students s ON s.id=m.student_id "
                        "UNION ALL "
                        "SELECT COALESCE(s.username,s.email) AS username,r.homework AS homework,r.task AS task,'RECHECK_' || r.verdict AS verdict,r.points AS points,r.message AS message,r.created_at AS created_at "
                        "FROM recheck_results r JOIN students s ON s.id=r.student_id"
                        ") ORDER BY username,homework,task,created_at"
                    )
                ]
            )
        else:
            _, manifest, _ = active_manifest()
            header = ["username", "full_name"]
            for homework in manifest.homeworks:
                header.extend(f"{homework.id}_{task.id}" for task in homework.tasks)
                header.append(f"{homework.id}_sum")
            header.append("total")
            writer.writerow(header)
            for student in db.execute(
                "SELECT id,COALESCE(username,email) AS username,full_name FROM students ORDER BY username"
            ):
                cells = []
                grand = Decimal(0)
                for homework in manifest.homeworks:
                    scores = [
                        highest_raw_points(db, student["id"], homework.id, task.id)
                        for task in homework.tasks
                    ]
                    homework_sum = sum(scores, Decimal(0))
                    grand += homework_sum
                    cells.extend(_csv_points(score) for score in scores)
                    cells.append(_csv_points(homework_sum))
                writer.writerow(
                    [student["username"], student["full_name"], *cells, _csv_points(grand)]
                )
    record_audit(actor, "csv_exported", {"homework": "*", "kind": kind})
    filename = "course-gradebook.csv" if kind == "gradebook" else "course-journal.csv"
    return _csv_attachment(output, filename)


@admin_router.get("/export/{hw_id}/{kind}.csv")
def export_csv(hw_id: str, kind: str, actor: str = Depends(admin)):
    if kind not in {"results", "results_milestones", "results_details"}:
        raise HTTPException(404, "Неизвестная выгрузка")
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    with store.closing(store.connect()) as db:
        if kind == "results_details":
            writer.writerow(
                ["username", "homework", "task", "verdict", "points", "message", "created_at"]
            )
            writer.writerows(
                [
                    [r[0], r[1], r[2], r[3], str(r[4]).replace(".", ","), r[5], r[6]]
                    for r in db.execute(
                        "SELECT username,homework,task,verdict,points,message,created_at FROM (SELECT COALESCE(s.username,s.email) AS username,a.homework AS homework,a.task AS task,a.verdict AS verdict,a.points AS points,a.message AS message,a.created_at AS created_at FROM attempts a JOIN students s ON s.id=a.student_id WHERE a.homework=? UNION ALL SELECT COALESCE(s.username,s.email) AS username,m.homework AS homework,m.task AS task,'MANUAL' AS verdict,m.points AS points,m.comment AS message,m.created_at AS created_at FROM manual_grades m JOIN students s ON s.id=m.student_id WHERE m.homework=? UNION ALL SELECT COALESCE(s.username,s.email) AS username,r.homework AS homework,r.task AS task,'RECHECK_' || r.verdict AS verdict,r.points AS points,r.message AS message,r.created_at AS created_at FROM recheck_results r JOIN students s ON s.id=r.student_id WHERE r.homework=?) ORDER BY username,task,created_at",
                        (hw_id, hw_id, hw_id),
                    )
                ]
            )
        elif kind == "results_milestones":
            _, manifest, _ = active_manifest()
            hw = next((h for h in manifest.homeworks if h.id == hw_id), None)
            if not hw:
                raise HTTPException(404, "ДЗ не найдено")
            policy = manifest.policy
            writer.writerow(
                [
                    "username",
                    "homework",
                    "task",
                    "raw_points",
                    "final_points",
                    "achieved_at",
                    "soft_deadline",
                    "hard_deadline",
                    "formula",
                    "period_days",
                    "period_multiplier",
                    "max_penalty_fraction",
                    "minimum_score_fraction",
                    "round_digits",
                    "rounding_mode",
                ]
            )
            records = db.execute(
                "SELECT COALESCE(s.username,s.email),a.homework,a.task,a.points,a.created_at FROM achievements a JOIN students s ON s.id=a.student_id WHERE a.homework=? ORDER BY s.username,a.task,a.id",
                (hw_id,),
            ).fetchall()
            for row in records:
                score = Decimal(row[3])
                achieved = datetime.fromisoformat(row[4])
                soft = parse_deadline(hw.soft_deadline, manifest.timezone)
                adjusted = final_score(score, achieved, soft, policy)
                writer.writerow(
                    [
                        row[0],
                        row[1],
                        row[2],
                        str(score).replace(".", ","),
                        str(adjusted).replace(".", ","),
                        row[4],
                        hw.soft_deadline,
                        hw.hard_deadline,
                        policy.formula,
                        policy.period_days,
                        str(policy.period_multiplier).replace(".", ","),
                        str(policy.max_penalty_fraction).replace(".", ","),
                        str(policy.minimum_score_fraction).replace(".", ","),
                        policy.round_digits,
                        policy.rounding_mode,
                    ]
                )
        else:
            _, manifest, _ = active_manifest()
            hw = next((h for h in manifest.homeworks if h.id == hw_id), None)
            if not hw:
                raise HTTPException(404, "ДЗ не найдено")
            writer.writerow(["username", "full_name", *[t.id for t in hw.tasks], "sum"])
            for s in db.execute(
                "SELECT id,COALESCE(username,email) AS username,full_name FROM students ORDER BY username"
            ):
                vals = [highest_raw_points(db, s["id"], hw_id, t.id) for t in hw.tasks]
                total = sum(vals, Decimal(0))
                writer.writerow(
                    [
                        s["username"],
                        s["full_name"],
                        *[str(x).replace(".", ",") for x in vals],
                        str(total).replace(".", ","),
                    ]
                )
    record_audit(actor, "csv_exported", {"homework": hw_id, "kind": kind})
    return _csv_attachment(output, f"{kind}.csv")
