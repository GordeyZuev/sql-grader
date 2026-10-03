import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cabinet import api
from cabinet.infrastructure import sqlite_store as store


def test_api_routes_are_grouped_and_score_preview_is_bounded():
    routes = {route.path: route for route in api.app.routes if hasattr(route, "path")}
    assert "administration" in routes["/api/admin/students"].tags
    assert "authentication" in routes["/api/auth/login"].tags
    assert "coursework" in routes["/api/homeworks"].tags
    with pytest.raises(ValidationError):
        api.ScorePreviewRequest(achievements=[{}] * 10_001)
    with TestClient(api.app) as client:
        assert client.get("/health", headers={"content-length": "25000001"}).status_code == 413
        assert client.post("/api/score/preview", json={"achievements": []}).status_code == 403


def test_student_teacher_workflow_and_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    monkeypatch.setattr(api, "MANIFEST_FILE", Path(__file__).parents[1] / "manifest.example.json")
    monkeypatch.setenv("CABINET_ADMIN_TOKEN", "test-teacher-token")

    def fake_execute(sql, *, row_limit=200):
        value = "2" if "SELECT 2" in sql.upper() else "1"
        return ["answer"], [[value]], 1, 1

    monkeypatch.setattr(api, "execute_readonly", fake_execute)
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 200
        page = client.get("/").text
        assert "SQL Trainer" in page
        assert "Кабинет" not in page and "Практика и проверка запросов" not in page
        assert 'class="site-footer"' in page
        assert 'id="feedback-form"' not in page
        assert 'class="brand-mark"' not in page
        assert client.get("/static/app.js").status_code == 200
        auth_ui = client.get("/static/app.js").text
        assert "password-confirm" in auth_ui and "password-toggle" in auth_ui
        assert 'minlength="8"' in auth_ui and "защищённого хеша" not in auth_ui
        assert 'name="username"' in auth_ui and 'name="email"' not in auth_ui
        assert (
            "homework-tile" in auth_ui and "#homework/${encodeURIComponent(homework.id)}" in auth_ui
        )
        assert "homework-tile-deadlines" in auth_ui
        assert 'class="task-nav-row ${active ?' in auth_ui and "task-nav-state" in auth_ui
        assert ".task-nav-row.partial:not(.active)" in client.get("/static/style.css").text
        assert "/api/me/password" in auth_ui and "task-result-card" in auth_ui
        assert "profile-logout" in auth_ui and "id=\"logout\"" not in auth_ui
        assert "Проверка доступна." not in auth_ui
        assert "page-breadcrumb" in auth_ui and "Все задачи ДЗ" not in auth_ui
        assert "Лучший результат" not in auth_ui
        assert "Пишите запросы, запускайте их и сразу смотрите результат." not in auth_ui
        assert "sandbox-launch" in auth_ui
        assert client.get("/static/markdown.js").status_code == 200
        editor_ui = client.get("/static/sql-editor.js").text
        styles = client.get("/static/style.css").text
        assert "data-gutter" in editor_ui and "data-format" in editor_ui
        assert "data-editor-mode" not in editor_ui and "editor-mode-switch" not in editor_ui
        assert "psql-help" not in editor_ui and "<circle cx=\"12\" cy=\"12\" r=\"9\"" in editor_ui
        assert "⌨" not in editor_ui
        assert "Таблица результата. Прокручивайте" in auth_ui
        assert 'data-result-mode="pretty"' in editor_ui
        assert 'data-result-mode="simple"' in editor_ui
        assert "CabinetApp.dataTable" in editor_ui and "CabinetApp.textTable" in editor_ui
        assert "editor-hints" not in editor_ui
        assert "подсказки" not in editor_ui.lower()
        assert "task-requirements" in auth_ui and "эталон" not in auth_ui.lower()
        assert 'data-target="${id}"' in auth_ui
        assert "['current-password', 'current_password'" in auth_ui
        assert "['new-password', 'new_password'" in auth_ui
        assert "['confirm-password', 'password_confirm'" in auth_ui
        assert 'data-target="staff-phrase-input"' in auth_ui
        assert 'id="session-logout"' in auth_ui
        assert ".admin-pane { display: none;" in styles
        assert ".admin-pane.active { display: block;" in styles
        assert "success-symbol" not in editor_ui
        assert "PostgreSQL" not in editor_ui
        assert "CabinetApp.escape(value)" in editor_ui
        assert "Запустите запрос, чтобы увидеть таблицу или сообщение об ошибке." not in editor_ui
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "student_26",
                "full_name": "Student Example",
                "password": "long-example-password",
            },
        )
        assert registered.status_code == 200
        token = registered.json()["token"]
        student_headers = {"Authorization": f"Bearer {token}"}
        teacher_headers = {"X-Admin-Token": "test-teacher-token"}
        failed_login = client.post(
            "/api/auth/login", json={"username": "student_26", "password": "wrong-password-123"}
        )
        assert failed_login.status_code == 401
        throttled_user = client.post(
            "/api/auth/register",
            json={
                "username": "throttle_26",
                "full_name": "Throttle Example",
                "password": "long-example-password",
            },
        )
        assert throttled_user.status_code == 200
        for _ in range(10):
            assert client.post(
                "/api/auth/login",
                json={"username": "throttle_26", "password": "wrong-password-123"},
            ).status_code == 401
        throttled_login = client.post(
            "/api/auth/login",
            json={"username": "throttle_26", "password": "wrong-password-123"},
        )
        assert throttled_login.status_code == 429
        assert int(throttled_login.headers["Retry-After"]) > 0
        super_login = client.post("/api/admin/auth/token-login", headers=teacher_headers)
        assert super_login.status_code == 200
        created_admin = client.post(
            "/api/admin/accounts",
            headers=teacher_headers,
            json={"username": "assistant_1", "full_name": "Teaching Assistant"},
        )
        assert created_admin.status_code == 200
        temporary_admin_password = created_admin.json()["temporary_password"]
        failed_admin_login = client.post(
            "/api/admin/auth/login",
            json={"username": "assistant_1", "password": "wrong-password-123"},
        )
        assert failed_admin_login.status_code == 401
        admin_login = client.post(
            "/api/admin/auth/login",
            json={"username": "assistant_1", "password": temporary_admin_password},
        )
        assert admin_login.status_code == 200
        assistant_headers = {"X-Admin-Session": admin_login.json()["token"]}
        assert client.get("/api/admin/students", headers=assistant_headers).status_code == 200
        assert client.get("/api/admin/accounts", headers=assistant_headers).status_code == 403
        phrase_user = client.post(
            "/api/auth/register",
            json={
                "username": "phrase_26",
                "full_name": "Phrase User",
                "password": "long-example-password",
            },
        )
        assert phrase_user.status_code == 200
        phrase_headers = {"Authorization": f"Bearer {phrase_user.json()['token']}"}
        phrase_id = client.get("/api/me", headers=phrase_headers).json()["id"]
        assert client.get("/api/admin/students", headers=phrase_headers).status_code == 403
        assert (
            client.post(
                "/api/me/superadmin", headers=phrase_headers, json={"phrase": "not-the-phrase"}
            ).status_code
            == 403
        )
        claimed = client.post(
            "/api/me/superadmin",
            headers=phrase_headers,
            json={"phrase": "test-teacher-token"},
        )
        assert claimed.status_code == 200 and claimed.json()["is_superadmin"] is True
        assert client.get("/api/admin/audit", headers=phrase_headers).status_code == 200
        helper = client.post(
            "/api/auth/register",
            json={
                "username": "helper_26",
                "full_name": "Helper User",
                "password": "long-example-password",
            },
        )
        helper_headers = {"Authorization": f"Bearer {helper.json()['token']}"}
        helper_id = client.get("/api/me", headers=helper_headers).json()["id"]
        assert (
            client.patch(
                f"/api/admin/students/{helper_id}/role",
                headers=phrase_headers,
                json={"is_assistant": True},
            ).status_code
            == 200
        )
        assert client.get("/api/admin/students", headers=helper_headers).status_code == 200
        assert client.get("/api/admin/audit", headers=helper_headers).status_code == 403
        assert client.get("/api/admin/manifest", headers=helper_headers).status_code == 403
        assert client.post("/api/admin/publish", headers=helper_headers, json={"manifest": {}}).status_code == 403
        assert client.post("/api/admin/recheck", headers=helper_headers).status_code == 403
        assert client.get("/api/me", headers=phrase_headers).json()["id"] == phrase_id
        assert client.get("/api/admin/audit", headers=assistant_headers).status_code == 403
        assert client.post("/api/admin/publish", headers=assistant_headers, json={"manifest": {}}).status_code == 403
        assert client.post("/api/admin/recheck", headers=assistant_headers).status_code == 403
        assert (
            client.post(
                f"/api/admin/accounts/{created_admin.json()['id']}/reset-password",
                headers=assistant_headers,
            ).status_code
            == 403
        )
        assert client.post(
            "/api/auth/register",
            json={"username": "short_26", "full_name": "Short Password", "password": "1234567"},
        ).status_code == 422
        assert client.post(
            "/api/auth/register",
            json={"username": "eight_26", "full_name": "Eight Password", "password": "12345678"},
        ).status_code == 200
        assert client.get("/api/auth/username-availability", params={"username": "STUDENT_26"}).json() == {
            "valid": True,
            "available": False,
        }
        assert client.get("/api/auth/username-availability", params={"username": "new_user"}).json() == {
            "valid": True,
            "available": True,
        }
        assert client.get("/api/auth/username-availability", params={"username": "ab"}).json() == {
            "valid": False,
            "available": False,
        }

        assert client.get("/api/me", headers=student_headers).json()["username"] == "student_26"
        changed = client.patch(
            "/api/me", headers=student_headers, json={"full_name": "Student Changed"}
        )
        assert changed.status_code == 200
        password_change = client.post(
            "/api/me/password",
            headers=student_headers,
            json={
                "current_password": "long-example-password",
                "new_password": "another-long-password",
            },
        )
        assert password_change.status_code == 200
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "student_26", "password": "another-long-password"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/me/password",
                headers=student_headers,
                json={"current_password": "wrong-password", "new_password": "third-long-password"},
            ).status_code
            == 400
        )
        duplicate = client.post(
            "/api/auth/register",
            json={
                "username": "STUDENT_26",
                "full_name": "Duplicate",
                "password": "long-example-password",
            },
        )
        assert duplicate.status_code == 409

        manifest = json.loads((Path(__file__).parents[1] / "manifest.example.json").read_text())
        # Keep the fixture's deadlines in the future so this flow can grade normally.
        manifest["hard_deadline"] = "2099-10-10T23:59:00+03:00"
        manifest["homeworks"][0]["soft_deadline"] = "2099-09-25T23:59:00+03:00"
        manifest["homeworks"][0]["hard_deadline"] = "2099-11-10T23:59:00+03:00"
        second_task = dict(manifest["homeworks"][0]["tasks"][0])
        second_task.update(
            id="Q02",
            title="Required UNION",
            required=["union_distinct"],
            reference_sql="SELECT 1 AS answer UNION SELECT 1 AS answer",
        )
        manifest["homeworks"][0]["tasks"].append(second_task)
        preview = client.post(
            "/api/admin/import/preview", headers=teacher_headers, json={"manifest": manifest}
        )
        assert preview.status_code == 200
        assert preview.json()["penalty"]["max_penalty_fraction"] == "0.4"
        assert preview.json()["hard_deadline"] == manifest["hard_deadline"]
        assert (
            preview.json()["homeworks"][0]["hard_deadline"]
            == manifest["homeworks"][0]["hard_deadline"]
        )
        assert preview.json()["homeworks"][0]["tasks"][0]["title"] != "Q01"
        published = client.post(
            "/api/admin/publish", headers=teacher_headers, json={"manifest": manifest}
        )
        assert published.status_code == 200

        homework = manifest["homeworks"][0]["id"]
        task = manifest["homeworks"][0]["tasks"][0]["id"]
        task_url = f"/api/homeworks/{homework}/tasks/{task}"
        assert (
            client.put(
                task_url + "/draft", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
            ).status_code
            == 200
        )
        assert client.get(task_url, headers=student_headers).json()["draft"] == "SELECT 1 AS answer"
        run = client.post(
            task_url + "/run", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        assert run["status"] == "ok"
        assert run["row_count"] == 1
        first = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        second = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        wrong = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 2 AS answer"}
        ).json()
        assert first["points"] == "1" and first["improved"]
        assert first["query_status"] == "ok" and first["row_count"] == 1
        assert isinstance(first["elapsed_ms"], int)
        assert not second["improved"]
        assert wrong["points"] == "0"
        assert wrong["message"] == "Не совпадает строка 1"
        task_state = client.get(task_url, headers=student_headers).json()
        assert task_state["homework"]["hard_deadline"] == manifest["homeworks"][0]["hard_deadline"]
        assert task_state["homework"]["grading_open"] is True
        assert len(task_state["attempts"]) == 3
        assert len(task_state["runs"]) == 1
        assert task_state["runs"][0]["result"]["rows"] == [["1"]]
        assert task_state["score"] == "1"
        assert task_state["attempts"][0]["query_status"] == "ok"
        assert isinstance(task_state["attempts"][0]["elapsed_ms"], int)
        assert task_state["attempts"][0]["elapsed_ms"] >= 0
        assert task_state["attempts"][0]["row_count"] == 1
        assert task_state["attempts"][0]["result"]["columns"] == ["answer"]
        assert task_state["attempts"][0]["result"]["rows"] == [["2"]]
        query_history = client.get(task_url + "/history?offset=0&limit=2", headers=student_headers).json()
        assert query_history["total"] == 4
        assert query_history["has_more"] is True
        assert [row["kind"] for row in query_history["items"]] == ["check", "check"]
        older_history = client.get(
            task_url + "/history?offset=2&limit=2", headers=student_headers
        ).json()
        assert older_history["has_more"] is False
        assert any(row["kind"] == "task" and row["result"]["rows"] == [["1"]] for row in older_history["items"])
        assert client.get("/api/sandbox/history", headers=student_headers).json() == []
        sandbox_run = client.post(
            "/api/run", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        )
        assert sandbox_run.status_code == 200
        sandbox_history = client.get("/api/sandbox/history", headers=student_headers).json()
        assert len(sandbox_history) == 1
        assert sandbox_history[0]["result"]["rows"] == [["1"]]
        formatted = client.post(
            "/api/format",
            headers=student_headers,
            json={"sql": "select answer from demo where answer=1"},
        )
        assert formatted.status_code == 200
        assert "select" in formatted.json()["sql"]
        case_preserved = client.post(
            "/api/format",
            headers=student_headers,
            json={"sql": "SELECT airplane_code, model, range FROM airplanes ORDER BY range, airplane_code"},
        )
        assert case_preserved.status_code == 200
        assert "range" in case_preserved.json()["sql"]
        assert "RANGE" not in case_preserved.json()["sql"]
        forbidden_format = client.post(
            "/api/format", headers=student_headers, json={"sql": "DROP TABLE students"}
        )
        assert forbidden_format.status_code == 422

        structure_url = f"/api/homeworks/{homework}/tasks/Q02"
        structured = client.post(
            structure_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        wrong_structure = client.post(
            structure_url + "/check", headers=student_headers, json={"sql": "SELECT 2 AS answer"}
        ).json()
        assert structured["verdict"] == "STRUCT" and structured["points"] == "0.5"
        assert "UNION" in structured["message"]
        assert wrong_structure["verdict"] == "WRONG" and wrong_structure["points"] == "0"
        filtered = client.get(
            "/api/admin/students",
            headers=teacher_headers,
            params={"homework": homework, "status": "partial"},
        )
        assert filtered.status_code == 200 and filtered.json()[0]["status"] == "partial"

        sandbox = client.post(
            "/api/run", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        assert sandbox["status"] == "ok"
        sandbox_history = client.get("/api/sandbox/history", headers=student_headers).json()
        assert len(sandbox_history) == 2
        assert sandbox_history[0]["result"]["rows"] == [["1"]]

        monkeypatch.setattr(api, "grading_open", lambda *_args: False)
        late = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        assert late["verdict"] == "HARD_LATE" and late["points"] == "0"
        student_id = client.get("/api/me", headers=student_headers).json()["id"]
        allowed = client.post(
            f"/api/admin/students/{student_id}/deadline-override/{homework}",
            headers=teacher_headers,
            params={"comment": "Approved extension"},
        )
        assert allowed.status_code == 200
        allowed_check = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        ).json()
        assert allowed_check["points"] == "1"

        recheck = client.post(
            "/api/admin/recheck", headers=teacher_headers, params={"homework": homework}
        )
        assert recheck.status_code == 202
        progress = client.get(
            f"/api/admin/recheck/{recheck.json()['job_id']}", headers=teacher_headers
        ).json()
        assert progress["status"] == "completed"
        assert progress["total"] == progress["completed"] == 2

        manual = client.post(
            f"/api/admin/students/{student_id}/manual-grade/{homework}",
            headers=assistant_headers,
            json={"task": task, "points": "0.5", "comment": "Manual review"},
        )
        assert manual.status_code == 200
        assert not manual.json()["improved"]
        corrected_task = client.get(task_url, headers=student_headers).json()
        assert len(corrected_task["attempts"]) == 5
        assert corrected_task["score"] == "0.5"
        assert corrected_task["manual_grade"]["comment"] == "Manual review"
        assert corrected_task["manual_grade"]["actor"] == "assistant_1"
        held = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 2 AS answer"}
        ).json()
        assert held["points"] == "0"
        assert held["score"] == "0.5"
        assert held["score_locked"] is True
        assert not held["improved"]
        locked_check = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 1 AS answer"}
        )
        assert locked_check.status_code == 200
        assert locked_check.json()["points"] == "1"
        assert locked_check.json()["score"] == "1"
        assert locked_check.json()["score_locked"] is False
        assert locked_check.json()["improved"]
        assert client.get(task_url, headers=student_headers).json()["score"] == "1"
        later_miss = client.post(
            task_url + "/check", headers=student_headers, json={"sql": "SELECT 2 AS answer"}
        ).json()
        assert later_miss["points"] == "0"
        assert later_miss["score"] == "1"
        assert later_miss["score_locked"] is False
        assert not later_miss["improved"]
        assert client.get(
            f"/api/admin/students/{student_id}", headers=teacher_headers
        ).json()["homeworks"][0]["tasks"][task]["score"] == "1"
        corrected_filter = client.get(
            "/api/admin/students",
            headers=teacher_headers,
            params={"homework": homework, "task": task, "status": "complete"},
        )
        assert corrected_filter.json()[0]["score"] == "1"
        history = client.get(
            f"/api/admin/students/{student_id}/history", headers=teacher_headers
        ).json()
        assert any(item["action"] == "deadline_override_granted" for item in history)
        assert any(item["action"] == "profile_update" for item in history)
        milestones = client.get(
            f"/api/admin/export/{homework}/results_milestones.csv", headers=teacher_headers
        )
        assert milestones.status_code == 200
        assert milestones.content.startswith(b"\xef\xbb\xbf")
        assert b"max_penalty_fraction" in milestones.content
        details = client.get(
            f"/api/admin/export/{homework}/results_details.csv", headers=teacher_headers
        )
        assert b"HARD_LATE" in details.content
        assert b"MANUAL" in details.content
        gradebook = client.get(
            "/api/admin/course-export/gradebook.csv", headers=teacher_headers
        )
        assert gradebook.status_code == 200
        assert b"total" in gradebook.content and b"student_26" in gradebook.content
        journal = client.get("/api/admin/course-export/journal.csv", headers=teacher_headers)
        assert journal.status_code == 200
        assert b"MANUAL" in journal.content
        assert (
            client.get("/api/admin/course-export/milestones.csv", headers=teacher_headers).status_code
            == 404
        )
        assert client.post("/api/admin/auth/logout", headers=teacher_headers).status_code == 200

        logged_out = client.post("/api/auth/logout", headers=student_headers)
        assert logged_out.status_code == 200
        assert client.get("/api/me", headers=student_headers).status_code == 401
        active_session = client.post(
            "/api/auth/login",
            json={"username": "student_26", "password": "another-long-password"},
        ).json()["token"]

        reset = client.post(
            f"/api/admin/students/{student_id}/reset-password", headers=assistant_headers
        )
        assert reset.status_code == 200
        assert client.get(
            "/api/me", headers={"Authorization": f"Bearer {active_session}"}
        ).status_code == 401
        assert reset.json()["temporary_password"] not in json.dumps(
            client.get(f"/api/admin/students/{student_id}/history", headers=teacher_headers).json()
        )
        assert client.post("/api/admin/auth/logout", headers=assistant_headers).status_code == 200
        assert client.post(
            "/api/auth/login",
            json={"username": "student_26", "password": reset.json()["temporary_password"]},
        ).status_code == 200
        audit_rows = client.get("/api/admin/audit", headers=teacher_headers).json()
        audit_actions = {item["action"] for item in audit_rows}
        assert {
            "login_failed",
            "admin_login_failed",
            "super_admin_login",
            "admin_logout",
            "manifest_previewed",
            "manifest_published",
            "query_run",
            "task_checked",
            "achievement_created",
            "password_change_failed",
            "deadline_override_granted",
            "manual_grade",
            "recheck_started",
            "recheck_completed",
            "csv_exported",
            "student_password_reset",
        }.issubset(audit_actions)
        assert reset.json()["temporary_password"] not in json.dumps(audit_rows)
        reset_admin = client.post(
            f"/api/admin/accounts/{created_admin.json()['id']}/reset-password",
            headers=teacher_headers,
        )
        assert reset_admin.status_code == 200
        assert reset_admin.json()["temporary_password"] not in json.dumps(
            client.get("/api/admin/audit", headers=teacher_headers).json()
        )
        assert (
            client.post(
                "/api/admin/auth/login",
                json={
                    "username": "assistant_1",
                    "password": reset_admin.json()["temporary_password"],
                },
            ).status_code
            == 200
        )
        global_page_1 = client.get(
            "/api/admin/audit?limit=1&offset=0", headers=teacher_headers
        ).json()
        global_page_2 = client.get(
            "/api/admin/audit?limit=1&offset=1", headers=teacher_headers
        ).json()
        assert len(global_page_1) == len(global_page_2) == 1
        assert global_page_1[0]["action"] != global_page_2[0]["action"]
        student_page = client.get(
            f"/api/admin/students/{student_id}/history?limit=1&offset=0",
            headers=teacher_headers,
        ).json()
        assert len(student_page) == 1


