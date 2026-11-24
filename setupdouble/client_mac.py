"""Agent macOS : reçoit clavier/souris du PC, échange l'audio et le presse-papiers.

    python -m setupdouble.client_mac [--config config.toml] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import dashboard
from . import protocol as P
from .dashboard import check

log = logging.getLogger("setupdouble")

LOG_DIR = Path.home() / "Library" / "Logs" / "SetupDouble"
DEAD_AFTER = 10.0
CLIP_LIMIT = 60_000
KEEPALIVE = 2.0
DEVICE_CACHE_S = 5.0


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


def _computer_name() -> str:
    """Nom du Mac tel qu'affiché dans Réglages (« MacBook Pro de … »)."""
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                             text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    host = socket.gethostname()
    for suffix in (".local", ".home", ".lan"):
        host = host.removesuffix(suffix)
    return host


class Client:
    def __init__(self, cfg: dict, injector, clipboard=None, use_audio: bool = True,
                 router=None):
        self.cfg = cfg
        self.ccfg = cfg["client"]
        self.acfg = cfg["audio"]
        self.secret = cfg["secret"]
        self.name = self.ccfg["name"] or _computer_name()
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
        # --- état lisible par le tableau de bord
        self.link_state = "searching"  # searching, connecting, connected, auth_failed, error
        self.link_detail = ""
        self.server_ip: str | None = None
        self.since: float | None = None
        self.rtt_ms: float | None = None
        self.peer_info: dict = {}
        self.audio_flags: int | None = None  # dernier AUDIO_CTRL reçu
        self.accessibility = None  # callable -> bool, None = mode test
        self.sleep_guard = None    # mac_system.SleepGuard
        self.womp_check = None     # callable -> bool | None (réveil par le réseau)
        self.wake_targets: list[dict] = []
        self.dry_run = False
        self._ping_sent = 0.0
        self._info_sent: str | None = None
        self._session_stop: threading.Event | None = None
        self._devices: tuple[float, dict] = (0.0, {})

    # --- boucle principale --------------------------------------------------------

    def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            target = self._target()
            if target is None:
                log.info("Recherche du PC sur le réseau…")
                if self.link_state != "auth_failed":
                    self.link_state, self.link_detail = "searching", "Recherche du PC sur le réseau…"
                self._stop.wait(1.0)
                continue
            host, port = target
            self.server_ip = host
            if self.link_state not in ("auth_failed", "error"):
                self.link_state, self.link_detail = "connecting", f"Connexion à {host}…"
            try:
                self.session(host, port)
                delay = 1.0
                self.link_state, self.link_detail = "searching", "Connexion perdue, reconnexion…"
            except AuthError:
                log.error("Secret refusé par le PC : vérifie 'secret' dans les deux config.toml")
                self.link_state = "auth_failed"
                self.link_detail = "Le PC refuse le secret : il doit être identique dans les deux config.toml"
                delay = 15.0
            except (OSError, ConnectionError) as exc:
                log.info("Connexion à %s:%s impossible ou perdue : %s", host, port, exc)
                self.link_state, self.link_detail = "error", f"PC injoignable ({host}) : {exc}"
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
        self.server_ip = host
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
            self.link_state, self.link_detail = "connected", f"{server_name} ({host})"
            self.since = time.time()
            from .mac_system import network_identity
            self.wake_targets = network_identity()
            if self.sleep_guard is not None:
                self.sleep_guard.start()
            log.info("Connecté à %s (%s)", server_name, host)
            if self.use_audio:
                self._start_audio(host, server_audio_port)
            self._session_stop = threading.Event()
            threading.Thread(target=self._keepalive, args=(self._session_stop,),
                             name="keepalive", daemon=True).start()
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
                self.audio_flags = payload[0] if payload else 0
                self._apply_audio_flags(self.audio_flags)
            elif msg_type == P.PING:
                self._send(P.encode(P.PONG))
            elif msg_type == P.PONG:
                if self._ping_sent:
                    self.rtt_ms = (time.monotonic() - self._ping_sent) * 1000
            elif msg_type == P.INFO:
                try:
                    info = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(info, dict):
                    self.peer_info = info

    def _keepalive(self, stop: threading.Event) -> None:
        """Mesure la latence et tient le PC au courant de l'état du Mac."""
        try:
            self._send_info()
            while not stop.wait(KEEPALIVE):
                self._ping_sent = time.monotonic()
                self._send(P.encode(P.PING))
                self._send_info()
        except OSError:
            return

    def _send_info(self) -> None:
        data = json.dumps(self.info(), ensure_ascii=False)
        if data != self._info_sent:
            self._info_sent = data
            self._send(P.enc_text(P.INFO, data))

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
        if self.sleep_guard is not None:
            self.sleep_guard.stop()
        if self._session_stop is not None:
            self._session_stop.set()
            self._session_stop = None
        self.rtt_ms = None
        self.since = None
        self.peer_info = {}
        self.audio_flags = None
        self._info_sent = None
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

    # --- tableau de bord ------------------------------------------------------------------

    def _device_presence(self) -> dict:
        """Les BlackHole sont-ils installés ? (mis en cache quelques secondes)"""
        stamp, cached = self._devices
        if time.monotonic() - stamp < DEVICE_CACHE_S:
            return cached
        result = {}
        try:
            from . import audio
            for key, name, kind in (("capture", self.acfg["mac_capture_device"], "input"),
                                    ("mic", self.acfg["mac_mic_device"], "output")):
                try:
                    audio.find_device(name, kind)
                    result[key] = True
                except Exception:
                    result[key] = False
        except Exception:  # sounddevice absent
            result = {"capture": False, "mic": False}
        self._devices = (time.monotonic(), result)
        return result

    def local_checks(self) -> list[dict]:
        checks = []
        if self.accessibility is None:
            checks.append(check("accessibility", "Accessibilité", "warn",
                                "Mode test (--dry-run) : rien n'est injecté"))
        elif self.accessibility():
            checks.append(check("accessibility", "Accessibilité", "ok", "Autorisée"))
        else:
            checks.append(check("accessibility", "Accessibilité", "error",
                                "À autoriser : Réglages Système → Confidentialité et sécurité "
                                "→ Accessibilité → " + os.path.realpath(sys.executable)))
        checks += self._sleep_checks()
        if not self.use_audio:
            checks.append(check("mac_audio", "Audio", "off", "Désactivé"))
            return checks
        dev = self._device_presence()
        cap, mic = self.acfg["mac_capture_device"], self.acfg["mac_mic_device"]
        checks.append(check("mac_capture", "Capture du son", "ok" if dev["capture"] else "error",
                            cap if dev["capture"]
                            else f"{cap} introuvable : ./scripts/install_mac.sh"))
        checks.append(check("mac_mic", "Micro virtuel", "ok" if dev["mic"] else "error",
                            mic if dev["mic"] else f"{mic} introuvable : ./scripts/install_mac.sh"))
        if self.router is not None:
            if self.router.tool is None:
                checks.append(check("mac_route", "Routage audio", "warn",
                                    "SwitchAudioSource absent : choisis BlackHole à la main"))
            else:
                checks.append(check("mac_route", "Routage audio", "ok", "Automatique"))
        return checks

    def _sleep_checks(self) -> list[dict]:
        checks = []
        guard = self.sleep_guard
        if guard is not None:
            if guard.mode == "never":
                checks.append(check("mac_sleep", "Veille", "off",
                                    "Le Mac peut s'endormir (prevent_sleep = \"never\")"))
            else:
                where = "sur secteur" if guard.mode == "power" else "même sur batterie"
                checks.append(check("mac_sleep", "Veille", "ok",
                                    f"Reste joignable {where} : le raccourci rallume l'écran"))
        if self.womp_check is not None:
            stamp, womp = getattr(self, "_womp_cache", (0.0, None))
            if time.monotonic() - stamp > 30:
                womp = self.womp_check()
                self._womp_cache = (time.monotonic(), womp)
            if womp:
                checks.append(check("mac_wake", "Réveil par le réseau", "ok",
                                    "Le PC peut réveiller le Mac endormi"))
            elif womp is False:
                checks.append(check("mac_wake", "Réveil par le réseau", "warn",
                                    "Désactivé : sudo pmset -a womp 1"))
        return checks

    def _mic_flowing(self) -> bool:
        from .audio import flowing
        rx = self._mic_rx
        return rx is not None and rx.enabled and flowing(rx.last_packet)

    def info(self) -> dict:
        return {"os": "mac", "name": self.name, "checks": self.local_checks(),
                "flows": {"mic_rx": self._mic_flowing()}, "wake": self.wake_targets}

    def status(self) -> dict:
        connected = self.link_state == "connected"
        peer = self.peer_info
        if connected and self.rtt_ms is not None:
            detail = f"{self.link_detail} · {self.rtt_ms:.0f} ms"
        else:
            detail = self.link_detail
        flags = self.audio_flags
        if flags is None:
            sound, mic = bool(self.acfg["mac_sound"]), bool(self.acfg["mic"])
        else:
            sound, mic = bool(flags & P.AUDIO_SOUND_ON), bool(flags & P.AUDIO_MIC_ON)
        checks = [dict(c, machine="pc") for c in peer.get("checks", [])] if connected else []
        checks += [dict(c, machine="mac") for c in self.local_checks()]
        return {
            "role": "mac",
            "version": __version__,
            "machines": {
                "pc": {"name": peer.get("name") or self.connected_to or "PC Windows",
                       "ip": self.server_ip, "here": False},
                "mac": {"name": self.name, "ip": None, "here": True},
            },
            "link": {"state": self.link_state, "detail": detail, "rtt_ms": self.rtt_ms,
                     "since": self.since},
            "active": "mac" if self.active else "pc",
            "hotkey": peer.get("hotkey", ""),
            "audio": {
                "enabled": self.use_audio,
                "sound": sound, "mic": mic,
                "sound_flow": peer.get("flows", {}).get("sound_rx") if connected else None,
                "mic_flow": self._mic_flowing() if connected else None,
            },
            "checks": checks,
            "peer_reported": bool(peer),
        }

    def action(self, name: str, value=None) -> tuple[bool, str]:
        if name == "open_log":
            log_file = LOG_DIR / "client.log"
            if not log_file.exists():
                return False, "Pas encore de journal : l'agent n'a jamais tourné sur ce Mac."
            subprocess.Popen(["open", str(log_file)])
            return True, ""
        if self.link_state != "connected" or self._writer is None:
            return False, "Le Mac n'est pas connecté au PC."
        try:
            if name in ("mac", "pc", "toggle"):
                remote = name == "mac" or (name == "toggle" and not self.active)
                self._send(P.encode(P.REQ_MODE, bytes([1 if remote else 0])))
                return True, ""
            if name in ("sound", "mic"):
                if not self.use_audio:
                    return False, "L'audio est désactivé sur le Mac."
                st = self.status()["audio"]
                sound = bool(value) if name == "sound" else st["sound"]
                mic = bool(value) if name == "mic" else st["mic"]
                flags = (P.AUDIO_SOUND_ON if sound else 0) | (P.AUDIO_MIC_ON if mic else 0)
                self._send(P.encode(P.REQ_AUDIO, bytes([flags])))
                return True, ""
        except OSError as exc:
            return False, f"Envoi impossible : {exc}"
        return False, f"Action inconnue : {name}"

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

    accessibility = None
    if args.dry_run:
        injector = RecordingInjector()
        router = None
        clipboard = None
    else:
        from . import keymap
        from .mac_inject import MacInjector, accessibility_granted, request_accessibility

        c = cfg["client"]
        injector = MacInjector(keymap.KeyMapper(c["modifier_layout"], c["swap_iso_keys"]),
                               float(c["mouse_speed"]), float(c["scroll_speed"]),
                               bool(c["invert_scroll"]))
        accessibility = accessibility_granted
        if not request_accessibility():  # affiche la demande de macOS si besoin
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
    client.accessibility = accessibility
    client.dry_run = args.dry_run
    if not args.dry_run:
        from .mac_system import SleepGuard, wake_on_lan_enabled
        client.sleep_guard = SleepGuard(cfg["client"]["prevent_sleep"])
        client.womp_check = wake_on_lan_enabled
    dashboard.start(client, int(cfg["client"]["ui_port"]))
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
