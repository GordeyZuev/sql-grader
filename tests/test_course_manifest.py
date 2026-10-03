import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from cabinet.api import STRUCTURE_PATTERNS
from cabinet.domains.manifests import validate_manifest
from cabinet.domains.sql_safety import inspect_sql

COURSE_MANIFEST = Path(__file__).parents[1] / "manifest.course.json"
pytestmark = pytest.mark.skipif(
    not COURSE_MANIFEST.is_file(),
    reason="course manifest stays on disk and out of the public repository",
)


def test_full_course_manifest_preserves_all_current_assignments():
    path = COURSE_MANIFEST
    manifest = validate_manifest(json.loads(path.read_text(encoding="utf-8")))

    assert [len(hw.tasks) for hw in manifest.homeworks] == [19, 17, 16, 19]
    assert [[task.id for task in hw.tasks] for hw in manifest.homeworks] == [
        [f"Q{index:02d}" for index in range(1, count + 1)] for count in [19, 17, 16, 19]
    ]
    assert [sum((task.points for task in hw.tasks), Decimal(0)) for hw in manifest.homeworks] == [
        Decimal("20.0"),
        Decimal("20"),
        Decimal("20.0"),
        Decimal("25"),
    ]
    assert [sum(bool(task.required) for task in hw.tasks) for hw in manifest.homeworks] == [
        0,
        3,
        11,
        19,
    ]


def test_course_reference_sql_is_safe_and_contains_required_constructs():
    manifest = validate_manifest(json.loads(COURSE_MANIFEST.read_text(encoding="utf-8")))
    for homework in manifest.homeworks:
        for task in homework.tasks:
            code, error = inspect_sql(task.reference_sql)
            assert error is None, f"{homework.id}/{task.id}: {error}"
            missing = [
                name
                for name in task.required
                if name not in STRUCTURE_PATTERNS
                or not re.search(STRUCTURE_PATTERNS[name], code, re.I)
            ]
            assert missing == [], f"{homework.id}/{task.id} missing {missing}"