@pytest.mark.skipif(
    not (Path(__file__).parents[1] / "manifest.course.json").is_file(),
    reason="course manifest stays on disk and out of the public repository",
)
def test_course_manifest_allows_student_to_open_cabinet_before_teacher_publish(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    monkeypatch.setattr(
        api,
        "MANIFEST_FILE",
        Path(__file__).parents[1] / "manifest.course.json",
    )
    with TestClient(api.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "full-course",
                "full_name": "Full Course",
                "password": "course-test-password",
            },
        )
        assert registered.status_code == 200
        response = client.get(
            "/api/homeworks",
            headers={"Authorization": f"Bearer {registered.json()['token']}"},
        )
        assert response.status_code == 200
        homeworks = response.json()
        assert len(homeworks) == 4
        assert sum(len(homework["tasks"]) for homework in homeworks) == 71
        task = client.get(
            "/api/homeworks/hw1/tasks/Q03",
            headers={"Authorization": f"Bearer {registered.json()['token']}"},
        )
        assert task.status_code == 200
        assert "hints" not in task.json()["task"]
        task_headers = {"Authorization": f"Bearer {registered.json()['token']}"}
        for task_id, sql in (("Q01", "SELECT 1"), ("Q02", "SELECT 2")):
            draft_url = f"/api/homeworks/hw1/tasks/{task_id}/draft"
            assert client.put(draft_url, headers=task_headers, json={"sql": sql}).status_code == 200
            assert client.get(
                f"/api/homeworks/hw1/tasks/{task_id}", headers=task_headers
            ).json()["draft"] == sql


