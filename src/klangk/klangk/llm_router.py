"""In-process LLM router backed by ``litellm.Router`` (#2070).

Replaces the LiteLLM sidecar container with a lightweight in-process
router that dispatches ``/llm-proxy/`` requests to the configured
providers.

Accepts model entries in two formats:

1. **Colon-delimited strings** (for env vars):
   ``provider/model:api_base:api_key`` — parsed via
   :func:`~klangk.litellm.parse_model_entry`.

2. **LiteLLM-native dicts** (for ``klangkd.yaml``): the same
   ``model_name`` / ``litellm_params`` shape documented by LiteLLM.
   Keys accept both kebab-case and snake_case (``api-key`` or
   ``api_key``, ``model-name`` or ``model_name``).  String values
   support ``file:`` and ``cmd:`` indirection so secrets stay out of
   the config file.

Follows the subsystem pattern: ``__init__(app)``, ``reconfigure(app)``,
stored on ``app.state.llm_router``.
"""

from __future__ import annotations

import logging
from typing import Any

from litellm import Router

from klangk.litellm import parse_model_entry
from klangk.settings import _resolve_indirection

logger = logging.getLogger(__name__)

# litellm_params keys whose values may contain secrets and should
# support file:/cmd: indirection.
_INDIRECT_KEYS = frozenset({"api_key", "api_base"})


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


class LLMRouter:
    """In-process LLM router (subsystem).

    Constructed from ``app`` following the subsystem pattern.  Creates a
    ``litellm.Router`` when ``llm_aggregator_models`` is configured;
    otherwise the router is ``None`` and the endpoints return 503.
    """

    def __init__(self, app) -> None:
        self._app = app
        settings = app.state.settings
        models = settings.llm_models
        if models:
            model_list = _build_model_list(models, settings.llm_api_key)
            self._router: Router | None = Router(
                model_list=model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )
        else:
            self._router = None

    @property
    def active(self) -> bool:
        """Whether the router has models configured."""
        return self._router is not None

    def reconfigure(self, app) -> None:
        """Reconfigure the router from new settings (called on SIGHUP)."""
        self._app = app
        settings = app.state.settings
        models = settings.llm_models
        if models:
            model_list = _build_model_list(models, settings.llm_api_key)
            if self._router is not None:
                self._router.set_model_list(model_list)
            else:
                self._router = Router(
                    model_list=model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            logger.info(
                "llm router reconfigured with %d model(s)", len(model_list)
            )
        else:
            self._router = None

    async def acompletion(self, **kwargs: Any) -> Any:
        """Proxy to ``litellm.Router.acompletion``."""
        if self._router is None:
            raise RuntimeError("LLM router not configured")
        return await self._router.acompletion(**kwargs)

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
