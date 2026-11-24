"""Réglages modifiables depuis la fenêtre de statut.

Chaque champ connaît sa section, son type et sa validation. L'enregistrement
réécrit config.toml (commentaires conservés) puis relance SetupDouble.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading

from . import config as config_mod
from . import hotkeys

log = logging.getLogger(__name__)

# (section, clé, type, rôles concernés)
FIELDS: list[tuple[str | None, str, str, tuple[str, ...]]] = [
    (None, "secret", "secret", ("server", "client")),
    ("server", "name", "str", ("server",)),
    ("server", "hotkey_toggle", "hotkey", ("server",)),
    ("server", "hotkey_remote", "hotkey?", ("server",)),
    ("server", "hotkey_local", "hotkey?", ("server",)),
    ("server", "clipboard", "bool", ("server",)),
    ("server", "wake_on_lan", "bool", ("server",)),
    ("server", "modifier_layout", "choice", ("server",)),
    ("client", "name", "str", ("client",)),
    ("client", "server", "host?", ("client",)),
    ("client", "clipboard", "bool", ("client",)),
    ("client", "modifier_layout", "choice", ("client",)),
    ("client", "swap_iso_keys", "bool", ("client",)),
    ("client", "mouse_profile", "choice", ("client",)),
    ("client", "match_screen", "bool", ("client",)),
    ("client", "mouse_speed", "float:0.1:5", ("client",)),
    ("client", "scroll_speed", "float:0.5:20", ("client",)),
    ("client", "invert_scroll", "bool", ("client",)),
    ("client", "prevent_sleep", "choice", ("client",)),
    ("audio", "enabled", "bool", ("server", "client")),
    ("audio", "sound_mode", "choice", ("server",)),
    ("audio", "mic_mode", "choice", ("server",)),
    ("audio", "server_output_device", "device", ("server",)),
    ("audio", "server_input_device", "device", ("server",)),
    ("audio", "mic_channel", "int:1:8", ("server",)),
    ("audio", "sound_gain", "float:0:4", ("server",)),
    ("audio", "mic_gain", "float:0:4", ("server",)),
    ("audio", "client_capture_device", "device", ("client",)),
    ("audio", "client_mic_device", "device", ("client",)),
    ("audio", "latency_ms", "int:10:200", ("server", "client")),
    ("display", "enabled", "bool", ("server", "client")),
    ("display", "monitor", "str", ("server", "client")),
    ("display", "input", "input", ("server", "client")),
]

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-.]{0,252})$")


class SettingsError(ValueError):
    pass


def _validate(section, key, kind, value):
    label = f"{section + '.' if section else ''}{key}"
    if kind == "secret":
        value = str(value).strip()
        if not config_mod.SECRET_RE.match(value):
            raise SettingsError("La clé doit faire au moins 12 caractères "
                                "(lettres, chiffres, « . », « _ » ou « - »).")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{label} : vrai ou faux attendu")
        return value
    if kind == "choice":
        choices = config_mod.CHOICES[(section, key)]
        if value not in choices:
            raise SettingsError(f"{label} : valeur parmi {', '.join(choices)}")
        return value
    if kind.startswith(("int:", "float:")):
        typ, lo, hi = kind.split(":")
        try:
            num = int(value) if typ == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{label} : nombre attendu") from None
        if not float(lo) <= num <= float(hi):
            raise SettingsError(f"{label} : entre {lo} et {hi}")
        return num
    if kind == "input":
        from .display import INPUTS, parse_input
        try:
            code = parse_input(value)
        except ValueError as exc:
            raise SettingsError(str(exc)) from None
        if code is None:
            return ""
        return next((n for n, c in INPUTS.items() if c == code), f"0x{code:02x}")
    value = "" if value is None else str(value).strip()
    if kind in ("hotkey", "hotkey?"):
        if not value and kind == "hotkey?":
            return ""
        try:
            hotkeys.parse_hotkey(value, "toggle")
        except ValueError as exc:
            raise SettingsError(f"Raccourci invalide : {exc}") from None
        return value.lower().replace(" ", "")
    if kind == "host?":
        if not value:
            return ""
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not _HOST_RE.match(value):
                raise SettingsError("Adresse du serveur invalide (ex. 192.168.1.15)") from None
        return value
    if len(value) > 200 or any(c in value for c in "\n\r"):
        raise SettingsError(f"{label} : texte invalide")
    return value


def fields_for(role: str):
    return [(sec, key, kind) for sec, key, kind, roles in FIELDS if role in roles]


def current_values(cfg: dict, role: str) -> dict:
    out: dict = {}
    for sec, key, kind in fields_for(role):
        if kind == "secret":
            continue  # jamais renvoyée à la page : on peut seulement la remplacer
        src = cfg if sec is None else cfg[sec]
        out.setdefault(sec or "general", {})[key] = src.get(key)
    return out


def validate(updates: dict, role: str) -> dict:
    """``{"server": {...}, "general": {"secret": ...}}`` -> updates pour config.save."""
    allowed = {(sec or "general", key): (sec, kind) for sec, key, kind in fields_for(role)}
    clean: dict = {}
    for sec_name, values in (updates or {}).items():
        if not isinstance(values, dict):
            raise SettingsError("format invalide")
        for key, value in values.items():
            spec = allowed.get((sec_name, key))
            if spec is None:
                raise SettingsError(f"réglage inconnu : {sec_name}.{key}")
            sec, kind = spec
            if kind == "secret" and (value is None or value == ""):
                continue  # champ laissé vide : clé inchangée
            clean.setdefault(sec, {})[key] = _validate(sec, key, kind, value)
    return clean


def device_lists(cfg: dict, role: str) -> dict:
    """Périphériques proposés dans l'interface, selon le rôle."""
    try:
        from . import audio
        api = cfg["audio"]["server_hostapi"] if role == "server" else None
        return {"inputs": audio.list_devices("input", api or None),
                "outputs": audio.list_devices("output", api or None)}
    except Exception as exc:  # sounddevice absent
        return {"inputs": [], "outputs": [], "error": str(exc)}


