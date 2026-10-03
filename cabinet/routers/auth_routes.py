# ruff: noqa: I001
"""HTTP endpoints for this domain."""

from fastapi.exceptions import (
    HTTPException,
)
from fastapi import (
    Request,
)
from fastapi.param_functions import (
    Depends,
    Header,
)
from typing import (
    Annotated,
)
import cabinet.infrastructure.sqlite_store as store
import hmac
import json
import os
import re
from cabinet.security.login_limiter import (
    login_rate_limiter,
    request_client_ip,
)
from cabinet.api import (
    AdministratorLoginInput,
    AuthInput,
    PasswordChangeInput,
    ProfileInput,
    RegisterInput,
    SuperadminPhraseInput,
    public_user,
    record_audit,
    student,
)
from cabinet.routers.auth import router as auth_router

@auth_router.post("/auth/register")
def register(data: RegisterInput):
    username = data.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,32}", username):
        raise HTTPException(
            422, "Логин: 4–32 символа, латинские буквы, цифры, точка, дефис или подчёркивание"
        )
    try:
        with store.transaction() as db:
            cur = db.execute(
                "INSERT INTO students(email,username,full_name,password_hash,created_at) VALUES(?,?,?,?,?)",
                (
                    f"{username.lower()}@cabinet.local",
                    username,
                    data.full_name.strip(),
                    store.hash_password(data.password),
                    store.now_iso(),
                ),
            )
            token = store.issue_session(db, cur.lastrowid)
            store.audit(db, username, "register", student_id=cur.lastrowid)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            record_audit(username, "registration_failed", {"username": username, "reason": "username_taken"})
            raise HTTPException(409, "Этот логин уже занят") from exc
        raise
    return {"token": token, "username": username, "full_name": data.full_name.strip()}


@auth_router.get("/auth/username-availability")
def username_availability(username: str):
    username = username.strip()
    valid = bool(re.fullmatch(r"[A-Za-z0-9._-]{4,32}", username))
    if not valid:
        return {"valid": False, "available": False}
    with store.closing(store.connect()) as db:
        exists = db.execute(
            "SELECT 1 FROM students WHERE username=? COLLATE NOCASE", (username,)
        ).fetchone()
    return {"valid": True, "available": exists is None}


@auth_router.post("/auth/login")
def login(data: AuthInput, request: Request):
    attempted_username = data.username.strip()
    client_ip = request_client_ip(request)
    login_rate_limiter.consume(
        scope="student",
        client_ip=client_ip,
        username=attempted_username,
        account_limit=10,
        address_limit=300,
    )
    with store.closing(store.connect()) as db:
        row = db.execute(
            "SELECT * FROM students WHERE username=? COLLATE NOCASE", (data.username.strip(),)
        ).fetchone()
    if not row or not store.verify_password(data.password, row["password_hash"]):
        record_audit(
            attempted_username or "anonymous",
            "login_failed",
            {"username": attempted_username},
        )
        raise HTTPException(401, "Неверный логин или пароль")
    with store.transaction() as db:
        token = store.issue_session(db, row["id"])
        store.audit(db, row["username"], "login", student_id=row["id"])
    login_rate_limiter.reset_account(
        scope="student", client_ip=client_ip, username=attempted_username
    )
    return {"token": token, "username": row["username"], "full_name": row["full_name"]}


@auth_router.post("/auth/assistant")
def assistant_login(data: AuthInput, request: Request):
    client_ip = request_client_ip(request)
    attempted_username = data.username.strip()
    login_rate_limiter.consume(
        scope="assistant",
        client_ip=client_ip,
        username=attempted_username,
        account_limit=10,
        address_limit=300,
    )
    with store.closing(store.connect()) as db:
        row = db.execute("SELECT * FROM students WHERE username=? COLLATE NOCASE", (attempted_username,)).fetchone()
    if not row or not store.verify_password(data.password, row["password_hash"]):
        record_audit(attempted_username, "login_failed", {"ip": client_ip})
        raise HTTPException(401, "Неверный логин или пароль")
    if not row["is_assistant"]:
        record_audit(row["username"], "assistant_login_denied", {}, row["id"])
        raise HTTPException(403, "Эта учётная запись не ассистент. Вход закрыт.")
    with store.transaction() as db:
        token = store.issue_session(db, row["id"])
        store.audit(db, row["username"], "assistant_login", student_id=row["id"])
    login_rate_limiter.reset_account(
        scope="assistant", client_ip=client_ip, username=attempted_username
    )
    return {"token": token, "username": row["username"], "full_name": row["full_name"]}


