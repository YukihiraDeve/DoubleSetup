"""Agent macOS : reçoit clavier/souris du PC, échange l'audio et le presse-papiers.

    python -m setupdouble.client_mac [--config config.toml] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as config_mod
from . import protocol as P

log = logging.getLogger("setupdouble")

LOG_DIR = Path.home() / "Library" / "Logs" / "SetupDouble"
DEAD_AFTER = 10.0
CLIP_LIMIT = 60_000


class AuthError(Exception):
    pass


class RecordingInjector:
    """Injecteur factice : journalise et mémorise les événements (tests, --dry-run)."""

    def __init__(self):
        self.events: list[tuple] = []

    def key(self, scancode, down, repeat):
        self.events.append(("key", scancode, down, repeat))
        log.info("touche %#06x %s%s", scancode, "↓" if down else "↑", " (répétition)" if repeat else "")

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))
        log.debug("souris %+d %+d", dx, dy)

    def button(self, button, down):
        self.events.append(("button", button, down))
        log.info("bouton %d %s", button, "↓" if down else "↑")

    def wheel(self, dx, dy):
        self.events.append(("wheel", dx, dy))
        log.info("molette %+d %+d", dx, dy)

    def wake(self):
        self.events.append(("wake",))

    def release_all(self):
        self.events.append(("release_all",))


def discover(timeout: float = 3.0) -> tuple[str, int, str] | None:
    """Écoute les annonces UDP du PC sur le réseau local."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("", P.BEACON_PORT))
        sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, addr = sock.recvfrom(512)
            except socket.timeout:
                return None
            beacon = P.dec_beacon(packet)
            if beacon:
                return addr[0], beacon[0], beacon[1]
        return None
    finally:
        sock.close()


