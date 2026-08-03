"""LiteLLM aggregator sidecar: renderer + watchdog (#2046).

When the operator configures ``KLANGKD_LLM_AGGREGATOR_MODELS``, klangk runs a
LiteLLM container as a supervised sidecar.  The container exposes a single
OpenAI-compatible endpoint that routes per-request by ``model`` name to the
configured providers.  The proxy's ``KLANGKD_LLM_BASE_URL`` should point at
this sidecar (``http://127.0.0.1:<port>/v1``); the existing ``/llm-proxy/``
block and ``llm-proxy-models.ts`` model discovery keep working unchanged.

Architecture mirrors :mod:`klangk.proxy` (``ProxyRenderer`` + ``ProxyWatchdog``):

- :class:`LiteLLMRenderer` is a pure function of the merged settings.  It
  emits a ``config.yaml`` that LiteLLM reads at startup (config-only mode,
  no DB, no UI).
- :class:`LiteLLMWatchdog` owns the podman container lifecycle
  (create / start / stop / remove) and supervises it with a respawn loop.

The container publishes its port on ``127.0.0.1`` only (loopback) so that
the proxy can reach it at ``127.0.0.1:<port>`` while the sidecar is not
reachable from the LAN.
"""

from __future__ import annotations

import asyncio
import logging
import os

import yaml

logger = logging.getLogger(__name__)

_CONTAINER_NAME = "klangk-litellm"
_CONTAINER_LABELS = {"klangk.managed": "true"}

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


def parse_model_entry(entry: str) -> dict:
    """Parse a ``provider/model:api_base:api_key`` string into a LiteLLM
    ``model_list`` entry dict.

    The format uses the **first** colon as the model/api_base boundary and
    the **last** colon as the api_base/api_key boundary::

        litellm_model:api_base:api_key

    ``litellm_model`` is in LiteLLM's ``provider/model`` notation (e.g.
    ``openai/gpt-4o``, ``anthropic/claude-sonnet-4``,
    ``ollama/llama3``).  ``api_base`` can be empty to use the provider's
    default.  ``api_key`` can be empty for keyless providers (e.g. local
    Ollama).  Because URLs contain ``://``, a naive ``split(":")`` would
    break; the first/last boundary rule handles this correctly:

    - ``openai/gpt-4o::sk-xxx`` → model ``openai/gpt-4o``, base ``""``,
      key ``sk-xxx``
    - ``ollama/llama3:http://gpu:11434:`` → model ``ollama/llama3``,
      base ``http://gpu:11434``, key ``""``

    Returns a dict suitable for inclusion in LiteLLM's ``model_list``.
    """
    # Split on first colon → (model, rest), then split rest on last colon
    # → (api_base, api_key).  This keeps URLs intact in api_base.
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

    # Derive a human-friendly model_name (the name clients use in the
    # request body's "model" field).  Use the part after the provider
    # slash if present.
    if "/" in litellm_model:
        provider, model_name = litellm_model.split("/", 1)
    else:
        provider = ""
        model_name = litellm_model

    # Resolve api_base from provider defaults if not explicit.
    if not api_base and provider in _PROVIDER_DEFAULTS:
        api_base = _PROVIDER_DEFAULTS[provider]

    params: dict = {
        "model": litellm_model,
    }
    if api_base:
        params["api_base"] = api_base
    if api_key:
        params["api_key"] = api_key

    return {
        "model_name": model_name,
        "litellm_params": params,
    }


