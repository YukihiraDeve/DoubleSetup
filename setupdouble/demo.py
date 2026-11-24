"""Démo locale de l'interface, sans PC ni Accessibilité.

    python -m setupdouble.demo [--window]

Lance un serveur (clavier simulé) et un agent Mac factice sur 127.0.0.1,
avec leurs deux tableaux de bord :
  * vue PC  : http://127.0.0.1:47820/
  * vue Mac : http://127.0.0.1:47821/
"""

from __future__ import annotations

import argparse
import copy
import logging
import threading
import time

from . import config as config_mod
from . import dashboard
from .client import Client, RecordingInjector
from .server_core import Server

SECRET = "demo-secret-local-123"


class _FakeStream:
    """Flux audio simulé : « reçoit » des paquets tant qu'il est activé."""

    def __init__(self):
        self.enabled = True
        self.dest = None
        self.allowed_ip = None

    @property
    def last_packet(self) -> float:
        return time.monotonic() if self.enabled else 0.0

    def reset(self):
        pass

    def stop(self):
        pass


def _fake_server_audio(server: Server) -> None:
    server._sound_rx, server._mic_tx = _FakeStream(), _FakeStream()
    server.sound_device = "Haut-parleurs (Scarlett Solo USB)"
    server.mic_device = "Microphone (Scarlett Solo USB) · entrée 1"
    server.audio_status = "actif (simulé)"


def _fake_client_audio(client: Client, host: str, port: int) -> None:
    client._sound_tx, client._mic_rx = _FakeStream(), _FakeStream()


class _FakeInput:
    def __init__(self, server: Server):
        self.server = server

    def request_mode(self, remote: bool) -> None:
        if remote != self.server.remote:
            self.server.on_mode(remote)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SetupDouble – démo de l'interface")
    parser.add_argument("--window", action="store_true", help="ouvre aussi la fenêtre Mac")
    args = parser.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")

    cfg = copy.deepcopy(config_mod.DEFAULTS)
    cfg["secret"] = SECRET
    cfg["server"].update(bind="127.0.0.1", control_port=0, beacon=False, name="PC-GAMING")
    cfg["client"]["name"] = "MacBook Pro"

    server = Server(cfg)
    server.os = "windows"
    server._pointer_info = lambda: {"os": "windows", "level": 10, "speed": 1.0, "accel": False,
                                    "screen": [2560, 1440]}
    server.save_settings = lambda updates: (True, "Démo : réglages non enregistrés.")
    server._start_audio = lambda: _fake_server_audio(server)
    server.input = _FakeInput(server)
    server.start()
    client = Client(cfg, RecordingInjector(), use_audio=True)
    client._start_audio = lambda host, port: _fake_client_audio(client, host, port)
    client._device_presence = lambda: {"capture": True, "mic": True}
    client.accessibility = lambda: True
    client.os = "mac"
    client.save_settings = lambda updates: (True, "Démo : réglages non enregistrés.")
    d_pc = dashboard.Dashboard(server, 47820).start()
    d_mac = dashboard.Dashboard(client, 47821).start()

    def keep_connected():
        while True:
            try:
                client.session("127.0.0.1", server.bound_port)
            except (OSError, ConnectionError):
                pass
            time.sleep(1)

    threading.Thread(target=keep_connected, daemon=True).start()
    print(f"Vue PC  : {d_pc.url}\nVue Mac : {d_mac.url}\nCtrl+C pour arrêter.")
    if args.window:
        from .mac_window import run_window
        run_window(d_mac.url)
        return 0
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
