"""python -m setupdouble <commande>

  server   la machine où sont branchés clavier, souris et carte son
  client   la machine pilotée
  app      ouvre la fenêtre de statut
  update   met à jour depuis GitHub
  devices  liste les périphériques audio
  demo     démo de l'interface, sans rien installer
"""

import sys

COMMANDS = {
    "server": "setupdouble.server",
    "client": "setupdouble.client",
    "app": "setupdouble.app",
    "update": "setupdouble.updater",
    "devices": "setupdouble.devices",
    "demo": "setupdouble.demo",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    import importlib
    module = importlib.import_module(COMMANDS[sys.argv[1]])
    sys.argv = [f"setupdouble {sys.argv[1]}"] + sys.argv[2:]
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
