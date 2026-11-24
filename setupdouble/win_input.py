"""Capture clavier/souris sous Windows (ctypes, sans dépendance).

Principe, pensé pour ne pas gêner le jeu :
  * Mode PC (local) : AUCUN hook n'est installé. Seul ``RegisterHotKey``
    écoute le raccourci : zéro coût par événement, aucune latence ajoutée.
  * Mode Mac (distant) : des hooks bas niveau (WH_KEYBOARD_LL / WH_MOUSE_LL)
    interceptent tout, le bloquent pour Windows et l'envoient au Mac.
    Le curseur Windows est « garé » au centre de l'écran principal ; le
    déplacement relatif est calculé par rapport à ce point.

Tout le travail Win32 se fait dans un thread dédié qui possède la boucle
de messages (obligatoire pour les hooks bas niveau).
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

from . import protocol
from .hotkeys import (MOD_FLAGS, MOD_NOREPEAT, VK_LCONTROL, VK_LSHIFT, VK_RSHIFT,
                      Hotkey)

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
HC_ACTION = 0
WM_QUIT, WM_HOTKEY, WM_APP = 0x0012, 0x0312, 0x8000
WM_APP_MODE = WM_APP + 1
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_MOUSEMOVE = 0x200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x201, 0x202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x204, 0x205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x207, 0x208
WM_MOUSEWHEEL, WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEHWHEEL = 0x20A, 0x20B, 0x20C, 0x20E
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
LLMHF_INJECTED = 0x01

VK_NUMLOCK, VK_PAUSE = 0x90, 0x13
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
ALTGR_FAKE_CTRL_SCAN = 0x21D  # Ctrl fantôme envoyé par Windows avec AltGr

def scancode_of(kb: KBDLLHOOKSTRUCT) -> int:
    vk = kb.vkCode
    if vk == VK_NUMLOCK:
        return 0x45
    if vk == VK_PAUSE:
        return 0xE046
    if vk == VK_RSHIFT:
        return 0x36
    if vk == VK_LSHIFT:
        return 0x2A
    code = kb.scanCode & 0xFF
    if kb.flags & LLKHF_EXTENDED:
        code |= 0xE000
    return code


Sink = Callable[[tuple], None]


class InputCapture:
    """Gère le raccourci global et la capture en mode distant."""

    def __init__(self, hotkeys: list[Hotkey], on_hotkey: Callable[[str], None],
                 sink: Sink, on_mode: Callable[[bool], None]):
        self.hotkeys = hotkeys
        self.on_hotkey = on_hotkey
        self.sink = sink
        self.on_mode = on_mode
        self.remote = False
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._kb_hook = None
        self._ms_hook = None
        self._fallback_kb_hook = None
        self._kb_proc = HOOKPROC(self._guard(self._kb_callback))
        self._ms_proc = HOOKPROC(self._guard(self._ms_callback))
        self._fallback_proc = HOOKPROC(self._guard(self._fallback_callback))
        self._registered: list[int] = []
        self._down: set[int] = set()        # vk physiquement enfoncés (mode distant)
        self._held_keys: set[int] = set()   # enfoncés au moment de basculer
        self._held_buttons: set[int] = set()
        self._swallow: set[int] = set()     # touches du raccourci à ne pas transmettre
        self._park = wintypes.POINT()
        self._saved_pos = wintypes.POINT()
        self._fallback_down: set[int] = set()

    # --- API publique (thread-safe) ------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="win-input", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def request_mode(self, remote: bool) -> None:
        user32.PostThreadMessageW(self._thread_id, WM_APP_MODE, 1 if remote else 0, 0)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # --- Boucle de messages -------------------------------------------------------

    def _run(self) -> None:
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # crée la file de messages
        self._thread_id = kernel32.GetCurrentThreadId()
        self._register_hotkeys()
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    idx = int(msg.wParam)
                    if 0 <= idx < len(self.hotkeys) and not self.remote:
                        self._fire(self.hotkeys[idx].action)
                elif msg.message == WM_APP_MODE:
                    if msg.wParam and not self.remote:
                        self._enter_remote()
                    elif not msg.wParam and self.remote:
                        self._leave_remote()
        finally:
            if self.remote:
                self._leave_remote()
            for idx in self._registered:
                user32.UnregisterHotKey(None, idx)
            if self._fallback_kb_hook:
                user32.UnhookWindowsHookEx(self._fallback_kb_hook)

    def _register_hotkeys(self) -> None:
        failed = []
        for idx, hk in enumerate(self.hotkeys):
            flags = MOD_NOREPEAT
            for m in hk.mods:
                flags |= MOD_FLAGS[m]
            if user32.RegisterHotKey(None, idx, flags, hk.vk):
                self._registered.append(idx)
            else:
                failed.append(hk.text)
        if failed:
            # Raccourci déjà pris par une autre appli : on se rabat sur un hook
            # clavier léger, qui ne fait que comparer le raccourci.
            log.warning("RegisterHotKey a échoué pour %s : hook clavier de secours", failed)
            self._fallback_kb_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._fallback_proc, kernel32.GetModuleHandleW(None), 0)

    def _fire(self, action: str) -> None:
        try:
            self.on_hotkey(action)
        except Exception:
            log.exception("erreur dans le gestionnaire de raccourci")

    # --- Bascule ------------------------------------------------------------------

    def _snapshot_held(self) -> None:
        self._held_keys.clear()
        self._held_buttons.clear()
        for vk in range(1, 255):
            if user32.GetAsyncKeyState(vk) & 0x8000:
                if vk in (0x01, 0x02, 0x04, 0x05, 0x06):
                    self._held_buttons.add(vk)
                elif vk not in (0x10, 0x11, 0x12):  # ignorer Shift/Ctrl/Alt génériques
                    self._held_keys.add(vk)

    def _enter_remote(self) -> None:
        self._snapshot_held()
        self._down = set(self._held_keys)
        self._swallow.clear()
        user32.GetCursorPos(ctypes.byref(self._saved_pos))
        self._park.x = user32.GetSystemMetrics(0) // 2
        self._park.y = user32.GetSystemMetrics(1) // 2
        user32.SetCursorPos(self._park.x, self._park.y)
        hmod = kernel32.GetModuleHandleW(None)
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        if not self._kb_hook or not self._ms_hook:
            log.error("Impossible d'installer les hooks (erreur %s)", ctypes.get_last_error())
            self._unhook()
            return
        self.remote = True
        log.info("Mode Mac : clavier et souris envoyés au Mac")
        self.on_mode(True)

    def _unhook(self) -> None:
        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None

    def _leave_remote(self) -> None:
        self._unhook()
        self.remote = False
        user32.SetCursorPos(self._saved_pos.x, self._saved_pos.y)
        log.info("Mode PC : clavier et souris rendus à Windows")
        self.on_mode(False)

    # --- Hooks --------------------------------------------------------------------

    @staticmethod
    def _guard(fn):
        """Une exception dans un hook ne doit jamais bloquer le clavier."""

        def wrapper(n_code, w_param, l_param):
            try:
                return fn(n_code, w_param, l_param)
            except Exception:
                log.exception("erreur dans un hook")
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

        return wrapper

    def _kb_callback(self, n_code, w_param, l_param):
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if kb.flags & LLKHF_INJECTED:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        vk = kb.vkCode
        down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)

        # Le volume reste piloté par Windows (c'est lui qui alimente le casque).
        if vk in (VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP):
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Touche déjà enfoncée avant la bascule : Windows doit voir son relâchement.
        if vk in self._held_keys:
            if down:
                return 1
            self._held_keys.discard(vk)
            self._down.discard(vk)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        if vk == VK_LCONTROL and kb.scanCode == ALTGR_FAKE_CTRL_SCAN:
            return 1  # Ctrl fantôme d'AltGr : inutile côté Mac

        repeat = down and vk in self._down
        if down:
            if not repeat:
                for hk in self.hotkeys:
                    if hk.action in ("toggle", "pc") and hk.matches(vk, self._down):
                        self._swallow.add(vk)
                        self._down.add(vk)
                        self._fire(hk.action)
                        return 1
            self._down.add(vk)
        else:
            self._down.discard(vk)

        if vk in self._swallow:
            if not down:
                self._swallow.discard(vk)
            return 1

        self.sink(("key", scancode_of(kb), down, repeat))
        return 1

    def _ms_callback(self, n_code, w_param, l_param):
        if n_code != HC_ACTION:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if ms.flags & LLMHF_INJECTED:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        msg = w_param
        if msg == WM_MOUSEMOVE:
            dx, dy = ms.pt.x - self._park.x, ms.pt.y - self._park.y
            if dx or dy:
                self.sink(("move", dx, dy))
            return 1

        button = None
        down = False
        held_vk = None
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            button, down, held_vk = protocol.BTN_LEFT, msg == WM_LBUTTONDOWN, 0x01
        elif msg in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            button, down, held_vk = protocol.BTN_RIGHT, msg == WM_RBUTTONDOWN, 0x02
        elif msg in (WM_MBUTTONDOWN, WM_MBUTTONUP):
            button, down, held_vk = protocol.BTN_MIDDLE, msg == WM_MBUTTONDOWN, 0x04
        elif msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            which = (ms.mouseData >> 16) & 0xFFFF
            button = protocol.BTN_X1 if which == 1 else protocol.BTN_X2
            down, held_vk = msg == WM_XBUTTONDOWN, 0x05 if which == 1 else 0x06
        elif msg == WM_MOUSEWHEEL:
            self.sink(("wheel", 0, ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value))
            return 1
        elif msg == WM_MOUSEHWHEEL:
            self.sink(("wheel", ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value, 0))
            return 1

        if button is not None:
            if held_vk in self._held_buttons:
                if not down:
                    self._held_buttons.discard(held_vk)
                    return user32.CallNextHookEx(None, n_code, w_param, l_param)
                return 1
            self.sink(("button", button, down))
        return 1

    def _fallback_callback(self, n_code, w_param, l_param):
        """Hook de secours en mode PC : détecte seulement le raccourci."""
        if n_code == HC_ACTION and not self.remote:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if down:
                fresh = vk not in self._fallback_down
                self._fallback_down.add(vk)
                if fresh:
                    for idx, hk in enumerate(self.hotkeys):
                        if idx not in self._registered and hk.action in ("toggle", "mac") \
                                and hk.matches(vk, self._fallback_down):
                            self._fire(hk.action)
                            return 1
            else:
                self._fallback_down.discard(vk)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)