class Client:
    def __init__(self, cfg: dict, injector, clipboard=None, use_audio: bool = True,
                 router=None):
        self.cfg = cfg
        self.ccfg = cfg["client"]
        self.acfg = cfg["audio"]
        self.secret = cfg["secret"]
        self.name = self.ccfg["name"] or socket.gethostname().removesuffix(".local")
        self.injector = injector
        self.clipboard = clipboard if self.ccfg["clipboard"] else None
        self.use_audio = use_audio and bool(self.acfg["enabled"])
        self.router = router
        self.active = False
        self.connected_to: str | None = None
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._writer: P.FrameWriter | None = None
        self._clip_last: str | None = None
        self._udp: socket.socket | None = None
        self._sound_tx = None
        self._mic_rx = None
        self._udp_stop: threading.Event | None = None

    # --- boucle principale --------------------------------------------------------

    def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            target = self._target()
            if target is None:
                log.info("Recherche du PC sur le réseau…")
                self._stop.wait(1.0)
                continue
            host, port = target
            try:
                self.session(host, port)
                delay = 1.0
            except AuthError:
                log.error("Secret refusé par le PC : vérifie 'secret' dans les deux config.toml")
                delay = 15.0
            except (OSError, ConnectionError) as exc:
                log.info("Connexion à %s:%s impossible ou perdue : %s", host, port, exc)
                delay = min(delay * 1.5, 5.0)
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _target(self) -> tuple[str, int] | None:
        if self.ccfg["server"]:
            return self.ccfg["server"], int(self.ccfg["control_port"])
        found = discover(3.0)
        if found:
            log.info("PC trouvé : %s (%s)", found[2], found[0])
            return found[0], found[1]
        return None

    # --- session ------------------------------------------------------------------------

    def session(self, host: str, port: int) -> None:
        sock = socket.create_connection((host, port), timeout=5)
        P.tune_tcp(sock)
        self._sock = sock
        try:
            msg_type, payload = P.read_frame(sock)
            if msg_type != P.HELLO or len(payload) < 18:
                raise ConnectionError("réponse inattendue du PC")
            (version,) = struct.unpack_from(">H", payload)
            if version != P.PROTOCOL_VERSION:
                raise ConnectionError(f"version de protocole différente ({version})")
            nonce, server_name = payload[2:18], payload[18:].decode("utf-8", "replace")
            sock.sendall(P.encode(P.AUTH, P.compute_auth(self.secret, nonce)
                                  + struct.pack(">H", int(self.ccfg["audio_port"]))
                                  + self.name.encode("utf-8")))
            msg_type, payload = P.read_frame(sock)
            if msg_type == P.AUTH_FAIL:
                raise AuthError()
            if msg_type != P.AUTH_OK:
                raise ConnectionError("authentification inattendue")
            (server_audio_port,) = struct.unpack(">H", payload[:2])
            self._writer = P.FrameWriter(sock)
            self.connected_to = server_name
            log.info("Connecté à %s (%s)", server_name, host)
            if self.use_audio:
                self._start_audio(host, server_audio_port)
            sock.settimeout(DEAD_AFTER)
            try:
                self._loop(sock)
            except (OSError, ConnectionError):
                if not self._stop.is_set():
                    raise
        finally:
            self._end_session()

    def _loop(self, sock: socket.socket) -> None:
        inj = self.injector
        while not self._stop.is_set():
            msg_type, payload = P.read_frame(sock)
            if msg_type == P.MOVE:
                dx, dy = struct.unpack(">hh", payload)
                inj.move(dx, dy)
            elif msg_type == P.KEY:
                sc, flags = struct.unpack(">HB", payload)
                inj.key(sc, bool(flags & P.KEY_DOWN), bool(flags & P.KEY_REPEAT))
            elif msg_type == P.BUTTON:
                b, down = struct.unpack(">BB", payload)
                inj.button(b, bool(down))
            elif msg_type == P.WHEEL:
                dx, dy = struct.unpack(">hh", payload)
                inj.wheel(dx, dy)
            elif msg_type == P.ENTER:
                self.active = True
                log.info("Le Mac prend le clavier et la souris")
                inj.wake()
            elif msg_type == P.LEAVE:
                self.active = False
                inj.release_all()
                log.info("Le PC reprend le clavier et la souris")
                self._push_clipboard()
            elif msg_type == P.CLIPBOARD:
                text = payload.decode("utf-8", "replace")
                if self.clipboard is not None and text:
                    self.clipboard.set_text(text)
                    self._clip_last = text
            elif msg_type == P.AUDIO_CTRL:
                self._apply_audio_flags(payload[0] if payload else 0)
            elif msg_type == P.PING:
                self._send(P.encode(P.PONG))

    def _send(self, data: bytes) -> None:
        if self._writer is not None:
            self._writer.send(data)

    def _push_clipboard(self) -> None:
        if self.clipboard is None:
            return
        text = self.clipboard.get_text()
        if text and text != self._clip_last:
            self._clip_last = text
            self._send(P.enc_text(P.CLIPBOARD, text, CLIP_LIMIT))

    def _end_session(self) -> None:
        if self.active:
            self.active = False
        try:
            self.injector.release_all()
        except Exception:
            log.exception("release_all")
        self._stop_audio()
        if self.router is not None:
            self.router.restore()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._writer = None
        if self.connected_to:
            log.info("Déconnecté de %s", self.connected_to)
        self.connected_to = None

    # --- audio ------------------------------------------------------------------------

    def _start_audio(self, host: str, server_audio_port: int) -> None:
        try:
            from . import audio
        except Exception as exc:
            log.warning("Audio indisponible : %s", exc)
            return
        a = self.acfg
        rate, block = int(a["sample_rate"]), int(a["block_ms"])
        try:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp.bind(("0.0.0.0", int(self.ccfg["audio_port"])))
        except OSError as exc:
            log.error("Port audio %s indisponible : %s", self.ccfg["audio_port"], exc)
            self._udp = None
            return
        try:
            dev = audio.find_device(a["mac_capture_device"], "input")
            self._sound_tx = audio.AudioSender(
                self._udp, P.STREAM_MAC_SOUND, dev, channels=2, sample_rate=rate,
                block_ms=block)
            self._sound_tx.dest = (host, server_audio_port)
            self._sound_tx.enabled = False  # activé par AUDIO_CTRL
            self._sound_tx.start()
        except Exception as exc:
            self._sound_tx = None
            log.error("Capture du son du Mac impossible : %s", exc)
        try:
            dev = audio.find_device(a["mac_mic_device"], "output")
            self._mic_rx = audio.AudioReceiver(
                P.STREAM_MIC, dev, in_channels=1,
                device_channels=audio.device_channels(dev, "output", 2),
                sample_rate=rate, block_ms=block, latency_ms=float(a["latency_ms"]))
            self._mic_rx.allowed_ip = host
            self._mic_rx.enabled = False
            self._mic_rx.start()
            self._udp_stop = threading.Event()
            audio.udp_listener(self._udp, self._mic_rx.feed, self._udp_stop)
        except Exception as exc:
            self._mic_rx = None
            log.error("Micro virtuel indisponible : %s", exc)

    def _apply_audio_flags(self, flags: int) -> None:
        sound = bool(flags & P.AUDIO_SOUND_ON) and self._sound_tx is not None
        mic = bool(flags & P.AUDIO_MIC_ON) and self._mic_rx is not None
        if self._sound_tx is not None:
            self._sound_tx.enabled = sound
        if self._mic_rx is not None:
            self._mic_rx.enabled = mic
            self._mic_rx.reset()
        if self.router is not None:
            self.router.apply(sound, mic)
        log.info("Audio : son du Mac %s, micro %s", "on" if sound else "off", "on" if mic else "off")

    def _stop_audio(self) -> None:
        if self._udp_stop is not None:
            self._udp_stop.set()
            self._udp_stop = None
        for obj in (self._sound_tx, self._mic_rx):
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
        self._sound_tx = self._mic_rx = None
        if self._udp is not None:
            self._udp.close()
            self._udp = None


