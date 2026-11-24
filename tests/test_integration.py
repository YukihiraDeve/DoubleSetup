"""Serveur (cœur, sans Windows) + agent Mac (injecteur factice) sur 127.0.0.1."""

import copy
import threading
import time

import pytest

from setupdouble import config as config_mod
from setupdouble import protocol as P
from setupdouble.client_mac import Client, RecordingInjector
from setupdouble.server_core import Server

SECRET = "secret-de-test-123"


def make_cfg(secret=SECRET):
    cfg = copy.deepcopy(config_mod.DEFAULTS)
    cfg["secret"] = secret
    cfg["server"].update(bind="127.0.0.1", control_port=0, beacon=False)
    cfg["audio"]["enabled"] = False
    return cfg


class FakeInput:
    """Simule win_input.InputCapture : bascule immédiatement."""

    def __init__(self, server):
        self.server = server
        self.requests = []

    def request_mode(self, remote):
        self.requests.append(remote)
        if remote != self.server.remote:
            self.server.on_mode(remote)


class MemClipboard:
    def __init__(self, text=None):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text
        return True


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def pair():
    server_clip, client_clip = MemClipboard("copié sur le PC"), MemClipboard()
    server = Server(make_cfg(), clipboard=server_clip)
    fake = FakeInput(server)
    server.input = fake
    server.start()
    inj = RecordingInjector()
    client = Client(make_cfg(), inj, clipboard=client_clip, use_audio=False)
    t = threading.Thread(target=client.session, args=("127.0.0.1", server.bound_port),
                         daemon=True)
    t.start()
    assert wait_for(lambda: server.connected), "le Mac ne s'est pas connecté"
    yield server, fake, client, inj, server_clip, client_clip
    client.stop()
    server.stop()


def test_full_switch_cycle(pair):
    server, fake, client, inj, server_clip, client_clip = pair

    server.on_hotkey("toggle")  # -> Mac
    assert server.remote and fake.requests == [True]
    assert wait_for(lambda: client.active)
    assert wait_for(lambda: client_clip.text == "copié sur le PC")

    server.sink(("key", 0x1E, True, False))
    server.sink(("key", 0x1E, False, False))
    for _ in range(50):
        server.sink(("move", 2, -1))
    server.sink(("button", P.BTN_LEFT, True))
    server.sink(("button", P.BTN_LEFT, False))
    server.sink(("wheel", 0, -240))
    assert wait_for(lambda: ("wheel", 0, -240) in inj.events)

    keys = [e for e in inj.events if e[0] == "key"]
    assert keys == [("key", 0x1E, True, False), ("key", 0x1E, False, False)]
    moves = [e for e in inj.events if e[0] == "move"]
    assert sum(m[1] for m in moves) == 100 and sum(m[2] for m in moves) == -50
    assert ("button", P.BTN_LEFT, True) in inj.events
    assert ("wake",) in inj.events

    client_clip.text = "copié sur le Mac"
    server.on_hotkey("toggle")  # -> PC
    assert not server.remote
    assert wait_for(lambda: not client.active)
    assert ("release_all",) in inj.events
    assert wait_for(lambda: server_clip.text == "copié sur le Mac")


def test_disconnect_returns_keyboard_to_pc(pair):
    server, fake, client, inj, *_ = pair
    server.request_mode(True)
    assert server.remote
    client.stop()  # le Mac disparaît
    assert wait_for(lambda: not server.connected)
    assert wait_for(lambda: not server.remote), "le clavier doit revenir au PC"
    assert fake.requests[-1] is False


def test_refuses_switch_without_mac():
    server = Server(make_cfg())
    notes = []
    server.notify = notes.append
    server.input = FakeInput(server)
    server.request_mode(True)
    assert not server.remote and notes


def test_wrong_secret_is_rejected():
    from setupdouble.client_mac import AuthError

    server = Server(make_cfg())
    server.input = FakeInput(server)
    server.start()
    try:
        client = Client(make_cfg("mauvais-secret-456"), RecordingInjector(), use_audio=False)
        with pytest.raises(AuthError):
            client.session("127.0.0.1", server.bound_port)
        assert not server.connected
    finally:
        server.stop()
