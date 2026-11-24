"""Linux : injection (uinput) et capture (evdev) du clavier et de la souris.

Fonctionne sous X11 comme sous Wayland, car on passe par le noyau.
Droits nécessaires (le script d'installation s'en charge) :
  * injection : écriture sur /dev/uinput (règle udev + groupe « input ») ;
  * capture   : lecture de /dev/input/event* (groupe « input »).
"""

from __future__ import annotations

import logging
import selectors
import threading
import time
from typing import Callable

from . import protocol as P
from .hotkeys import MOD_SC_ALL, Hotkey
from .keymap import LINUX_TO_SCANCODE, SCANCODE_TO_LINUX

log = logging.getLogger(__name__)

# linux/input-event-codes.h
EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0x00, 0x01, 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0B, 0x0C
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
KEY_A = 30

BUTTON_TO_LINUX = {P.BTN_LEFT: BTN_LEFT, P.BTN_RIGHT: BTN_RIGHT, P.BTN_MIDDLE: BTN_MIDDLE,
                   P.BTN_X1: BTN_SIDE, P.BTN_X2: BTN_EXTRA}
LINUX_TO_BUTTON = {v: k for k, v in BUTTON_TO_LINUX.items()}


# --- Injection ---------------------------------------------------------------------

class LinuxInjector:
    """Clavier + souris virtuels via uinput. Même interface que MacInjector."""

    def __init__(self, mouse_speed: float = 1.0, scroll_speed: float = 3.0,
                 invert_scroll: bool = False, device=None):
        self.mouse_speed = mouse_speed
        self.scroll_factor = max(0.1, scroll_speed / 3.0)
        self.invert = -1 if invert_scroll else 1
        self.keys_down: set[int] = set()
        self.buttons_down: set[int] = set()
        self._fx = self._fy = 0.0
        self.dev = device or self._create_device()

    @staticmethod
    def _create_device():
        from evdev import UInput, ecodes  # paquet evdev-binary

        keys = sorted(set(SCANCODE_TO_LINUX.values()) | set(BUTTON_TO_LINUX.values()))
        caps = {
            ecodes.EV_KEY: keys,
            ecodes.EV_REL: [REL_X, REL_Y, REL_WHEEL, REL_HWHEEL],
        }
        return UInput(caps, name="SetupDouble virtual input")

    def _emit(self, etype: int, code: int, value: int) -> None:
        self.dev.write(etype, code, value)

    def _sync(self) -> None:
        self.dev.syn()

    def key(self, scancode: int, down: bool, repeat: bool = False) -> None:
        code = SCANCODE_TO_LINUX.get(scancode)
        if code is None:
            return
        if down:
            self.keys_down.add(code)
        else:
            self.keys_down.discard(code)
        self._emit(EV_KEY, code, 2 if repeat else (1 if down else 0))
        self._sync()

    def move(self, dx: int, dy: int) -> None:
        self._fx += dx * self.mouse_speed
        self._fy += dy * self.mouse_speed
        ix, iy = int(self._fx), int(self._fy)
        self._fx -= ix
        self._fy -= iy
        if ix:
            self._emit(EV_REL, REL_X, ix)
        if iy:
            self._emit(EV_REL, REL_Y, iy)
        if ix or iy:
            self._sync()

    def button(self, button: int, down: bool) -> None:
        code = BUTTON_TO_LINUX.get(button)
        if code is None:
            return
        (self.buttons_down.add if down else self.buttons_down.discard)(code)
        self._emit(EV_KEY, code, 1 if down else 0)
        self._sync()

    def wheel(self, dx: int, dy: int) -> None:
        notches_y = round(dy / 120 * self.scroll_factor) * self.invert
        notches_x = round(dx / 120 * self.scroll_factor)
        if notches_y:
            self._emit(EV_REL, REL_WHEEL, notches_y)
        if notches_x:
            self._emit(EV_REL, REL_HWHEEL, notches_x)
        if notches_x or notches_y:
            self._sync()

    def wake(self) -> None:
        self.move(1, 0)
        self.move(-1, 0)

    def release_all(self) -> None:
        for code in list(self.keys_down) + list(self.buttons_down):
            self._emit(EV_KEY, code, 0)
        self.keys_down.clear()
        self.buttons_down.clear()
        self._sync()


