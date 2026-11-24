"""Cœur du serveur (côté PC) : réseau, session Mac, audio, état.

Indépendant de Windows pour pouvoir être testé ailleurs : la capture
clavier/souris (``win_input.InputCapture``) et le presse-papiers sont
injectés de l'extérieur.
"""

from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from typing import Callable, Protocol

from . import protocol as P

log = logging.getLogger(__name__)

PING_INTERVAL = 2.0
DEAD_AFTER = 8.0
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


class Server:
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
        self.sound_on = bool(self.acfg["mac_sound"])
        self.mic_on = bool(self.acfg["mic"])
        self.audio_status = "désactivé"
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
            return "En attente du Mac…"
        where = "Mac actif" if self.remote else "PC actif"
        return f"{where} · {s.name} ({s.addr[0]})"

    # --- Entrées venant de la capture (thread win-input) -------------------------

    def sink(self, event: tuple) -> None:
        self.events.put(event)

    def on_hotkey(self, action: str) -> None:
        if action == "toggle":
            self.request_mode(not self.remote)
        elif action == "mac":
            self.request_mode(True)
        elif action == "pc":
            self.request_mode(False)

    def on_mode(self, remote: bool) -> None:
        self.remote = remote
        self.events.put(("enter",) if remote else ("leave",))
        self._changed()

    def request_mode(self, remote: bool) -> None:
        if remote and self.session is None:
            self.notify("Le Mac n'est pas connecté.")
            return
        if self.input is not None:
            self.input.request_mode(remote)

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
            out_dev = audio.find_device(a["win_output_device"], "output", a["win_hostapi"])
            self._sound_rx = audio.AudioReceiver(
                P.STREAM_MAC_SOUND, out_dev, in_channels=2,
                device_channels=audio.device_channels(out_dev, "output", 2),
                sample_rate=rate, block_ms=block, latency_ms=float(a["latency_ms"]),
                gain=float(a["mac_sound_gain"]))
            self._sound_rx.enabled = self.sound_on
            self._sound_rx.start()
        except Exception as exc:
            self._sound_rx = None
            errors.append(f"sortie : {exc}")
        try:
            ch = int(a["mic_channel"])
            in_dev = audio.find_device(a["win_input_device"], "input", a["win_hostapi"])
            channels = audio.device_channels(in_dev, "input", max(2, ch))
            self._mic_tx = audio.AudioSender(
                self._udp, P.STREAM_MIC, in_dev, channels=channels, sample_rate=rate,
                block_ms=block, select_channel=min(ch, channels) - 1,
                gain=float(a["mic_gain"]))
            self._mic_tx.enabled = self.mic_on
            self._mic_tx.start()
        except Exception as exc:
            self._mic_tx = None
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
        if self._sound_rx is not None:
            self._sound_rx.enabled = on
            self._sound_rx.reset()
        self._send_audio_ctrl()
        self._changed()

    def set_mic(self, on: bool) -> None:
        self.mic_on = on
        if self._mic_tx is not None:
            self._mic_tx.enabled = on
        self._send_audio_ctrl()
        self._changed()

    def _audio_flags(self) -> int:
        if not self.acfg["enabled"]:
            return 0
        return (P.AUDIO_SOUND_ON if self.sound_on else 0) | (P.AUDIO_MIC_ON if self.mic_on else 0)

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
        except OSError:
            self._drop(session)
            return
        if self._mic_tx is not None:
            self._mic_tx.dest = (addr[0], audio_port)
        if self._sound_rx is not None:
            self._sound_rx.allowed_ip = addr[0]
            self._sound_rx.reset()
        log.info("Mac connecté : %s (%s)", name, addr[0])
        self.notify(f"Mac connecté : {name}")
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
        except (OSError, ConnectionError):
            pass
        finally:
            self._drop(session)

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
        log.info("Mac déconnecté")
        if self.remote and self.input is not None:
            # sécurité : on ne laisse jamais le clavier capturé sans Mac
            self.input.request_mode(False)
        if not self._stop.is_set():
            self.notify("Mac déconnecté")
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
                continue
            if time.monotonic() - s.last_rx > DEAD_AFTER:
                log.warning("Le Mac ne répond plus")
                self._drop(s)
                continue
            self.events.put(("raw", P.encode(P.PING)))

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
