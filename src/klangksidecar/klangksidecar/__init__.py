"""klangksidecar — the network sidecar's FQDN egress DNS proxy + consent gate.

Historically a single ``proxy.py`` module; split into per-concern submodules
(config, state, allowlist, rules, resolve, packets, consent, nfqueue, app). This
``__init__`` re-exports every submodule's names so the flat ``klangksidecar.X``
API the tests and ``python -m klangksidecar`` entry use keeps working. The
sidecar image runs ``python3 -m klangksidecar`` (see ``__main__.py``); the
proxy still binds the DNS listener, applies the FQDN allow-list, learns A-record
IPs, and (when a consent endpoint is set) holds denied egress at the SYN over
NFQUEUE pending a verdict. See the submodule docstrings for the details.
"""

from __future__ import annotations

from . import (  # noqa: F401 (re-exported)
    allowlist,
    app,
    config,
    consent,
    nfqueue,
    packets,
    resolve,
    rules,
    state,
)

# Re-export each submodule's public + private names so ``klangksidecar.X``
# resolves for any X that lived in the old monolithic proxy.py (the test suite
# and callers reference them flat, e.g. ``klangksidecar.ports_for``). Imported
# stdlib modules (time, subprocess, socket, dns, ...) come along too, so tests
# that patch e.g. ``klangksidecar.time.time`` / ``klangksidecar.subprocess.run``
# still affect every submodule — those are singleton module objects shared by
# all. (Monkeypatching a package-defined *function* targets its defining
# submodule directly; this re-export is only for reads.)
for _sub in (config, state, allowlist, rules, resolve, packets, consent, nfqueue, app):
    for _name in dir(_sub):
        if not _name.startswith("__"):
            globals()[_name] = getattr(_sub, _name)
del _sub, _name
