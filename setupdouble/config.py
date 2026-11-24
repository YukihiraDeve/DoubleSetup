"""Configuration (TOML) : valeurs par défaut, anciens noms, enregistrement.

Le même fichier sert aux deux rôles :
  * [server] : la machine où sont branchés clavier, souris et carte son ;
  * [client] : la machine pilotée à distance.
Chaque rôle fonctionne sous Windows, macOS et Linux.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from . import protocol

ROOT = Path(__file__).resolve().parent.parent


def _client_audio_defaults(platform: str) -> dict:
    """Périphériques virtuels conseillés pour la machine pilotée, selon son système."""
    if platform == "darwin":
        return {"client_capture_device": "BlackHole 2ch", "client_mic_device": "BlackHole 16ch"}
    if platform == "win32":  # VB-CABLE : https://vb-audio.com/Cable/
        return {"client_capture_device": "", "client_mic_device": "CABLE Input"}
    return {"client_capture_device": "", "client_mic_device": ""}


DEFAULTS: dict = {
    "secret": "",
    # rôle de cette machine (écrit par l'installation) : "server" ou "client"
    "role": "server" if sys.platform == "win32" else "client",
    "server": {
        "bind": "0.0.0.0",
        "control_port": protocol.CONTROL_PORT,
        "audio_port": protocol.SERVER_AUDIO_PORT,
        "beacon": True,
        "hotkey_toggle": "ctrl+alt+space",
        "hotkey_remote": "",   # facultatif : aller directement sur la machine pilotée
        "hotkey_local": "",    # facultatif : revenir directement ici
        "clipboard": True,
        "name": "",
        "ui_port": 47810,      # fenêtre de statut (127.0.0.1), 0 = désactivée
        "wake_on_lan": True,   # le raccourci réveille la machine pilotée si elle dort
        "modifier_layout": "pc",  # côté Mac serveur : Cmd = Ctrl ("pc") ou Cmd = Win ("mac")
    },
    "client": {
        "server": "",          # IP du serveur ; vide = découverte automatique
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
        "prevent_sleep": "power",  # power, always ou never
    },
    "audio": {
        "enabled": True,
        "sample_rate": 48000,
        "block_ms": 5,
        "latency_ms": 30,
        "sound": True,         # le son de la machine pilotée sort dans le casque du serveur
        "mic": True,           # le micro du serveur est disponible sur la machine pilotée
        "sound_mode": "mix",   # mix : les deux sons en même temps ; follow : seulement si pilotée
        "mic_mode": "always",  # always : micro partagé en permanence ; follow : seulement si pilotée
        # Serveur ("" = périphérique par défaut du système)
        "server_output_device": "",
        "server_input_device": "",
        "server_hostapi": "WASAPI" if sys.platform == "win32" else "",
        "mic_channel": 1,
        "sound_gain": 1.0,
        "mic_gain": 1.0,
        # Machine pilotée ("" = désactivé)
        **_client_audio_defaults(sys.platform),
        "client_auto_route": True,  # macOS : bascule l'entrée/sortie par défaut
    },
    "log_level": "INFO",
}

# Anciens noms (v0.1) -> nouveaux, par section
ALIASES: dict[str, dict[str, str]] = {
    "server": {"hotkey_mac": "hotkey_remote", "hotkey_pc": "hotkey_local"},
    "audio": {
        "mac_sound": "sound",
        "mac_sound_gain": "sound_gain",
        "win_output_device": "server_output_device",
        "win_input_device": "server_input_device",
        "win_hostapi": "server_hostapi",
        "mac_capture_device": "client_capture_device",
        "mac_mic_device": "client_mic_device",
        "mac_auto_route": "client_auto_route",
    },
}

CHOICES = {
    ("client", "modifier_layout"): ("pc", "mac"),
    ("server", "modifier_layout"): ("pc", "mac"),
    ("client", "prevent_sleep"): ("power", "always", "never"),
    ("audio", "sound_mode"): ("mix", "follow"),
    ("audio", "mic_mode"): ("always", "follow"),
}

SECRET_RE = re.compile(r"^[A-Za-z0-9_.\-]{12,128}$")


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


def migrate(data: dict) -> dict:
    """Renomme les anciennes clés (le nouveau nom gagne s'il est présent)."""
    data = copy.deepcopy(data)
    for section, names in ALIASES.items():
        sec = data.get(section)
        if not isinstance(sec, dict):
            continue
        for old, new in names.items():
            if old in sec:
                value = sec.pop(old)
                sec.setdefault(new, value)
    return data


def default_path() -> Path:
    env = os.environ.get("SETUPDOUBLE_CONFIG")
    if env:
        return Path(env).expanduser()
    return ROOT / "config.toml"


def check_secret(secret: str) -> None:
    if not secret or secret == "change-me":
        raise ConfigError("Renseigne un 'secret' identique sur les deux machines dans config.toml.")
    if len(secret) < 12:
        raise ConfigError("Le 'secret' doit faire au moins 12 caractères.")


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path).expanduser() if path else default_path()
    if not p.exists():
        raise ConfigError(
            f"Fichier de configuration introuvable : {p}\n"
            "Lance le script d'installation, ou copie config.example.toml en config.toml."
        )
    text = p.read_bytes().decode("utf-8-sig")  # PowerShell 5.1 ajoute un BOM
    cfg = _merge(DEFAULTS, migrate(tomllib.loads(text)))
    check_secret(cfg["secret"])
    cfg["_path"] = str(p)
    return cfg


