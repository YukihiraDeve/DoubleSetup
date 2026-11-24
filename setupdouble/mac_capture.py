"""macOS comme machine source : capture du clavier et de la souris (CGEventTap).

  * Mode local : le « tap » ne fait que repérer le raccourci et laisse tout passer.
  * Mode distant : il avale clavier et souris, les traduit en scancodes PC et
    les envoie ; le curseur est figé (CGAssociateMouseAndMouseCursorPosition).

Nécessite l'autorisation Accessibilité (et Surveillance de l'entrée) pour Python.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import protocol as P
from .hotkeys import MOD_SC_ALL, Hotkey
from .keymap import MacToScancode

log = logging.getLogger(__name__)

# CGEventType
K_KEY_DOWN, K_KEY_UP, K_FLAGS = 10, 11, 12
K_LDOWN, K_LUP, K_RDOWN, K_RUP = 1, 2, 3, 4
K_MOVED, K_LDRAG, K_RDRAG = 5, 6, 7
K_SCROLL, K_ODOWN, K_OUP, K_ODRAG = 22, 25, 26, 27
K_TAP_DISABLED_TIMEOUT, K_TAP_DISABLED_USER = 0xFFFFFFFE, 0xFFFFFFFF
# CGEventField
F_KEYCODE, F_AUTOREPEAT = 9, 8
F_DELTA_X, F_DELTA_Y, F_BUTTON = 4, 5, 3
F_SCROLL_Y, F_SCROLL_X = 11, 12

# keycode Mac des modificateurs -> masque « côté » (IOLLEvent.h)
MOD_SIDE_BITS = {0x3B: 0x0001, 0x38: 0x0002, 0x3C: 0x0004, 0x37: 0x0008, 0x36: 0x0010,
                 0x3A: 0x0020, 0x3D: 0x0040, 0x3E: 0x2000}
CAPS_KEYCODE, FLAG_CAPS = 0x39, 0x00010000
OTHER_BUTTONS = {2: P.BTN_MIDDLE, 3: P.BTN_X1, 4: P.BTN_X2}


def translate(etype: int, keycode: int, flags: int, repeat: bool, button: int,
              dx: int, dy: int, scroll_x: int, scroll_y: int,
              mapper: MacToScancode) -> tuple | None:
    """Événement Quartz -> événement SetupDouble (même format que win_input)."""
    if etype in (K_KEY_DOWN, K_KEY_UP):
        sc = mapper.to_scancode(keycode)
        return None if sc is None else ("key", sc, etype == K_KEY_DOWN, repeat)
    if etype == K_FLAGS:
        sc = mapper.to_scancode(keycode)
        if sc is None:
            return None
        if keycode == CAPS_KEYCODE:
            return ("key", sc, True, False)  # Verr. Maj : un appui = un basculement
        bit = MOD_SIDE_BITS.get(keycode)
        return None if bit is None else ("key", sc, bool(flags & bit), False)
    if etype in (K_MOVED, K_LDRAG, K_RDRAG, K_ODRAG):
        return ("move", dx, dy) if (dx or dy) else None
    if etype in (K_LDOWN, K_LUP):
        return ("button", P.BTN_LEFT, etype == K_LDOWN)
    if etype in (K_RDOWN, K_RUP):
        return ("button", P.BTN_RIGHT, etype == K_RDOWN)
    if etype in (K_ODOWN, K_OUP):
        b = OTHER_BUTTONS.get(button)
        return None if b is None else ("button", b, etype == K_ODOWN)
    if etype == K_SCROLL:
        return ("wheel", scroll_x * 120, scroll_y * 120) if (scroll_x or scroll_y) else None
    return None


class MacCapture:
    def __init__(self, hotkeys: list[Hotkey], on_hotkey: Callable[[str], None],
                 sink: Callable[[tuple], None], on_mode: Callable[[bool], None],
                 modifier_layout: str = "pc"):
        self.hotkeys = hotkeys
        self.on_hotkey = on_hotkey
        self.sink = sink
        self.on_mode = on_mode
        self.mapper = MacToScancode(modifier_layout)
        self.remote = False
        self._down: set[int] = set()
        self._held_at_switch: set[int] = set()
        self._tap = None
        self._loop = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="mac-input", daemon=True).start()
        self._ready.wait(5)
        if self._error:
            raise self._error

    def request_mode(self, remote: bool) -> None:
        import Quartz as Q

        if remote == self.remote:
            return
        if remote:
            self._held_at_switch = set(self._down)
        self.remote = remote
        Q.CGAssociateMouseAndMouseCursorPosition(not remote)
        self.on_mode(remote)

    def stop(self) -> None:
        import Quartz as Q

        if self.remote:
            self.request_mode(False)
        if self._loop is not None:
            Q.CFRunLoopStop(self._loop)

    def _run(self) -> None:
        import Quartz as Q

        mask = 0
        for t in (K_KEY_DOWN, K_KEY_UP, K_FLAGS, K_LDOWN, K_LUP, K_RDOWN, K_RUP, K_MOVED,
                  K_LDRAG, K_RDRAG, K_SCROLL, K_ODOWN, K_OUP, K_ODRAG):
            mask |= 1 << t
        self._tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                       Q.kCGEventTapOptionDefault, mask, self._callback, None)
        if self._tap is None:
            self._error = PermissionError(
                "Capture impossible : autorise Python dans Réglages Système → Confidentialité "
                "et sécurité → Accessibilité (et Surveillance de l'entrée).")
            self._ready.set()
            return
        source = Q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Q.CFRunLoopGetCurrent()
        Q.CFRunLoopAddSource(self._loop, source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self._tap, True)
        self._ready.set()
        Q.CFRunLoopRun()

    def _callback(self, proxy, etype, event, refcon):
        import Quartz as Q

        if etype in (K_TAP_DISABLED_TIMEOUT, K_TAP_DISABLED_USER):
            Q.CGEventTapEnable(self._tap, True)  # macOS coupe un tap trop lent
            return event
        try:
            ev = translate(
                etype, Q.CGEventGetIntegerValueField(event, F_KEYCODE),
                Q.CGEventGetFlags(event),
                bool(Q.CGEventGetIntegerValueField(event, F_AUTOREPEAT)),
                Q.CGEventGetIntegerValueField(event, F_BUTTON),
                Q.CGEventGetIntegerValueField(event, F_DELTA_X),
                Q.CGEventGetIntegerValueField(event, F_DELTA_Y),
                Q.CGEventGetIntegerValueField(event, F_SCROLL_X),
                Q.CGEventGetIntegerValueField(event, F_SCROLL_Y),
                self.mapper)
        except Exception:
            log.exception("traduction d'événement")
            return event
        if ev is None:
            return None if self.remote else event
        if ev[0] == "key":
            sc, down, repeat = ev[1], ev[2], ev[3]
            if down and not repeat:
                fresh = sc not in self._down
                self._down.add(sc)
                if fresh and sc not in MOD_SC_ALL:
                    for hk in self.hotkeys:
                        if hk.matches_sc(sc, self._down):
                            self.on_hotkey(hk.action)
                            return None  # la touche du raccourci n'est jamais transmise
            elif not down:
                self._down.discard(sc)
                if sc in self._held_at_switch:
                    # relâchement d'une touche enfoncée avant la bascule : pour macOS
                    self._held_at_switch.discard(sc)
                    return event
        if not self.remote:
            return event
        self.sink(ev)
        return None
