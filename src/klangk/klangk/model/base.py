"""Shared base for the model package's app-bound submodels."""


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
