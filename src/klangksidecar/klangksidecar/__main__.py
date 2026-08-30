"""Entry point: ``python -m klangksidecar`` runs the sidecar DNS proxy (PID 1)."""

# PID-1-only entry (#2834): executed only as the sidecar container's init
# (``python3 -m klangksidecar``, see the image Dockerfile) and exercised
# end-to-end by the real-podman e2e (test_network_sidecar_e2e.py), never
# imported by the unit suite.
from .app import main  # pragma: no cover

if __name__ == "__main__":  # pragma: no cover
    main()
