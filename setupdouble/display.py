"""Bascule de l'entrée de l'écran (DDC/CI) avec le clavier et la souris.

Quand le même écran est branché sur les deux machines, SetupDouble lui
demande de changer d'entrée au moment de la bascule (code VCP 0x60, norme
MCCS). Outils utilisés :
  * Windows : API intégrée (dxva2), rien à installer ;
  * macOS (Apple Silicon) : m1ddc (brew install m1ddc), sinon betterdisplaycli ;
  * Linux : ddcutil.

Certains docks/adaptateurs bloquent le DDC : c'est pourquoi les deux machines
essaient, celle qu'on quitte d'abord, celle qui arrive en renfort.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

from .dashboard import check

log = logging.getLogger(__name__)

VCP_INPUT = 0x60

# Entrées MCCS courantes (certains écrans, Samsung notamment, utilisent d'autres codes :
# le bouton « Détecter » lit le code réel).
INPUTS: dict[str, int] = {
    "vga1": 0x01, "vga2": 0x02, "dvi1": 0x03, "dvi2": 0x04,
    "dp1": 0x0F, "dp2": 0x10, "hdmi1": 0x11, "hdmi2": 0x12, "usbc": 0x1B,
}
INPUT_LABELS = {"vga1": "VGA 1", "vga2": "VGA 2", "dvi1": "DVI 1", "dvi2": "DVI 2",
                "dp1": "DisplayPort 1", "dp2": "DisplayPort 2", "hdmi1": "HDMI 1",
                "hdmi2": "HDMI 2", "usbc": "USB-C"}


def parse_input(value) -> int | None:
    """« hdmi1 », « 17 », « 0x11 » ou 17 -> 17 ; vide -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("entrée d'écran invalide")
    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip().lower().replace(" ", "").replace("-", "")
        if text in INPUTS:
            return INPUTS[text]
        try:
            code = int(text, 16) if text.startswith("0x") else int(text)
        except ValueError:
            raise ValueError(f"entrée d'écran inconnue : {value!r} (ex. hdmi1, dp1, 0x11)") from None
    if not 0 < code < 0x100:
        raise ValueError(f"entrée d'écran hors limites : {value!r}")
    return code


def input_label(code: int | None) -> str:
    if code is None:
        return "non définie"
    for name, c in INPUTS.items():
        if c == code:
            return f"{INPUT_LABELS[name]} (0x{code:02x})"
    return f"code 0x{code:02x}"


# --- Outils par système ---------------------------------------------------------------

class Switcher:
    """Interface commune. ``monitor`` : identifiant renvoyé par list_monitors()."""

    tool = ""

    def list_monitors(self) -> list[dict]:
        raise NotImplementedError

    def get_input(self, monitor: str) -> int | None:
        raise NotImplementedError

    def set_input(self, monitor: str, code: int) -> bool:
        raise NotImplementedError

    def resolve(self, monitor: str) -> str | None:
        """Identifiant configuré (ou vide = premier écran DDC) -> identifiant réel."""
        monitors = self.list_monitors()
        if not monitors:
            return None
        if not monitor:
            return monitors[0]["id"]
        for m in monitors:
            if monitor in (m["id"], m["name"]) or monitor.lower() in m["name"].lower():
                return m["id"]
        return None


def _run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


class M1ddcSwitcher(Switcher):
    tool = "m1ddc"

    def __init__(self, path: str, run=_run):
        self.path, self.run = path, run

    def list_monitors(self) -> list[dict]:
        code, out = self.run([self.path, "display", "list"])
        return parse_m1ddc_list(out) if code == 0 else []

    def get_input(self, monitor: str) -> int | None:
        code, out = self.run([self.path, "display", monitor, "get", "input"])
        m = re.search(r"^\s*(\d+)\s*$", out, re.M)
        return int(m.group(1)) if code == 0 and m else None

    def set_input(self, monitor: str, value: int) -> bool:
        code, out = self.run([self.path, "display", monitor, "set", "input", str(value)])
        if code != 0 or "failure" in out.lower():
            log.warning("m1ddc : %s", out.strip())
            return False
        return True


