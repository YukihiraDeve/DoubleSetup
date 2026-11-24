"""Logique des hooks Windows testée hors Windows, avec une user32 simulée."""

import ctypes
import importlib
import sys

import pytest


class FakeDLL:
    """Accepte argtypes/restype et renvoie des fonctions simulées."""

    def __init__(self):
        self.calls = []
        self.async_state = {}
        self.cursor = (100, 100)

    def __getattr__(self, name):
        dll = self

        class Fn:
            argtypes = restype = None

            def __call__(self, *args):
                dll.calls.append((name, args))
                if name == "CallNextHookEx":
                    return 0
                if name == "GetAsyncKeyState":
                    return -32768 if dll.async_state.get(args[0]) else 0
                if name == "GetSystemMetrics":
                    return 1920 if args[0] == 0 else 1080
                if name == "GetCursorPos":
                    return 1
                if name in ("SetWindowsHookExW", "RegisterHotKey"):
                    return 1
                return 0

        fn = Fn()
        object.__setattr__(self, name, fn)
        return fn


@pytest.fixture
def win(monkeypatch):
    fake = FakeDLL()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    sys.modules.pop("setupdouble.win_input", None)
    mod = importlib.import_module("setupdouble.win_input")
    yield mod, fake
    sys.modules.pop("setupdouble.win_input", None)


def kb_event(mod, vk, scan, down, extended=False):
    s = mod.KBDLLHOOKSTRUCT(vk, scan, mod.LLKHF_EXTENDED if extended else 0, 0, 0)
    msg = mod.WM_KEYDOWN if down else mod.WM_KEYUP
    return msg, s


def make_capture(mod, fake, held=()):
    from setupdouble.hotkeys import parse_hotkey

    events, fired = [], []
    cap = mod.InputCapture([parse_hotkey("ctrl+alt+space", "toggle")],
                           fired.append, events.append, lambda remote: None)
    for vk in held:
        fake.async_state[vk] = True
    cap._enter_remote()
    for vk in held:
        fake.async_state[vk] = False

    def press(vk, scan, down, extended=False):
        msg, s = kb_event(mod, vk, scan, down, extended)
        return cap._kb_callback(0, msg, ctypes.addressof(s))

    return cap, events, fired, press


def test_held_hotkey_modifiers_are_released_on_windows(win):
    mod, fake = win
    # Ctrl+Alt encore enfoncés au moment de la bascule
    cap, events, fired, press = make_capture(mod, fake, held=(0xA2, 0xA4))
    assert cap.remote
    fake.calls.clear()
    assert press(0xA2, 0x1D, False) == 0  # relâchement transmis à Windows
    assert any(c[0] == "CallNextHookEx" for c in fake.calls)
    assert press(0xA4, 0x38, False) == 0
    assert events == []  # rien envoyé au Mac


def test_keys_are_captured_and_forwarded(win):
    mod, fake = win
    cap, events, fired, press = make_capture(mod, fake)
    assert press(0x41, 0x10, True) == 1  # bloquée pour Windows
    assert press(0x41, 0x10, True) == 1  # répétition
    assert press(0x41, 0x10, False) == 1
    assert press(0x25, 0x4B, True, extended=True) == 1
    assert events == [("key", 0x10, True, False), ("key", 0x10, True, True),
                      ("key", 0x10, False, False), ("key", 0xE04B, True, False)]


def test_altgr_fake_ctrl_is_dropped(win):
    mod, fake = win
    cap, events, fired, press = make_capture(mod, fake)
    press(0xA2, 0x21D, True)  # Ctrl fantôme
    press(0xA5, 0x38, True, extended=True)  # AltGr
    assert events == [("key", 0xE038, True, False)]


def test_hotkey_in_remote_mode_is_swallowed(win):
    mod, fake = win
    cap, events, fired, press = make_capture(mod, fake)
    press(0xA2, 0x1D, True)
    press(0xA4, 0x38, True)
    assert press(0x20, 0x39, True) == 1
    assert fired == ["toggle"]
    press(0x20, 0x39, False)
    sent = [e[1] for e in events]
    assert 0x39 not in sent  # l'espace du raccourci n'atteint jamais le Mac
    assert sent == [0x1D, 0x38]  # Ctrl/Alt partent, LEAVE les relâchera côté Mac


def test_volume_keys_stay_on_windows(win):
    mod, fake = win
    cap, events, fired, press = make_capture(mod, fake)
    assert press(0xAF, 0x30, True, extended=True) == 0
    assert events == []


def test_mouse_relative_motion(win):
    mod, fake = win
    cap, events, fired, press = make_capture(mod, fake)
    s = mod.MSLLHOOKSTRUCT(mod.wintypes.POINT(960 + 7, 540 - 3), 0, 0, 0, 0)
    assert cap._ms_callback(0, mod.WM_MOUSEMOVE, ctypes.addressof(s)) == 1
    wheel = mod.MSLLHOOKSTRUCT(mod.wintypes.POINT(960, 540), (0xFF88 << 16), 0, 0, 0)
    cap._ms_callback(0, mod.WM_MOUSEWHEEL, ctypes.addressof(wheel))
    x2 = mod.MSLLHOOKSTRUCT(mod.wintypes.POINT(960, 540), (2 << 16), 0, 0, 0)
    cap._ms_callback(0, mod.WM_XBUTTONDOWN, ctypes.addressof(x2))
    assert events == [("move", 7, -3), ("wheel", 0, -120), ("button", 5, True)]
