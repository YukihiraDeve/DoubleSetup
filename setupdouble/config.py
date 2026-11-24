"""Chargement de la configuration (TOML) avec valeurs par défaut."""

from __future__ import annotations

import copy
import os
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from . import protocol

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "secret": "",
    "server": {
        "bind": "0.0.0.0",
        "control_port": protocol.CONTROL_PORT,
        "audio_port": protocol.SERVER_AUDIO_PORT,
        "beacon": True,
        "hotkey_toggle": "ctrl+alt+space",
        "hotkey_mac": "",
        "hotkey_pc": "",
        "clipboard": True,
        "name": "",
        "ui_port": 47810,  # tableau de bord local (127.0.0.1), 0 = désactivé
    },
    "client": {
        "server": "",
        "control_port": protocol.CONTROL_PORT,
        "audio_port": protocol.CLIENT_AUDIO_PORT,
        "modifier_layout": "pc",
        "swap_iso_keys": False,
        "mouse_speed": 1.0,
        "scroll_speed": 3.0,
        "invert_scroll": False,
        "clipboard": True,
        "name": "",
        "ui_port": 47811,
    },
    "audio": {
        "enabled": True,
        "sample_rate": 48000,
        "block_ms": 5,
        "latency_ms": 30,
        "mac_sound": True,
        "mic": True,
        "win_output_device": "Focusrite",
        "win_input_device": "Focusrite",
        "win_hostapi": "WASAPI",
        "mic_channel": 1,
        "mac_sound_gain": 1.0,
        "mic_gain": 1.0,
        "mac_capture_device": "BlackHole 2ch",
        "mac_mic_device": "BlackHole 16ch",
        "mac_auto_route": True,
    },
    "log_level": "INFO",
}


class ConfigError(Exception):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def default_path() -> Path:
    env = os.environ.get("SETUPDOUBLE_CONFIG")
    if env:
        return Path(env).expanduser()
    return ROOT / "config.toml"


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path).expanduser() if path else default_path()
    if not p.exists():
        raise ConfigError(
            f"Fichier de configuration introuvable : {p}\n"
            "Copie config.example.toml en config.toml et renseigne 'secret'."
        )
    text = p.read_bytes().decode("utf-8-sig")  # PowerShell 5.1 ajoute un BOM
    data = tomllib.loads(text)
    cfg = _merge(DEFAULTS, data)
    if not cfg["secret"] or cfg["secret"] == "change-me":
        raise ConfigError("Renseigne un 'secret' identique sur le PC et le Mac dans config.toml.")
    if len(cfg["secret"]) < 12:
        raise ConfigError("Le 'secret' doit faire au moins 12 caractères.")
    cfg["_path"] = str(p)
    return cfg
