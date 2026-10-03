import json
import zipfile
from decimal import Decimal
from io import BytesIO

import pytest

from cabinet.domains.manifests import ManifestError, convert_legacy_zip, validate_manifest


def sample():
    with open("manifest.example.json", encoding="utf-8") as stream:
        return json.load(stream)


def test_example_manifest_parses_with_custom_policy():
    manifest = validate_manifest(sample())
    assert manifest.policy.formula == "score * multiplier ** (late_days / period_days)"
    assert manifest.policy.max_penalty_fraction == Decimal("0.4")
    assert manifest.homeworks[0].tasks[0].points == 1
    assert manifest.homeworks[0].tasks[0].title != manifest.homeworks[0].tasks[0].id


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(schema_version=2),
        lambda x: x["homeworks"][0]["tasks"][0].update(reference_sql="DROP TABLE users"),
        lambda x: x["homeworks"][0]["tasks"][0].update(reference_sql="SELECT 1; SELECT 2"),
        lambda x: x["homeworks"][0]["tasks"][0].update(points=-1),
        lambda x: x["homeworks"][0]["tasks"].append(x["homeworks"][0]["tasks"][0]),
        lambda x: x["homeworks"][0].update(soft_deadline="2026-10-11T00:00:00+03:00"),
        lambda x: x["penalty"].update(formula="score * unknown"),
    ],
)
def test_invalid_manifest_rejected(mutation):
    data = sample()
    mutation(data)
    with pytest.raises(ManifestError):
        validate_manifest(data)


def test_legacy_zip_hw_folder_conversion():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hw1/README.md", "# HW\n\n### Q01, 1 балл\n\nReturn one.\n")
        archive.writestr("hw1/gold.sql", "-- >>> Q01\nSELECT 1 AS answer;\n")
        archive.writestr("hw1/structure.json", "{}")
    result = convert_legacy_zip(
        buffer.getvalue(), "Europe/Moscow", "2026-10-10T23:59:00+03:00", "2026-09-25T23:59:00+03:00"
    )
    assert validate_manifest(result).homeworks[0].tasks[0].reference_sql == "SELECT 1 AS answer"


def test_legacy_zip_rejects_path_traversal():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../hw1/gold.sql", "SELECT 1")
    with pytest.raises(ManifestError):
        convert_legacy_zip(
            buffer.getvalue(),
            "Europe/Moscow",
            "2026-10-10T23:59:00+03:00",
            "2026-09-25T23:59:00+03:00",
        )


def test_custom_deadline_and_penalty_rules_are_loaded_from_manifest():
    data = sample()
    data["hard_deadline"] = "2026-11-01T23:59:00+03:00"
    data["homeworks"][0]["soft_deadline"] = "2026-10-01T23:59:00+03:00"
    data["penalty"].update(
        formula="score * (1 - max_penalty_fraction * late_days / period_days)",
        period_days=10,
        max_penalty_fraction=0.5,
        minimum_score_fraction=0,
    )
    manifest = validate_manifest(data)
    assert manifest.hard_deadline == data["hard_deadline"]
    assert manifest.homeworks[0].soft_deadline == data["homeworks"][0]["soft_deadline"]
    assert manifest.policy.period_days == 10
    assert manifest.policy.max_penalty_fraction == Decimal("0.5")


def test_homework_can_override_global_hard_deadline():
    data = sample()
    data["hard_deadline"] = "2026-11-01T23:59:00+03:00"
    data["homeworks"][0]["soft_deadline"] = "2026-10-01T23:59:00+03:00"
    data["homeworks"][0]["hard_deadline"] = "2026-12-01T23:59:00+03:00"

    manifest = validate_manifest(data)

    assert manifest.hard_deadline == data["hard_deadline"]
    assert manifest.homeworks[0].hard_deadline == data["homeworks"][0]["hard_deadline"]

    data["homeworks"][0]["soft_deadline"] = "2026-12-02T00:00:00+03:00"
    with pytest.raises(ManifestError, match="soft_deadline"):
        validate_manifest(data)


def test_each_homework_can_set_hard_deadline_without_global_default():
    data = sample()
    del data["hard_deadline"]
    data["homeworks"][0]["hard_deadline"] = "2026-10-10T23:59:00+03:00"

    manifest = validate_manifest(data)

    assert manifest.hard_deadline is None
    assert manifest.homeworks[0].hard_deadline == data["homeworks"][0]["hard_deadline"]
