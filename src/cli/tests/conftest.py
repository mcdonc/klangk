"""CLI test configuration."""

import os

# Use sysmon coverage engine for pytest-xdist compatibility.
os.environ.setdefault("COVERAGE_CORE", "sysmon")
