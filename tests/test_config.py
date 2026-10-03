import pytest

from cabinet.config import APP_ROOT, app_path, manifest_path


def test_relative_runtime_paths_are_anchored_to_app_directory():
    assert app_path("var/cabinet.sqlite3", "unused") == APP_ROOT / "var/cabinet.sqlite3"


@pytest.mark.skipif(
    not (APP_ROOT / "manifest.course.json").is_file(),
    reason="course manifest stays on disk and out of the public repository",
)
def test_missing_configured_manifest_falls_back_to_full_course_manifest(tmp_path):
    missing = tmp_path / "old-manifest.json"
    assert manifest_path(missing) == APP_ROOT / "manifest.course.json"
