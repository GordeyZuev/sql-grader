"""Paths and runtime configuration for the standalone cabinet."""

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def app_path(value: str | Path | None, default: str) -> Path:
    """Resolve a relative setting from cabinet-app, independent of shell cwd."""
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else APP_ROOT / path


def manifest_path(value: str | Path | None) -> Path:
    """Prefer a configured manifest, falling back to the bundled course snapshot."""
    configured = app_path(value, "manifest.course.json")
    course_manifest = APP_ROOT / "manifest.course.json"
    return configured if configured.is_file() or not course_manifest.is_file() else course_manifest
