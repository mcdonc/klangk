"""Tests for klangk.launcher — config-error exit-status translation (#2666).

The launcher turns uvicorn's generic STARTUP_FAILURE exit (3) into
``EX_CONFIG`` (78) when the lifespan flagged a deterministic
``ConfigurationError`` on ``app.state.startup_config_error``, so a
supervisor can stop restart-looping a config that cannot fix itself.
"""

import types

from klangk import launcher
from klangk.exceptions import EX_CONFIG


class TestConfigErrorExitStatus:
    def test_flagged_config_error_maps_to_ex_config(self):
        app_state = types.SimpleNamespace(
            startup_config_error=(
                "KLANGKD_DEFAULT_PASSWORD violates the configured "
                "password policy"
            )
        )
        assert launcher.config_error_exit_status(app_state) == EX_CONFIG

    def test_unflagged_state_maps_to_none(self):
        # No attribute at all (normal startup, non-config crash).
        assert (
            launcher.config_error_exit_status(types.SimpleNamespace()) is None
        )

    def test_none_flag_maps_to_none(self):
        app_state = types.SimpleNamespace(startup_config_error=None)
        assert launcher.config_error_exit_status(app_state) is None

    def test_ex_config_is_78(self):
        """sysexits.h EX_CONFIG — the value systemd configs will pin via
        ``RestartPreventExitStatus=78``."""
        assert EX_CONFIG == 78