class SettingsMixin:
    """À combiner avec Server ou Client : ajoute settings() et save_settings()."""

    role: str
    cfg: dict

    def display_info(self) -> dict:
        """Écrans détectés pour l'onglet Écran (peut prendre quelques secondes)."""
        link = getattr(self, "display", None)
        if link is None:
            return {"supported": False, "monitors": []}
        info = link.monitors()
        info["my_input"] = link.my_input
        peer = getattr(self, "_peer_display_input", lambda: None)()
        info["peer_input"] = peer
        return info

    def settings(self) -> dict:
        return {"role": self.role, "values": current_values(self.cfg, self.role),
                "devices": device_lists(self.cfg, self.role),
                "choices": {f"{s}.{k}": list(v) for (s, k), v in config_mod.CHOICES.items()}}

    def save_settings(self, updates: dict) -> tuple[bool, str]:
        try:
            clean = validate(updates, self.role)
        except SettingsError as exc:
            return False, str(exc)
        if not clean:
            return True, "Rien à changer."
        try:
            config_mod.save(clean, self.cfg.get("_path"))
        except Exception as exc:
            log.exception("enregistrement des réglages")
            return False, f"Enregistrement impossible : {exc}"
        log.info("Réglages enregistrés : %s",
                 ", ".join(f"{s or 'general'}.{k}" for s, v in clean.items() for k in v
                           if k != "secret"))
        restart = getattr(self, "restart", None)
        if restart is None:
            return True, "Enregistré. Redémarre SetupDouble pour appliquer."
        # laisse le temps à la réponse HTTP de partir
        threading.Timer(0.6, restart).start()
        return True, "Enregistré. SetupDouble redémarre…"