def parse_m1ddc_list(text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        m = re.match(r"\s*\[(\d+)\]\s+(.*?)\s*(?:\(([0-9A-Fa-f-]{36})\))?\s*$", line)
        if m:
            result.append({"id": m.group(1), "name": m.group(2) or f"Écran {m.group(1)}"})
    return result


class BetterDisplaySwitcher(Switcher):
    tool = "betterdisplaycli"

    def __init__(self, path: str, run=_run):
        self.path, self.run = path, run

    def list_monitors(self) -> list[dict]:
        code, out = self.run([self.path, "get", "-identifiers"])
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', out) if code == 0 else []
        return [{"id": n, "name": n} for n in dict.fromkeys(names)]

    def get_input(self, monitor: str) -> int | None:
        code, out = self.run([self.path, "get", f"-name={monitor}", "-ddc", "-vcp=inputSelect"])
        m = re.search(r"(\d+)", out)
        return int(m.group(1)) if code == 0 and m else None

    def set_input(self, monitor: str, value: int) -> bool:
        code, out = self.run([self.path, "set", f"-name={monitor}", "-ddc", "-vcp=inputSelect",
                              f"-value={value}"])
        return code == 0 and "fail" not in out.lower()


class DdcutilSwitcher(Switcher):
    tool = "ddcutil"

    def __init__(self, path: str, run=_run):
        self.path, self.run = path, run
        self._cache: tuple[float, list[dict]] = (0.0, [])

    def list_monitors(self) -> list[dict]:
        stamp, cached = self._cache
        if time.monotonic() - stamp < 60:
            return cached
        code, out = self.run([self.path, "detect", "--terse"], timeout=20)
        monitors = parse_ddcutil_detect(out) if code == 0 else []
        self._cache = (time.monotonic(), monitors)
        return monitors

    def get_input(self, monitor: str) -> int | None:
        code, out = self.run([self.path, "--display", monitor, "getvcp", "60", "--brief"])
        m = re.search(r"VCP 60 \w+ x([0-9a-fA-F]+)", out)
        return int(m.group(1), 16) if code == 0 and m else None

    def set_input(self, monitor: str, value: int) -> bool:
        code, out = self.run([self.path, "--display", monitor, "setvcp", "60", f"0x{value:02x}"])
        if code != 0:
            log.warning("ddcutil : %s", out.strip())
        return code == 0


def parse_ddcutil_detect(text: str) -> list[dict]:
    result, current = [], None
    for line in text.splitlines():
        m = re.match(r"^Display (\d+)", line)
        if m:
            current = {"id": m.group(1), "name": f"Écran {m.group(1)}"}
            result.append(current)
            continue
        m = re.match(r"^\s+Monitor:\s+(.+)$", line)
        if m and current is not None:
            parts = m.group(1).split(":")
            current["name"] = parts[1] if len(parts) > 1 and parts[1] else m.group(1)
    return result


class WindowsSwitcher(Switcher):
    """DDC/CI par l'API Windows (dxva2), sans outil externe."""

    tool = "Windows (dxva2)"

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

        class PHYSICAL_MONITOR(ctypes.Structure):
            _fields_ = [("handle", wintypes.HANDLE), ("description", wintypes.WCHAR * 128)]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]

        self.PHYSICAL_MONITOR, self.MONITORINFOEXW = PHYSICAL_MONITOR, MONITORINFOEXW
        self.MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        P, D = ctypes.POINTER(PHYSICAL_MONITOR), ctypes.POINTER(wintypes.DWORD)
        u, d = self.user32, self.dxva2
        u.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, self.MONITORENUMPROC,
                                          wintypes.LPARAM]
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        d.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, D]
        d.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, P]
        d.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, P]
        d.GetVCPFeatureAndVCPFeatureReply.argtypes = [wintypes.HANDLE, wintypes.BYTE,
                                                      ctypes.c_void_p, D, D]
        d.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]

    def _each(self, fn: Callable) -> list:
        """Appelle fn(id, nom, handle) pour chaque écran physique, puis libère les handles."""
        c, w = self.ctypes, self.wintypes
        hmons = []
        cb = self.MONITORENUMPROC(lambda h, dc, rc, lp: hmons.append(h) or True)
        self.user32.EnumDisplayMonitors(None, None, cb, 0)
        results = []
        for hmon in hmons:
            info = self.MONITORINFOEXW()
            info.cbSize = c.sizeof(info)
            self.user32.GetMonitorInfoW(hmon, c.byref(info))
            r = info.rcMonitor
            count = w.DWORD()
            if not self.dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, c.byref(count)):
                continue
            arr = (self.PHYSICAL_MONITOR * count.value)()
            if not self.dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count, arr):
                continue
            try:
                for i, pm in enumerate(arr):
                    ident = f"{info.szDevice}#{i}"
                    label = pm.description or "Écran"
                    name = f"{label} — {r.right - r.left}×{r.bottom - r.top} ({info.szDevice[4:]})"
                    results.append(fn(ident, name, pm.handle))
            finally:
                self.dxva2.DestroyPhysicalMonitors(count, arr)
        return results

    def list_monitors(self) -> list[dict]:
        return self._each(lambda ident, name, h: {"id": ident, "name": name})

    def get_input(self, monitor: str) -> int | None:
        c, w = self.ctypes, self.wintypes

        def read(ident, name, h):
            if ident != monitor:
                return None
            cur, mx = w.DWORD(), w.DWORD()
            ok = self.dxva2.GetVCPFeatureAndVCPFeatureReply(h, VCP_INPUT, None, c.byref(cur),
                                                            c.byref(mx))
            return cur.value & 0xFF if ok else None
        return next((v for v in self._each(read) if v is not None), None)

    def set_input(self, monitor: str, value: int) -> bool:
        def write(ident, name, h):
            return ident == monitor and bool(self.dxva2.SetVCPFeature(h, VCP_INPUT, value))
        return any(self._each(write))


