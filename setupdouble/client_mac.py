"""Ancien point d'entrée (v0.1), gardé pour les LaunchAgents déjà installés."""

import sys

from .client import AuthError, Client, RecordingInjector, discover, main

__all__ = ["AuthError", "Client", "RecordingInjector", "discover", "main"]

if __name__ == "__main__":
    sys.exit(main())
