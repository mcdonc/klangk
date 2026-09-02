"""Shared plumbing for the model package: the app-bound submodel base
and small helpers used across the domain modules."""

import time


class Submodel:
    """Base for :class:`~klangk.model.model.Model`'s subdomains.

    Every submodel holds only the ``app`` reference (the app-ownership
    rule: constructed as ``X(app)``, caching only ``self.app``, reading
    ``self.app.state.*`` live at call time). ``reconfigure`` rebinds the
    reference on a settings swap (the SIGHUP config reload, #1587).
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app


def resolve_prune_now(now: float | None) -> float:
    """A prune sweep's reference clock (caller-supplied or wall clock)."""
    return time.time() if now is None else now
