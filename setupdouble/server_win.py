"""Ancien point d'entrée Windows (v0.1), gardé pour les tâches planifiées déjà installées.

    pythonw -m setupdouble.server_win  ==  pythonw -m setupdouble.server
"""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