@pytest.mark.skipif(
    not (Path(__file__).parents[1] / "manifest.course.json").is_file(),
    reason="course manifest stays on disk and out of the public repository",
)
def test_bundled_manifest_can_grade_before_teacher_publish(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    monkeypatch.setattr(api, "MANIFEST_FILE", Path(__file__).parents[1] / "manifest.course.json")
    monkeypatch.setattr(api, "grading_open", lambda *_args: True)
    monkeypatch.setattr(
        api,
        "execute_readonly",
        lambda _sql, *, row_limit=200: (["answer"], [["1"]], 1, 1),
    )
    with TestClient(api.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "before_publish",
                "full_name": "Student",
                "password": "course-test-password",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}
        checked = client.post(
            "/api/homeworks/hw1/tasks/Q01/check", headers=headers, json={"sql": "SELECT 1"}
        )
        assert checked.status_code == 200
        assert checked.json()["verdict"] == "OK"
        assert checked.json()["points"] == "1"


def test_sql_syntax_errors_keep_postgres_reason(monkeypatch):
    class PgSyntaxError(Exception):
        sqlstate = "42601"

    def fail_on_student_query(_sql, *, row_limit=200):
        raise PgSyntaxError('syntax error at or near "FROM"')

    monkeypatch.setattr(
        api, "get_reference_result", lambda *_args: {"columns": ["x"], "rows": [["1"]]}
    )
    monkeypatch.setattr(api, "execute_readonly", fail_on_student_query)
    task = SimpleNamespace(id="Q01", required=[], points=Decimal("1"))
    verdict, points, message, _elapsed, _rows, status, output = api.evaluate_task(
        "hw1", task, None, "SELECT FROM"
    )
    assert verdict == "SYNTAX"
    assert points == 0 and status == "error"
    assert 'syntax error at or near "FROM"' in message
    assert output["error"] == 'syntax error at or near "FROM"'


def test_staff_doors_and_empty_course(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    monkeypatch.setattr(api, "MANIFEST_FILE", tmp_path / "missing-manifest.json")
    monkeypatch.setenv("CABINET_ADMIN_TOKEN", "test-teacher-token")
    with TestClient(api.app) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "username": "plain_student",
                "full_name": "Plain Student",
                "password": "long-example-password",
            },
        )
        assert registered.status_code == 200
        student_headers = {"Authorization": f"Bearer {registered.json()['token']}"}
        denied = client.post(
            "/api/auth/assistant",
            json={"username": "plain_student", "password": "long-example-password"},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Эта учётная запись не ассистент. Вход закрыт."
        assert client.post(
            "/api/auth/assistant",
            json={"username": "plain_student", "password": "wrong-password"},
        ).status_code == 401
        wrong_key = client.post(
            "/api/auth/administrator",
            json={
                "username": "plain_student",
                "password": "long-example-password",
                "phrase": "wrong-key",
            },
        )
        assert wrong_key.status_code == 403
        assert wrong_key.json()["detail"] == "Ключ не подошёл"
        assert client.get("/api/me", headers=student_headers).json()["is_superadmin"] is False
        opened = client.post(
            "/api/auth/administrator",
            json={
                "username": "plain_student",
                "password": "long-example-password",
                "phrase": "test-teacher-token",
            },
        )
        assert opened.status_code == 200
        admin_headers = {"Authorization": f"Bearer {opened.json()['token']}"}
        assert client.get("/api/me", headers=admin_headers).json()["is_superadmin"] is True
        still_student = client.post(
            "/api/auth/assistant",
            json={"username": "plain_student", "password": "long-example-password"},
        )
        assert still_student.status_code == 403
        assert still_student.json()["detail"] == "Эта учётная запись не ассистент. Вход закрыт."
        manifest = client.get("/api/admin/manifest", headers=admin_headers)
        assert manifest.status_code == 200
        assert manifest.json() == {"manifest": None, "version": None, "source": "empty"}
        helper = client.post(
            "/api/auth/register",
            json={
                "username": "helper_door",
                "full_name": "Helper Door",
                "password": "long-example-password",
            },
        )
        helper_id = client.get(
            "/api/me", headers={"Authorization": f"Bearer {helper.json()['token']}"}
        ).json()["id"]
        assert client.patch(
            f"/api/admin/students/{helper_id}/role",
            headers=admin_headers,
            json={"is_assistant": True},
        ).status_code == 200
        assistant = client.post(
            "/api/auth/assistant",
            json={"username": "helper_door", "password": "long-example-password"},
        )
        assert assistant.status_code == 200
        assistant_headers = {"Authorization": f"Bearer {assistant.json()['token']}"}
        assert client.get("/api/admin/students", headers=assistant_headers).status_code == 200
        assert client.get("/api/admin/manifest", headers=assistant_headers).status_code == 403
