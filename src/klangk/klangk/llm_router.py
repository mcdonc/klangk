"""In-process LLM router backed by ``litellm.Router`` (#2070).

Replaces the LiteLLM sidecar container with a lightweight in-process
router that dispatches ``/llm-proxy/`` requests to the configured
providers.

Accepts model entries in two formats:

1. **Colon-delimited strings** (for env vars):
   ``provider/model:api_base:api_key`` — parsed via
   :func:`parse_model_entry`.

2. **LiteLLM-native dicts** (for ``klangkd.yaml``): the same
   ``model_name`` / ``litellm_params`` shape documented by LiteLLM.
   Keys accept both kebab-case and snake_case (``api-key`` or
   ``api_key``, ``model-name`` or ``model_name``).  String values
   support ``file:`` and ``cmd:`` indirection so secrets stay out of
   the config file.

**Passthrough mode** (#2070): when the model list has exactly one entry
and its ``model_name`` contains ``*`` (wildcard), the Router bypasses
litellm and forwards requests directly to the upstream.  The
``/models`` endpoint queries the upstream's ``/models`` for dynamic
discovery, and ``/chat/completions`` forwards verbatim.  This preserves
the pre-litellm single-provider experience where all upstream models
were automatically available.

Public API:

- :class:`LLMRouter` — create from model entries, call
  ``acompletion()``, list models, reconfigure on SIGHUP.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

# ``litellm`` is imported lazily at first ``Router`` construction (in
# ``LLMRouter._configure_from_settings``), not at module scope: its import
# cost is ~5s, and ``klangk.main`` (the ``klangkd`` CLI) imports this
# module — a module-scope import would tax every ``klangkd --help`` /
# ``doctor`` invocation with the full litellm tree (#2757 review).
if TYPE_CHECKING:
    from litellm import Router

from klangk.settings import _resolve_indirection

logger = logging.getLogger(__name__)

# Provider defaults: well-known providers whose api_base can be omitted.
_PROVIDER_DEFAULTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "cohere": "https://api.cohere.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks_ai": "https://api.fireworks.ai/inference/v1",
}

# litellm_params keys whose values may contain secrets and should
# support file:/cmd: indirection.
_INDIRECT_KEYS = frozenset({"api_key", "api_base"})


def parse_model_entry(entry: str) -> dict:
    """Parse a ``provider/model:api_base:api_key`` string into a LiteLLM
    ``model_list`` entry dict.

    The format uses the **first** colon as the model/api_base boundary and
    the **last** colon as the api_base/api_key boundary::

        litellm_model:api_base:api_key

    Returns a dict suitable for inclusion in LiteLLM's ``model_list``.
    """
    first_colon = entry.find(":")
    if first_colon == -1:
        litellm_model = entry
        api_base = ""
        api_key = ""
    else:
        litellm_model = entry[:first_colon]
        rest = entry[first_colon + 1 :]
        last_colon = rest.rfind(":")
        if last_colon == -1:
            api_base = rest
            api_key = ""
        else:
            api_base = rest[:last_colon]
            api_key = rest[last_colon + 1 :]

    if "/" in litellm_model:
        provider, model_name = litellm_model.split("/", 1)
    else:
        provider = ""
        model_name = litellm_model

    if not api_base and provider in _PROVIDER_DEFAULTS:
        api_base = _PROVIDER_DEFAULTS[provider]

    params: dict = {"model": litellm_model}
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key

    return {
        "model_name": model_name,
        "litellm_params": params,
    }


def _normalize_key(key: str) -> str:
    """Convert kebab-case to snake_case."""
    return key.replace("-", "_")


def _normalize_dict_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize a LiteLLM model-list dict entry.

    Accepts kebab-case or snake_case keys at both the top level
    (``model-name`` / ``model_name``) and inside ``litellm-params`` /
    ``litellm_params``.  Resolves ``file:``/``cmd:`` indirection on
    string values inside ``litellm_params``.
    """
    norm: dict[str, Any] = {}
    for k, v in entry.items():
        nk = _normalize_key(k)
        # Accept "params" as a shorthand for "litellm_params".
        if nk == "params":
            nk = "litellm_params"
        if nk == "litellm_params" and isinstance(v, dict):
            params: dict[str, Any] = {}
            for pk, pv in v.items():
                npk = _normalize_key(pk)
                if npk in _INDIRECT_KEYS and isinstance(pv, str):
                    pv = _resolve_indirection(pv, npk) or ""
                params[npk] = pv
            norm[nk] = params
        else:
            norm[nk] = v
    return norm


def _build_model_list(
    entries: list[str | dict[str, Any]],
    default_api_key: str,
) -> list[dict[str, Any]]:
    """Build a litellm model_list from mixed string/dict entries."""
    items = []
    for entry in entries:
        if isinstance(entry, dict):
            parsed = _normalize_dict_entry(entry)
        else:
            parsed = parse_model_entry(entry)
        params = parsed.get("litellm_params", {})
        if default_api_key and "api_key" not in params:
            params["api_key"] = default_api_key
        items.append(parsed)
    return items


