"""Liste les périphériques audio et indique ceux que la config va utiliser.

    python -m setupdouble.devices
"""

from __future__ import annotations

import sys


def main() -> int:
    import sounddevice as sd

    from . import config as config_mod

    apis = sd.query_hostapis()
    print("Périphériques audio :")
    for idx, dev in enumerate(sd.query_devices()):
        api = apis[dev["hostapi"]]["name"]
        if sys.platform == "win32" and "WASAPI" not in api:
            continue
        io = f"{dev['max_input_channels']} entrée(s), {dev['max_output_channels']} sortie(s)"
        print(f"  [{idx:>2}] {dev['name']}  —  {io}  [{api}]")

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError:
        return 0
    from .audio import find_device

    a = cfg["audio"]
    if sys.platform == "win32":
        wanted = [("Sortie casque", a["win_output_device"], "output", a["win_hostapi"]),
                  ("Entrée micro", a["win_input_device"], "input", a["win_hostapi"])]
    else:
        wanted = [("Capture du son du Mac", a["mac_capture_device"], "input", None),
                  ("Micro virtuel", a["mac_mic_device"], "output", None)]
    print("\nSélection selon config.toml :")
    for label, name, kind, api in wanted:
        try:
            idx = find_device(name, kind, api)
            print(f"  {label:<22} -> [{idx}] {sd.query_devices(idx)['name']}")
        except RuntimeError:
            print(f"  {label:<22} -> INTROUVABLE (« {name} »)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
