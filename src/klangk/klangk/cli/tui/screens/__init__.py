"""TUI screens subpackage — re-exports all public screen classes.

Existing ``from klangk.cli.tui.screens import FooScreen`` imports
continue to work unchanged.
"""

from .account import AccountScreen
from ._base import (
    ConfirmScreen,
    DuplicateScreen,
    NonFocusableVerticalScroll,
    ServerListView,
    SpatialListView,
    SpatialNavScreen,
    TabSkipMixin,
    WorkspaceListView,
)
from .login import LoginScreen
from .main import MainScreen
from .server import AddServerScreen, EditServerScreen, ServerSwitchScreen
from .workspace_detail import WorkspaceDetailScreen
from .workspace_form import CreateWorkspaceScreen, EditWorkspaceScreen

__all__ = [
    "AccountScreen",
    "AddServerScreen",
    "ConfirmScreen",
    "CreateWorkspaceScreen",
    "DuplicateScreen",
    "EditServerScreen",
    "EditWorkspaceScreen",
    "LoginScreen",
    "MainScreen",
    "NonFocusableVerticalScroll",
    "ServerListView",
    "ServerSwitchScreen",
    "SpatialListView",
    "SpatialNavScreen",
    "TabSkipMixin",
    "WorkspaceDetailScreen",
    "WorkspaceListView",
]