# --- Capture -----------------------------------------------------------------------

def translate(etype: int, code: int, value: int) -> tuple | None:
    """Événement evdev -> événement SetupDouble (même format que win_input)."""
    if etype == EV_KEY:
        if code in LINUX_TO_BUTTON:
            return ("button", LINUX_TO_BUTTON[code], value != 0)
        sc = LINUX_TO_SCANCODE.get(code)
        if sc is None:
            return None
        return ("key", sc, value != 0, value == 2)
    if etype == EV_REL:
        if code == REL_X:
            return ("move", value, 0)
        if code == REL_Y:
            return ("move", 0, value)
        if code == REL_WHEEL:
            return ("wheel", 0, value * 120)
        if code == REL_HWHEEL:
            return ("wheel", value * 120, 0)
    return None


class LinuxCapture:
    """Raccourci global + capture exclusive (grab) en mode distant."""

    def __init__(self, hotkeys: list[Hotkey], on_hotkey: Callable[[str], None],
                 sink: Callable[[tuple], None], on_mode: Callable[[bool], None]):
        self.hotkeys = hotkeys
        self.on_hotkey = on_hotkey
        self.sink = sink
        self.on_mode = on_mode
        self.remote = False
        self._want: bool | None = None
        self._down: set[int] = set()      # scancodes enfoncés
        self._devices: list = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import evdev

        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            caps = dev.capabilities()
            is_kbd = KEY_A in caps.get(EV_KEY, [])
            is_mouse = REL_X in caps.get(EV_REL, [])
            if (is_kbd or is_mouse) and "SetupDouble" not in dev.name:
                self._devices.append(dev)
        if not self._devices:
            raise PermissionError("Aucun clavier/souris lisible : ajoute ton utilisateur au "
                                  "groupe « input » (sudo usermod -aG input $USER), puis "
                                  "reconnecte-toi.")
        log.info("Capture Linux : %s", ", ".join(d.name for d in self._devices))
        self._thread = threading.Thread(target=self._run, name="linux-input", daemon=True)
        self._thread.start()

    def request_mode(self, remote: bool) -> None:
        with self._lock:
            self._want = remote
        if not remote or not self._down:
            self._apply_pending()

    def stop(self) -> None:
        self._stop.set()
        self._set_grab(False)

    def _set_grab(self, grab: bool) -> None:
        for dev in self._devices:
            try:
                dev.grab() if grab else dev.ungrab()
            except OSError:
                pass

    def _apply_pending(self) -> None:
        with self._lock:
            want, self._want = self._want, None
        if want is None or want == self.remote:
            return
        # on ne « grab » qu'une fois toutes les touches relâchées : sinon le
        # système garderait Ctrl/Alt enfoncés indéfiniment
        self._set_grab(want)
        self.remote = want
        self.on_mode(want)

    def _run(self) -> None:
        sel = selectors.DefaultSelector()
        for dev in self._devices:
            sel.register(dev, selectors.EVENT_READ)
        pending_since = 0.0
        while not self._stop.is_set():
            for key, _ in sel.select(timeout=0.2):
                try:
                    events = list(key.fileobj.read())
                except OSError:
                    continue
                for ev in events:
                    self._handle(ev.type, ev.code, ev.value)
            if self._want is not None:
                pending_since = pending_since or time.monotonic()
                if not self._down or time.monotonic() - pending_since > 1.0:
                    self._apply_pending()
                    pending_since = 0.0

    def _handle(self, etype: int, code: int, value: int) -> None:
        ev = translate(etype, code, value)
        if ev is None:
            return
        if ev[0] == "key":
            sc, down = ev[1], ev[2]
            if down and not ev[3]:
                fresh = sc not in self._down
                self._down.add(sc)
                if fresh and sc not in MOD_SC_ALL:
                    for hk in self.hotkeys:
                        if hk.matches_sc(sc, self._down):
                            self.on_hotkey(hk.action)
                            return  # la touche du raccourci n'est jamais transmise
            elif not down:
                self._down.discard(sc)
        if self.remote:
            self.sink(ev)
