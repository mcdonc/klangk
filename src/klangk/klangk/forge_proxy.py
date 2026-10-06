"""Host-side custody of git-forge OAuth tokens for workspaces.

A workspace never holds a forge token. klangkd runs the PKCE flow (the
verifier stays here), exchanges the authorization code when the browser
bridge returns it, keeps the access and refresh tokens per (workspace,
forge host), and injects them into an allow-listed reverse proxy that
containers reach at ``/forge-proxy/<host>/...`` with their workspace
JWT. The browser-delegate relay refuses container-built authorization
flows, PAT prompts, and credential-cache reads for proxied forges, so a
container cannot obtain a forge credential through the bridge either.

Tokens and transactions live in memory (one klangkd process; a restart
means re-authorizing). ``forge_proxy_allowed_repos`` names the
repositories a workspace reaches (``owner/repo``, ``owner/*``, or ``*`` for
all; a ``:ro`` suffix for read-only and ``:issues`` for read-only plus
issue work); with no matching rule a repository is
refused. A push that deletes a ref is refused.
``forge_proxy_template_repos`` names the templates a workspace may
generate a repository from (into the signed-in account, or an owner in
``forge_proxy_template_owners``), and ``forge_proxy_collaborator_accounts``
names the accounts a workspace may add, read-only, as collaborators on a
repository the signed-in account owns.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from . import settings as settings_mod

logger = logging.getLogger(__name__)

PROVIDERS_KEY = "KLANGKWS_FEATURE_OAUTH_PROVIDERS"
TXN_TTL_SECONDS = 600
MAX_PENDING_TXNS_PER_WORKSPACE = 8
MAX_PENDING_TXNS = 1000
REFRESH_SKEW_SECONDS = 120

_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
_REPO = rf"/(?P<owner>{_NAME})/(?P<repo>{_NAME})"
_NUM = r"(?P<num>[1-9][0-9]{0,9})"

# (method, compiled path pattern, route kind, allowed query keys).
_ROUTES = [
    (
        "GET",
        re.compile(rf"^{_REPO}(?:\.git)?/info/refs$"),
        "git-refs",
        {"service"},
    ),
    (
        "POST",
        re.compile(rf"^{_REPO}(?:\.git)?/git-upload-pack$"),
        "git-upload",
        set(),
    ),
    (
        "POST",
        re.compile(rf"^{_REPO}(?:\.git)?/git-receive-pack$"),
        "git-receive",
        set(),
    ),
    ("GET", re.compile(r"^/api/v1/user$"), "api-read", set()),
    ("GET", re.compile(rf"^/api/v1/repos{_REPO}$"), "api-read", set()),
    (
        "GET",
        re.compile(rf"^/api/v1/repos{_REPO}/issues$"),
        "api-read",
        {"state", "page", "limit", "type"},
    ),
    (
        "GET",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}$"),
        "api-read",
        set(),
    ),
    (
        "GET",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}/comments$"),
        "api-read",
        set(),
    ),
    (
        "POST",
        re.compile(rf"^/api/v1/repos{_REPO}/issues$"),
        "issue-create",
        set(),
    ),
    (
        "POST",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}/comments$"),
        "comment-create",
        set(),
    ),
    (
        "PATCH",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}$"),
        "issue-state",
        set(),
    ),
    (
        "GET",
        re.compile(rf"^/api/v1/repos{_REPO}/(?:labels|milestones)$"),
        "api-read",
        {"state", "page", "limit"},
    ),
    (
        "GET",
        re.compile(rf"^/api/v1/repos{_REPO}/(?:assignees|collaborators)$"),
        "api-read",
        {"page", "limit"},
    ),
    (
        "POST",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}/labels$"),
        "issue-labels",
        set(),
    ),
    (
        "PUT",
        re.compile(rf"^/api/v1/repos{_REPO}/issues/{_NUM}/labels$"),
        "issue-labels",
        set(),
    ),
    (
        "POST",
        re.compile(rf"^/api/v1/repos{_REPO}/generate$"),
        "repo-generate",
        set(),
    ),
    (
        "PUT",
        re.compile(rf"^/api/v1/repos{_REPO}/collaborators/(?P<user>{_NAME})$"),
        "collaborator-add",
        set(),
    ),
]

_GIT_SERVICES = {"git-upload-pack", "git-receive-pack"}

# Percent-encoding, backslashes, control characters and spaces, repeated
# slashes, and "." or ".." segments.
_BAD_PATH = re.compile(r"[%\\#?\x00-\x20\x7f]|//|/\.{1,2}(?:/|$)")

# JSON write bodies per route kind: (allowed key -> value type, required keys).
_BODY_RULES = {
    "issue-create": (
        {
            "title": str,
            "body": str,
            "assignees": list,
            "labels": list,
            "milestone": int,
        },
        {"title"},
    ),
    "comment-create": ({"body": str}, {"body"}),
    # Issue edits: title, body, open/close, assignees and milestone.
    "issue-state": (
        {
            "title": str,
            "body": str,
            "state": str,
            "assignees": list,
            "milestone": int,
        },
        set(),
    ),
    "issue-labels": ({"labels": list}, {"labels"}),
    "repo-generate": (
        {
            "owner": str,
            "name": str,
            "description": str,
            "private": bool,
            "git_content": bool,
            "default_branch": str,
        },
        {"owner", "name"},
    ),
    "collaborator-add": ({"permission": str}, {"permission"}),
}
# Exact-value checks after the type checks: (kind, key) -> allowed values.
_BODY_VALUES = {
    ("issue-state", "state"): {"open", "closed"},
    ("collaborator-add", "permission"): {"read"},
}
_NAME_ONLY = re.compile(rf"^{_NAME}$")
MAX_ASSIGNEES = 10
MAX_LABELS = 20
MAX_JSON_BODY = 64 * 1024
# Upper bound for one git pack request streamed through the proxy.
MAX_GIT_BODY = 512 * 1024 * 1024
# Upper bound for the ref-update commands read ahead of a push's pack.
MAX_PUSH_COMMANDS = 1024 * 1024

# Features an operator enables in ``forge_proxy_features``. Every route
# belongs to one; a route whose feature is not enabled is refused (403),
# so an empty setting (the default) allows no forge operation at all.
FEATURES = {
    "read": "read the account, repositories, issues, comments, labels",
    "git-read": "git clone and fetch",
    "git-push": "git push (a push that deletes a ref is always refused)",
    "issues": "open issues and comments",
    "issue-edit": "edit, close, assign and label issues",
    "site-create": "generate a repository from a listed template",
    "collaborators": "add a listed account as a read-only collaborator",
    "api-passthrough": "any other /api/v1/ call; the forge decides",
}
_KIND_FEATURE = {
    "api-read": "read",
    "git-refs": "git-read",
    "git-upload": "git-read",
    "git-receive": "git-push",
    "issue-create": "issues",
    "comment-create": "issues",
    "issue-state": "issue-edit",
    "issue-labels": "issue-edit",
    "repo-generate": "site-create",
    "collaborator-add": "collaborators",
    "api-pass-read": "api-passthrough",
    "api-pass-write": "api-passthrough",
}


def parse_features(raw: str) -> set[str]:
    """Enabled features; ``*`` enables all. Unknown names are ignored."""
    names = {n.strip().lower() for n in (raw or "").split(",") if n.strip()}
    return set(FEATURES) if "*" in names else names & set(FEATURES)


def route_feature(route: Route) -> str:
    """The feature a route needs (a push advertisement needs git-push)."""
    if "service=git-receive-pack" in route.query:
        return "git-push"
    return _KIND_FEATURE[route.kind]


def check_feature(route: Route, enabled: set[str]) -> None:
    if route_feature(route) not in enabled:
        raise ForgeProxyError(
            403, "operation not enabled through the forge proxy"
        )


# Route kinds a ``:issues`` repo rule allows on top of read-only access:
# open issues and comments, open/close an issue, and set its assignees.
ISSUE_KINDS = {"issue-create", "comment-create", "issue-state", "issue-labels"}

# Route kinds that change the forge; refused for a read-only repo rule.
WRITE_KINDS = {
    "git-receive",
    "issue-create",
    "comment-create",
    "issue-state",
    "issue-labels",
    "api-pass-write",
    "repo-generate",
    "collaborator-add",
}

# A pkt-line ref update: "<old-oid> <new-oid> <ref>" (oid: 40 or 64 hex).
_PUSH_COMMAND = re.compile(
    r"^(?P<old>[0-9a-f]{40}|[0-9a-f]{64}) "
    r"(?P<new>[0-9a-f]{40}|[0-9a-f]{64}) (?P<ref>\S+)$"
)
_SHALLOW = re.compile(r"^shallow [0-9a-f]{40,64}$")

# Request headers forwarded upstream; everything else (cookies, the
# workspace JWT, forwarded-identity headers, Sudo) is dropped.
FORWARD_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "content-encoding",
    "git-protocol",
    "user-agent",
)
# Response headers returned to the container.
FORWARD_RESPONSE_HEADERS = (
    "content-type",
    "content-encoding",
    "cache-control",
    "expires",
    "pragma",
    "x-total-count",
)


class ForgeProxyError(Exception):
    """A refusal with an HTTP status and a sanitized message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Txn:
    """A pending authorization: the verifier never leaves klangkd."""

    txn_id: str
    workspace_id: str
    host: str
    verifier: str
    state: str
    created: float
    in_flight: bool = False


