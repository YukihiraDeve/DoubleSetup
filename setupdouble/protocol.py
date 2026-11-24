"""Protocole réseau entre le serveur Windows et l'agent Mac.

Canal de contrôle : TCP, trames ``[type:u8][longueur:u16][payload]``.
Canal audio      : UDP, paquets ``[magic:4][flux:u8][seq:u32][pcm int16]``.

Authentification : le serveur envoie un nonce, le client répond
HMAC-SHA256(secret, nonce). Sans le bon secret, aucun événement clavier
n'est envoyé.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import threading

PROTOCOL_VERSION = 1

# Ports par défaut
BEACON_PORT = 47800       # UDP broadcast : le PC annonce sa présence
CONTROL_PORT = 47801      # TCP : contrôle
SERVER_AUDIO_PORT = 47802  # UDP : Mac -> PC (son du Mac)
CLIENT_AUDIO_PORT = 47803  # UDP : PC -> Mac (micro)

BEACON_MAGIC = b"SDBEACON1"
AUDIO_MAGIC = b"SDA1"

# Types de messages
HELLO = 1        # s->c : version u16, nonce 16 octets, nom (utf8)
AUTH = 2         # c->s : hmac 32 octets, port audio u16, nom (utf8)
AUTH_OK = 3      # s->c : port audio u16
AUTH_FAIL = 4    # s->c
ENTER = 10       # s->c : le Mac prend le contrôle
LEAVE = 11       # s->c : le PC reprend le contrôle (relâcher tout)
KEY = 12         # s->c : scancode u16, flags u8 (bit0=down, bit1=repeat)
MOVE = 13        # s->c : dx i16, dy i16
BUTTON = 14      # s->c : bouton u8, down u8
WHEEL = 15       # s->c : dx i16, dy i16 (unités Windows, 120 = 1 cran)
CLIPBOARD = 20   # les deux : texte utf8
AUDIO_CTRL = 21  # s->c : flags u8 (bit0 = son Mac->PC, bit1 = micro PC->Mac)
PING = 30
PONG = 31

# Boutons souris
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_X1, BTN_X2 = 1, 2, 3, 4, 5

# Flux audio
STREAM_MAC_SOUND = 1  # Mac -> PC
STREAM_MIC = 2        # PC -> Mac

KEY_DOWN = 0x01
KEY_REPEAT = 0x02

AUDIO_SOUND_ON = 0x01
AUDIO_MIC_ON = 0x02

_HDR = struct.Struct(">BH")
MAX_PAYLOAD = 0xFFFF


class ProtocolError(Exception):
    pass


def compute_auth(secret: str, nonce: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), nonce, hashlib.sha256).digest()


def check_auth(secret: str, nonce: bytes, digest: bytes) -> bool:
    return hmac.compare_digest(compute_auth(secret, nonce), digest)


def new_nonce() -> bytes:
    return os.urandom(16)


def encode(msg_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("payload trop grand")
    return _HDR.pack(msg_type, len(payload)) + payload


# --- Encodeurs pratiques -------------------------------------------------

def clamp16(v: int) -> int:
    return max(-32768, min(32767, int(v)))


def enc_key(scancode: int, down: bool, repeat: bool = False) -> bytes:
    flags = (KEY_DOWN if down else 0) | (KEY_REPEAT if repeat else 0)
    return encode(KEY, struct.pack(">HB", scancode & 0xFFFF, flags))


def enc_move(dx: int, dy: int) -> bytes:
    return encode(MOVE, struct.pack(">hh", clamp16(dx), clamp16(dy)))


def enc_button(button: int, down: bool) -> bytes:
    return encode(BUTTON, struct.pack(">BB", button, 1 if down else 0))


def enc_wheel(dx: int, dy: int) -> bytes:
    return encode(WHEEL, struct.pack(">hh", clamp16(dx), clamp16(dy)))


def enc_text(msg_type: int, text: str, limit: int = MAX_PAYLOAD) -> bytes:
    data = text.encode("utf-8")
    if len(data) > limit:
        data = data[:limit]
        # ne pas couper un caractère UTF-8 au milieu
        data = data.decode("utf-8", "ignore").encode("utf-8")
    return encode(msg_type, data)


# --- Lecture de trames ----------------------------------------------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connexion fermée")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    msg_type, length = _HDR.unpack(_recv_exact(sock, _HDR.size))
    payload = _recv_exact(sock, length) if length else b""
    return msg_type, payload


class FrameWriter:
    """Écriture thread-safe de trames sur un socket TCP."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._lock = threading.Lock()

    def send(self, data: bytes) -> None:
        with self._lock:
            self.sock.sendall(data)


def tune_tcp(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


# --- Audio ----------------------------------------------------------------

_AUDIO_HDR = struct.Struct(">4sBI")
AUDIO_HDR_SIZE = _AUDIO_HDR.size


def enc_audio(stream: int, seq: int, pcm: bytes) -> bytes:
    return _AUDIO_HDR.pack(AUDIO_MAGIC, stream, seq & 0xFFFFFFFF) + pcm


def dec_audio(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < AUDIO_HDR_SIZE:
        return None
    magic, stream, seq = _AUDIO_HDR.unpack_from(packet)
    if magic != AUDIO_MAGIC:
        return None
    return stream, seq, packet[AUDIO_HDR_SIZE:]


# --- Découverte ---------------------------------------------------------------

def enc_beacon(control_port: int, name: str) -> bytes:
    return BEACON_MAGIC + struct.pack(">H", control_port) + name.encode("utf-8")[:200]


def dec_beacon(packet: bytes) -> tuple[int, str] | None:
    if not packet.startswith(BEACON_MAGIC) or len(packet) < len(BEACON_MAGIC) + 2:
        return None
    (port,) = struct.unpack_from(">H", packet, len(BEACON_MAGIC))
    name = packet[len(BEACON_MAGIC) + 2:].decode("utf-8", "replace")
    return port, name