def _is_passthrough(model_list: list[dict[str, Any]]) -> bool:
    """True when the config is a single wildcard entry (passthrough mode).

    Only ``model_name: "*"`` triggers passthrough — not a name that
    happens to contain ``*`` (e.g. ``"my*model"``).
    """
    if len(model_list) != 1:
        return False
    return model_list[0].get("model_name", "") == "*"


class LLMRouter:
    """In-process LLM router (subsystem).

    Constructed from ``app`` following the subsystem pattern.  Creates a
    ``litellm.Router`` when ``llm_models`` is configured; otherwise the
    router is ``None`` and the endpoints return 503.

    **Passthrough mode**: when the config has exactly one wildcard entry
    (``model_name`` contains ``*``), litellm is bypassed entirely.
    Requests are forwarded to the upstream via httpx, and ``/models``
    queries the upstream for dynamic model discovery.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._passthrough_base: str | None = None
        self._passthrough_key: str = ""
        self._router: Router | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._configure_from_settings(app.state.settings)

    def _configure_from_settings(self, settings) -> None:
        from litellm import Router  # allow-deferred-import (see module top)

        models = settings.llm_models
        if not models:
            self._router = None
            self._passthrough_base = None
            return

        model_list = _build_model_list(models, settings.llm_api_key)

        # Close any previous passthrough client before switching modes.
        if self._http_client is not None:
            try:
                asyncio.get_event_loop().create_task(
                    self._http_client.aclose()
                )
            except RuntimeError:  # pragma: no cover
                pass
            self._http_client = None

        if _is_passthrough(model_list):
            params = model_list[0].get("litellm_params", {})
            self._passthrough_base = params.get("api_base", "")
            self._passthrough_key = params.get("api_key", "")
            self._router = None
            self._http_client = httpx.AsyncClient(timeout=300)
            logger.info(
                "llm router: passthrough mode → %s",
                self._passthrough_base,
            )
        else:
            self._passthrough_base = None
            self._passthrough_key = ""
            self._router = Router(
                model_list=model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )

    @property
    def active(self) -> bool:
        """Whether the router has models configured."""
        return self._router is not None or self._passthrough_base is not None

    @property
    def passthrough(self) -> bool:
        """Whether passthrough mode is active."""
        return self._passthrough_base is not None

    def reconfigure(self, app) -> None:
        """Reconfigure the router from new settings (called on SIGHUP)."""
        self._app = app
        self._configure_from_settings(app.state.settings)
        if self.active:
            logger.info("llm router reconfigured")

    async def acompletion(self, **kwargs: Any) -> Any:
        """Proxy to ``litellm.Router.acompletion`` or passthrough.

        In passthrough mode, forwards the request body to the upstream
        via httpx.  In router mode, when ``model`` is empty, missing,
        or does not match any configured ``model_name``, the first
        configured model is used.
        """
        if self._passthrough_base is not None:
            return await self._passthrough_completion(**kwargs)
        if self._router is None:
            raise RuntimeError("LLM router not configured")
        model = kwargs.get("model", "")
        names = self.get_model_names()
        if not names:
            raise RuntimeError("LLM router has no models configured")
        if not model or model not in names:
            kwargs["model"] = names[0]
        return await self._router.acompletion(**kwargs)

    def _passthrough_headers(self) -> dict[str, str]:
        """Build headers for passthrough requests."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._passthrough_key:
            headers["Authorization"] = f"Bearer {self._passthrough_key}"
        return headers

    async def _passthrough_completion(self, **kwargs: Any) -> dict:
        """Forward a non-streaming completion request to the upstream."""
        assert self._http_client is not None
        url = f"{self._passthrough_base}/chat/completions"
        resp = await self._http_client.post(
            url, json=kwargs, headers=self._passthrough_headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def passthrough_completion_stream(
        self, body: dict[str, Any]
    ) -> httpx.Response:
        """Forward a streaming completion request; return the raw response.

        The caller is responsible for streaming ``resp.aiter_lines()``
        to the client (e.g. via ``StreamingResponse``).
        """
        assert self._http_client is not None
        url = f"{self._passthrough_base}/chat/completions"
        resp = await self._http_client.send(
            self._http_client.build_request(
                "POST",
                url,
                json=body,
                headers=self._passthrough_headers(),
            ),
            stream=True,
        )
        resp.raise_for_status()
        return resp

    async def list_upstream_models(self) -> list[dict[str, Any]]:
        """Query the upstream's /models endpoint (passthrough mode).

        Returns the upstream's model list in OpenAI format.  In router
        mode, returns the configured model names.
        """
        if self._passthrough_base is not None:
            assert self._http_client is not None
            url = f"{self._passthrough_base}/models"
            headers = self._passthrough_headers()
            try:
                resp = await self._http_client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", [])
            except Exception:
                logger.exception("Failed to query upstream models at %s", url)
                return []
        return [
            {"id": name, "object": "model", "owned_by": "klangk"}
            for name in self.get_model_names()
        ]

    def get_model_names(self) -> list[str]:
        """Return the list of logical model names the router knows about."""
        if self._router is None:
            return []
        return list(self._router.get_model_names())

    def get_model_list(self) -> list[dict[str, Any]]:
        """Return the full model list (for introspection / debugging)."""
        if self._router is None:
            return []
        return self._router.get_model_list()
