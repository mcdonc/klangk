"""Klangk backend: FastAPI app with HTTP + WebSocket endpoints."""

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import container, model, oidc
from .api import router
from .model import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
from .util import resolve_env_secret
from .wshandler import handle_websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def seed_default_acls(admin_group_id: str) -> None:
    """Seed default ACL entries if none exist yet."""
    existing = await model.get_acl_tree_summary()
    if existing:
        return
    # /: Authenticated users can view, deny everyone else
    await model.add_acl_entry(
        "/",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await model.add_acl_entry(
        "/",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    # /workspaces: Authenticated users can create
    await model.add_acl_entry(
        "/workspaces",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    # /groups: Authenticated users can create groups
    await model.add_acl_entry(
        "/groups",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    # /admin: admin group gets full access, deny everyone else
    await model.add_acl_entry(
        "/admin",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_GROUP,
        group_id=admin_group_id,
    )
    await model.add_acl_entry(
        "/admin",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    logger.info("Seeded default ACL entries")


async def ensure_admin_group() -> str:
    """Ensure the 'admin' group exists. Returns the group ID."""
    group = await model.get_group_by_name("admin")
    if group is None:
        group = await model.create_group("admin", description="Administrators")
        logger.info("Created admin group: %s", group["id"])
    return group["id"]


async def seed_default_user() -> None:
    """Create default user if it doesn't exist.

    If KLANGK_DEFAULT_PASSWORD is set, use it. Otherwise generate a random
    password and print it to the console (only on first creation).
    """
    import secrets

    import bcrypt

    admin_group_id = await ensure_admin_group()
    await seed_default_acls(admin_group_id)

    email = resolve_env_secret("KLANGK_DEFAULT_USER", "admin@example.com")
    password = resolve_env_secret("KLANGK_DEFAULT_PASSWORD")
    existing = await model.get_user_by_email(email)
    if existing is None:
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(16)
        password_hash = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()
        user = await model.create_user(email, password_hash, verified=True)
        await model.add_user_to_group(user["id"], admin_group_id)
        if generated:
            logger.info(
                "Created default admin user '%s' with generated password: %s",
                email,
                password,
            )
        else:
            logger.info("Created default user '%s' in admin group", email)
    else:
        # Ensure existing default user is in admin group
        await model.add_user_to_group(existing["id"], admin_group_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import auth

    auth.require_secure_jwt_secret()
    await model.init_db()
    oidc.init_providers()
    oidc.load_login_hook()
    await seed_default_user()
    from . import wshandler

    container.registry.set_on_workspace_killed(wshandler.reset_workspace_state)
    await container.registry.prewarm_podman()
    await container.registry.adopt_orphaned_containers()
    container.registry.start_cleanup_loop()
    logger.info("Klangk backend started")
    yield
    await container.registry.shutdown()
    logger.info("Klangk backend stopped")


app = FastAPI(title="Klangk", lifespan=lifespan)


def setup_logfire(app: FastAPI) -> bool:
    """Enable Logfire instrumentation if LOGFIRE_TOKEN is set."""
    if not resolve_env_secret("LOGFIRE_TOKEN"):
        return False
    import logfire

    base_url = resolve_env_secret("LOGFIRE_BASE_URL")
    environment = resolve_env_secret("LOGFIRE_ENVIRONMENT")
    kwargs: dict = {}
    if base_url:
        kwargs["base_url"] = base_url
    if environment:
        kwargs["environment"] = environment
    logfire.configure(**kwargs)
    logfire.instrument_fastapi(app)
    logger.info("Logfire instrumentation enabled")
    return True


setup_logfire(app)


def _cors_origins() -> list[str]:
    """Build the CORS allowed-origins list.

    Priority: KLANGK_CORS_ORIGINS (comma-separated) > derived from
    hosting env vars > localhost with nginx port.
    """
    explicit = resolve_env_secret("KLANGK_CORS_ORIGINS")
    if explicit:
        return [o.strip() for o in explicit.split(",") if o.strip()]
    hostname = resolve_env_secret("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_secret("KLANGK_HOSTING_PROTO", "http")
    if hostname:
        return [f"{proto}://{hostname}"]
    nginx_port = resolve_env_secret("KLANGK_NGINX_PORT", "8995")
    return [f"http://localhost:{nginx_port}"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


# --- WebSocket ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
    await handle_websocket(ws)


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files and add no-cache middleware."""
    static_app = StaticFiles(directory=str(frontend_dir), html=True)

    @app.middleware("http")
    async def add_no_cache_headers(request, call_next):
        response = await call_next(request)
        if (
            request.url.path.endswith((".html", ".js"))
            or request.url.path == "/"
        ):
            response.headers["Cache-Control"] = (
                "no-cache, no-store, must-revalidate"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.mount("/", static_app, name="frontend")


_frontend_dir = (
    Path(__file__).parent.parent.parent / "frontend" / "build" / "web"
)
if _frontend_dir.exists():  # pragma: no cover
    setup_static_files(app, _frontend_dir)
