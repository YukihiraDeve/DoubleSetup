"""v0.2 : réglages, modes audio, et pilotes multi-systèmes (testés hors de leur système)."""

import ctypes
import importlib
import json
import sys

import pytest

from setupdouble import config as config_mod
from setupdouble import dashboard, keymap, settings
from setupdouble import protocol as P
from setupdouble.client import Client, RecordingInjector
from setupdouble.server_core import Server

from test_dashboard import post
from test_integration import make_cfg, wait_for

# --- configuration --------------------------------------------------------------------

OLD_CONFIG = '''# mon fichier
secret = "ancien-secret-solide"

[server]
hotkey_toggle = "ctrl+alt+space"   # bascule
hotkey_mac = "ctrl+alt+right"

[audio]
win_output_device = "Scarlett"   # casque
mac_sound = false
'''


def test_old_names_are_migrated():
    cfg = config_mod._merge(config_mod.DEFAULTS,
                            config_mod.migrate(config_mod.tomllib.loads(OLD_CONFIG)))
    assert cfg["server"]["hotkey_remote"] == "ctrl+alt+right"
    assert cfg["audio"]["server_output_device"] == "Scarlett"
    assert cfg["audio"]["sound"] is False
    assert "win_output_device" not in cfg["audio"]


def test_save_keeps_comments_and_renames_old_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_CONFIG, encoding="utf-8")
    config_mod.save({None: {"secret": "nouveau-secret-123"},
                     "audio": {"server_output_device": "Haut-parleurs (Scarlett Solo USB)",
                               "sound_mode": "follow"},
                     "client": {"server": "192.168.1.15"}}, path)
    text = path.read_text(encoding="utf-8")
    assert "# mon fichier" in text and "# casque" in text and "# bascule" in text
    assert 'server_output_device = "Haut-parleurs (Scarlett Solo USB)"   # casque' in text
    assert "win_output_device" not in text
    cfg = config_mod.load(path)
    assert cfg["secret"] == "nouveau-secret-123"
    assert cfg["audio"]["sound_mode"] == "follow"
    assert cfg["client"]["server"] == "192.168.1.15"
    assert cfg["server"]["hotkey_remote"] == "ctrl+alt+right"


def test_toml_values_are_escaped(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('secret = "un-secret-assez-long"\n', encoding="utf-8")
    config_mod.save({"server": {"name": 'PC "Gaming" \\ #1'}}, path)
    assert config_mod.load(path)["server"]["name"] == 'PC "Gaming" \\ #1'


# --- réglages depuis l'interface --------------------------------------------------------

def test_settings_validation():
    ok = settings.validate({"server": {"hotkey_toggle": "Ctrl + Alt + F12"},
                            "audio": {"sound_mode": "follow", "latency_ms": "40"},
                            "general": {"secret": ""}}, "server")
    assert ok == {"server": {"hotkey_toggle": "ctrl+alt+f12"},
                  "audio": {"sound_mode": "follow", "latency_ms": 40}}
    bad = [({"server": {"hotkey_toggle": "ctrl+banane"}}, "server", "Raccourci"),
           ({"general": {"secret": "court"}}, "server", "12 caractères"),
           ({"client": {"server": "pas une ip !"}}, "client", "Adresse"),
           ({"audio": {"sound_mode": "fort"}}, "server", "mix"),
           ({"client": {"server": "1.2.3.4"}}, "server", "inconnu"),  # pas pour ce rôle
           ({"audio": {"latency_ms": 5000}}, "client", "entre")]
    for updates, role, msg in bad:
        with pytest.raises(settings.SettingsError, match=msg):
            settings.validate(updates, role)


def test_settings_api_saves_and_restarts(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('secret = "secret-de-test-123"\n', encoding="utf-8")
    cfg = make_cfg()
    cfg["_path"] = str(path)
    server = Server(cfg)
    restarted = []
    server.restart = lambda: restarted.append(True)
    d = dashboard.Dashboard(server, 0).start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=3)
        conn.request("GET", "/api/settings", headers={"X-SD-Token": d.token})
        data = json.loads(conn.getresponse().read())
        assert data["role"] == "server" and "secret" not in json.dumps(data["values"])
        assert "hotkey_toggle" in data["values"]["server"]

        code, body = post_settings(d, {"server": {"hotkey_toggle": "ctrl+alt+f12"},
                                       "audio": {"mic_mode": "follow"}})
        assert code == 200 and body["ok"]
        assert wait_for(lambda: restarted == [True])
        saved = config_mod.load(path)
        assert saved["server"]["hotkey_toggle"] == "ctrl+alt+f12"
        assert saved["audio"]["mic_mode"] == "follow"

        code, body = post_settings(d, {"general": {"secret": "x"}})
        assert code == 400 and "12" in body["message"]
        assert config_mod.load(path)["secret"] == "secret-de-test-123"
    finally:
        d.stop()


def post_settings(dash, values):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=3)
    conn.request("POST", "/api/settings", json.dumps({"values": values}),
                 {"X-SD-Token": dash.token, "Content-Type": "application/json"})
    r = conn.getresponse()
    return r.status, json.loads(r.read())


