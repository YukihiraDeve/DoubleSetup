"""Presse-papiers et routage audio sur macOS."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

_ENV = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")


class MacClipboard:
    def get_text(self) -> str | None:
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, env=_ENV, timeout=2)
            return out.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            return None

    def set_text(self, text: str) -> bool:
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=_ENV, timeout=2,
                           check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def _find_switchaudio() -> str | None:
    return shutil.which("SwitchAudioSource") or next(
        (p for p in ("/opt/homebrew/bin/SwitchAudioSource", "/usr/local/bin/SwitchAudioSource")
         if os.path.exists(p)), None)


class AudioRouter:
    """Bascule la sortie/l'entrée par défaut du Mac vers BlackHole, et restaure.

    Utilise ``SwitchAudioSource`` (brew install switchaudio-osx).
    """

    def __init__(self, capture_device: str, mic_device: str):
        self.capture_device = capture_device
        self.mic_device = mic_device
        self.tool = _find_switchaudio()
        self._saved: dict[str, str] = {}
        if self.tool is None:
            log.warning("SwitchAudioSource introuvable : routage audio manuel "
                        "(brew install switchaudio-osx)")

    def _run(self, *args: str) -> str | None:
        if self.tool is None:
            return None
        try:
            out = subprocess.run([self.tool, *args], capture_output=True, text=True, timeout=3)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _route(self, kind: str, device: str) -> None:
        current = self._run("-c", "-t", kind)
        if current is None or current == device:
            return
        if kind not in self._saved:
            self._saved[kind] = current
        if self._run("-s", device, "-t", kind) is not None:
            log.info("%s du Mac -> %s", "Sortie" if kind == "output" else "Entrée", device)
            if kind == "output":
                self._run("-s", device, "-t", "system")

    def _restore(self, kind: str) -> None:
        prev = self._saved.pop(kind, None)
        if prev and self._run("-s", prev, "-t", kind) is not None:
            log.info("%s du Mac restaurée : %s", "Sortie" if kind == "output" else "Entrée", prev)
            if kind == "output":
                self._run("-s", prev, "-t", "system")

    def apply(self, sound: bool, mic: bool) -> None:
        if sound:
            self._route("output", self.capture_device)
        else:
            self._restore("output")
        if mic:
            self._route("input", self.mic_device)
        else:
            self._restore("input")

    def restore(self) -> None:
        self._restore("output")
        self._restore("input")


# --- Veille et réveil -------------------------------------------------------------

_SKIP_IFACES = ("lo", "utun", "awdl", "llw", "bridge", "gif", "stf", "anpi", "ap")


def parse_ifconfig(text: str) -> list[dict]:
    """Interfaces actives avec IPv4 : [{"iface", "mac", "ip", "broadcast"}]."""
    result, cur = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            name = line.split(":", 1)[0]
            cur = {"iface": name, "mac": "", "ip": "", "broadcast": "", "active": True}
            if not name.startswith(_SKIP_IFACES):
                result.append(cur)
            continue
        if cur is None:
            continue
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ether" and len(parts) > 1:
            cur["mac"] = parts[1].lower()
        elif parts[0] == "inet" and len(parts) > 1 and not cur["ip"]:
            cur["ip"] = parts[1]
            if "broadcast" in parts:
                cur["broadcast"] = parts[parts.index("broadcast") + 1]
        elif parts[0] == "status:":
            cur["active"] = parts[1] == "active"
    return [{k: v for k, v in i.items() if k != "active"} for i in result
            if i["mac"] and i["ip"] and not i["ip"].startswith("127.") and i["active"]]


def network_identity() -> list[dict]:
    """Adresses que le PC peut utiliser pour réveiller ce Mac (Wake-on-LAN)."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
        return parse_ifconfig(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


def wake_on_lan_enabled() -> bool | None:
    """Réglage « Réveiller pour l'accès au réseau » (pmset womp)."""
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "womp":
            return parts[1] == "1"
    return None


class SleepGuard:
    """Empêche la mise en veille du Mac (pas de l'écran) tant que le PC est connecté.

    mode "power"  : seulement sur secteur (caffeinate -s) ;
    mode "always" : aussi sur batterie (caffeinate -i) ;
    mode "never"  : rien.
    """

    def __init__(self, mode: str = "power"):
        if mode not in ("power", "always", "never"):
            raise ValueError(f"prevent_sleep inconnu : {mode!r} (power, always ou never)")
        self.mode = mode
        self._proc: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.mode == "never" or self.active:
            return
        flag = "-s" if self.mode == "power" else "-i"
        try:
            self._proc = subprocess.Popen(["caffeinate", flag, "-w", str(os.getpid())],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("Veille du Mac bloquée tant que le PC est connecté (%s)",
                     "sur secteur" if self.mode == "power" else "toujours")
        except OSError as exc:
            log.warning("caffeinate indisponible : %s", exc)

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