@dataclass(eq=False)
class ForgeToken:
    """Compared by identity: replacing or forgetting checks it is still
    the stored object, so a stale refresh or 401 never clobbers a newer one."""

    access_token: str
    refresh_token: str
    expires_at: float
    login: str


@dataclass
class Route:
    kind: str
    owner: str
    repo: str
    path: str
    query: str
    user: str = ""


def s256_challenge(verifier: str) -> str:
    """The S256 code_challenge for a verifier (RFC 7636 section 4.2)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _host_of_url(url: str) -> str:
    """Lowercased hostname (no port) of a URL, or ''."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _ambiguous_netloc(url: str) -> bool:
    """True when a URL's authority could be read differently by a browser
    (percent-encoding, userinfo, non-ASCII, unparsable)."""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return True
    return "%" in netloc or "@" in netloc or not netloc.isascii()


def _norm_host(host: str) -> str:
    """Lowercase, no port, no trailing dot."""
    return (host or "").strip().lower().split(":", 1)[0].rstrip(".")


def validate_raw_path(raw_path: str) -> str:
    """Reject ambiguous or encoded paths before any routing.

    Percent-encoding, backslashes, repeated slashes and dot segments are
    all refused, so the path the router matches is exactly the path
    forwarded upstream.
    """
    if not raw_path.startswith("/"):
        raise ForgeProxyError(400, "path must be absolute")
    if _BAD_PATH.search(raw_path):
        raise ForgeProxyError(400, "path not allowed")
    return raw_path


