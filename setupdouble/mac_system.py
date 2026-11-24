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
