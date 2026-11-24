"""Sensibilité de la souris sur la machine pilotée.

Le serveur envoie le déplacement tel qu'il le voit (après ses propres réglages)
et décrit ces réglages (vitesse, accélération, taille d'écran). La machine
pilotée choisit ensuite comment le reproduire :

  * "source" : exactement comme sur la machine source (ex. Windows sans accélération) ;
  * "raw"    : brut, sans aucune accélération ni vitesse système (1 point = 1 pixel) ;
  * "local"  : comme les réglages de CETTE machine (accélération macOS reproduite,
               ballistique Windows, profil libinput sous Linux).

Puis un multiplicateur (« vitesse ») et, si demandé, une adaptation à la taille
des écrans (traverser l'écran demande le même geste des deux côtés).
"""

from __future__ import annotations

import math
import subprocess
import sys
import time

PROFILES = ("source", "raw", "local")

# Vitesse du pointeur Windows (1..20) -> multiplicateur appliqué sans accélération
WINDOWS_SPEED = {1: 1 / 32, 2: 1 / 16, 3: 1 / 8, 4: 2 / 8, 5: 3 / 8, 6: 4 / 8, 7: 5 / 8,
                 8: 6 / 8, 9: 7 / 8, 10: 1.0, 11: 1.25, 12: 1.5, 13: 1.75, 14: 2.0,
                 15: 2.25, 16: 2.5, 17: 2.75, 18: 3.0, 19: 3.25, 20: 3.5}


# --- Réglages du système local ---------------------------------------------------------

def windows_pointer_info() -> dict:
    import ctypes
    user32 = ctypes.windll.user32
    speed = ctypes.c_int(10)
    params = (ctypes.c_int * 3)()
    user32.SystemParametersInfoW(0x0070, 0, ctypes.byref(speed), 0)   # SPI_GETMOUSESPEED
    user32.SystemParametersInfoW(0x0003, 0, params, 0)                # SPI_GETMOUSE
    return {"os": "windows", "level": speed.value,
            "speed": WINDOWS_SPEED.get(speed.value, 1.0), "accel": bool(params[2]),
            "screen": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]}


def _defaults(key: str) -> str | None:
    try:
        out = subprocess.run(["defaults", "read", "-g", key], capture_output=True, text=True,
                             timeout=2)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def mac_pointer_info() -> dict:
    try:
        scaling = float(_defaults("com.apple.mouse.scaling") or 0.6875)
    except ValueError:
        scaling = 0.6875
    linear = (_defaults("com.apple.mouse.linear") or "0") in ("1", "true", "YES")
    screen = None
    try:
        import Quartz as Q
        b = Q.CGDisplayBounds(Q.CGMainDisplayID())
        screen = [int(b.size.width), int(b.size.height)]
    except Exception:
        pass
    # les deltas capturés sur Mac sont déjà passés par sa courbe : vitesse « 1 » côté envoi
    return {"os": "mac", "scaling": scaling, "accel": not linear, "speed": 1.0, "screen": screen}


def local_pointer_info() -> dict:
    if sys.platform == "win32":
        try:
            return windows_pointer_info()
        except Exception:
            return {"os": "windows", "speed": 1.0, "accel": False, "screen": None}
    if sys.platform == "darwin":
        return mac_pointer_info()
    return {"os": "linux", "speed": 1.0, "accel": False, "screen": None}  # evdev : comptes bruts


def describe(info: dict | None) -> str:
    if not info:
        return "inconnue"
    os_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}.get(info.get("os"), "")
    parts = []
    if info.get("os") == "windows" and info.get("level"):
        parts.append(f"vitesse {info['level']}/20 (×{info.get('speed', 1):.2f})")
    if info.get("os") == "mac" and info.get("scaling") is not None:
        parts.append(f"vitesse de suivi {info['scaling']:g}")
    parts.append("accélération activée" if info.get("accel") else "sans accélération")
    return f"{os_name} : " + ", ".join(parts)


# --- Courbe d'accélération façon macOS ---------------------------------------------------