def _strict_query(query: str, allowed: set[str], kind: str) -> str:
    """Rebuild the query from allowed keys; refuse unknown or repeated keys."""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    try:
        params = _no_duplicate_keys(pairs)
    except ValueError:
        raise ForgeProxyError(400, "repeated query parameter")
    if set(params) - allowed:
        raise ForgeProxyError(400, "query parameter not allowed")
    if kind == "git-refs" and params.get("service") not in _GIT_SERVICES:
        raise ForgeProxyError(400, "service not allowed")
    return urllib.parse.urlencode(pairs)


# Passthrough mode: any forge API call, decided by the forge's own
# permissions and the token's scopes. These query keys still never pass.
PASS_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
PASS_DENIED_QUERY = {"access_token", "token", "sudo"}
MAX_PASS_BODY = 25 * 1024 * 1024
_PASS_REPO = re.compile(rf"^/api/v1/repos{_REPO}(?:/|$)")


def match_route(
    method: str, raw_path: str, query: str, passthrough: bool = False
) -> Route:
    """The allow-listed route for a request, or ForgeProxyError(403).

    With ``passthrough``, an ``/api/v1/`` call no allow-listed route
    matches becomes an ``api-pass-read`` or ``api-pass-write`` route.
    """
    path = validate_raw_path(raw_path)
    try:
        kind, allowed, groups = _find_route(method, path)
    except ForgeProxyError:
        if not (passthrough and path.startswith("/api/v1/")):
            raise
        return _pass_route(method, path, query)
    return Route(
        kind=kind,
        owner=groups.get("owner", ""),
        # Logged only; the forwarded path is the validated raw path.
        repo=groups.get("repo", "").removesuffix(".git"),
        path=path,
        query=_strict_query(query, allowed, kind),
        user=groups.get("user", ""),
    )


def _pass_route(method: str, path: str, query: str) -> Route:
    if method not in PASS_METHODS:
        raise ForgeProxyError(405, "method not allowed")
    groups = _pass_groups(path)
    return Route(
        kind="api-pass-read" if method == "GET" else "api-pass-write",
        owner=groups.get("owner", ""),
        repo=groups.get("repo", ""),
        path=path,
        query=_pass_query(query),
    )


def _pass_groups(path: str) -> dict:
    m = _PASS_REPO.match(path)
    return m.groupdict() if m else {}


def _pass_query(query: str) -> str:
    """The query unchanged, unless it carries credentials (400)."""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if {k.lower() for k, _ in pairs} & PASS_DENIED_QUERY:
        raise ForgeProxyError(400, "query parameter not allowed")
    return urllib.parse.urlencode(pairs)


