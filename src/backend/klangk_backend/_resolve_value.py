"""CLI front-end for prefixed-value resolution (``file:``/``cmd:``).

This is the console-script twin of [klangk_backend.util.resolve_file_value].
It exists so non-Python callers — currently ``scripts/nginx.sh``, which runs
under devenv / the host container's shell and consumes a few ``KLANGK_LLM_*``
vars via bash expansion — can resolve prefixed values without reimplementing
the prefix logic in shell.

The prefix logic itself lives once, in [klangk_backend.util], and is
fully unit-tested there. This module only wires it up to argv/stdout and is
registered as the ``klangk-resolve-value`` console script in
``src/backend/pyproject.toml``.

Usage::

    klangk-resolve-value 'file:/run/secrets/jwt'
    klangk-resolve-value 'cmd:aws secretsmanager ... | jq -r .SecretString'
    klangk-resolve-value 'plain-value'   # -> plain-value (verbatim)

Failures mirror resolve_file_value: a ``file:``/``cmd:`` error is logged
(to stderr) and the empty string is printed to stdout.
"""

import logging
import sys

from .util import resolve_file_value


def main() -> None:
    """Resolve a single prefixed value from argv[1] and print it."""
    if len(sys.argv) != 2:
        print("usage: klangk-resolve-value <value>", file=sys.stderr)
        raise SystemExit(2)
    # resolve_file_value returns "" on failure (logging the reason to
    # stderr via basicConfig in environments that configure it), matching
    # the backend's own failure behavior. Configure logging so the reason
    # is visible when run as a console script.
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    sys.stdout.write(resolve_file_value(sys.argv[1]))


if __name__ == "__main__":  # pragma: no cover
    main()