# --- Enregistrement (garde les commentaires) ----------------------------------------

def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # chaîne TOML « basique » valide
    raise TypeError(f"type non pris en charge : {type(value).__name__}")


_SECTION_RE = re.compile(r"^\s*\[([A-Za-z0-9_.\-]+)\]\s*(#.*)?$")


def _key_re(key: str) -> re.Pattern:
    return re.compile(rf"^(\s*){re.escape(key)}(\s*=\s*)(.*)$")


def _split_comment(rest: str) -> tuple[str, str]:
    """Sépare la valeur du commentaire de fin de ligne (hors chaînes)."""
    in_str, quote, i = False, "", 0
    while i < len(rest):
        c = rest[i]
        if in_str:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c == "#":
            return rest[:i].rstrip(), rest[i:]
        i += 1
    return rest.rstrip(), ""


def update_text(text: str, updates: dict) -> str:
    """Applique ``{section|None: {clé: valeur}}`` au texte TOML, commentaires compris."""
    lines = text.splitlines()
    for section, values in updates.items():
        for key, value in values.items():
            names = [key] + [old for old, new in ALIASES.get(section or "", {}).items()
                             if new == key]
            lines = _set_value(lines, section, key, names, toml_value(value))
    return "\n".join(lines) + "\n"


def _set_value(lines: list[str], section: str | None, key: str, names: list[str],
               value: str) -> list[str]:
    current, start, end = None, 0, None
    bounds: dict[str | None, tuple[int, int]] = {}
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            bounds[current] = (start, i)
            current, start = m.group(1), i + 1
    bounds[current] = (start, len(lines))
    if section not in bounds:
        return lines + ["", f"[{section}]", f"{key} = {value}"]
    start, end = bounds[section]
    for name in names:
        rx = _key_re(name)
        for i in range(start, end):
            m = rx.match(lines[i])
            if m:
                _, comment = _split_comment(m.group(3))
                lines[i] = f"{m.group(1)}{key}{m.group(2)}{value}" + (f"   {comment}" if comment else "")
                return lines
    # clé absente : après la dernière affectation de la section
    insert = start
    for i in range(start, end):
        if re.match(r"^\s*[A-Za-z0-9_\-]+\s*=", lines[i]):
            insert = i + 1
    return lines[:insert] + [f"{key} = {value}"] + lines[insert:]


def save(updates: dict, path: str | os.PathLike | None = None) -> Path:
    """Enregistre des valeurs dans config.toml (crée le fichier depuis l'exemple si besoin)."""
    p = Path(path).expanduser() if path else default_path()
    if p.exists():
        raw = p.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
    else:
        bom, text = False, (ROOT / "config.example.toml").read_text(encoding="utf-8")
    new = update_text(text, updates)
    tomllib.loads(new)  # ne jamais écrire un fichier illisible
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p