@auth_router.post("/auth/administrator")
def administrator_login(data: AdministratorLoginInput, request: Request):
    client_ip = request_client_ip(request)
    attempted_username = data.username.strip()
    login_rate_limiter.consume(
        scope="superadmin",
        client_ip=client_ip,
        username=attempted_username,
        account_limit=5,
        address_limit=20,
    )
    with store.closing(store.connect()) as db:
        row = db.execute("SELECT * FROM students WHERE username=? COLLATE NOCASE", (attempted_username,)).fetchone()
    if not row or not store.verify_password(data.password, row["password_hash"]):
        record_audit(attempted_username, "login_failed", {"ip": client_ip})
        raise HTTPException(401, "Неверный логин или пароль")
    expected = os.getenv("CABINET_ADMIN_TOKEN", "")
    phrase = data.phrase.strip()
    if not expected or not hmac.compare_digest(expected, phrase):
        record_audit(row["username"], "superadmin_claim_failed", {}, row["id"])
        raise HTTPException(403, "Ключ не подошёл")
    with store.transaction() as db:
        db.execute("UPDATE students SET is_superadmin=1 WHERE id=?", (row["id"],))
        token = store.issue_session(db, row["id"])
        store.audit(db, row["username"], "super_admin_login", "{}", row["id"])
    login_rate_limiter.reset_account(
        scope="superadmin", client_ip=client_ip, username=attempted_username
    )
    return {"token": token, "username": row["username"], "full_name": row["full_name"]}


@auth_router.get("/me")
def me(user=Depends(student)):
    return public_user(user)


@auth_router.post("/me/superadmin")
def claim_superadmin(data: SuperadminPhraseInput, request: Request, user=Depends(student)):
    client_ip = request_client_ip(request)
    login_rate_limiter.consume(
        scope="superadmin",
        client_ip=client_ip,
        username=user["username"],
        account_limit=5,
        address_limit=20,
    )
    expected = os.getenv("CABINET_ADMIN_TOKEN", "")
    phrase = data.phrase.strip()
    if not expected or not hmac.compare_digest(expected, phrase):
        record_audit(user["username"], "superadmin_claim_failed", {}, user["id"])
        raise HTTPException(403, "Ключ не подошёл")
    with store.transaction() as db:
        db.execute("UPDATE students SET is_superadmin=1 WHERE id=?", (user["id"],))
        store.audit(db, user["username"], "superadmin_claimed", "{}", user["id"])
    login_rate_limiter.reset_account(scope="superadmin", client_ip=client_ip, username=user["username"])
    return public_user({**dict(user), "is_superadmin": 1})


@auth_router.post("/auth/logout")
def logout(authorization: Annotated[str | None, Header()] = None, user=Depends(student)):
    token = authorization[7:]
    token_hash = __import__("hashlib").sha256(token.encode()).hexdigest()
    with store.transaction() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        store.audit(db, user["username"], "logout", student_id=user["id"])
    return {"status": "ok"}


@auth_router.patch("/me")
def update_me(data: ProfileInput, user=Depends(student)):
    with store.transaction() as db:
        db.execute(
            "UPDATE students SET full_name=? WHERE id=?", (data.full_name.strip(), user["id"])
        )
        store.audit(
            db,
            user["username"],
            "profile_update",
            json.dumps({"full_name": data.full_name}, ensure_ascii=False),
            user["id"],
        )
    return {"full_name": data.full_name.strip()}


@auth_router.post("/me/password")
def change_password(data: PasswordChangeInput, user=Depends(student)):
    password_valid = False
    with store.transaction() as db:
        row = db.execute("SELECT password_hash FROM students WHERE id=?", (user["id"],)).fetchone()
        if not row or not store.verify_password(data.current_password, row["password_hash"]):
            store.audit(
                db,
                user["username"],
                "password_change_failed",
                json.dumps({"reason": "current_password_mismatch"}),
                user["id"],
            )
        else:
            db.execute(
                "UPDATE students SET password_hash=? WHERE id=?",
                (store.hash_password(data.new_password), user["id"]),
            )
            store.audit(db, user["username"], "password_change", student_id=user["id"])
            password_valid = True
    if not password_valid:
        raise HTTPException(400, "Текущий пароль не совпадает")
    return {"status": "ok"}