def test_post_without_json_content_type_is_refused():
    server = Server(make_cfg())
    d = dashboard.Dashboard(server, 0).start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=3)
        conn.request("POST", "/api/action", '{"action": "client"}',
                     {"X-SD-Token": d.token, "Content-Type": "text/plain"})
        assert conn.getresponse().status == 415
    finally:
        d.stop()


def test_favicon_served():
    d = dashboard.Dashboard(Server(make_cfg()), 0).start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=3)
        conn.request("GET", "/favicon.svg")
        r = conn.getresponse()
        assert r.status == 200 and r.getheader("Content-Type") == "image/svg+xml"
        assert r.read().startswith(b"<svg")
    finally:
        d.stop()


# --- modes audio ---------------------------------------------------------------------------

def test_follow_modes_only_share_while_remote(pair):
    server, fake, client, *_ = pair
    server.acfg.update(enabled=True, sound_mode="follow", mic_mode="always")
    server.set_sound(True)
    server.set_mic(True)
    assert wait_for(lambda: client.audio_flags == P.AUDIO_MIC_ON)  # son coupé tant qu'ici
    server.on_hotkey("toggle")  # -> machine pilotée
    assert wait_for(lambda: client.audio_flags == P.AUDIO_MIC_ON | P.AUDIO_SOUND_ON)
    server.on_hotkey("toggle")  # -> retour
    assert wait_for(lambda: client.audio_flags == P.AUDIO_MIC_ON)
    assert wait_for(lambda: client.status()["audio"]["sound"] is True)  # interrupteur « on »
    st = client.status()["audio"]
    assert st["sound_mode"] == "follow" and st["sound_live"] is False

    server.acfg["mic_mode"] = "follow"
    server.set_mic(True)
    assert wait_for(lambda: client.audio_flags == 0)


# --- pilotes : Windows (SendInput) ----------------------------------------------------------

class FakeUser32:
    def __init__(self):
        self.inputs = []

    def __getattr__(self, name):
        dll = self

        class Fn:
            argtypes = restype = None

            def __call__(self, *args):
                if name == "SendInput":
                    inp = ctypes.cast(args[1], ctypes.POINTER(dll.INPUT)).contents
                    if inp.type == 1:
                        k = inp.u.ki
                        dll.inputs.append(("key", k.wVk, k.wScan, k.dwFlags))
                    else:
                        m = inp.u.mi
                        dll.inputs.append(("mouse", m.dx, m.dy, ctypes.c_int32(m.mouseData).value,
                                           m.dwFlags))
                    return 1
                return 0

        fn = Fn()
        object.__setattr__(self, name, fn)
        return fn


