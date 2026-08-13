"""Entry point: ``python -m klangksidecar`` runs the sidecar DNS proxy (PID 1)."""

from .app import main

if __name__ == "__main__":
    main()
