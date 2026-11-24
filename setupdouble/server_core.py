"""Cœur du serveur (la machine où sont branchés clavier, souris et carte son) :
réseau, session avec la machine pilotée, audio, état.

Indépendant du système : la capture clavier/souris (``platforms.make_capture``)
et le presse-papiers sont injectés de l'extérieur.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from . import __version__
from . import platforms
from . import protocol as P
from .dashboard import check
from .display import DisplayLink
from .hotkeys import pretty
from .settings import SettingsMixin

log = logging.getLogger(__name__)

PING_INTERVAL = 2.0
DEAD_AFTER = 8.0
STALE_AFTER = 4.5    # plus de réponse aux pings : le Mac s'est probablement endormi
WAKE_TIMEOUT = 30.0  # temps laissé au Mac pour se réveiller et se reconnecter
_STOP = ("stop",)


class InputLike(Protocol):
    def request_mode(self, remote: bool) -> None: ...


class Clipboard(Protocol):
    def get_text(self) -> str | None: ...
    def set_text(self, text: str) -> bool: ...


class Session:
    def __init__(self, sock: socket.socket, addr, name: str, audio_port: int):
        self.sock = sock
        self.addr = addr
        self.name = name
        self.audio_port = audio_port
        self.writer = P.FrameWriter(sock)
        self.last_rx = time.monotonic()
        self.since = time.time()
        self.ping_sent = 0.0
        self.rtt_ms: float | None = None
        self.peer_info: dict = {}
        self.info_sent: str | None = None
        self.closed = False

    def send(self, data: bytes) -> None:
        self.writer.send(data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()


class Server(SettingsMixin):
    role = "server"
    os = platforms.OS  # remplaçable (démo)

    def __init__(self, cfg: dict, clipboard: Clipboard | None = None,
                 notify: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.scfg = cfg["server"]
        self.acfg = cfg["audio"]
        self.secret = cfg["secret"]
        self.name = self.scfg["name"] or socket.gethostname()
        self.clipboard = clipboard if self.scfg["clipboard"] else None
        self.notify = notify or (lambda msg: log.info("%s", msg))
        self.input: InputLike | None = None
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.session: Session | None = None
        self.remote = False
        self.sound_on = bool(self.acfg["sound"])
        self.mic_on = bool(self.acfg["mic"])
        self.audio_status = "désactivé"
        self.sound_device = ""  # nom du périphérique ouvert, pour l'interface
        self.mic_device = ""
        self.sound_error = ""
        self.mic_error = ""
        self.hotkey_text = self.scfg["hotkey_toggle"] or self.scfg["hotkey_remote"]
        self.hotkey_check: Callable[[], dict] | None = None  # fourni par server_win
        self.open_log: Callable[[], None] | None = None
        self.restart: Callable[[], None] | None = None  # relance après changement de réglages
        self.wake_store: Path | None = None  # où retenir l'adresse du Mac (server_win)
        self.wake_targets: list[dict] = []
        self.display = DisplayLink(cfg["display"])
        self._wake_until = 0.0
        self.listeners: list[Callable[[], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._sound_rx = None  # audio.AudioReceiver
        self._mic_tx = None    # audio.AudioSender
        self.bound_port = 0

    # --- Cycle de vie -----------------------------------------------------------

    def start(self) -> None:
        self._load_wake_targets()
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((self.scfg["bind"], int(self.scfg["control_port"])))
        self._tcp.listen(2)
        self.bound_port = self._tcp.getsockname()[1]
        log.info("Serveur à l'écoute sur %s:%s", self.scfg["bind"], self.bound_port)
        self._thread(self._accept_loop, "accept")
        self._thread(self._sender_loop, "sender")
        self._thread(self._ping_loop, "ping")
        if self.scfg["beacon"]:
            self._thread(self._beacon_loop, "beacon")
        if self.acfg["enabled"]:
            self._start_audio()

    def stop(self) -> None:
        self._stop.set()
        if self.input is not None and self.remote:
            self.input.request_mode(False)
        self.events.put(_STOP)
        with self._lock:
            if self.session:
                self.session.close()
        for s in (self._tcp, self._udp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        for a in (self._sound_rx, self._mic_tx):
            if a is not None:
                try:
                    a.stop()
                except Exception:
                    pass

    def _thread(self, target, name: str) -> threading.Thread:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        return t

    def _changed(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:
                log.exception("listener")

    # --- État lisible par l'interface ---------------------------------------------

    @property
    def connected(self) -> bool:
        return self.session is not None

    def status_text(self) -> str:
        s = self.session
        if s is None:
            return "Réveil de la machine pilotée…" if self.waking \
                else "En attente de la machine pilotée…"
        where = f"Contrôle : {s.name}" if self.remote else "Contrôle : cet ordinateur"
        return f"{where} · {s.name} ({s.addr[0]})"

    def local_checks(self) -> list[dict]:
        checks = []
        if self.hotkey_check is not None:
            checks.append(self.hotkey_check())
        if self.display.enabled:
            checks.append(self.display.check())
        if not self.acfg["enabled"]:
            checks.append(check("server_audio", "Audio", "off", "Désactivé (Réglages → Audio)"))
            return checks
        checks.append(check("server_out", "Sortie audio (casque)",
                            "ok" if self._sound_rx is not None else "error",
                            self.sound_device or self.sound_error or "Démarrage…"))
        checks.append(check("server_in", "Entrée audio (micro)",
                            "ok" if self._mic_tx is not None else "error",
                            self.mic_device or self.mic_error or "Démarrage…"))
        return checks

    def _sound_flowing(self) -> bool:
        from .audio import flowing
        rx = self._sound_rx
        return rx is not None and flowing(rx.last_packet)

    def info(self) -> dict:
        """Ce que la machine pilotée a besoin de savoir pour son interface."""
        return {"os": self.os, "name": self.name, "hotkey": pretty(self.hotkey_text),
                "checks": self.local_checks(), "flows": {"sound_rx": self._sound_flowing()},
                "audio_master": {"sound": self.sound_on, "mic": self.mic_on},
                "sound_mode": self.acfg["sound_mode"], "mic_mode": self.acfg["mic_mode"],
                "display": self.display.info(), "pointer": self._pointer_info()}

    def status(self) -> dict:
        """État complet pour le tableau de bord (voir dashboard.py)."""
        s = self.session
        peer = s.peer_info if s else {}
        if self.waking:
            link = {"state": "waking", "detail": "Réveil de la machine pilotée…",
                    "rtt_ms": None, "since": None}
        elif s is not None:
            rtt = f" · {s.rtt_ms:.0f} ms" if s.rtt_ms is not None else ""
            link = {"state": "connected", "detail": f"{s.name} ({s.addr[0]}){rtt}",
                    "rtt_ms": s.rtt_ms, "since": s.since}
        else:
            link = {"state": "waiting", "detail": "En attente de la machine pilotée…",
                    "rtt_ms": None, "since": None}
        checks = [dict(c, machine="server") for c in self.local_checks()]
        checks += [dict(c, machine="client") for c in peer.get("checks", [])]
        return {
            "role": "server",
            "version": __version__,
            "machines": {
                "server": {"name": self.name, "ip": None, "here": True, "os": self.os},
                "client": {"name": s.name if s else "Machine pilotée",
                           "ip": s.addr[0] if s else None, "here": False,
                           "os": peer.get("os", "")},
            },
            "link": link,
            "active": "client" if self.remote else "server",
            "hotkey": pretty(self.hotkey_text),
            "audio": {
                "enabled": bool(self.acfg["enabled"]),
                "sound": self.sound_on, "mic": self.mic_on,
                "sound_mode": self.acfg["sound_mode"], "mic_mode": self.acfg["mic_mode"],
                "sound_live": self._effective_sound(), "mic_live": self._effective_mic(),
                "sound_flow": self._sound_flowing() if s else None,
                "mic_flow": peer.get("flows", {}).get("mic_rx") if s else None,
            },
            "checks": checks,
            "peer_reported": bool(peer),
            "can_wake": self.can_wake,
        }

    def action(self, name: str, value=None) -> tuple[bool, str]:
        """Actions déclenchées depuis le tableau de bord."""
        name = {"mac": "client", "pc": "server"}.get(name, name)  # anciens noms
        if name in ("client", "toggle") and self.session is None \
                and (name == "client" or not self.remote):
            if self.wake_mac():
                return True, "Réveil de la machine pilotée… la bascule se fera dès qu'elle répond."
            return False, "La machine pilotée n'est pas connectée."
        if name in ("client", "server", "toggle"):
            self.on_hotkey({"client": "remote", "server": "local"}.get(name, name))
            return True, ""
        if name in ("sound", "mic"):
            if not self.acfg["enabled"]:
                return False, "L'audio est désactivé (Réglages → Audio)."
            (self.set_sound if name == "sound" else self.set_mic)(bool(value))
            return True, ""
        if name == "display_test":
            return self.display.test_round_trip(self._peer_display_input())
        if name == "open_log" and self.open_log is not None:
            self.open_log()
            return True, ""
        return False, f"Action inconnue : {name}"

    # --- Entrées venant de la capture (thread win-input) -------------------------

    def sink(self, event: tuple) -> None:
        self.events.put(event)

    def on_hotkey(self, action: str) -> None:
        if action == "toggle":
            self.request_mode(not self.remote)
        elif action in ("remote", "mac"):
            self.request_mode(True)
        elif action in ("local", "pc"):
            self.request_mode(False)

    def on_mode(self, remote: bool) -> None:
        self.remote = remote
        self.events.put(("enter",) if remote else ("leave",))
        self._switch_display(remote)
        if self.acfg["sound_mode"] == "follow" or self.acfg["mic_mode"] == "follow":
            self._apply_audio()
        self._changed()

    def _pointer_info(self) -> dict:
        """Réglages souris de cette machine (vitesse, accélération, écran), relus toutes les 30 s."""
        from .pointer import local_pointer_info
        stamp, info = getattr(self, "_pointer_cache", (0.0, None))
        if info is None or time.monotonic() - stamp > 30:
            try:
                info = local_pointer_info()
            except Exception:
                info = {}
            self._pointer_cache = (time.monotonic(), info)
        return info

    def _peer_display_input(self):
        s = self.session
        return ((s.peer_info.get("display") or {}).get("input") if s else None)

    def _switch_display(self, remote: bool) -> None:
        """L'écran suit le clavier : la machine qu'on quitte envoie la commande, celle qui
        arrive la renvoie en renfort (certains docks bloquent le DDC d'un côté)."""
        if not self.display.enabled:
            return
        if remote:
            self.display.switch(self._peer_display_input())
        else:
            self.display.switch(self.display.my_input,
                                delay=self.display.FALLBACK_DELAY if self.session else 0.0,
                                still_valid=lambda: not self.remote)

    def request_mode(self, remote: bool) -> None:
        s = self.session
        if remote and s is not None and not self.remote \
                and time.monotonic() - s.last_rx > STALE_AFTER and self.can_wake:
            # le Mac ne répond plus aux pings : il vient sans doute de s'endormir
            log.info("Le Mac ne répond plus depuis %.0f s : réveil",
                     time.monotonic() - s.last_rx)
            self.wake_mac()
            return
        if remote and self.session is None:
            if not self.wake_mac():
                self.notify("La machine pilotée n'est pas connectée.")
            return
        if not remote:
            self._wake_until = 0.0  # on abandonne un réveil en cours
        if self.input is not None:
            self.input.request_mode(remote)

    # --- Réveil du Mac (Wake-on-LAN) ---------------------------------------------------

    @property
    def can_wake(self) -> bool:
        return bool(self.scfg.get("wake_on_lan", True) and self.wake_targets)

    @property
    def waking(self) -> bool:
        return self._wake_until > time.monotonic()

    def wake_mac(self) -> bool:
        """Envoie le paquet magique et bascule dès que le Mac se reconnecte."""
        if not self.can_wake:
            return False
        first = not self.waking
        self._wake_until = time.monotonic() + WAKE_TIMEOUT
        self._send_magic()
        if first:
            log.info("Réveil de la machine pilotée demandé")
            self.notify("Réveil de la machine pilotée…")
            self._changed()
        return True

    def _send_magic(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for t in self.wake_targets:
                try:
                    packet = P.magic_packet(t["mac"])
                except (KeyError, ValueError):
                    continue
                dests = {"255.255.255.255", t.get("broadcast") or "", t.get("ip") or ""}
                for host in filter(None, dests):
                    for port in P.WOL_PORTS:
                        try:
                            sock.sendto(packet, (host, port))
                        except OSError:
                            pass
        finally:
            sock.close()

    def _load_wake_targets(self) -> None:
        if self.wake_store is None or not self.wake_store.exists():
            return
        try:
            data = json.loads(self.wake_store.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.wake_targets = [t for t in data if isinstance(t, dict) and t.get("mac")]
        except (OSError, ValueError):
            pass

    def _remember_wake_targets(self, targets) -> None:
        if not isinstance(targets, list):
            return
        clean = [{k: str(t.get(k, "")) for k in ("iface", "mac", "ip", "broadcast")}
                 for t in targets if isinstance(t, dict) and t.get("mac")]
        if not clean or clean == self.wake_targets:
            return
        self.wake_targets = clean
        if self.wake_store is not None:
            try:
                self.wake_store.parent.mkdir(parents=True, exist_ok=True)
                self.wake_store.write_text(json.dumps(clean, indent=2), encoding="utf-8")
                log.info("Adresse de réveil du Mac enregistrée : %s",
                         ", ".join(t["mac"] for t in clean))
            except OSError as exc:
                log.warning("Impossible d'enregistrer l'adresse du Mac : %s", exc)

    # --- Audio ----------------------------------------------------------------------

    def _start_audio(self) -> None:
        try:
            from . import audio
        except Exception as exc:  # numpy/sounddevice absents
            self.audio_status = f"indisponible ({exc})"
            log.warning("Audio indisponible : %s", exc)
            return
        a = self.acfg
        rate, block = int(a["sample_rate"]), int(a["block_ms"])
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind((self.scfg["bind"], int(self.scfg["audio_port"])))
        errors = []
        try:
            out_dev = audio.find_device(a["server_output_device"], "output",
                                        a["server_hostapi"], empty="default")
            self._sound_rx = audio.AudioReceiver(
                P.STREAM_MAC_SOUND, out_dev, in_channels=2,
                device_channels=audio.device_channels(out_dev, "output", 2),
                sample_rate=rate, block_ms=block, latency_ms=float(a["latency_ms"]),
                gain=float(a["sound_gain"]))
            self._sound_rx.enabled = self._effective_sound()
            self._sound_rx.start()
            self.sound_device = audio.device_name(out_dev)
        except Exception as exc:
            self._sound_rx = None
            self.sound_error = str(exc)
            errors.append(f"sortie : {exc}")
        try:
            ch = int(a["mic_channel"])
            in_dev = audio.find_device(a["server_input_device"], "input",
                                       a["server_hostapi"], empty="default")
            channels = audio.device_channels(in_dev, "input", max(2, ch))
            self._mic_tx = audio.AudioSender(
                self._udp, P.STREAM_MIC, in_dev, channels=channels, sample_rate=rate,
                block_ms=block, select_channel=min(ch, channels) - 1,
                gain=float(a["mic_gain"]))
            self._mic_tx.enabled = self._effective_mic()
            self._mic_tx.start()
            self.mic_device = f"{audio.device_name(in_dev)} · entrée {ch}"
        except Exception as exc:
            self._mic_tx = None
            self.mic_error = str(exc)
            errors.append(f"micro : {exc}")
        stop = self._stop
        if self._sound_rx is not None:
            audio.udp_listener(self._udp, self._sound_rx.feed, stop)
        if errors:
            self.audio_status = "partiel : " + " ; ".join(errors)
            log.error("Audio : %s", self.audio_status)
        else:
            self.audio_status = "actif"
            log.info("Audio actif")

    def set_sound(self, on: bool) -> None:
        self.sound_on = on
        self._apply_audio()
        self._changed()

    def set_mic(self, on: bool) -> None:
        self.mic_on = on
        self._apply_audio()
        self._changed()

    def _effective_sound(self) -> bool:
        """Son de la machine pilotée dans le casque : toujours (mix) ou si pilotée (follow)."""
        return bool(self.acfg["enabled"] and self.sound_on
                    and (self.acfg["sound_mode"] != "follow" or self.remote))

    def _effective_mic(self) -> bool:
        return bool(self.acfg["enabled"] and self.mic_on
                    and (self.acfg["mic_mode"] != "follow" or self.remote))

    def _apply_audio(self) -> None:
        sound, mic = self._effective_sound(), self._effective_mic()
        if self._sound_rx is not None and self._sound_rx.enabled != sound:
            self._sound_rx.enabled = sound
            self._sound_rx.reset()
        if self._mic_tx is not None:
            self._mic_tx.enabled = mic
        self._send_audio_ctrl()
        session = self.session
        if session is not None:
            self._send_info(session)  # l'interface de l'autre machine suit tout de suite

    def _audio_flags(self) -> int:
        return ((P.AUDIO_SOUND_ON if self._effective_sound() else 0)
                | (P.AUDIO_MIC_ON if self._effective_mic() else 0))

    def _send_audio_ctrl(self) -> None:
        self.events.put(("raw", P.encode(P.AUDIO_CTRL, bytes([self._audio_flags()]))))

    # --- Réseau -----------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._tcp.accept()
            except OSError:
                break
            self._thread(lambda s=sock, a=addr: self._handle(s, a), "session")

    def _handle(self, sock: socket.socket, addr) -> None:
        P.tune_tcp(sock)
        sock.settimeout(5)
        try:
            nonce = P.new_nonce()
            sock.sendall(P.encode(P.HELLO, struct.pack(">H", P.PROTOCOL_VERSION) + nonce
                                  + self.name.encode("utf-8")))
            msg_type, payload = P.read_frame(sock)
            if msg_type != P.AUTH or len(payload) < 34 \
                    or not P.check_auth(self.secret, nonce, payload[:32]):
                log.warning("Authentification refusée depuis %s", addr[0])
                sock.sendall(P.encode(P.AUTH_FAIL))
                sock.close()
                return
            (audio_port,) = struct.unpack_from(">H", payload, 32)
            name = payload[34:].decode("utf-8", "replace") or addr[0]
        except (OSError, ConnectionError, struct.error) as exc:
            log.info("Poignée de main échouée avec %s : %s", addr[0], exc)
            sock.close()
            return

        session = Session(sock, addr, name, audio_port)
        with self._lock:
            old, self.session = self.session, session
        if old is not None:
            log.info("Nouveau Mac connecté, l'ancienne session est fermée")
            old.close()
        try:
            session.send(P.encode(P.AUTH_OK, struct.pack(">H", int(self.scfg["audio_port"]))))
            session.send(P.encode(P.AUDIO_CTRL, bytes([self._audio_flags()])))
            self._send_info(session, force=True)
        except OSError:
            self._drop(session)
            return
        if self._mic_tx is not None:
            self._mic_tx.dest = (addr[0], audio_port)
        if self._sound_rx is not None:
            self._sound_rx.allowed_ip = addr[0]
            self._sound_rx.reset()
        log.info("Machine pilotée connectée : %s (%s)", name, addr[0])
        self.notify(f"Connecté à {name}")
        if self._wake_until > time.monotonic():
            self._wake_until = 0.0
            log.info("Mac réveillé : bascule demandée pendant le réveil")
            self.request_mode(True)
        self._changed()

        sock.settimeout(DEAD_AFTER + 2)
        try:
            while not self._stop.is_set():
                msg_type, payload = P.read_frame(sock)
                session.last_rx = time.monotonic()
                if msg_type == P.CLIPBOARD and self.clipboard is not None:
                    text = payload.decode("utf-8", "replace")
                    if text:
                        self.clipboard.set_text(text)
                elif msg_type == P.PING:
                    session.send(P.encode(P.PONG))
                elif msg_type == P.PONG:
                    if session.ping_sent:
                        session.rtt_ms = (time.monotonic() - session.ping_sent) * 1000
                    if self._wake_until and session is self.session:
                        # le Mac répond de nouveau : il ne dormait pas, on bascule
                        self._wake_until = 0.0
                        self.request_mode(True)
                        self._changed()
                elif msg_type == P.INFO:
                    try:
                        info = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    if isinstance(info, dict):
                        session.peer_info = info
                        self._remember_wake_targets(info.get("wake"))
                        self._changed()
                elif msg_type == P.REQ_MODE:
                    self.on_hotkey("mac" if payload[:1] == b"\x01" else "pc")
                elif msg_type == P.REQ_AUDIO:
                    flags = payload[0] if payload else 0
                    if self.acfg["enabled"]:
                        if bool(flags & P.AUDIO_SOUND_ON) != self.sound_on:
                            self.set_sound(not self.sound_on)
                        if bool(flags & P.AUDIO_MIC_ON) != self.mic_on:
                            self.set_mic(not self.mic_on)
                        self._send_audio_ctrl()  # confirme l'état réel
                        self._send_info(session)
        except (OSError, ConnectionError):
            pass
        finally:
            self._drop(session)

    def _send_info(self, session: Session, force: bool = False) -> None:
        data = json.dumps(self.info(), ensure_ascii=False)
        if force or data != session.info_sent:
            session.info_sent = data
            self.events.put(("raw", P.enc_text(P.INFO, data)))

    def _drop(self, session: Session) -> None:
        session.close()
        with self._lock:
            if self.session is not session:
                return
            self.session = None
        if self._mic_tx is not None:
            self._mic_tx.dest = None
        if self._sound_rx is not None:
            self._sound_rx.allowed_ip = None
        log.info("Machine pilotée déconnectée")
        if self.remote and self.input is not None:
            # sécurité : on ne laisse jamais le clavier capturé sans Mac
            self.input.request_mode(False)
        if not self._stop.is_set():
            self.notify("Machine pilotée déconnectée")
        self._changed()

    def _send(self, data: bytes) -> None:
        s = self.session
        if s is None:
            return
        try:
            s.send(data)
        except OSError:
            self._drop(s)

    def _sender_loop(self) -> None:
        pending = None
        while True:
            ev = pending if pending is not None else self.events.get()
            pending = None
            kind = ev[0]
            if kind == "stop":
                return
            if kind == "move":
                dx, dy = ev[1], ev[2]
                while True:  # regroupe les mouvements en attente
                    try:
                        nxt = self.events.get_nowait()
                    except queue.Empty:
                        break
                    if nxt[0] == "move":
                        dx += nxt[1]
                        dy += nxt[2]
                    else:
                        pending = nxt
                        break
                data = P.enc_move(dx, dy)
            elif kind == "key":
                data = P.enc_key(ev[1], ev[2], ev[3])
            elif kind == "button":
                data = P.enc_button(ev[1], ev[2])
            elif kind == "wheel":
                data = P.enc_wheel(ev[1], ev[2])
            elif kind == "enter":
                data = P.encode(P.ENTER)
                if self.clipboard is not None:
                    text = self.clipboard.get_text()
                    if text:
                        data += P.enc_text(P.CLIPBOARD, text)
            elif kind == "leave":
                data = P.encode(P.LEAVE)
            elif kind == "raw":
                data = ev[1]
            else:
                continue
            self._send(data)

    def _ping_loop(self) -> None:
        while not self._stop.wait(PING_INTERVAL):
            s = self.session
            if s is None:
                if self._wake_until:
                    if time.monotonic() < self._wake_until:
                        self._send_magic()  # certains Mac ne se réveillent qu'au 2e ou 3e paquet
                    else:
                        self._wake_until = 0.0
                        log.warning("La machine pilotée ne s'est pas réveillée")
                        self.notify("La machine pilotée ne s'est pas réveillée.")
                        self._changed()
                continue
            if time.monotonic() - s.last_rx > DEAD_AFTER:
                log.warning("Le Mac ne répond plus")
                self._drop(s)
                continue
            s.ping_sent = time.monotonic()
            self.events.put(("raw", P.encode(P.PING)))
            self._send_info(s)

    def _beacon_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        packet = P.enc_beacon(self.bound_port, self.name)
        while not self._stop.is_set():
            if self.session is None:
                try:
                    sock.sendto(packet, ("255.255.255.255", P.BEACON_PORT))
                except OSError:
                    pass
            self._stop.wait(2.0)
        sock.close()
