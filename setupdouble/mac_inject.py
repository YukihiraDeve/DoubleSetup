"""Injection d'événements clavier/souris sur macOS (Quartz / CGEvent).

Nécessite l'autorisation « Accessibilité » pour l'exécutable Python.
"""

from __future__ import annotations

import ctypes
import logging
import math
import time

import Quartz as Q

from . import keymap as K
from . import protocol as P

log = logging.getLogger(__name__)

_BUTTONS = {
    P.BTN_LEFT: (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventLeftMouseDragged, 0),
    P.BTN_RIGHT: (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGEventRightMouseDragged, 1),
    P.BTN_MIDDLE: (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 2),
    P.BTN_X1: (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 3),
    P.BTN_X2: (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp, Q.kCGEventOtherMouseDragged, 4),
}


def accessibility_granted() -> bool:
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return True  # on ne bloque pas si la vérification échoue


def _double_click_interval() -> float:
    try:
        from AppKit import NSEvent
        return float(NSEvent.doubleClickInterval())
    except Exception:
        return 0.5


class MacInjector:
    def __init__(self, mapper: K.KeyMapper, mouse_speed: float = 1.0,
                 scroll_speed: float = 3.0, invert_scroll: bool = False):
        self.mapper = mapper
        self.mouse_speed = mouse_speed
        self.scroll_speed = scroll_speed
        self.invert_scroll = invert_scroll
        self.src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
        self.mods: set[int] = set()
        self.keys_down: set[int] = set()
        self.buttons: set[int] = set()
        self.caps = False
        self._frac = [0.0, 0.0]
        self._scroll = [0.0, 0.0]
        self._dbl = _double_click_interval()
        self._last_click = (0, 0.0, (0.0, 0.0))
        self._click_count = 1
        self._displays: list[tuple[float, float, float, float]] = []
        self._displays_at = 0.0

    # --- utilitaires -------------------------------------------------------------

    def _post(self, ev) -> None:
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def _flags(self) -> int:
        return K.flags_for(self.mods, self.caps)

    def _location(self) -> tuple[float, float]:
        loc = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return loc.x, loc.y

    def _display_rects(self):
        now = time.monotonic()
        if now - self._displays_at > 2.0 or not self._displays:
            err, ids, count = Q.CGGetActiveDisplayList(16, None, None)
            rects = []
            for did in (ids or [])[:count]:
                b = Q.CGDisplayBounds(did)
                rects.append((b.origin.x, b.origin.y, b.size.width, b.size.height))
            self._displays = rects or [(0, 0, 1440, 900)]
            self._displays_at = now
        return self._displays

    def _clamp(self, x: float, y: float, cur: tuple[float, float]) -> tuple[float, float]:
        rects = self._display_rects()
        for rx, ry, rw, rh in rects:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return x, y
        home = next(((rx, ry, rw, rh) for rx, ry, rw, rh in rects
                     if rx <= cur[0] < rx + rw and ry <= cur[1] < ry + rh), rects[0])
        rx, ry, rw, rh = home
        return min(max(x, rx), rx + rw - 1), min(max(y, ry), ry + rh - 1)

    # --- clavier --------------------------------------------------------------------

    def key(self, scancode: int, down: bool, repeat: bool) -> None:
        kc = self.mapper.to_mac(scancode)
        if kc is None:
            log.debug("scancode non géré : %#x", scancode)
            return
        if kc >= K.MEDIA_BASE:
            self._media(kc - K.MEDIA_BASE, down)
            return
        if kc == K.MAC_CAPS:
            if down and not repeat:
                self.caps = not self.caps
                ev = Q.CGEventCreateKeyboardEvent(self.src, kc, True)
                Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
                Q.CGEventSetFlags(ev, self._flags())
                self._post(ev)
            return
        if K.is_modifier(kc):
            if repeat:
                return
            if down:
                self.mods.add(kc)
            else:
                self.mods.discard(kc)
            ev = Q.CGEventCreateKeyboardEvent(self.src, kc, down)
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
            Q.CGEventSetFlags(ev, self._flags())
            self._post(ev)
            return
        ev = Q.CGEventCreateKeyboardEvent(self.src, kc, down)
        flags = self._flags()
        if kc in K.ARROW_KEYS:
            flags |= K.FLAG_NUMPAD | K.FLAG_FN
        elif kc in K.NAV_KEYS:
            flags |= K.FLAG_FN
        elif kc in K.NUMPAD_KEYS:
            flags |= K.FLAG_NUMPAD
        Q.CGEventSetFlags(ev, flags)
        if repeat:
            Q.CGEventSetIntegerValueField(ev, Q.kCGKeyboardEventAutorepeat, 1)
        if down:
            self.keys_down.add(kc)
        else:
            self.keys_down.discard(kc)
        self._post(ev)

    def _media(self, nx_type: int, down: bool) -> None:
        from AppKit import NSEvent

        data1 = (nx_type << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), 0xA00 if down else 0xB00, 0, 0, None, 8, data1, -1)
        self._post(ev.CGEvent())

    # --- souris -----------------------------------------------------------------------

    def move(self, dx: int, dy: int) -> None:
        rx, ry = dx * self.mouse_speed, dy * self.mouse_speed
        # deltas entiers (lus par les jeux / apps 3D) avec report des fractions
        fx, fy = rx + self._frac[0], ry + self._frac[1]
        ix, iy = math.trunc(fx), math.trunc(fy)
        self._frac = [fx - ix, fy - iy]
        cur = self._location()
        x, y = self._clamp(cur[0] + rx, cur[1] + ry, cur)
        ev_type, btn = Q.kCGEventMouseMoved, 0
        for b in (P.BTN_LEFT, P.BTN_RIGHT, P.BTN_MIDDLE, P.BTN_X1, P.BTN_X2):
            if b in self.buttons:
                ev_type, btn = _BUTTONS[b][2], _BUTTONS[b][3]
                break
        ev = Q.CGEventCreateMouseEvent(self.src, ev_type, (x, y), btn)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaX, ix)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventDeltaY, iy)
        Q.CGEventSetFlags(ev, self._flags())
        self._post(ev)

    def button(self, button: int, down: bool) -> None:
        if button not in _BUTTONS:
            return
        down_t, up_t, _, num = _BUTTONS[button]
        pos = self._location()
        if down:
            last_b, last_t, last_pos = self._last_click
            now = time.monotonic()
            if (last_b == button and now - last_t < self._dbl
                    and abs(pos[0] - last_pos[0]) < 5 and abs(pos[1] - last_pos[1]) < 5):
                self._click_count += 1
            else:
                self._click_count = 1
            self._last_click = (button, now, pos)
            self.buttons.add(button)
        else:
            self.buttons.discard(button)
        ev = Q.CGEventCreateMouseEvent(self.src, down_t if down else up_t, pos, num)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventButtonNumber, num)
        Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, self._click_count)
        Q.CGEventSetFlags(ev, self._flags())
        self._post(ev)

    def wheel(self, dx: int, dy: int) -> None:
        sign = -1.0 if self.invert_scroll else 1.0
        self._scroll[0] += -dx / 120.0 * self.scroll_speed * sign
        self._scroll[1] += dy / 120.0 * self.scroll_speed * sign
        iy, ix = math.trunc(self._scroll[1]), math.trunc(self._scroll[0])
        if not ix and not iy:
            return
        self._scroll[0] -= ix
        self._scroll[1] -= iy
        ev = Q.CGEventCreateScrollWheelEvent(self.src, Q.kCGScrollEventUnitLine, 2, iy, ix)
        Q.CGEventSetFlags(ev, self._flags())
        self._post(ev)

    # --- état ---------------------------------------------------------------------------

    def wake(self) -> None:
        """Réveille l'écran (équivalent d'une activité utilisateur)."""
        import subprocess
        try:
            subprocess.Popen(["caffeinate", "-u", "-t", "2"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def release_all(self) -> None:
        for kc in list(self.keys_down):
            ev = Q.CGEventCreateKeyboardEvent(self.src, kc, False)
            Q.CGEventSetFlags(ev, 0x100)
            self._post(ev)
        self.keys_down.clear()
        for b in list(self.buttons):
            self.button(b, False)
        if self.mods:
            for kc in list(self.mods):
                self.mods.discard(kc)
                ev = Q.CGEventCreateKeyboardEvent(self.src, kc, False)
                Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
                Q.CGEventSetFlags(ev, self._flags())
                self._post(ev)
        self._scroll = [0.0, 0.0]
