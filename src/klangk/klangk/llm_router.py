"""In-process LLM router backed by ``litellm.Router`` (#2070).

Replaces the LiteLLM sidecar container with a lightweight in-process
router that dispatches ``/llm-proxy/`` requests to the configured
providers.  The router is instantiated from the same
``provider/model:api_base:api_key`` entry format used by the old
``KLANGKD_LLM_AGGREGATOR_MODELS`` setting (and by its successor
``KLANGKD_LLM_MODELS``).

Public API:

- :class:`LLMRouter` — create from model entries, call
  ``acompletion()``, list models, reconfigure on SIGHUP.
"""

from __future__ import annotations

import logging
from typing import Any

from litellm import Router

from klangk.litellm import parse_model_entry

logger = logging.getLogger(__name__)


class LLMRouter:
    """Thin wrapper around ``litellm.Router``.

    Constructed from a list of colon-delimited model-entry strings (the
    format ``parse_model_entry`` already understands).  Exposes
    ``acompletion`` for the FastAPI endpoint and ``get_model_names`` for
    ``/models`` discovery.  Supports live reconfiguration via
    :meth:`reconfigure`.
    """

    def __init__(
        self,
        model_entries: list[str],
        default_api_key: str = "",
    ) -> None:
        self._default_api_key = default_api_key
        model_list = self._build_model_list(model_entries)
        self._router = Router(
            model_list=model_list,
            routing_strategy="simple-shuffle",
            num_retries=2,
        )

    def _build_model_list(self, entries: list[str]) -> list[dict[str, Any]]:
        items = []
        for entry in entries:
            parsed = parse_model_entry(entry)
            if (
                self._default_api_key
                and "api_key" not in parsed["litellm_params"]
            ):
                parsed["litellm_params"]["api_key"] = self._default_api_key
            items.append(parsed)
        return items

    def reconfigure(
        self,
        model_entries: list[str],
        default_api_key: str = "",
    ) -> None:
        """Replace the model list (called on SIGHUP)."""
        self._default_api_key = default_api_key
        model_list = self._build_model_list(model_entries)
        self._router.set_model_list(model_list)
        logger.info(
            "llm router reconfigured with %d model(s)", len(model_list)
        )

    async def acompletion(self, **kwargs: Any) -> Any:
        """Proxy to ``litellm.Router.acompletion``."""
        return await self._router.acompletion(**kwargs)

    def get_model_names(self) -> list[str]:
        """Return the list of logical model names the router knows about."""
        return list(self._router.get_model_names())

    def get_model_list(self) -> list[dict[str, Any]]:
        """Return the full model list (for introspection / debugging)."""
        return self._router.get_model_list()