def _find_route(method: str, path: str):
    """(kind, allowed query keys, path groups) of the matching route."""
    for route_method, pattern, kind, allowed in _ROUTES:
        m = pattern.match(path) if method == route_method else None
        if m:
            return kind, allowed, m.groupdict()
    raise ForgeProxyError(403, "operation not allowed through the forge proxy")


def validate_json_body(kind: str, body: bytes) -> bytes:
    """Parse, check and re-serialize a write body for an API route."""
    rules = _BODY_RULES.get(kind)
    if rules is None:
        return body
    if len(body) > MAX_JSON_BODY:
        raise ForgeProxyError(413, "request body too large")
    data = _json_object(body)
    _check_fields(data, *rules)
    _check_values(kind, data)
    return json.dumps(data).encode()


def _json_object(body: bytes) -> dict:
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        raise ForgeProxyError(400, "request body must be a JSON object")
    if not isinstance(data, dict):
        raise ForgeProxyError(400, "request body must be a JSON object")
    return data


def _check_fields(data: dict, types: dict, required: set) -> None:
    keys = set(data)
    if not (keys and keys <= set(types) and required <= keys):
        raise ForgeProxyError(400, "request body fields not allowed")
    _check_types(data, types)


def _check_types(data: dict, types: dict) -> None:
    if not all(type(v) is types[k] for k, v in data.items()):
        raise ForgeProxyError(400, "request body value has the wrong type")


def _check_values(kind: str, data: dict) -> None:
    for key, value in data.items():
        _check_exact(kind, key, value)
        _LIST_CHECKS.get(key, _no_check)(value)
    if kind == "repo-generate":
        _check_names(data["owner"], data["name"])


def _check_exact(kind: str, key: str, value) -> None:
    allowed = _BODY_VALUES.get((kind, key))
    if allowed is not None and value not in allowed:
        raise ForgeProxyError(400, f"{key} value not allowed")


def _no_check(value) -> None:
    """Values with no extra check beyond their type."""


def _check_assignees(logins: list) -> None:
    """Assignees: up to MAX_ASSIGNEES account names (strings)."""
    ok = len(logins) <= MAX_ASSIGNEES and all(
        isinstance(n, str) and _NAME_ONLY.match(n) for n in logins
    )
    if not ok:
        raise ForgeProxyError(400, "assignees value not allowed")


def _check_labels(labels: list) -> None:
    """Labels: up to MAX_LABELS label ids (ints) or names (short strings)."""
    ok = len(labels) <= MAX_LABELS and all(map(_label_ok, labels))
    if not ok:
        raise ForgeProxyError(400, "labels value not allowed")


def _label_ok(label) -> bool:
    if type(label) is int:
        return label > 0
    return isinstance(label, str) and 0 < len(label) <= 100


_LIST_CHECKS = {"assignees": _check_assignees, "labels": _check_labels}


def _check_names(*names: str) -> None:
    if not all(_NAME_ONLY.match(n) for n in names):
        raise ForgeProxyError(400, "repository owner or name not allowed")


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


# --- repository allow-list ---------------------------------------------------


@dataclass(frozen=True)
class RepoRule:
    owner: str
    repo: str  # a repository name, or "*" for every repo of the owner
    read_only: bool
    issues: bool = False  # read-only, but issues and comments may be opened


def parse_repo_rules(raw: str) -> list[RepoRule]:
    """Rules from ``forge_proxy_allowed_repos``.

    Unset, empty or unusable entries allow nothing: a repository is
    reachable only through a matching rule, and ``*`` matches every one.
    """
    entries = [e.strip().lower() for e in (raw or "").split(",")]
    return list(filter(None, map(_parse_rule, filter(None, entries))))


_RULE = re.compile(
    r"^(?:\*(?:/\*)?|(?P<owner>[a-z0-9][a-z0-9_.-]*)/"
    r"(?P<repo>\*|[a-z0-9][a-z0-9_.-]*))(?P<mode>:ro|:issues)?$"
)


def _parse_rule(entry: str) -> RepoRule | None:
    m = _RULE.match(entry)
    if m is None:
        return None
    return RepoRule(
        owner=m.group("owner") or "*",
        repo=(m.group("repo") or "*").removesuffix(".git"),
        read_only=m.group("mode") is not None,
        issues=m.group("mode") == ":issues",
    )


def is_write(route: Route) -> bool:
    """True for a route that changes the forge (a push advertisement too)."""
    return (
        route.kind in WRITE_KINDS or "service=git-receive-pack" in route.query
    )