def make_switcher() -> Switcher | None:
    if sys.platform == "win32":
        try:
            return WindowsSwitcher()
        except OSError:
            return None
    if sys.platform == "darwin":
        for name, cls in (("m1ddc", M1ddcSwitcher), ("betterdisplaycli", BetterDisplaySwitcher)):
            path = shutil.which(name) or next(
                (p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}")
                 if os.path.exists(p)), None)
            if path:
                return cls(path)
        return None
    path = shutil.which("ddcutil")
    return DdcutilSwitcher(path) if path else None


def install_hint() -> str:
    if sys.platform == "darwin":
        return "brew install m1ddc"
    if sys.platform.startswith("linux"):
        return "installe ddcutil (relance install.sh)"
    return ""


# --- Lien avec la bascule clavier/souris --------------------------------------------------

class DisplayLink:
    """Envoie le changement d'entrée au bon moment, sans jamais bloquer la bascule."""

    FALLBACK_DELAY = 1.2  # la machine qui arrive renvoie la commande en renfort

    def __init__(self, dcfg: dict, switcher: Switcher | None = None, auto: bool = True):
        self.cfg = dcfg
        if switcher is None and auto and dcfg.get("enabled"):
            switcher = make_switcher()
        self.switcher = switcher
        self._generation = 0
        self._lock = threading.Lock()
        self.last_result = ""

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def my_input(self) -> int | None:
        try:
            return parse_input(self.cfg.get("input"))
        except ValueError:
            return None

    def info(self) -> dict:
        """Ce que l'autre machine doit savoir (dans le message INFO)."""
        return {"enabled": self.enabled, "input": self.my_input}

    def switch(self, code: int | None, delay: float = 0.0,
               still_valid: Callable[[], bool] = lambda: True) -> None:
        """Demande l'entrée ``code`` (en arrière-plan). Annulée si une autre bascule suit."""
        if not self.enabled or self.switcher is None or code is None:
            return
        with self._lock:
            self._generation += 1
            gen = self._generation

        def work():
            if delay:
                time.sleep(delay)
            if gen != self._generation or not still_valid():
                return
            monitor = self.switcher.resolve(str(self.cfg.get("monitor") or ""))
            if monitor is None:
                self.last_result = "écran introuvable"
                log.warning("Bascule d'écran : écran introuvable")
                return
            ok = self.switcher.set_input(monitor, code)
            self.last_result = f"{'ok' if ok else 'échec'} vers {input_label(code)}"
            log.info("Bascule d'écran vers %s : %s", input_label(code), "ok" if ok else "échec")

        threading.Thread(target=work, name="display", daemon=True).start()

    def monitors(self) -> dict:
        """Pour l'onglet Écran : écrans détectés et entrée actuelle de chacun."""
        sw = self.switcher or make_switcher()
        if sw is None:
            return {"supported": False, "tool": "", "hint": install_hint(), "monitors": []}
        monitors = []
        for m in sw.list_monitors():
            current = sw.get_input(m["id"])
            monitors.append(dict(m, input=current, input_label=input_label(current)
                                 if current is not None else "illisible"))
        return {"supported": True, "tool": sw.tool, "hint": "", "monitors": monitors}

    def check(self) -> dict:
        if not self.enabled:
            return check("display", "Bascule d'écran", "off", "Désactivée (Réglages → Écran)")
        if self.switcher is None:
            hint = install_hint()
            return check("display", "Bascule d'écran", "error",
                         "Outil DDC absent" + (f" : {hint}" if hint else ""))
        if self.my_input is None:
            return check("display", "Bascule d'écran", "warn",
                         "Entrée de cet ordinateur à définir (Réglages → Écran → Détecter)")
        detail = f"Entrée {input_label(self.my_input)}"
        if self.last_result:
            detail += f" · dernier essai : {self.last_result}"
        return check("display", "Bascule d'écran", "ok", detail)

    def test_round_trip(self, other: int | None, back_after: float = 5.0) -> tuple[bool, str]:
        """Affiche l'autre entrée puis revient : vérifie que l'écran accepte la commande
        même quand il affiche l'autre machine."""
        if self.switcher is None:
            return False, "Outil DDC absent."
        if other is None or self.my_input is None:
            return False, "Définis l'entrée des deux machines d'abord."
        monitor = self.switcher.resolve(str(self.cfg.get("monitor") or ""))
        if monitor is None:
            return False, "Écran introuvable."
        if not self.switcher.set_input(monitor, other):
            return False, "L'écran refuse la commande depuis cet ordinateur."

        def back():
            time.sleep(back_after)
            ok = self.switcher.set_input(monitor, self.my_input)
            log.info("Test d'écran, retour : %s", "ok" if ok else "échec")
        threading.Thread(target=back, daemon=True).start()
        return True, (f"Bascule envoyée. Retour automatique dans {back_after:.0f} s : s'il ne "
                      "revient pas, ton écran n'accepte la commande que depuis l'entrée affichée.")
