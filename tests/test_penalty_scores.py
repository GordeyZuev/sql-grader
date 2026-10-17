import json
from decimal import Decimal

from fastapi.testclient import TestClient

from cabinet import api
from cabinet.infrastructure import sqlite_store as store

MANIFEST = {
    "schema_version": 1,
    "timezone": "Europe/Moscow",
    "hard_deadline": "2099-12-31T23:59:00+03:00",
    "penalty": {
        "formula": "score - late_days * 0.1",
        "period_days": 7,
        "period_multiplier": 0.8,
        "minimum_score_fraction": 0,
        "max_penalty_fraction": 0.8,
        "round_digits": 2,
        "rounding_mode": "half_up",
    },
    "homeworks": [
        {
            "id": "hw1",
            "title": "ДЗ 1",
            "order": 1,
            "soft_deadline": "2026-01-01T00:00:00+03:00",
            "hard_deadline": "2099-12-31T23:59:00+03:00",
            "tasks": [
                {
                    "id": "Q01",
                    "title": "Task",
                    "statement": "Select one.",
                    "points": 1,
                    "required": [],
                    "hints": [],
                    "reference_sql": "SELECT 1 AS answer",
                },
                {
                    "id": "Q02",
                    "title": "Second",
                    "statement": "Select one.",
                    "points": 1,
                    "required": [],
                    "hints": [],
                    "reference_sql": "SELECT 1 AS answer",
                },
            ],
        }
    ],
}


def _client(tmp_path, monkeypatch, manifest=None):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "cabinet.sqlite3")
    monkeypatch.setattr(api, "MANIFEST_FILE", tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest or MANIFEST), encoding="utf-8"
    )
    monkeypatch.setenv("CABINET_ADMIN_TOKEN", "test-teacher-token")
    monkeypatch.setattr(
        api,
        "execute_readonly",
        lambda sql, *, row_limit=200: (["answer"], [["1"]], 1, 1),
    )
    return TestClient(api.app)


def _register(client, username="student_26"):
    token = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "full_name": "Student Example",
            "password": "long-example-password",
        },
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    student_id = client.get("/api/me", headers=headers).json()["id"]
    return headers, student_id


def _add_achievement(student_id, homework, task, points, created_at):
    with store.transaction() as db:
        attempt = db.execute(
            "INSERT INTO attempts(student_id,query_run_id,homework,task,sql,points,verdict,message,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (student_id, None, homework, task, "SELECT 1", str(points), "OK", "", created_at),
        )
        db.execute(
            "INSERT INTO achievements(student_id,homework,task,points,attempt_id,manual_grade_id,created_at) "
            "VALUES(?,?,?,?,?,NULL,?)",
            (student_id, homework, task, str(points), attempt.lastrowid, created_at),
        )