def check_repo(route: Route, rules: list[RepoRule]) -> None:
    """Refuse a route outside the allow-list (403).

    Allow-listed account routes pass. A passthrough call whose path names
    no repository (search, numeric ids, organizations, repository
    creation) passes only with an unrestricted ``*`` rule.
    """
    if not route.owner:
        _check_unscoped(route, rules)
        return
    _check_rule(route, _matching_rule(route, rules))


def _check_unscoped(route: Route, rules: list[RepoRule]) -> None:
    star = next((r for r in rules if (r.owner, r.repo) == ("*", "*")), None)
    if route.kind.startswith("api-pass-"):
        _check_rule(route, star)


def _check_rule(route: Route, rule: RepoRule | None) -> None:
    if rule is None:
        raise ForgeProxyError(
            403, "repository not allowed through the forge proxy"
        )
    if _read_only_refuses(route, rule):
        raise ForgeProxyError(
            403, "repository is read-only through the forge proxy"
        )


def _read_only_refuses(route: Route, rule: RepoRule) -> bool:
    """True when a read-only rule forbids this write (``:issues`` allows
    opening issues and comments)."""
    allowed = rule.issues and route.kind in ISSUE_KINDS
    return rule.read_only and is_write(route) and not allowed


def _matching_rule(route: Route, rules: list[RepoRule]) -> RepoRule | None:
    """The exact ``owner/repo`` rule, else ``owner/*``, else ``*``, else None."""
    index = {(r.owner, r.repo): r for r in reversed(rules)}
    owner, repo = route.owner.lower(), route.repo.lower()
    keys = ((owner, repo), (owner, "*"), ("*", "*"))
    return next(filter(None, map(index.get, keys)), None)


# --- site creation: templates and collaborators ------------------------------


def parse_names(raw: str) -> set[str]:
    """Lowercased entries of a comma-separated setting (``a/b`` or ``a``)."""
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


def check_template(route: Route, templates: set[str]) -> None:
    """Refuse generating from a repository not listed as a template (403)."""
    if f"{route.owner}/{route.repo}".lower() not in templates:
        raise ForgeProxyError(
            403, "template not allowed through the forge proxy"
        )


def check_new_repo(
    body: dict, login: str, owners: set[str], rules: list[RepoRule]
) -> None:
    """The generated repository's owner and the repo allow-list (403)."""
    owner = body["owner"].lower()
    if owner != (login or "").lower() and owner not in owners:
        raise ForgeProxyError(
            403, "repository owner not allowed through the forge proxy"
        )
    target = Route("repo-generate", body["owner"], body["name"], "", "")
    check_repo(target, rules)


def check_collaborator(route: Route, login: str, accounts: set[str]) -> None:
    """Only listed accounts, only on a repository the signed-in user owns."""
    if route.owner.lower() != (login or "").lower():
        raise ForgeProxyError(
            403, "collaborators only on your own repositories"
        )
    if route.user.lower() not in accounts:
        raise ForgeProxyError(
            403, "collaborator not allowed through the forge proxy"
        )


# --- push inspection ---------------------------------------------------------


def push_commands_end(buf: bytes) -> int | None:
    """Offset just past the flush-pkt ending a push's commands, or None.

    None means more bytes are needed. A malformed length is a 400.
    """
    pos = 0
    while pos + 4 <= len(buf):
        size = _pkt_len(buf[pos : pos + 4])
        if size == 0:
            return pos + 4
        pos += size
    return None


def _pkt_len(head: bytes) -> int:
    try:
        size = int(head, 16)
    except ValueError:
        raise ForgeProxyError(400, "malformed push request")
    if size != 0 and size < 4:
        raise ForgeProxyError(400, "malformed push request")
    return size


def check_push_commands(buf: bytes, end: int) -> None:
    """Refuse a push whose ref updates delete a ref (403)."""
    pos = 0
    while pos < end - 4:
        size = _pkt_len(buf[pos : pos + 4])
        _check_command(buf[pos + 4 : pos + size])
        pos += size


def _check_command(payload: bytes) -> None:
    line = payload.split(b"\0", 1)[0].decode("latin-1").rstrip("\n")
    if _SHALLOW.match(line):
        return
    m = _PUSH_COMMAND.match(line)
    if m is None:
        raise ForgeProxyError(400, "push command not allowed")
    if set(m.group("new")) == {"0"}:
        raise ForgeProxyError(
            403, "deleting a ref is not allowed through the forge proxy"
        )