@pytest.fixture
def win_inject(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    sys.modules.pop("setupdouble.win_inject", None)
    mod = importlib.import_module("setupdouble.win_inject")
    fake.INPUT = mod.INPUT
    yield mod, fake
    sys.modules.pop("setupdouble.win_inject", None)


def test_windows_injection_uses_scancodes(win_inject):
    mod, fake = win_inject
    inj = mod.WinInjector(mouse_speed=1.0, absolute=False)
    inj.key(0x10, True)          # touche A (AZERTY) : scancode 0x10
    inj.key(0xE04B, True)        # flèche gauche : étendue
    inj.key(0xE022, True)        # lecture/pause : code virtuel média
    inj.move(5, -3)
    inj.button(P.BTN_X2, True)
    inj.wheel(0, -120)
    inj.release_all()
    k = mod
    assert fake.inputs[0] == ("key", 0, 0x10, k.KEYEVENTF_SCANCODE)
    assert fake.inputs[1] == ("key", 0, 0x4B, k.KEYEVENTF_SCANCODE | k.KEYEVENTF_EXTENDEDKEY)
    assert fake.inputs[2][1] == 0xB3
    assert fake.inputs[3] == ("mouse", 5, -3, 0, k.MOUSEEVENTF_MOVE)
    assert fake.inputs[4] == ("mouse", 0, 0, k.XBUTTON2, k.MOUSEEVENTF_XDOWN)
    assert fake.inputs[5] == ("mouse", 0, 0, -120, k.MOUSEEVENTF_WHEEL)
    released = [i for i in fake.inputs[6:] if i[0] == "key"]
    assert len(released) == 3 and all(i[3] & k.KEYEVENTF_KEYUP for i in released)
    assert fake.inputs[-1] == ("mouse", 0, 0, k.XBUTTON2, k.MOUSEEVENTF_XUP)


# --- pilotes : Linux (uinput / evdev) --------------------------------------------------------

class FakeUInput:
    def __init__(self):
        self.events = []

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append("syn")


def test_linux_injection():
    from setupdouble import linux_input as L
    dev = FakeUInput()
    inj = L.LinuxInjector(device=dev)
    inj.key(0x1E, True)            # KEY_A
    inj.key(0xE048, True)          # KEY_UP
    inj.move(3, 0)
    inj.wheel(0, -240)
    inj.button(P.BTN_RIGHT, True)
    inj.release_all()
    ev = [e for e in dev.events if e != "syn"]
    assert ev[:5] == [(L.EV_KEY, 30, 1), (L.EV_KEY, 103, 1), (L.EV_REL, L.REL_X, 3),
                      (L.EV_REL, L.REL_WHEEL, -2), (L.EV_KEY, L.BTN_RIGHT, 1)]
    assert set(ev[5:]) == {(L.EV_KEY, 30, 0), (L.EV_KEY, 103, 0), (L.EV_KEY, L.BTN_RIGHT, 0)}


def test_linux_capture_translation_and_hotkey():
    from setupdouble import linux_input as L
    from setupdouble.hotkeys import parse_hotkey
    assert L.translate(L.EV_KEY, 30, 1) == ("key", 0x1E, True, False)
    assert L.translate(L.EV_KEY, 30, 2) == ("key", 0x1E, True, True)
    assert L.translate(L.EV_REL, L.REL_WHEEL, 1) == ("wheel", 0, 120)
    assert L.translate(L.EV_KEY, L.BTN_SIDE, 1) == ("button", P.BTN_X1, True)

    fired, sent, modes = [], [], []
    cap = L.LinuxCapture([parse_hotkey("ctrl+alt+space", "toggle")], fired.append,
                         sent.append, modes.append)
    for code in (29, 56, 57):      # Ctrl, Alt, Espace
        cap._handle(L.EV_KEY, code, 1)
    assert fired == ["toggle"] and sent == []  # mode local : rien n'est envoyé
    cap.remote = True
    cap._handle(L.EV_KEY, 30, 1)
    assert sent == [("key", 0x1E, True, False)]


def test_linux_keymap_round_trip():
    for sc, code in keymap.SCANCODE_TO_LINUX.items():
        assert keymap.LINUX_TO_SCANCODE[code] == sc


# --- pilotes : macOS comme source (CGEventTap) -------------------------------------------------

def test_mac_capture_translation():
    from setupdouble import mac_capture as M
    m = keymap.MacToScancode("pc")
    tr = lambda *a: M.translate(*a, mapper=m)  # noqa: E731
    assert tr(M.K_KEY_DOWN, 0x00, 0, False, 0, 0, 0, 0, 0) == ("key", 0x1E, True, False)
    # Cmd gauche enfoncé (bit 0x08) -> Ctrl gauche côté PC avec la disposition « pc »
    assert tr(M.K_FLAGS, 0x37, 0x00100008, False, 0, 0, 0, 0, 0) == ("key", 0x1D, True, False)
    assert tr(M.K_FLAGS, 0x37, 0x00000100, False, 0, 0, 0, 0, 0) == ("key", 0x1D, False, False)
    assert tr(M.K_MOVED, 0, 0, False, 0, 4, -2, 0, 0) == ("move", 4, -2)
    assert tr(M.K_ODOWN, 0, 0, False, 3, 0, 0, 0, 0) == ("button", P.BTN_X1, True)
    assert tr(M.K_SCROLL, 0, 0, False, 0, 0, 0, 0, -1) == ("wheel", 0, -120)
    assert keymap.MacToScancode("mac").to_scancode(0x37) == 0xE05B


# --- presse-papiers et identité réseau --------------------------------------------------------

def test_linux_clipboard_choice():
    from setupdouble.platforms import linux_clipboard_commands
    have = lambda *names: (lambda n: n if n in names else None)  # noqa: E731
    assert linux_clipboard_commands({"WAYLAND_DISPLAY": "wayland-0"},
                                    have("wl-copy", "wl-paste"))[1] == ["wl-copy"]
    assert linux_clipboard_commands({}, have("xclip"))[1][0] == "xclip"
    assert linux_clipboard_commands({}, have()) is None


def test_client_reports_its_os(pair):
    server, fake, client, *_ = pair
    client.os = "linux"
    client._info_sent = None
    client._send_info()
    assert wait_for(lambda: server.status()["machines"]["client"]["os"] == "linux")


def test_old_action_names_still_work(pair):
    server, fake, client, *_ = pair
    assert server.action("mac") == (True, "")
    assert wait_for(lambda: client.active)
    assert client.action("pc") == (True, "")
    assert wait_for(lambda: not server.remote)


def test_dashboard_mac_alias(pair):
    server, *_ = pair
    d = dashboard.Dashboard(server, 0).start()
    try:
        assert post(d, "client")[0] == 200
        assert wait_for(lambda: server.remote)
    finally:
        d.stop()


def test_recording_injector_still_available_from_old_module():
    from setupdouble.client_mac import RecordingInjector as Old
    assert Old is RecordingInjector and Client