def test_homeworks_keep_best_adjusted_score(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        headers, student_id = _register(client)
        empty = client.get("/api/homeworks", headers=headers).json()
        assert empty[0]["score"] == "0"
        assert empty[0]["final_score"] == "0"
        assert empty[0]["tasks"][0]["score"] == "0"
        assert empty[0]["tasks"][0]["final_score"] == "0"

        _add_achievement(student_id, "hw1", "Q01", "0.5", "2026-01-02T00:00:00+03:00")
        _add_achievement(student_id, "hw1", "Q01", "1", "2026-01-08T00:00:00+03:00")

        homeworks = client.get("/api/homeworks", headers=headers).json()
        task = homeworks[0]["tasks"][0]
        assert task["score"] == "1"
        assert Decimal(task["final_score"]) == Decimal("0.40")
        assert homeworks[0]["score"] == "1"
        assert Decimal(homeworks[0]["final_score"]) == Decimal("0.40")

        detail = client.get("/api/homeworks/hw1/tasks/Q01", headers=headers).json()
        assert detail["score"] == "1"
        assert Decimal(detail["final_score"]) == Decimal("0.40")

        checked = client.post(
            "/api/homeworks/hw1/tasks/Q01/check",
            headers=headers,
            json={"sql": "SELECT 1 AS answer"},
        ).json()
        assert checked["score"] == "1"
        assert "final_score" in checked


def test_manual_grade_does_not_erase_best_adjusted(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        headers, student_id = _register(client)
        teacher = {"X-Admin-Token": "test-teacher-token"}
        _add_achievement(student_id, "hw1", "Q01", "0.5", "2026-01-02T00:00:00+03:00")
        lowered = client.post(
            f"/api/admin/students/{student_id}/manual-grade/hw1",
            headers=teacher,
            json={"task": "Q01", "points": "0.2", "comment": "Снизили оценку вручную"},
        )
        assert lowered.status_code == 200
        detail = client.get("/api/homeworks/hw1/tasks/Q01", headers=headers).json()
        assert detail["score"] == "0.2"
        assert Decimal(detail["final_score"]) == Decimal("0.40")
        admin_detail = client.get(f"/api/admin/students/{student_id}", headers=teacher).json()
        assert admin_detail["homeworks"][0]["tasks"]["Q01"]["score"] == "0.2"
        assert Decimal(admin_detail["homeworks"][0]["tasks"]["Q01"]["final_score"]) == Decimal(
            "0.40"
        )


def test_admin_queue_and_final_export(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        headers, student_id = _register(client)
        teacher = {"X-Admin-Token": "test-teacher-token"}
        _add_achievement(student_id, "hw1", "Q01", "1", "2026-01-01T00:00:00+03:00")
        _add_achievement(student_id, "hw1", "Q02", "0.5", "2026-01-02T00:00:00+03:00")

        listed = client.get(
            "/api/admin/students", headers=teacher, params={"homework": "hw1"}
        ).json()
        row = next(item for item in listed["students"] if item["id"] == student_id)
        assert Decimal(row["score"]) == Decimal("1.5")
        assert Decimal(row["final_score"]) == Decimal("1.40")
        assert row["status"] == "partial"
        assert [item["id"] for item in listed["homeworks"]] == ["hw1"]
        assert [cell["id"] for cell in row["homeworks"]] == ["hw1"]

        complete = client.get(
            "/api/admin/students",
            headers=teacher,
            params={"homework": "hw1", "task": "Q01", "status": "complete"},
        ).json()
        assert complete["students"][0]["id"] == student_id
        assert complete["students"][0]["score"] == "1"
        assert Decimal(complete["students"][0]["final_score"]) == Decimal("1")

        raw = client.get("/api/admin/export/hw1/results.csv", headers=teacher)
        final = client.get("/api/admin/export/hw1/results_final.csv", headers=teacher)
        milestones = client.get("/api/admin/export/hw1/results_milestones.csv", headers=teacher)
        assert raw.status_code == 200
        assert final.status_code == 200
        assert b"Q01" in raw.content and b"sum" in raw.content
        assert b"0,4" in final.content or b"0.40" in final.content or b"1,4" in final.content
        assert b"final_points" in milestones.content

        assistant = client.post(
            "/api/auth/register",
            json={
                "username": "helper_26",
                "full_name": "Helper",
                "password": "long-example-password",
            },
        )
        helper_headers = {"Authorization": f"Bearer {assistant.json()['token']}"}
        helper_id = client.get("/api/me", headers=helper_headers).json()["id"]
        client.patch(
            f"/api/admin/students/{helper_id}/role",
            headers=teacher,
            json={"is_assistant": True},
        )
        assert client.get("/api/admin/manifest", headers=helper_headers).status_code == 403
        assert client.get("/api/homeworks", headers=helper_headers).status_code == 200
        assert (
            client.get(
                "/api/admin/export/hw1/results_final.csv", headers=helper_headers
            ).status_code
            == 200
        )


def test_gradebook_cells_follow_manifest_and_paginate(tmp_path, monkeypatch):
    course = json.loads(json.dumps(MANIFEST))
    course["homeworks"].append(
        {
            "id": "hw2",
            "title": "ДЗ 2",
            "order": 2,
            "soft_deadline": "2026-01-01T00:00:00+03:00",
            "hard_deadline": "2099-12-31T23:59:00+03:00",
            "tasks": [
                {
                    "id": "Q01",
                    "title": "Task",
                    "statement": "Select one.",
                    "points": 1,
                    "required": [],
                    "hints": [],
                    "reference_sql": "SELECT 1 AS answer",
                }
            ],
        }
    )
    with _client(tmp_path, monkeypatch, course) as client:
        teacher = {"X-Admin-Token": "test-teacher-token"}
        first_id = _register(client, "alpha_26")[1]
        _register(client, "beta_26")
        _add_achievement(first_id, "hw1", "Q01", "1", "2026-01-01T00:00:00+03:00")

        listed = client.get("/api/admin/students", headers=teacher, params={"sort": "username"}).json()
        assert [item["id"] for item in listed["homeworks"]] == ["hw1", "hw2"]
        assert listed["total"] == 2
        assert listed["offset"] == 0
        assert listed["limit"] == 50
        row = next(item for item in listed["students"] if item["id"] == first_id)
        assert [cell["id"] for cell in row["homeworks"]] == ["hw1", "hw2"]
        assert row["homeworks"][0]["status"] == "partial"
        assert row["homeworks"][1]["status"] == "not_started"

        page = client.get(
            "/api/admin/students", headers=teacher, params={"sort": "username", "limit": 1, "offset": 0}
        ).json()
        assert page["total"] == 2
        assert len(page["students"]) == 1
        rest = client.get(
            "/api/admin/students", headers=teacher, params={"sort": "username", "limit": 1, "offset": 1}
        ).json()
        assert rest["students"][0]["id"] != page["students"][0]["id"]

        scoped = client.get(
            "/api/admin/students",
            headers=teacher,
            params={"homework": "hw1", "sort": "username"},
        ).json()
        assert [item["id"] for item in scoped["homeworks"]] == ["hw1", "hw2"]
        scoped_row = next(item for item in scoped["students"] if item["id"] == first_id)
        assert [cell["id"] for cell in scoped_row["homeworks"]] == ["hw1", "hw2"]
        assert Decimal(scoped_row["score"]) == Decimal("1")
        assert scoped_row["homeworks"][1]["status"] == "not_started"

        past = client.get(
            "/api/admin/students",
            headers=teacher,
            params={"sort": "username", "limit": 1, "offset": 99},
        ).json()
        assert past["total"] == 2
        assert past["offset"] == 1
        assert len(past["students"]) == 1

        missing = client.get(
            "/api/admin/students",
            headers=teacher,
            params={"search": "zzz_no_such_student", "offset": 10},
        ).json()
        assert missing["total"] == 0
        assert missing["offset"] == 10
        assert missing["students"] == []