def setup_logging(level: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_DIR / "client.log", maxBytes=1_000_000, backupCount=2,
                            encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SetupDouble – agent Mac")
    parser.add_argument("--config", help="chemin du config.toml")
    parser.add_argument("--server", help="adresse du PC (sinon découverte automatique)")
    parser.add_argument("--dry-run", action="store_true",
                        help="n'injecte rien : affiche les événements reçus")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--list-devices", action="store_true",
                        help="affiche les périphériques audio et quitte")
    args = parser.parse_args(argv)

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return 0

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.server:
        cfg["client"]["server"] = args.server
    setup_logging(cfg["log_level"])

    from .mac_system import AudioRouter, MacClipboard

    if args.dry_run:
        injector = RecordingInjector()
        router = None
        clipboard = None
    else:
        from . import keymap
        from .mac_inject import MacInjector, accessibility_granted

        c = cfg["client"]
        injector = MacInjector(keymap.KeyMapper(c["modifier_layout"], c["swap_iso_keys"]),
                               float(c["mouse_speed"]), float(c["scroll_speed"]),
                               bool(c["invert_scroll"]))
        if not accessibility_granted():
            log.error(
                "Accessibilité non autorisée : le Mac ne pourra pas recevoir clavier et souris.\n"
                "Réglages Système → Confidentialité et sécurité → Accessibilité → ajoute :\n  %s",
                os.path.realpath(sys.executable))
        a = cfg["audio"]
        router = AudioRouter(a["mac_capture_device"], a["mac_mic_device"]) \
            if a["enabled"] and a["mac_auto_route"] and not args.no_audio else None
        clipboard = MacClipboard()

    client = Client(cfg, injector, clipboard=clipboard, use_audio=not args.no_audio,
                    router=router)
    try:
        client.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        if router is not None:
            router.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
