"""Réveil du Mac endormi : paquet magique, adresses retenues, bascule différée."""

import threading
import time

import pytest

from setupdouble import protocol as P
from setupdouble.client_mac import Client, RecordingInjector
from setupdouble.mac_system import SleepGuard, parse_ifconfig
from setupdouble.server_core import Server

from test_integration import FakeInput, make_cfg, wait_for

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether bc:d0:74:1c:85:4b
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 04:BF:1B:C0:56:0C
\tinet 192.168.1.17 netmask 0xffffff00 broadcast 192.168.1.255
\tmedia: autoselect (1000baseT <full-duplex>)
\tstatus: active
en1: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tether 36:2a:9b:11:22:33
\tstatus: inactive
awdl0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 9a:11:22:33:44:55
\tinet 169.254.1.1 netmask 0xffff0000
\tstatus: active
"""

TARGETS = [{"iface": "en7", "mac": "04:bf:1b:c0:56:0c", "ip": "192.168.1.17",
            "broadcast": "192.168.1.255"}]


def test_magic_packet():
    pkt = P.magic_packet("04:BF:1B:C0:56:0C")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("04bf1bc0560c") and pkt[-6:] == pkt[6:12]
    with pytest.raises(ValueError):
        P.magic_packet("pas-une-adresse")


def test_parse_ifconfig_keeps_active_lan_interfaces():
    assert parse_ifconfig(IFCONFIG) == [
        {"iface": "en0", "mac": "bc:d0:74:1c:85:4b", "ip": "192.168.1.23",
         "broadcast": "192.168.1.255"},
        {"iface": "en7", "mac": "04:bf:1b:c0:56:0c", "ip": "192.168.1.17",
         "broadcast": "192.168.1.255"},
    ]


def test_sleep_guard_modes():
    assert SleepGuard("never").mode == "never"
    SleepGuard("never").start()  # ne lance rien
    with pytest.raises(ValueError):
        SleepGuard("parfois")


@pytest.fixture
def waking_server(tmp_path):
    server = Server(make_cfg())
    server.input = FakeInput(server)
    server.wake_store = tmp_path / "mac.json"
    sent = []
    server._send_magic = lambda: sent.append(time.monotonic())
    server.start()
    yield server, sent
    server.stop()


def test_hotkey_wakes_sleeping_mac_then_switches(waking_server):
    server, sent = waking_server
    server.wake_targets = list(TARGETS)
    server.on_hotkey("toggle")  # aucun Mac connecté
    assert server.waking and sent and not server.remote
    assert server.status()["link"]["state"] == "waking"

    # le Mac se réveille et se reconnecte : la bascule se fait toute seule
    client = Client(make_cfg(), RecordingInjector(), use_audio=False)
    threading.Thread(target=client.session, args=("127.0.0.1", server.bound_port),
                     daemon=True).start()
    try:
        assert wait_for(lambda: server.remote and client.active)
        assert not server.waking
    finally:
        client.stop()


def test_without_known_address_the_switch_is_refused(waking_server):
    server, sent = waking_server
    notes = []
    server.notify = notes.append
    server.on_hotkey("toggle")
    assert not server.waking and not sent and notes == ["Le Mac n'est pas connecté."]
    ok, message = server.action("mac")
    assert not ok


def test_mac_address_is_learned_and_saved(waking_server):
    server, sent = waking_server
    client = Client(make_cfg(), RecordingInjector(), use_audio=False)
    client.wake_targets = list(TARGETS)
    # session() remplit wake_targets avec les vraies interfaces : on garde celles du test
    client_session = client.session

    def session(host, port):
        import setupdouble.mac_system as ms
        real = ms.network_identity
        ms.network_identity = lambda: list(TARGETS)
        try:
            client_session(host, port)
        finally:
            ms.network_identity = real

    threading.Thread(target=session, args=("127.0.0.1", server.bound_port), daemon=True).start()
    try:
        assert wait_for(lambda: server.wake_targets == TARGETS)
        assert "04:bf:1b:c0:56:0c" in server.wake_store.read_text()
        assert server.status()["can_wake"]
    finally:
        client.stop()

    # au redémarrage du PC, l'adresse est relue depuis le fichier
    again = Server(make_cfg())
    again.wake_store = server.wake_store
    again._load_wake_targets()
    assert again.wake_targets == TARGETS


def test_silent_mac_is_woken_instead_of_capturing_the_keyboard(waking_server):
    server, sent = waking_server
    server.wake_targets = list(TARGETS)
    client = Client(make_cfg(), RecordingInjector(), use_audio=False)
    threading.Thread(target=client.session, args=("127.0.0.1", server.bound_port),
                     daemon=True).start()
    try:
        assert wait_for(lambda: server.connected)
        server.session.last_rx -= 6  # plus de réponse depuis 6 s : le Mac s'endort
        server.on_hotkey("toggle")
        assert not server.remote and server.waking and sent
        # il répond au ping suivant : il ne dormait pas, la bascule se fait
        assert wait_for(lambda: server.remote, timeout=5)
    finally:
        client.stop()
