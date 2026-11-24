"""Machine pilotée (client) : reçoit clavier et souris du serveur, échange
l'audio et le presse-papiers. Fonctionne sous macOS, Windows et Linux.

    python -m setupdouble.client [--config config.toml] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import __version__
from . import config as config_mod
from . import dashboard
from . import platforms
from . import protocol as P
from .dashboard import check
from .display import DisplayLink
from .settings import SettingsMixin

log = logging.getLogger("setupdouble")

LOG_DIR = platforms.log_dir()
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


class Client(SettingsMixin):
    role = "client"
    os = platforms.OS  # remplaçable (démo)

    def __init__(self, cfg: dict, injector, clipboard=None, use_audio: bool = True,
                 router=None):
        self.cfg = cfg
        self.ccfg = cfg["client"]
        self.acfg = cfg["audio"]
        self.secret = cfg["secret"]
        self.name = self.ccfg["name"] or platforms.computer_name()
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
        self.accessibility = None  # callable -> bool : droit d'injecter ; None = rien à vérifier
        self.sleep_guard = None    # garde anti-veille (platforms.make_sleep_guard)
        self.womp_check = None     # callable -> bool | None (réveil par le réseau)
        self.restart = None        # callable : relance après changement de réglages
        self.wake_targets: list[dict] = []
        self.display = DisplayLink(cfg["display"])
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
                log.info("Recherche du serveur sur le réseau…")
                if self.link_state != "auth_failed":
                    self.link_state, self.link_detail = ("searching",
                                                         "Recherche du serveur sur le réseau…")
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
                log.error("Secret refusé par le serveur : il doit être identique sur les deux machines")
                self.link_state = "auth_failed"
                self.link_detail = ("Le serveur refuse le secret : il doit être identique sur les "
                                    "deux machines (Réglages → Connexion)")
                delay = 15.0
            except (OSError, ConnectionError) as exc:
                log.info("Connexion à %s:%s impossible ou perdue : %s", host, port, exc)
                self.link_state, self.link_detail = "error", f"Serveur injoignable ({host}) : {exc}"
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
            log.info("Serveur trouvé : %s (%s)", found[2], found[0])
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
                raise ConnectionError("réponse inattendue du serveur")
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
            self.wake_targets = platforms.network_identity()
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
                log.info("Cette machine prend le clavier et la souris")
                inj.wake()
                # renfort : le serveur a déjà demandé notre entrée à l'écran
                self.display.switch(self.display.my_input, delay=self.display.FALLBACK_DELAY,
                                    still_valid=lambda: self.active)
            elif msg_type == P.LEAVE:
                self.active = False
                self.display.switch(self._peer_display_input())
                inj.release_all()
                log.info("Le serveur reprend le clavier et la souris")
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

    def _peer_display_input(self):
        return (self.peer_info.get("display") or {}).get("input")

    def _keepalive(self, stop: threading.Event) -> None:
        """Mesure la latence et tient le serveur au courant de l'état de cette machine."""
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
        """Les périphériques audio virtuels sont-ils là ? (cache de quelques secondes)

        Valeurs : True (trouvé), False (introuvable), None (non configuré).
        """
        stamp, cached = self._devices
        if time.monotonic() - stamp < DEVICE_CACHE_S:
            return cached
        result: dict = {}
        try:
            from . import audio
            for key, name, kind in (("capture", self.acfg["client_capture_device"], "input"),
                                    ("mic", self.acfg["client_mic_device"], "output")):
                if not name:
                    result[key] = None
                    continue
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
        label, help_text = platforms.permission_help()
        if self.dry_run:
            checks.append(check("permission", label, "warn", "Mode test (--dry-run) : rien n'est injecté"))
        elif self.accessibility is not None:
            if self.accessibility():
                checks.append(check("permission", label, "ok", "Autorisé"))
            else:
                checks.append(check("permission", label, "error", help_text))
        checks += self._sleep_checks()
        if self.display.enabled:
            checks.append(self.display.check())
        if not self.use_audio:
            checks.append(check("client_audio", "Audio", "off", "Désactivé"))
            return checks
        dev = self._device_presence()
        for key, cid, label, cfg_key in (
                ("capture", "client_capture", "Capture du son", "client_capture_device"),
                ("mic", "client_mic", "Micro virtuel", "client_mic_device")):
            name = self.acfg[cfg_key]
            if dev.get(key) is None:
                checks.append(check(cid, label, "off", "Non configuré (Réglages → Audio)"))
            elif dev[key]:
                checks.append(check(cid, label, "ok", name))
            else:
                checks.append(check(cid, label, "error",
                                    f"« {name} » introuvable : choisis-en un dans Réglages → Audio"))
        if self.router is not None:
            if self.router.tool is None:
                checks.append(check("client_route", "Routage audio", "warn",
                                    "SwitchAudioSource absent : choisis BlackHole à la main"))
            else:
                checks.append(check("client_route", "Routage audio", "ok", "Automatique"))
        return checks

    def _sleep_checks(self) -> list[dict]:
        checks = []
        guard = self.sleep_guard
        if guard is not None:
            if guard.mode == "never":
                checks.append(check("client_sleep", "Veille", "off",
                                    "Peut s'endormir (Réglages → Veille)"))
            else:
                where = "sur secteur" if guard.mode == "power" else "même sur batterie"
                checks.append(check("client_sleep", "Veille", "ok",
                                    f"Reste joignable {where} : le raccourci rallume l'écran"))
        if self.womp_check is not None:
            stamp, womp = getattr(self, "_womp_cache", (0.0, None))
            if time.monotonic() - stamp > 30:
                womp = self.womp_check()
                self._womp_cache = (time.monotonic(), womp)
            if womp:
                checks.append(check("client_wake", "Réveil par le réseau", "ok",
                                    "Le serveur peut réveiller cette machine"))
            elif womp is False:
                checks.append(check("client_wake", "Réveil par le réseau", "warn",
                                    "Désactivé : sudo pmset -a womp 1"))
        return checks

    def _mic_flowing(self) -> bool:
        from .audio import flowing
        rx = self._mic_rx
        return rx is not None and rx.enabled and flowing(rx.last_packet)

    def info(self) -> dict:
        return {"os": self.os, "name": self.name, "checks": self.local_checks(),
                "flows": {"mic_rx": self._mic_flowing()}, "wake": self.wake_targets,
                "display": self.display.info()}

    def status(self) -> dict:
        connected = self.link_state == "connected"
        peer = self.peer_info
        if connected and self.rtt_ms is not None:
            detail = f"{self.link_detail} · {self.rtt_ms:.0f} ms"
        else:
            detail = self.link_detail
        flags = self.audio_flags
        live_sound = None if flags is None else bool(flags & P.AUDIO_SOUND_ON)
        live_mic = None if flags is None else bool(flags & P.AUDIO_MIC_ON)
        master = peer.get("audio_master") if connected else None
        if isinstance(master, dict):  # interrupteurs du serveur (indépendants du mode « suivre »)
            sound, mic = bool(master.get("sound")), bool(master.get("mic"))
        elif flags is not None:
            sound, mic = bool(live_sound), bool(live_mic)
        else:
            sound, mic = bool(self.acfg["sound"]), bool(self.acfg["mic"])
        checks = [dict(c, machine="server") for c in peer.get("checks", [])] if connected else []
        checks += [dict(c, machine="client") for c in self.local_checks()]
        return {
            "role": "client",
            "version": __version__,
            "machines": {
                "server": {"name": peer.get("name") or self.connected_to or "Serveur",
                           "ip": self.server_ip, "here": False, "os": peer.get("os", "")},
                "client": {"name": self.name, "ip": None, "here": True, "os": self.os},
            },
            "link": {"state": self.link_state, "detail": detail, "rtt_ms": self.rtt_ms,
                     "since": self.since},
            "active": "client" if self.active else "server",
            "hotkey": peer.get("hotkey", ""),
            "audio": {
                "enabled": self.use_audio,
                "sound": sound, "mic": mic,
                "sound_mode": peer.get("sound_mode", self.acfg["sound_mode"]),
                "mic_mode": peer.get("mic_mode", self.acfg["mic_mode"]),
                "sound_live": live_sound, "mic_live": live_mic,
                "sound_flow": peer.get("flows", {}).get("sound_rx") if connected else None,
                "mic_flow": self._mic_flowing() if connected else None,
            },
            "checks": checks,
            "peer_reported": bool(peer),
            "can_wake": False,
        }

    def action(self, name: str, value=None) -> tuple[bool, str]:
        name = {"mac": "client", "pc": "server"}.get(name, name)  # anciens noms
        if name == "open_log":
            log_file = LOG_DIR / "client.log"
            if not log_file.exists():
                return False, "Pas encore de journal : l'agent n'a jamais tourné sur cette machine."
            platforms.open_path(log_file)
            return True, ""
        if name == "display_test":
            return self.display.test_round_trip(self._peer_display_input())
        if self.link_state != "connected" or self._writer is None:
            return False, "Pas connecté au serveur."
        try:
            if name in ("client", "server", "toggle"):
                remote = name == "client" or (name == "toggle" and not self.active)
                self._send(P.encode(P.REQ_MODE, bytes([1 if remote else 0])))
                return True, ""
            if name in ("sound", "mic"):
                if not self.use_audio:
                    return False, "L'audio est désactivé sur cette machine."
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
            if not a["client_capture_device"]:
                raise RuntimeError("capture du son non configurée")
            dev = audio.find_device(a["client_capture_device"], "input")
            self._sound_tx = audio.AudioSender(
                self._udp, P.STREAM_MAC_SOUND, dev, channels=2, sample_rate=rate,
                block_ms=block)
            self._sound_tx.dest = (host, server_audio_port)
            self._sound_tx.enabled = False  # activé par AUDIO_CTRL
            self._sound_tx.start()
        except Exception as exc:
            self._sound_tx = None
            log.warning("Son de cette machine non envoyé : %s", exc)
        try:
            if not a["client_mic_device"]:
                raise RuntimeError("micro virtuel non configuré")
            dev = audio.find_device(a["client_mic_device"], "output")
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
            log.warning("Micro du serveur non reçu : %s", exc)

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
        log.info("Audio : son envoyé %s, micro reçu %s", "on" if sound else "off",
                 "on" if mic else "off")

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
    parser = argparse.ArgumentParser(description="SetupDouble – machine pilotée")
    parser.add_argument("--config", help="chemin du config.toml")
    parser.add_argument("--server", help="adresse du serveur (sinon découverte automatique)")
    parser.add_argument("--dry-run", action="store_true",
                        help="n'injecte rien : affiche les événements reçus")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--list-devices", action="store_true",
                        help="affiche les périphériques audio et quitte")
    parser.add_argument("--wait-instance", action="store_true",
                        help="attend la fin de l'instance précédente (redémarrage)")
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
    if not platforms.single_instance("Client", wait=20.0 if args.wait_instance else 0.0):
        log.error("SetupDouble (machine pilotée) tourne déjà")
        return 1

    if args.dry_run:
        injector, router, clipboard = RecordingInjector(), None, None
    else:
        injector = platforms.make_injector(cfg["client"])
        if not platforms.request_input_permission():  # macOS : affiche la demande
            label, help_text = platforms.permission_help()
            log.error("%s : %s", label, help_text)
        router = None if args.no_audio or not cfg["audio"]["enabled"] \
            else platforms.make_audio_router(cfg["audio"])
        clipboard = platforms.make_clipboard()

    client = Client(cfg, injector, clipboard=clipboard, use_audio=not args.no_audio,
                    router=router)
    client.dry_run = args.dry_run
    if not args.dry_run:
        client.accessibility = platforms.input_permission()
        client.sleep_guard = platforms.make_sleep_guard(cfg["client"]["prevent_sleep"])
        client.womp_check = platforms.wake_setting_check()

    def restart():
        client.stop()
        if router is not None:
            router.restore()
        extra = ["--config", cfg["_path"]] + (["--dry-run"] if args.dry_run else [])
        platforms.restart_self("setupdouble.client", extra)

    client.restart = restart
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
