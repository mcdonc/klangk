"""CLI unit test configuration."""

import os

# Use sysmon coverage engine for pytest-xdist compatibility — mirrors the
# server suite's conftest (src/klangk/klangkd-tests/tests/conftest.py) so
# coverage is tracked correctly across xdist workers (#1526).
os.environ.setdefault("COVERAGE_CORE", "sysmon")