@dataclass
class ForgeProxy:
    """Per-process token custody and policy for proxied forges."""

    app: object
    _txns: dict = field(default_factory=dict)
    _tokens: dict = field(default_factory=dict)
    _locks: dict = field(default_factory=dict)

    # --- configuration ---------------------------------------------------

    def _settings(self):
        return self.app.state.settings

    def proxied_hosts(self) -> set[str]:
        raw = self._settings().forge_proxy_hosts or ""
        return {_norm_host(h) for h in raw.split(",") if h.strip()}

    def provider(self, host: str) -> dict:
        """The authorization_code_pkce provider entry for a proxied host."""
        host = _norm_host(host)
        if host not in self.proxied_hosts():
            raise ForgeProxyError(404, "forge is not proxied")
        for entry in self._provider_entries():
            if _provider_matches(entry, host):
                return entry
        raise ForgeProxyError(404, "forge provider is not configured")

    def _provider_entries(self) -> list[dict]:
        raw = settings_mod.resolve_dynamic_config(
            PROVIDERS_KEY, features_config=self._settings().features_config
        )
        try:
            entries = json.loads(raw)
        except TypeError, ValueError:
            return []
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def origin(self, host: str) -> str:
        return f"https://{_norm_host(host)}"

    def repo_rules(self) -> list[RepoRule]:
        return parse_repo_rules(self._settings().forge_proxy_allowed_repos)

    def features(self) -> set[str]:
        return parse_features(self._settings().forge_proxy_features)

    def passthrough(self) -> bool:
        return "api-passthrough" in self.features()

    def check_route(self, route: Route) -> None:
        """Refuse a route whose feature is off or that is outside
        ``forge_proxy_allowed_repos`` (a template generate is checked
        against ``forge_proxy_template_repos``)."""
        check_feature(route, self.features())
        if route.kind == "repo-generate":
            check_template(route, self.template_repos())
            return
        check_repo(route, self.repo_rules())

    def template_repos(self) -> set[str]:
        return parse_names(self._settings().forge_proxy_template_repos)

    def check_actor(self, route: Route, body, login: str) -> None:
        """Checks that need the signed-in account (known once a token is)."""
        settings = self._settings()
        if route.kind == "repo-generate":
            owners = parse_names(settings.forge_proxy_template_owners)
            check_new_repo(json.loads(body), login, owners, self.repo_rules())
        elif route.kind == "collaborator-add":
            accounts = parse_names(settings.forge_proxy_collaborator_accounts)
            check_collaborator(route, login, accounts)

    # --- authorization transactions --------------------------------------

    def _prune(self, now: float) -> None:
        for txn_id in [
            t
            for t, txn in self._txns.items()
            if now - txn.created > TXN_TTL_SECONDS
        ]:
            del self._txns[txn_id]

    def start(self, workspace_id: str, host: str) -> str:
        """Create a transaction; returns only its opaque id."""
        self.provider(host)
        now = time.time()
        self._prune(now)
        mine = sum(
            1 for x in self._txns.values() if x.workspace_id == workspace_id
        )
        if (
            mine >= MAX_PENDING_TXNS_PER_WORKSPACE
            or len(self._txns) >= MAX_PENDING_TXNS
        ):
            raise ForgeProxyError(429, "too many pending authorizations")
        txn = Txn(
            txn_id=secrets.token_urlsafe(32),
            workspace_id=workspace_id,
            host=_norm_host(host),
            verifier=secrets.token_urlsafe(64),
            state=secrets.token_urlsafe(32),
            created=now,
        )
        self._txns[txn.txn_id] = txn
        return txn.txn_id

    def claim(self, workspace_id: str, txn_id: str) -> Txn:
        """Take a pending transaction for its workspace, at most once."""
        self._prune(time.time())
        txn = self._txns.get(txn_id or "")
        if txn is None or txn.workspace_id != workspace_id or txn.in_flight:
            raise ForgeProxyError(403, "unknown authorization transaction")
        txn.in_flight = True
        return txn

    def authorize_url(self, txn: Txn) -> str:
        entry = self.provider(txn.host)
        params = {
            "response_type": "code",
            "client_id": entry["client_id"],
            "redirect_uri": entry["redirect_uri"],
            "code_challenge": s256_challenge(txn.verifier),
            "code_challenge_method": "S256",
            "state": txn.state,
        }
        if entry.get("scope"):
            params["scope"] = entry["scope"]
        url = entry["authorize_url"]
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urllib.parse.urlencode(params)}"

    async def complete(self, txn: Txn, code: str, state: str, http) -> str:
        """Exchange the code (verifier from the txn); returns the login."""
        try:
            if not code or not secrets.compare_digest(state or "", txn.state):
                raise ForgeProxyError(403, "authorization state mismatch")
            if time.time() - txn.created > TXN_TTL_SECONDS:
                raise ForgeProxyError(
                    403, "authorization expired; start again"
                )
            entry = self.provider(txn.host)
            resp = await _call(
                http.post,
                entry["token_url"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": txn.verifier,
                    "client_id": entry["client_id"],
                    "redirect_uri": entry["redirect_uri"],
                },
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
            tokens = _token_json(resp)
            login = await self._login(txn.host, tokens["access_token"], http)
            self._tokens[(txn.workspace_id, txn.host)] = ForgeToken(
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token", ""),
                expires_at=_expiry(tokens),
                login=login,
            )
            logger.info(
                "forge-proxy: workspace %s authorized %s as %s (scope: %s)",
                txn.workspace_id,
                txn.host,
                login,
                granted_scope(tokens),
            )
            return login
        finally:
            self._txns.pop(txn.txn_id, None)

    async def _login(self, host: str, access_token: str, http) -> str:
        resp = await _call(
            http.get,
            f"{self.origin(host)}/api/v1/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            follow_redirects=False,
        )
        login = _login_of(resp)
        if not login:
            raise ForgeProxyError(502, "forge did not confirm the account")
        return login

    # --- tokens ----------------------------------------------------------

    def status(self, workspace_id: str, host: str) -> dict:
        self.provider(host)
        tok = self._tokens.get((workspace_id, _norm_host(host)))
        return {
            "connected": tok is not None,
            "login": tok.login if tok else None,
        }

    async def access_token(
        self, workspace_id: str, host: str, http
    ) -> ForgeToken:
        """The workspace's current token, refreshed host-side (serialized)."""
        key = (workspace_id, _norm_host(host))
        tok = self._tokens.get(key)
        if tok is None:
            raise ForgeProxyError(401, _needs_auth(host))
        if not _expiring(tok):
            return tok
        async with self._locks.setdefault(key, asyncio.Lock()):
            if self._tokens.get(key) is tok:
                return await self._refresh(key, tok, http)
            # Another request refreshed (or a new authorization replaced
            # it) while this one waited.
            return self._stored(key)

    def _stored(self, key) -> ForgeToken:
        current = self._tokens.get(key)
        if current is None:
            raise ForgeProxyError(401, _needs_auth(key[1]))
        return current

    def forget_token(
        self, workspace_id: str, host: str, tok: ForgeToken
    ) -> None:
        """Drop the token only if it is still the stored one."""
        key = (workspace_id, _norm_host(host))
        if self._tokens.get(key) is tok:
            del self._tokens[key]

    async def _refresh(self, key, tok: ForgeToken, http) -> ForgeToken:
        if not tok.refresh_token:
            self.forget_token(key[0], key[1], tok)
            raise ForgeProxyError(401, _needs_auth(key[1]))
        tokens = await self._refresh_grant(key, tok, http)
        fresh = ForgeToken(
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token") or tok.refresh_token,
            expires_at=_expiry(tokens),
            login=tok.login,
        )
        if self._tokens.get(key) is tok:
            self._tokens[key] = fresh
            return fresh
        # Replaced or forgotten while refreshing: serve what is stored now.
        return self._stored(key)

    async def _refresh_grant(self, key, tok: ForgeToken, http) -> dict:
        entry = self.provider(key[1])
        try:
            resp = await _call(
                http.post,
                entry["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tok.refresh_token,
                    "client_id": entry["client_id"],
                },
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
            return _token_json(resp)
        except ForgeProxyError:
            self.forget_token(key[0], key[1], tok)
            raise ForgeProxyError(401, _needs_auth(key[1]))

    # --- browser-delegate relay policy -----------------------------------

    def bridge_policy(self, payload: dict) -> str:
        """How the relay treats a git_credential bridge request.

        ``"txn"``: a klangkd-issued authorization (handled by
        :meth:`relay_authorization`); ``"refuse"``: a request that could
        put a proxied forge's credential in the container (a
        container-built authorize URL, a PAT prompt, a cache read);
        ``"pass"``: unrelated to proxied forges.
        """
        if payload.get("action") != "git_credential":
            return "pass"
        if (
            payload.get("txn_id")
            and payload.get("operation") == "auth_flow_start"
        ):
            return "txn"
        return "refuse" if self._may_leak(payload) else "pass"

    def _may_leak(self, payload: dict) -> bool:
        hosts = self.proxied_hosts()
        if not hosts:
            return False
        # Any container-built authorize URL is refused while a forge is
        # proxied: hostname parsing that differs from the browser's
        # (percent-encoding, IDNA) or a redirect chain could otherwise land
        # on the proxied forge's authorize page.
        if (
            payload.get("operation") == "auth_flow_start"
            or _norm_host(str(payload.get("host", ""))) in hosts
        ):
            return True
        return any(
            _leaky_uri(payload.get(k), hosts)
            for k in ("verification_uri", "verification_uri_complete")
        )

    async def relay_authorization(self, workspace_id, payload, dispatch, http):
        """Run a klangkd-issued authorization through the browser tab.

        The tab gets the klangkd-built authorize URL; its answer (the
        code) is consumed here and never relayed to the container.
        """
        txn = self.claim(workspace_id, str(payload.get("txn_id", "")))
        try:
            request = {
                "action": "git_credential",
                "operation": "auth_flow_start",
                "protocol": "https",
                "host": txn.host,
                "authorize_url": self.authorize_url(txn),
                "state": txn.state,
            }
        except ForgeProxyError:
            self._txns.pop(txn.txn_id, None)
            raise
        try:
            result = await dispatch(request, 900.0)
        except BaseException:
            self._txns.pop(txn.txn_id, None)
            raise
        answer = _unwrap(result)
        if answer.get("error"):
            self._txns.pop(txn.txn_id, None)
            cancelled = answer["error"] == "cancelled"
            return _wrap(
                {"error": "cancelled" if cancelled else "authorization failed"}
            )
        login = await self.complete(
            txn,
            str(answer.get("code", "")),
            str(answer.get("state", "")),
            http,
        )
        return _wrap({"status": "connected", "host": txn.host, "login": login})


def _provider_matches(entry: dict, host: str) -> bool:
    return (
        _norm_host(str(entry.get("host", ""))) == host
        and entry.get("flow") == "authorization_code_pkce"
        and bool(entry.get("client_id"))
        and _provider_urls_ok(entry)
    )


def _provider_urls_ok(entry: dict) -> bool:
    https_only = all(
        str(entry.get(k, "")).startswith("https://")
        for k in ("authorize_url", "token_url")
    )
    # A local dev deployment registers http://localhost:<port>/.
    redirect_ok = str(entry.get("redirect_uri", "")).startswith(
        ("https://", "http://localhost:", "http://127.0.0.1:")
    )
    return https_only and redirect_ok


def _leaky_uri(value, hosts: set[str]) -> bool:
    """A verification URI that is, or might be read as, a proxied forge."""
    uri = str(value or "")
    return bool(uri) and (_host_of_url(uri) in hosts or _ambiguous_netloc(uri))


def granted_scope(tokens: dict) -> str:
    """The scope a token response reports (logged; never the token)."""
    return str(tokens.get("scope") or "not reported")


def _login_of(resp) -> str:
    data = _json(resp) if resp.status_code == 200 and _is_json(resp) else None
    login = data.get("login") if isinstance(data, dict) else None
    return login if isinstance(login, str) else ""


def _expiring(tok: ForgeToken) -> bool:
    return bool(tok.expires_at) and (
        tok.expires_at - time.time() <= REFRESH_SKEW_SECONDS
    )


def _needs_auth(host: str) -> str:
    return (
        f"forge not authorized for this workspace; run "
        f"`git-credential-klangk forge-auth {_norm_host(host)}`"
    )


async def _call(method, *args, **kwargs):
    """An HTTP call to the forge; transport errors become a 502."""
    try:
        return await method(*args, **kwargs)
    except Exception as exc:  # httpx.HTTPError and friends
        logger.warning(
            "forge-proxy: forge call failed: %s", type(exc).__name__
        )
        raise ForgeProxyError(502, "forge unreachable")


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _is_json(resp) -> bool:
    return "json" in (resp.headers.get("content-type") or "")


def _token_json(resp) -> dict:
    if resp.status_code != 200 or not _is_json(resp):
        raise ForgeProxyError(502, "token endpoint refused the request")
    data = _json(resp)
    if not isinstance(data, dict) or not isinstance(
        data.get("access_token"), str
    ):
        raise ForgeProxyError(502, "token endpoint refused the request")
    return data


def _expiry(tokens: dict) -> float:
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return time.time() + float(expires_in)
    return 0.0


def _unwrap(result) -> dict:
    """The tab's answer inside the relay envelope {"status","result"}."""
    if not isinstance(result, dict):
        return {"error": "no answer from the browser"}
    if result.get("error"):
        return {"error": result["error"]}
    inner = _decode(result.get("result"))
    return inner if isinstance(inner, dict) else {"error": "no answer"}


def _decode(inner):
    if not isinstance(inner, str):
        return inner
    try:
        return json.loads(inner)
    except ValueError:
        return {"error": "unreadable answer from the browser"}


def _wrap(answer: dict) -> dict:
    return {"status": "ok", "result": json.dumps(answer)}