class LiteLLMRenderer:
    """Renders a LiteLLM ``config.yaml`` from klangk settings.

    Pure function of ``app.state.settings`` — no side effects beyond
    writing the config file to disk.
    """

    def __init__(self, app) -> None:
        self._app = app

    def reconfigure(self, app) -> None:
        self._app = app

    def render_config(self) -> str:
        """Return the YAML content for LiteLLM's ``config.yaml``."""
        settings = self._app.state.settings
        models = settings.llm_aggregator_models
        if not models:
            return ""

        model_list = [parse_model_entry(entry) for entry in models]

        config: dict = {
            "model_list": model_list,
        }

        master_key = settings.llm_aggregator_master_key
        if master_key:
            config["general_settings"] = {
                "master_key": master_key,
            }

        return yaml.dump(config, default_flow_style=False, sort_keys=False)

    def write_config(self, conf_path: str) -> None:
        """Render and write the config to *conf_path*."""
        content = self.render_config()
        if not content:
            return
        with open(
            os.open(conf_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
        ) as f:
            f.write(content)
        logger.debug("litellm config written to %s", conf_path)


class LiteLLMWatchdog:
    """Owns the LiteLLM sidecar container and its supervision (#2046).

    Mirrors :class:`klangk.proxy.ProxyWatchdog`: constructed with
    ``app``, stored on ``app.state.litellm_watchdog``; the lifespan
    calls ``.start()`` / ``.stop()``.

    The sidecar is a podman container (``ghcr.io/berriai/litellm``),
    not a native process, to avoid pulling LiteLLM's large dependency
    tree into klangk's venv.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._renderer = LiteLLMRenderer(app)
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._pending_reload = False

    def reconfigure(self, app) -> None:
        old = self._app.state.settings
        self._app = app
        self._renderer.reconfigure(app)
        new = app.state.settings
        if (
            old.llm_aggregator_models != new.llm_aggregator_models
            or old.llm_aggregator_master_key != new.llm_aggregator_master_key
            or old.llm_aggregator_port != new.llm_aggregator_port
            or old.llm_aggregator_image != new.llm_aggregator_image
        ):
            self._pending_reload = True

    async def apply_pending_reload(self) -> None:
        """Restart the sidecar container if settings changed."""
        if not self._pending_reload:
            return
        self._pending_reload = False
        settings = self._app.state.settings
        if not settings.llm_aggregator_models:
            self._stopping = True
            if self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
            await self._remove_container()
            return
        # Stop + restart with new config.
        await self._remove_container()
        conf_path = self._config_path()
        self._renderer.write_config(conf_path)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._stopping = False
        self._task = asyncio.create_task(self._watch(conf_path))

    def _config_path(self) -> str:
        return os.path.join(
            self._app.state.settings.state_dir, "litellm-config.yaml"
        )

    async def _watch(self, conf_path: str) -> None:
        """Create, start, and respawn the LiteLLM container on exit."""
        backoff = 1.0
        while not self._stopping:
            podman = self._app.state.podman
            settings = self._app.state.settings
            port = settings.llm_aggregator_port
            image = settings.llm_aggregator_image

            try:
                container_id = await podman.create_container(
                    _CONTAINER_NAME,
                    image,
                    labels=_CONTAINER_LABELS,
                    binds=[f"{conf_path}:/app/config.yaml:ro,Z"],
                    env=[
                        f"LITELLM_MASTER_KEY={settings.llm_aggregator_master_key}",
                        "DATABASE_URL=",
                        "STORE_MODEL_IN_DB=False",
                        "LITELLM_LOG=ERROR",
                    ],
                    publish=[("127.0.0.1", port, 4000)],
                    pull="missing",
                    replace=True,
                )
                await podman.start_container(container_id)
                logger.info(
                    "litellm sidecar started (container %s) on port %d",
                    container_id[:12],
                    port,
                )
            except Exception:
                logger.exception(
                    "litellm sidecar failed to start; retrying in %.1fs",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            # Wait for the container to exit.
            await self._wait_for_exit()

            if self._stopping:
                return
            # Reset backoff — the container ran (start succeeded).
            backoff = 1.0
            logger.warning(
                "litellm sidecar exited unexpectedly; restarting in %.1fs",
                backoff,
            )
            await asyncio.sleep(backoff)

    async def _wait_for_exit(self) -> None:
        """Poll until the container is no longer running."""
        podman = self._app.state.podman
        while not self._stopping:
            try:
                rc, out, _err = await podman.run(
                    [
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        _CONTAINER_NAME,
                    ],
                    check=False,
                )
                if rc != 0 or out.strip().lower() != "true":
                    return
            except Exception:
                return
            await asyncio.sleep(2.0)

    async def _remove_container(self) -> None:
        """Remove the sidecar container if it exists."""
        podman = self._app.state.podman
        try:
            await podman.remove_container(_CONTAINER_NAME)
        except Exception:
            logger.debug("litellm container removal (expected on first start)")

    async def start(self) -> None:
        """Render config and start the LiteLLM sidecar if configured."""
        settings = self._app.state.settings
        if not settings.llm_aggregator_models:
            return
        if os.environ.get("_KLANGKD_DISABLE_LITELLM"):
            return
        conf_path = self._config_path()
        self._renderer.write_config(conf_path)
        self._stopping = False
        self._task = asyncio.create_task(self._watch(conf_path))

    async def stop(self) -> None:
        """Stop the sidecar container and cancel the watchdog."""
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None
            await self._remove_container()
