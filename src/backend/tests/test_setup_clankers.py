"""Tests for klangk-setup-clankers.py (per-user Pi agent setup)."""

import importlib.util
import json
from pathlib import Path

import pytest

# Import the script as a module (it's not a package).
_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "containers"
    / "workspace"
    / "klangk-setup-clankers.py"
)
_spec = importlib.util.spec_from_file_location("setup_clankers", _SCRIPT)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Set HOME and IMAGE_DIR to temp directories."""
    home = tmp_path / "home" / "testuser"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    image_dir = tmp_path / "opt" / "klangk" / "pi-agent"
    for d in ("extensions", "skills", "prompts", "npm"):
        (image_dir / d).mkdir(parents=True)
    # Write a base settings.json the way the real image would.
    (image_dir / "settings.json").write_text(json.dumps({"version": 1}))

    monkeypatch.setattr(sc, "IMAGE_DIR", image_dir)
    monkeypatch.setattr(sc, "ERROR_LOG", tmp_path / "errors.log")
    return home


class TestSetupDirs:
    def test_creates_npm_and_extensions(self, fake_home):
        sc.setup_dirs()
        agent = fake_home / ".pi" / "agent"
        assert (agent / "npm").is_dir()
        assert (agent / "extensions").is_dir()


class TestWriteSettings:
    def test_creates_settings_with_all_keys(self, fake_home, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_MODEL", "test-model")
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)

        sc.write_settings()

        settings = json.loads((agent / "settings.json").read_text())
        assert settings["defaultProvider"] == "llm-proxy"
        assert settings["defaultModel"] == "test-model"
        assert settings["defaultThinkingLevel"] == "off"
        assert settings["extensions"] == [str(sc.IMAGE_DIR / "extensions")]
        assert settings["skills"] == [str(sc.IMAGE_DIR / "skills")]
        assert settings["prompts"] == [str(sc.IMAGE_DIR / "prompts")]

    def test_does_not_overwrite_existing(self, fake_home):
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)
        existing = {"custom": True}
        (agent / "settings.json").write_text(json.dumps(existing))

        sc.write_settings()

        assert json.loads((agent / "settings.json").read_text()) == existing


class TestEnsureSettingsKeys:
    def test_backfills_missing_keys(self, fake_home):
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)
        # Simulate old settings that only has extensions.
        old = {"extensions": ["/old/extensions"], "other": "keep"}
        (agent / "settings.json").write_text(json.dumps(old))

        sc.ensure_settings_keys()

        settings = json.loads((agent / "settings.json").read_text())
        assert settings["extensions"] == ["/old/extensions"]  # not overwritten
        assert settings["skills"] == [str(sc.IMAGE_DIR / "skills")]
        assert settings["prompts"] == [str(sc.IMAGE_DIR / "prompts")]
        assert settings["other"] == "keep"

    def test_no_write_when_all_present(self, fake_home):
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)
        full = {
            "extensions": ["/e"],
            "skills": ["/s"],
            "prompts": ["/p"],
        }
        (agent / "settings.json").write_text(json.dumps(full))
        mtime_before = (agent / "settings.json").stat().st_mtime_ns

        sc.ensure_settings_keys()

        # File should not have been rewritten.
        assert (agent / "settings.json").stat().st_mtime_ns == mtime_before

    def test_noop_when_no_settings(self, fake_home):
        # Should not raise when settings.json doesn't exist.
        sc.ensure_settings_keys()


class TestWriteModels:
    def test_creates_models_json(self, fake_home, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_PROXY_URL", "http://proxy:8080")
        monkeypatch.setenv("KLANGK_LLM_MODEL", "test-model")
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)

        sc.write_models()

        models = json.loads((agent / "models.json").read_text())
        provider = models["providers"]["llm-proxy"]
        assert provider["baseUrl"] == "http://proxy:8080"
        assert provider["models"][0]["id"] == "test-model"

    def test_empty_models_without_env(self, fake_home, monkeypatch):
        monkeypatch.delenv("KLANGK_LLM_MODEL", raising=False)
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)

        sc.write_models()

        models = json.loads((agent / "models.json").read_text())
        assert models["providers"]["llm-proxy"]["models"] == []


class TestBuildAgentContext:
    def test_copies_prompt(self, fake_home, monkeypatch):
        prompt_src = sc.IMAGE_DIR.parent / "agent-context.md"
        prompt_src.write_text("# Agent Context")
        monkeypatch.setattr(sc, "AGENT_CONTEXT_SRC", prompt_src)

        sc.build_agent_context()

        # Lands in Pi's global context-file slot, not home root.
        assert (
            fake_home / ".pi" / "agent" / "AGENTS.md"
        ).read_text() == "# Agent Context"
        assert not (fake_home / "AGENTS.md").exists()  # home-root copy is gone

    def test_does_not_overwrite_existing(self, fake_home, monkeypatch):
        prompt_src = sc.IMAGE_DIR.parent / "agent-context.md"
        prompt_src.write_text("# New")
        monkeypatch.setattr(sc, "AGENT_CONTEXT_SRC", prompt_src)
        agents_md = fake_home / ".pi" / "agent"
        agents_md.mkdir(parents=True)
        (agents_md / "AGENTS.md").write_text("# User Custom")

        sc.build_agent_context()

        assert (agents_md / "AGENTS.md").read_text() == "# User Custom"


class TestMain:
    def test_first_run_creates_everything(self, fake_home, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_MODEL", "m")
        monkeypatch.setenv("KLANGK_LLM_PROXY_URL", "http://x")
        monkeypatch.setattr("sys.argv", ["setup-clankers"])

        sc.main()

        agent = fake_home / ".pi" / "agent"
        assert (agent / "settings.json").exists()
        assert (agent / "models.json").exists()
        settings = json.loads((agent / "settings.json").read_text())
        assert "skills" in settings
        assert "prompts" in settings

    def test_existing_settings_gets_backfill(self, fake_home, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_MODEL", "m")
        monkeypatch.setenv("KLANGK_LLM_PROXY_URL", "http://x")
        monkeypatch.setattr("sys.argv", ["setup-clankers"])
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)
        # Old settings missing skills/prompts.
        old = {"extensions": ["/e"]}
        (agent / "settings.json").write_text(json.dumps(old))

        sc.main()

        settings = json.loads((agent / "settings.json").read_text())
        assert "skills" in settings
        assert "prompts" in settings
        assert settings["extensions"] == ["/e"]  # preserved

    def test_force_rewrites_settings(self, fake_home, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_MODEL", "m")
        monkeypatch.setenv("KLANGK_LLM_PROXY_URL", "http://x")
        monkeypatch.setattr("sys.argv", ["setup-clankers", "--force"])
        agent = fake_home / ".pi" / "agent"
        agent.mkdir(parents=True)
        (agent / "settings.json").write_text(json.dumps({"old": True}))

        sc.main()

        settings = json.loads((agent / "settings.json").read_text())
        assert "old" not in settings
        assert settings["defaultProvider"] == "llm-proxy"

    def test_skips_system_user(self, fake_home, monkeypatch):
        monkeypatch.setenv("HOME", "/home")
        monkeypatch.setattr("sys.argv", ["setup-clankers"])

        sc.main()  # should not raise or create anything

        assert not (Path("/home") / ".pi").exists()