class MacCurve:
    """Approximation de l'accélération de macOS, selon la vitesse de suivi (0 à 3).

    Lente : gain < 1 (précision) ; rapide : gain qui monte jusqu'à un plafond.
    Avec l'accélération désactivée (« linear »), gain constant.
    """

    def __init__(self, scaling: float = 0.6875, linear: bool = False):
        self.scaling = max(0.0, min(3.0, scaling))
        self.linear = linear

    def gain(self, counts_per_ms: float) -> float:
        s = self.scaling
        if self.linear:
            return 0.4 + 0.7 * s
        base = 0.35 + 0.4 * s
        top = 1.4 + 1.6 * s
        return base + (top - base) * (1 - math.exp(-counts_per_ms / (3.0 + 2.0 * s)))


# --- Mise en forme des déplacements ------------------------------------------------------------

class PointerShaper:
    def __init__(self, profile: str = "source", speed: float = 1.0, match_screen: bool = False,
                 local: dict | None = None, clock=time.monotonic):
        if profile not in PROFILES:
            raise ValueError(f"mouse_profile inconnu : {profile!r} ({', '.join(PROFILES)})")
        self.profile = profile
        self.speed = speed
        self.match_screen = match_screen
        self.local = local or {}
        self.source: dict = {}
        self.clock = clock
        self._last = 0.0
        self._frac = [0.0, 0.0]
        self.curve = None
        if profile == "local" and self.local.get("os") == "mac":
            self.curve = MacCurve(self.local.get("scaling", 0.6875), not self.local.get("accel"))

    def configure(self, profile: str, speed: float, match_screen: bool) -> None:
        """Changement à chaud (réglages envoyés par le serveur)."""
        if profile not in PROFILES:
            raise ValueError(f"mouse_profile inconnu : {profile!r}")
        self.profile, self.speed, self.match_screen = profile, float(speed), bool(match_screen)
        self.curve = None
        if profile == "local" and self.local.get("os") == "mac":
            self.curve = MacCurve(self.local.get("scaling", 0.6875), not self.local.get("accel"))
        self._frac = [0.0, 0.0]

    def set_source(self, info: dict | None) -> None:
        self.source = info or {}

    def _screen_ratio(self) -> float:
        src, loc = self.source.get("screen"), self.local.get("screen")
        if not (self.match_screen and src and loc and src[0] > 0):
            return 1.0
        return loc[0] / src[0]

    def factor_for(self, counts_per_ms: float) -> float:
        f = self.speed * self._screen_ratio()
        if self.profile == "source":
            return f
        src_speed = float(self.source.get("speed") or 1.0)
        raw = f / src_speed if src_speed > 0 else f   # retire la vitesse de la source
        if self.profile == "raw" or self.curve is None:
            return raw
        return raw * self.curve.gain(counts_per_ms / max(src_speed, 1e-6))

    def shape(self, dx: int, dy: int) -> tuple[int, int]:
        now = self.clock()
        dt_ms = min(max((now - self._last) * 1000.0, 1.0), 50.0) if self._last else 8.0
        self._last = now
        speed = math.hypot(dx, dy) / dt_ms
        f = self.factor_for(speed)
        fx, fy = dx * f + self._frac[0], dy * f + self._frac[1]
        ix, iy = math.trunc(fx), math.trunc(fy)
        self._frac = [fx - ix, fy - iy]
        return ix, iy

    @property
    def wants_system_ballistics(self) -> bool:
        """Vrai si l'injecteur doit laisser le système appliquer sa propre accélération."""
        return self.profile == "local" and self.local.get("os") in ("windows", "linux")

    def summary(self) -> tuple[str, str]:
        """(état, détail) pour la fenêtre de statut."""
        names = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}
        src = names.get(self.source.get("os", ""), "la source")
        here = names.get(self.local.get("os", ""), "cette machine")
        text = {"source": f"Comme sur {src}", "raw": "Brute, sans accélération",
                "local": f"Comme sur {here}"}[self.profile]
        text += f" · ×{self.speed:g}"
        if self.match_screen:
            text += " · adaptée à l'écran"
        state = "ok"
        if self.profile == "raw" and self.source.get("accel"):
            state = "warn"
            text += f" (accélération active sur {src} : approximation)"
        return state, text
