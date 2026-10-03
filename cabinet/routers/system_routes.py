# ruff: noqa: I001
"""HTTP endpoints for this domain."""

import anyio
from cabinet import api as core
from fastapi.exceptions import (
    HTTPException,
)
from pathlib import (
    Path,
)
from starlette.responses import (
    FileResponse,
)
import cabinet.infrastructure.sqlite_store as store
from cabinet.routers.system import router as system_router

_STATIC_HEADERS = {"Cache-Control": "no-cache"}
# Kept off the main request pool. A room of SQL checks can occupy every
# ordinary thread; these two threads still answer Docker, so the process is
# not restarted while those checks are being saved.
_HEALTH_THREADS = anyio.CapacityLimiter(2)


def _ping_database() -> None:
    with store.closing(store.connect()) as db:
        db.execute("SELECT 1").fetchone()


@system_router.get("/health")
async def health() -> dict:
    await anyio.to_thread.run_sync(_ping_database, limiter=_HEALTH_THREADS)
    return {"status": "ok"}


@system_router.get("/", include_in_schema=False)
def index():
    return FileResponse(
        Path(core.__file__).parent / "static" / "index.html",
        headers=_STATIC_HEADERS,
    )


@system_router.get("/static/{asset}", include_in_schema=False)
def static_asset(asset: str):
    if asset not in {"app.js", "style.css", "markdown.js", "sql-editor.js"}:
        raise HTTPException(404, "Файл не найден")
    return FileResponse(Path(core.__file__).parent / "static" / asset, headers=_STATIC_HEADERS)
