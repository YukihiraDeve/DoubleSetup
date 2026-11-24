"""Injection clavier/souris sous Windows (SendInput) : Windows comme machine pilotée.

Les touches sont envoyées par scancode : c'est la disposition de Windows
(AZERTY, etc.) qui produit le caractère, exactement comme un vrai clavier.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from . import protocol as P

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
XBUTTON1, XBUTTON2 = 1, 2
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2

# Touches mieux injectées par code virtuel que par scancode
SCANCODE_TO_VK = {
    0x45: 0x90,    # Verr. Num
    0xE046: 0x13,  # Pause
    0xE022: 0xB3, 0xE019: 0xB0, 0xE010: 0xB1, 0xE024: 0xB2,  # médias
    0xE020: 0xAD, 0xE02E: 0xAE, 0xE030: 0xAF,                # volume
}

BUTTONS = {
    P.BTN_LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    P.BTN_RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    P.BTN_MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    P.BTN_X1: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    P.BTN_X2: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD


def _send(inp: INPUT) -> None:
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        log.debug("SendInput refusé (erreur %s)", ctypes.get_last_error())


def key_input(scancode: int, down: bool) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    vk = SCANCODE_TO_VK.get(scancode)
    flags = 0 if down else KEYEVENTF_KEYUP
    if vk is not None:
        inp.u.ki = KEYBDINPUT(vk, 0, flags | (KEYEVENTF_EXTENDEDKEY if scancode > 0xFF else 0),
                              0, 0)
    else:
        flags |= KEYEVENTF_SCANCODE
        if scancode & 0xFF00 == 0xE000:
            flags |= KEYEVENTF_EXTENDEDKEY
        inp.u.ki = KEYBDINPUT(0, scancode & 0xFF, flags, 0, 0)
    return inp


def mouse_input(dx=0, dy=0, data=0, flags=0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


class WinInjector:
    """Même interface que mac_inject.MacInjector."""

    def __init__(self, mouse_speed: float = 1.0, scroll_speed: float = 3.0,
                 invert_scroll: bool = False, absolute: bool = True):
        """``absolute`` : positionne le curseur exactement (aucune accélération Windows).
        Sinon, déplacement relatif : Windows applique sa vitesse et son accélération."""
        self.mouse_speed = mouse_speed
        self.absolute = absolute
        self.scroll_factor = max(0.1, scroll_speed / 3.0)  # 3 lignes = réglage Windows par défaut
        self.invert = -1 if invert_scroll else 1
        self.keys_down: set[int] = set()
        self.buttons_down: set[int] = set()
        self._fx = self._fy = 0.0

    def key(self, scancode: int, down: bool, repeat: bool = False) -> None:
        if down:
            self.keys_down.add(scancode)
        else:
            self.keys_down.discard(scancode)
        _send(key_input(scancode, down))

    def move(self, dx: int, dy: int) -> None:
        self._fx += dx * self.mouse_speed
        self._fy += dy * self.mouse_speed
        ix, iy = int(self._fx), int(self._fy)
        self._fx -= ix
        self._fy -= iy
        if not (ix or iy):
            return
        if not self.absolute:
            _send(mouse_input(ix, iy, flags=MOUSEEVENTF_MOVE))
            return
        pos = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        vx, vy = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = max(2, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(2, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        x = min(max(pos.x + ix, vx), vx + vw - 1)
        y = min(max(pos.y + iy, vy), vy + vh - 1)
        nx = round((x - vx) * 65535 / (vw - 1))
        ny = round((y - vy) * 65535 / (vh - 1))
        _send(mouse_input(nx, ny, flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                          | MOUSEEVENTF_VIRTUALDESK))

    def button(self, button: int, down: bool) -> None:
        spec = BUTTONS.get(button)
        if spec is None:
            return
        flag_down, flag_up, data = spec
        if down:
            self.buttons_down.add(button)
        else:
            self.buttons_down.discard(button)
        _send(mouse_input(data=data, flags=flag_down if down else flag_up))

    def wheel(self, dx: int, dy: int) -> None:
        if dy:
            _send(mouse_input(data=int(dy * self.scroll_factor) * self.invert,
                              flags=MOUSEEVENTF_WHEEL))
        if dx:
            _send(mouse_input(data=int(dx * self.scroll_factor), flags=MOUSEEVENTF_HWHEEL))

    def wake(self) -> None:
        kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED)
        _send(mouse_input(1, 0, flags=MOUSEEVENTF_MOVE))
        _send(mouse_input(-1, 0, flags=MOUSEEVENTF_MOVE))

    def release_all(self) -> None:
        for sc in list(self.keys_down):
            _send(key_input(sc, False))
        self.keys_down.clear()
        for b in list(self.buttons_down):
            self.button(b, False)


class WinSleepGuard:
    """Empêche la mise en veille de Windows tant que le serveur est connecté."""

    def __init__(self, mode: str = "power"):
        if mode not in ("power", "always", "never"):
            raise ValueError(f"prevent_sleep inconnu : {mode!r}")
        self.mode = mode
        self.active = False

    def start(self) -> None:
        if self.mode != "never":
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            self.active = True

    def stop(self) -> None:
        if self.active:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.active = False
