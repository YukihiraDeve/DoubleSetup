"""Transport audio PCM 16 bits sur UDP, avec tampon anti-gigue.

Un *émetteur* lit un périphérique d'entrée et envoie des paquets UDP.
Un *récepteur* reçoit les paquets, les place dans un tampon et alimente
un périphérique de sortie. Le tampon corrige en douceur la dérive
d'horloge entre les deux cartes son (ajout/suppression d'un échantillon
par bloc), ce qui évite les craquements périodiques.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable

import numpy as np

from . import protocol

log = logging.getLogger(__name__)


class JitterBuffer:
    """Tampon circulaire d'échantillons int16 entrelacés."""

    def __init__(self, channels: int, sample_rate: int, target_ms: float,
                 max_ms: float | None = None):
        self.channels = channels
        self.target = max(1, int(sample_rate * target_ms / 1000))
        self.max = int(sample_rate * (max_ms or target_ms * 4 + 40) / 1000)
        self.tolerance = max(1, int(sample_rate * 0.004))  # ±4 ms
        self._buf = np.zeros((0, channels), dtype=np.int16)
        self._lock = threading.Lock()
        self._priming = True
        self.underruns = 0
        self.overflows = 0

    @property
    def level(self) -> int:
        return len(self._buf)

    def push(self, frames: np.ndarray) -> None:
        with self._lock:
            self._buf = np.concatenate((self._buf, frames))
            if len(self._buf) > self.max:
                self.overflows += 1
                self._buf = self._buf[-self.target:]

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros((n, self.channels), dtype=np.int16)
        with self._lock:
            if self._priming:
                if len(self._buf) < self.target:
                    return out
                self._priming = False
            level = len(self._buf)
            take = n
            if level > self.target + self.tolerance + n:
                take = n + 1  # trop plein : on consomme un échantillon de plus
            elif level < self.target - self.tolerance:
                take = n - 1  # presque vide : on en consomme un de moins
            if level < take:
                self.underruns += 1
                self._priming = True
                avail = self._buf
                self._buf = self._buf[:0]
                out[: len(avail)] = avail[:n]
                return out
            chunk = self._buf[:take]
            self._buf = self._buf[take:]
        if take == n:
            out[:] = chunk
        else:
            out[:] = _stretch(chunk, n)
        return out


def _stretch(chunk: np.ndarray, n: int) -> np.ndarray:
    """Rééchantillonne linéairement ``chunk`` pour obtenir ``n`` trames."""
    if len(chunk) == 0:
        return np.zeros((n, chunk.shape[1]), dtype=np.int16)
    src = np.linspace(0, len(chunk) - 1, num=len(chunk))
    dst = np.linspace(0, len(chunk) - 1, num=n)
    out = np.empty((n, chunk.shape[1]), dtype=np.int16)
    for c in range(chunk.shape[1]):
        out[:, c] = np.interp(dst, src, chunk[:, c]).astype(np.int16)
    return out


def apply_gain(frames: np.ndarray, gain: float) -> np.ndarray:
    if gain == 1.0:
        return frames
    return np.clip(frames.astype(np.float32) * gain, -32768, 32767).astype(np.int16)


# --- Sélection des périphériques -----------------------------------------------

def find_device(name: str, kind: str, hostapi: str | None = None) -> int:
    """Retourne l'index du premier périphérique dont le nom contient ``name``.

    ``name`` peut proposer plusieurs essais séparés par « | » (ex.
    "Scarlett|Focusrite") : le premier qui correspond l'emporte.
    ``kind`` vaut "input" ou "output". Si ``hostapi`` est donné (ex. "WASAPI"),
    les périphériques de cette API sont préférés.
    """
    import sounddevice as sd

    key = "max_input_channels" if kind == "input" else "max_output_channels"
    apis = sd.query_hostapis()
    devices = list(enumerate(sd.query_devices()))
    candidates = []
    for wanted in [w.strip().lower() for w in name.split("|") if w.strip()]:
        for idx, dev in devices:
            if dev[key] <= 0 or wanted not in dev["name"].lower():
                continue
            api_name = apis[dev["hostapi"]]["name"]
            score = 0 if hostapi and hostapi.lower() in api_name.lower() else 1
            candidates.append((score, idx))
        if candidates:
            break
    if not candidates:
        names = [d["name"] for d in sd.query_devices() if d[key] > 0]
        raise RuntimeError(
            f"Périphérique {kind} contenant « {name} » introuvable. Disponibles : {names}"
        )
    return sorted(candidates)[0][1]


def _extra_settings(device: int):
    """Sous Windows/WASAPI, active la conversion de fréquence automatique."""
    import sounddevice as sd

    try:
        api = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"]
    except Exception:
        return None
    if "WASAPI" not in api:
        return None
    try:
        return sd.WasapiSettings(auto_convert=True)
    except TypeError:  # anciennes versions de sounddevice
        return None


# --- Émetteur / récepteur ------------------------------------------------------

