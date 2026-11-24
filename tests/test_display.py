"""Bascule d'écran (DDC/CI) : analyse des outils et enchaînement avec la bascule."""

import threading
import time

import pytest

from setupdouble import display as D
from setupdouble import settings
from setupdouble.client import Client, RecordingInjector
from setupdouble.server_core import Server

from test_integration import FakeInput, make_cfg, wait_for

HDMI1, DP1 = 0x11, 0x0F


class FakeSwitcher(D.Switcher):
    """Écran simulé. ``accepts`` : la commande est-elle acceptée depuis cette machine ?"""

    tool = "faux"

    def __init__(self, screen, accepts=True):
        self.screen, self.accepts = screen, accepts

    def list_monitors(self):
        return [{"id": "1", "name": "Odyssey G60SD"}]

    def get_input(self, monitor):
        return self.screen["input"]

    def set_input(self, monitor, code):
        self.screen.setdefault("log", []).append(code)
        if self.accepts:
            self.screen["input"] = code
        return self.accepts


def test_parse_input():
    assert D.parse_input("hdmi1") == HDMI1 and D.parse_input("HDMI 2") == 0x12
    assert D.parse_input("0x0f") == DP1 and D.parse_input("17") == HDMI1 and D.parse_input(5) == 5
    assert D.parse_input("") is None
    for bad in ("hdmi9", "0x1ff", True):
        with pytest.raises(ValueError):
            D.parse_input(bad)
    assert D.input_label(HDMI1) == "HDMI 1 (0x11)" and D.input_label(0x05) == "code 0x05"


def test_tool_output_parsing():
    assert D.parse_m1ddc_list("[1] Odyssey G60SD (A6E60805-E4CD-41C5-8A40-34260AC81721)\n"
                              "[2] DELL U2720Q (11111111-2222-3333-4444-555555555555)\n") == [
        {"id": "1", "name": "Odyssey G60SD"}, {"id": "2", "name": "DELL U2720Q"}]
    out = ("Display 1\n   I2C bus:  /dev/i2c-4\n   Monitor:  SAM:Odyssey G60SD:HNTW700123\n\n"
           "Display 2\n   I2C bus:  /dev/i2c-6\n   Monitor:  DEL:DELL U2720Q:ABC\n")
    assert D.parse_ddcutil_detect(out) == [{"id": "1", "name": "Odyssey G60SD"},
                                           {"id": "2", "name": "DELL U2720Q"}]


def test_m1ddc_commands():
    calls = []

    def run(cmd, timeout=8.0):
        calls.append(cmd)
        if cmd[-2:] == ["get", "input"]:
            return 0, "15\n"
        if cmd[1:3] == ["display", "list"]:
            return 0, "[1] Odyssey G60SD (A6E60805-E4CD-41C5-8A40-34260AC81721)\n"
        return 0, ""

    sw = D.M1ddcSwitcher("/opt/homebrew/bin/m1ddc", run=run)
    assert sw.resolve("odyssey") == "1" and sw.get_input("1") == DP1
    assert sw.set_input("1", HDMI1)
    assert calls[-1] == ["/opt/homebrew/bin/m1ddc", "display", "1", "set", "input", "17"]
    failing = D.M1ddcSwitcher("m1ddc", run=lambda c, timeout=8.0: (1, "DDC communication failure"))
    assert not failing.set_input("1", HDMI1)


def test_settings_normalize_input():
    assert settings.validate({"display": {"input": "HDMI 1", "enabled": True}}, "server") == \
        {"display": {"input": "hdmi1", "enabled": True}}
    assert settings.validate({"display": {"input": "5"}}, "client") == {"display": {"input": "0x05"}}
    with pytest.raises(settings.SettingsError):
        settings.validate({"display": {"input": "hdmi7"}}, "client")


def make_pair(screen, server_accepts=True, client_accepts=True):
    scfg, ccfg = make_cfg(), make_cfg()
    scfg["display"] = {"enabled": True, "monitor": "", "input": "hdmi1"}   # PC sur HDMI 1
    ccfg["display"] = {"enabled": True, "monitor": "", "input": "dp1"}     # Mac sur DP 1
    server = Server(scfg)
    server.display = D.DisplayLink(scfg["display"], FakeSwitcher(screen, server_accepts))
    server.display.FALLBACK_DELAY = 0.2
    server.input = FakeInput(server)
    server.start()
    client = Client(ccfg, RecordingInjector(), use_audio=False)
    client.display = D.DisplayLink(ccfg["display"], FakeSwitcher(screen, client_accepts))
    client.display.FALLBACK_DELAY = 0.2
    threading.Thread(target=client.session, args=("127.0.0.1", server.bound_port),
                     daemon=True).start()
    assert wait_for(lambda: server.connected and client.peer_info.get("display")
                    and server.session.peer_info.get("display"))
    return server, client


def test_screen_follows_keyboard_both_ways():
    screen = {"input": HDMI1}
    server, client = make_pair(screen)
    try:
        server.on_hotkey("toggle")             # -> Mac : le PC envoie DP 1
        assert wait_for(lambda: screen["input"] == DP1)
        server.on_hotkey("toggle")             # -> PC : le Mac envoie HDMI 1
        assert wait_for(lambda: screen["input"] == HDMI1)
        time.sleep(0.4)                        # les renforts ne contredisent rien
        assert screen["input"] == HDMI1
    finally:
        client.stop()
        server.stop()


def test_blocked_dock_on_mac_is_covered_by_the_pc():
    """Cas réel : le Mac (dock Dell) ne parle pas à l'écran, le PC si."""
    screen = {"input": HDMI1}
    server, client = make_pair(screen, client_accepts=False)
    try:
        server.on_hotkey("toggle")
        assert wait_for(lambda: screen["input"] == DP1)
        server.on_hotkey("toggle")             # le Mac échoue, le PC renvoie HDMI 1 en renfort
        assert wait_for(lambda: screen["input"] == HDMI1, timeout=2)
    finally:
        client.stop()
        server.stop()


def test_quick_double_switch_is_not_undone_by_a_late_fallback():
    screen = {"input": HDMI1}
    server, client = make_pair(screen)
    try:
        server.on_hotkey("toggle")
        server.on_hotkey("toggle")             # aller-retour immédiat
        time.sleep(0.6)                        # après tous les renforts
        assert screen["input"] == HDMI1 and not server.remote
    finally:
        client.stop()
        server.stop()


def test_disabled_display_sends_nothing():
    screen = {"input": HDMI1}
    link = D.DisplayLink({"enabled": False, "input": "hdmi1"}, FakeSwitcher(screen))
    link.switch(DP1)
    time.sleep(0.1)
    assert "log" not in screen
    assert link.check()["state"] == "off"


def test_round_trip_test():
    screen = {"input": HDMI1}
    link = D.DisplayLink({"enabled": True, "input": "hdmi1"}, FakeSwitcher(screen))
    ok, msg = link.test_round_trip(DP1, back_after=0.1)
    assert ok and screen["input"] == DP1
    assert wait_for(lambda: screen["input"] == HDMI1)
    assert not link.test_round_trip(None)[0]
