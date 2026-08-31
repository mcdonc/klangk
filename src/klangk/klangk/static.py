"""Static file serving (Flutter Web frontend + branding assets).

Moved out of ``main.py`` in the #2738 module split. ``setup_static_files``
performs *mounts only* — the no-cache middleware is registered exactly once
by ``main.build_app`` via :func:`no_cache_headers`. It deliberately does not
call ``app.add_middleware`` itself: Starlette raises ``RuntimeError`` for
middleware added after the app has started serving, which the SIGHUP
``frontend_dir`` remount path (``Lifecycle.remount_frontend``) would hit
on a live server — the #2738 audit found the old in-function registration
broke exactly that path.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


async def no_cache_headers(request, call_next):
    """Serve Flutter Web entry documents (``/``, ``*.html``, ``*.js``)
    no-cache so clients pick up new builds without a hard refresh.

    Registered once in :func:`klangk.main.build_app` as an HTTP
    middleware. Branding assets are cacheable (logos rarely change).
    """
    response = await call_next(request)
    if request.url.path.endswith((".html", ".js")) or request.url.path == "/":
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files at ``/`` (must be added last so API
    routes take priority).

    Optionally mounts a branding directory at ``/branding`` so a custom
    logo / assets can be served without a Flutter rebuild.  Prefers
    ``<KLANGKD_CUSTOMIZE_DIR>/branding`` when it exists; falls back to
    ``<KLANGKD_DATA_DIR>/branding`` if that exists.  If neither directory
    exists, the ``/branding`` mount is skipped entirely.  Mounted before
    the catch-all ``/`` frontend mount so it takes priority, and without
    ``html=True`` (no directory listing). See #1152, #1360.
    """
    static_app = StaticFiles(directory=str(frontend_dir), html=True)

    candidate = Path(app.state.util.customize_dir()) / "branding"
    if candidate.is_dir():
        branding_dir = candidate
    else:
        fallback = Path(app.state.settings.data_dir) / "branding"
        branding_dir = fallback if fallback.is_dir() else None
    if branding_dir is not None:
        logger.info("Branding served from %s", branding_dir)
        app.mount(
            "/branding",
            StaticFiles(directory=str(branding_dir)),
            name="branding",
        )

    app.mount("/", static_app, name="frontend")


def remove_static_mounts(app: FastAPI) -> None:
    """Drop the frontend + branding StaticFiles mounts (by name).

    Used by the SIGHUP ``frontend_dir`` remount so both mounts are
    re-resolved from the live settings: branding's directory derives
    from ``customize_dir``/``data_dir`` at setup time, and keeping the
    old mount would keep serving a stale directory after a reload.
    """
    app.routes[:] = [
        r
        for r in app.routes
        if not (hasattr(r, "name") and r.name in ("frontend", "branding"))
    ]