class AudioSender:
    """Capture un périphérique d'entrée et l'envoie en UDP."""

    def __init__(self, sock: socket.socket, stream_id: int, device: int,
                 channels: int, sample_rate: int, block_ms: int,
                 select_channel: int | None = None, gain: float = 1.0,
                 out_channels: int | None = None):
        self.sock = sock
        self.stream_id = stream_id
        self.device = device
        self.channels = channels
        self.sample_rate = sample_rate
        self.blocksize = max(32, int(sample_rate * block_ms / 1000))
        self.select_channel = select_channel  # 0-based, None = tous
        self.gain = gain
        self.out_channels = out_channels or (1 if select_channel is not None else channels)
        self.dest: tuple[str, int] | None = None
        self.enabled = True
        self.last_sent = 0.0  # time.monotonic() du dernier paquet envoyé
        self._seq = 0
        self._stream = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if not self.enabled or self.dest is None:
            return
        data = indata
        if self.select_channel is not None:
            data = data[:, self.select_channel:self.select_channel + 1]
        data = apply_gain(np.ascontiguousarray(data), self.gain)
        try:
            self.sock.sendto(protocol.enc_audio(self.stream_id, self._seq, data.tobytes()),
                             self.dest)
            self.last_sent = time.monotonic()
        except OSError:
            pass
        self._seq += 1

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.InputStream(
            device=self.device, channels=self.channels, samplerate=self.sample_rate,
            dtype="int16", blocksize=self.blocksize, latency="low",
            callback=self._callback, extra_settings=_extra_settings(self.device),
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioReceiver:
    """Reçoit un flux UDP et le joue sur un périphérique de sortie."""

    def __init__(self, stream_id: int, device: int, in_channels: int,
                 device_channels: int, sample_rate: int, block_ms: int,
                 latency_ms: float, gain: float = 1.0):
        self.stream_id = stream_id
        self.device = device
        self.in_channels = in_channels
        self.device_channels = device_channels
        self.sample_rate = sample_rate
        self.blocksize = max(32, int(sample_rate * block_ms / 1000))
        self.gain = gain
        self.buffer = JitterBuffer(in_channels, sample_rate, latency_ms)
        self.enabled = True
        self.allowed_ip: str | None = None
        self.last_packet = 0.0  # time.monotonic() du dernier paquet accepté
        self._last_seq: int | None = None
        self._stream = None

    def feed(self, packet: bytes, addr: tuple[str, int]) -> None:
        if not self.enabled:
            return
        if self.allowed_ip is not None and addr[0] != self.allowed_ip:
            return
        decoded = protocol.dec_audio(packet)
        if decoded is None:
            return
        stream, seq, pcm = decoded
        if stream != self.stream_id or len(pcm) % (2 * self.in_channels):
            return
        if self._last_seq is not None:
            delta = (seq - self._last_seq) & 0xFFFFFFFF
            if delta == 0 or delta > 0x7FFFFFFF:
                return  # doublon ou paquet en retard
        self._last_seq = seq
        self.last_packet = time.monotonic()
        frames = np.frombuffer(pcm, dtype=np.int16).reshape(-1, self.in_channels)
        self.buffer.push(apply_gain(frames, self.gain))

    def reset(self) -> None:
        self._last_seq = None

    def _callback(self, outdata, frames, time_info, status):  # noqa: ARG002
        data = self.buffer.pull(frames)
        if self.device_channels == self.in_channels:
            outdata[:] = data
        else:
            outdata.fill(0)
            n = min(self.device_channels, max(self.in_channels, 2))
            for c in range(n):
                outdata[:, c] = data[:, c % self.in_channels]

    def start(self) -> None:
        import sounddevice as sd

        self._stream = sd.OutputStream(
            device=self.device, channels=self.device_channels, samplerate=self.sample_rate,
            dtype="int16", blocksize=self.blocksize, latency="low",
            callback=self._callback, extra_settings=_extra_settings(self.device),
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def udp_listener(sock: socket.socket, handler: Callable[[bytes, tuple], None],
                 stop: threading.Event) -> threading.Thread:
    """Lance un thread qui lit ``sock`` et passe chaque paquet à ``handler``."""

    def run():
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                packet, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if stop.is_set():
                    break
                continue
            try:
                handler(packet, addr)
            except Exception:  # pragma: no cover - ne jamais tuer le thread
                log.exception("erreur audio")

    t = threading.Thread(target=run, name="udp-audio", daemon=True)
    t.start()
    return t


def flowing(timestamp: float, within: float = 1.5) -> bool:
    """Vrai si un paquet audio est passé il y a moins de ``within`` secondes."""
    return timestamp > 0 and time.monotonic() - timestamp < within


def device_name(device: int) -> str:
    import sounddevice as sd

    return str(sd.query_devices(device)["name"])


def device_channels(device: int, kind: str, wanted: int) -> int:
    import sounddevice as sd

    info = sd.query_devices(device)
    available = info["max_input_channels" if kind == "input" else "max_output_channels"]
    return max(1, min(wanted, available))
