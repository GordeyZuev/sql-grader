"""HTTP route groups for the cabinet API."""

from cabinet.routers import admin, auth, coursework, queries, scoring, system

__all__ = ["admin", "auth", "coursework", "queries", "scoring", "system"]
