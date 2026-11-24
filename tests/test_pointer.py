"""Sensibilité de la souris : profils source / brute / locale, vitesse, taille d'écran."""

import threading

import pytest

from setupdouble.client import Client, RecordingInjector
from setupdouble.pointer import MacCurve, PointerShaper, describe
from setupdouble.server_core import Server

from test_integration import FakeInput, make_cfg, wait_for

WIN_FAST = {"os": "windows", "level": 14, "speed": 2.0, "accel": False, "screen": [2560, 1440]}
MAC_LINEAR = {"os": "mac", "scaling": 0.875, "accel": False, "speed": 1.0, "screen": [1280, 720]}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def move(shaper, clock, dx, dy, dt_ms=8.0):
    clock.t += dt_ms / 1000
    return shaper.shape(dx, dy)


def total(shaper, clock, steps, dx, dy=0, dt_ms=8.0):
    sx = sy = 0
    for _ in range(steps):
        x, y = move(shaper, clock, dx, dy, dt_ms)
        sx, sy = sx + x, sy + y
    return sx, sy


def test_source_profile_keeps_the_source_feel():
    clock = Clock()
    sh = PointerShaper("source", local=MAC_LINEAR, clock=clock)
    sh.set_source(WIN_FAST)
    assert total(sh, clock, 100, 3, -2) == (300, -200)


def test_raw_profile_removes_the_source_speed():
    clock = Clock()
    sh = PointerShaper("raw", local=MAC_LINEAR, clock=clock)
    sh.set_source(WIN_FAST)                      # Windows vitesse 14/20 = ×2
    assert total(sh, clock, 100, 4) == (200, 0)  # on revient aux comptes de la souris


def test_speed_and_fractions_are_carried():
    clock = Clock()
    sh = PointerShaper("source", speed=0.5, clock=clock)
    assert total(sh, clock, 9, 1) == (4, 0)      # 4,5 -> 4, la moitié restante est reportée
    assert total(sh, clock, 1, 1) == (1, 0)


def test_match_screen_scales_by_width():
    clock = Clock()
    sh = PointerShaper("source", match_screen=True, local=MAC_LINEAR, clock=clock)
    sh.set_source(WIN_FAST)                      # 2560 px -> 1280 points : moitié
    assert total(sh, clock, 10, 10) == (50, 0)


def test_local_mac_curve_accelerates_fast_moves():
    curve = MacCurve(0.875, linear=False)
    assert curve.gain(0.2) < 1.0 < curve.gain(20)
    assert MacCurve(0.875, linear=True).gain(0.2) == MacCurve(0.875, linear=True).gain(20)
    clock = Clock()
    mac_accel = dict(MAC_LINEAR, accel=True)
    sh = PointerShaper("local", local=mac_accel, clock=clock)
    sh.set_source({"os": "windows", "speed": 1.0, "accel": False})
    slow = total(sh, clock, 50, 1, dt_ms=8)[0]       # 50 comptes lentement
    fast = total(sh, clock, 5, 10, dt_ms=8)[0]       # 50 comptes d'un coup sec
    assert fast > slow


def test_local_profile_on_windows_leaves_ballistics_to_windows():
    sh = PointerShaper("local", local={"os": "windows", "speed": 1.0})
    assert sh.wants_system_ballistics
    assert not PointerShaper("source", local={"os": "windows"}).wants_system_ballistics


def test_summary_and_describe():
    sh = PointerShaper("raw", local=MAC_LINEAR)
    sh.set_source(dict(WIN_FAST, accel=True))
    state, text = sh.summary()
    assert state == "warn" and "approximation" in text
    assert describe(WIN_FAST) == "Windows : vitesse 14/20 (×2.00), sans accélération"
    assert "vitesse de suivi 0.875" in describe(MAC_LINEAR)
    with pytest.raises(ValueError):
        PointerShaper("turbo")


def test_server_pointer_settings_reach_the_client():
    server = Server(make_cfg())
    server._pointer_info = lambda: dict(WIN_FAST)
    server.input = FakeInput(server)
    server.start()
    cfg = make_cfg()
    cfg["client"]["mouse_profile"] = "raw"
    inj = RecordingInjector()
    client = Client(cfg, inj, use_audio=False)
    threading.Thread(target=client.session, args=("127.0.0.1", server.bound_port),
                     daemon=True).start()
    try:
        assert wait_for(lambda: client.pointer.source.get("speed") == 2.0)
        server.on_hotkey("toggle")
        assert wait_for(lambda: client.active)
        for _ in range(10):
            server.sink(("move", 4, 0))
        assert wait_for(lambda: sum(e[1] for e in inj.events if e[0] == "move") == 20)
        assert "Windows" in client.status()["pointer"]["source"]
    finally:
        client.stop()
        server.stop()
