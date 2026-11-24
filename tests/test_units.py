import socket

import numpy as np
import pytest

from setupdouble import audio, hotkeys, keymap
from setupdouble import protocol as P


# --- protocole --------------------------------------------------------------

def test_frames_roundtrip():
    a, b = socket.socketpair()
    try:
        a.sendall(P.enc_key(0xE038, True, repeat=True) + P.enc_move(-5, 300)
                  + P.enc_button(P.BTN_X2, False) + P.enc_wheel(0, -120)
                  + P.enc_text(P.CLIPBOARD, "é@€ 漢字"))
        assert P.read_frame(b) == (P.KEY, bytes([0xE0, 0x38, 0x03]))
        t, pl = P.read_frame(b)
        assert t == P.MOVE and pl == b"\xff\xfb\x01\x2c"
        assert P.read_frame(b) == (P.BUTTON, bytes([5, 0]))
        assert P.read_frame(b)[0] == P.WHEEL
        t, pl = P.read_frame(b)
        assert t == P.CLIPBOARD and pl.decode() == "é@€ 漢字"
    finally:
        a.close()
        b.close()


def test_move_is_clamped():
    t, pl = P.encode(P.MOVE, b"")[0], P.enc_move(10**6, -10**6)[3:]
    assert t == P.MOVE
    assert pl == b"\x7f\xff\x80\x00"


def test_text_truncation_keeps_valid_utf8():
    data = P.enc_text(P.CLIPBOARD, "é" * 10, limit=5)[3:]
    assert data.decode("utf-8") == "éé"


def test_auth():
    nonce = P.new_nonce()
    digest = P.compute_auth("un-secret-solide", nonce)
    assert P.check_auth("un-secret-solide", nonce, digest)
    assert not P.check_auth("autre-secret-xyz", nonce, digest)


def test_audio_and_beacon_packets():
    pkt = P.enc_audio(P.STREAM_MIC, 0xFFFFFFFF + 3, b"\x01\x02")
    assert P.dec_audio(pkt) == (P.STREAM_MIC, 2, b"\x01\x02")
    assert P.dec_audio(b"junk") is None
    assert P.dec_beacon(P.enc_beacon(47801, "PC-GAMING")) == (47801, "PC-GAMING")
    assert P.dec_beacon(b"nope") is None


# --- clavier ----------------------------------------------------------------

def test_keymap_letters_are_positional():
    m = keymap.KeyMapper("pc")
    # la touche sous le « A » AZERTY est la position Q du QWERTY -> kVK_ANSI_Q
    assert m.to_mac(0x10) == 0x0C
    assert m.to_mac(0x1E) == 0x00  # position A QWERTY (Q en AZERTY)
    assert m.to_mac(0xE04B) == 0x7B  # flèche gauche
    assert m.to_mac(0x9999) is None


def test_modifier_layouts():
    pc, mac = keymap.KeyMapper("pc"), keymap.KeyMapper("mac")
    assert pc.to_mac(keymap.SC_LCTRL) == keymap.MAC_CMD
    assert pc.to_mac(keymap.SC_LWIN) == keymap.MAC_CTRL
    assert mac.to_mac(keymap.SC_LCTRL) == keymap.MAC_CTRL
    assert mac.to_mac(keymap.SC_LWIN) == keymap.MAC_CMD
    assert pc.to_mac(keymap.SC_RALT) == keymap.MAC_ROPT  # AltGr
    with pytest.raises(ValueError):
        keymap.KeyMapper("azerty")


def test_iso_swap():
    normal, swapped = keymap.KeyMapper(), keymap.KeyMapper(swap_iso_keys=True)
    assert normal.to_mac(0x29) == swapped.to_mac(0x56)
    assert normal.to_mac(0x56) == swapped.to_mac(0x29)


def test_no_duplicate_mac_codes_except_aliases():
    codes = [v for v in keymap.SCANCODE_TO_MAC.values()]
    dupes = {c for c in codes if codes.count(c) > 1}
    # Arrêt défil -> F14 et F14 natif partagent volontairement le même code
    assert dupes <= {0x6B, 0x69, 0x71}


def test_flags():
    f = keymap.flags_for({keymap.MAC_CMD, keymap.MAC_RSHIFT}, caps_on=True)
    assert f & keymap.FLAG_CMD and f & keymap.FLAG_SHIFT and f & keymap.FLAG_CAPS
    assert not f & keymap.FLAG_ALT


# --- raccourcis -------------------------------------------------------------

def test_parse_hotkey():
    hk = hotkeys.parse_hotkey("Ctrl + Alt + Space", "toggle")
    assert hk.mods == {"ctrl", "alt"} and hk.vk == 0x20
    assert hk.matches(0x20, {hotkeys.VK_LCONTROL, hotkeys.VK_RMENU})
    assert not hk.matches(0x20, {hotkeys.VK_LCONTROL})
    assert hotkeys.parse_hotkey("ctrl+alt+right", "mac").vk == 0x27
    assert hotkeys.parse_hotkey("win+f13", "pc").vk == 0x7C
    for bad in ("", "ctrl+alt", "ctrl+a+b", "ctrl+banane"):
        with pytest.raises(ValueError):
            hotkeys.parse_hotkey(bad, "toggle")


# --- tampon audio -----------------------------------------------------------

def _block(n, ch=2, value=1000):
    return np.full((n, ch), value, dtype=np.int16)


def test_jitter_priming_then_steady():
    jb = audio.JitterBuffer(2, 48000, target_ms=20)
    jb.push(_block(240))
    assert not jb.pull(240).any()  # attend d'avoir 20 ms
    for _ in range(4):
        jb.push(_block(240))
    out = jb.pull(240)
    assert out.shape == (240, 2) and (out == 1000).all()


def test_jitter_compensates_clock_drift():
    """Émetteur 0,5 % plus rapide : le niveau doit rester borné, sans vidage brutal."""
    jb = audio.JitterBuffer(2, 48000, target_ms=30)
    for i in range(4000):
        jb.push(_block(241 if i % 2 == 0 else 240))
        jb.pull(240)
    assert jb.overflows == 0
    assert abs(jb.level - jb.target) <= jb.tolerance + 240 * 2


def test_jitter_underrun_recovers():
    jb = audio.JitterBuffer(1, 48000, target_ms=10)
    for _ in range(3):
        jb.push(_block(240, 1))
    jb.pull(240)
    jb.pull(240)
    jb.pull(240)
    jb.pull(240)
    assert jb.underruns >= 1
    assert not jb.pull(240).any()  # repasse en amorçage


def test_audio_udp_loopback():
    """Émetteur -> UDP -> récepteur, sans carte son (callbacks appelés à la main)."""
    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.bind(("127.0.0.1", 0))
    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender = audio.AudioSender(tx_sock, P.STREAM_MIC, device=0, channels=2,
                                   sample_rate=48000, block_ms=5, select_channel=0, gain=2.0)
        sender.dest = rx_sock.getsockname()
        receiver = audio.AudioReceiver(P.STREAM_MIC, device=0, in_channels=1,
                                       device_channels=2, sample_rate=48000, block_ms=5,
                                       latency_ms=10)
        receiver.allowed_ip = "127.0.0.1"
        frames = np.zeros((240, 2), dtype=np.int16)
        frames[:, 0] = 100   # micro sur l'entrée 1
        frames[:, 1] = -7    # entrée 2 ignorée
        rx_sock.settimeout(1)
        for _ in range(4):
            sender._callback(frames, 240, None, None)
            pkt, addr = rx_sock.recvfrom(4096)
            receiver.feed(pkt, addr)
        receiver.feed(pkt, addr)  # doublon ignoré
        assert receiver.buffer.level == 960
        out = np.zeros((240, 2), dtype=np.int16)
        receiver._callback(out, 240, None, None)
        assert (out == 200).all()  # gain x2, mono dupliqué sur les 2 canaux
        level = receiver.buffer.level
        assert level < 960
        receiver.feed(pkt, ("10.0.0.9", 1))  # IP non autorisée
        assert receiver.buffer.level == level
    finally:
        rx_sock.close()
        tx_sock.close()


def test_find_device_alternatives_and_wasapi_preference(monkeypatch):
    import sounddevice as sd

    devs = [
        {"name": "Haut-parleurs (Scarlett Solo USB)", "max_input_channels": 0,
         "max_output_channels": 2, "hostapi": 0},
        {"name": "Haut-parleurs (Scarlett Solo USB)", "max_input_channels": 0,
         "max_output_channels": 2, "hostapi": 1},
        {"name": "Microphone (Scarlett Solo USB)", "max_input_channels": 2,
         "max_output_channels": 0, "hostapi": 1},
    ]
    monkeypatch.setattr(sd, "query_devices", lambda *a: devs)
    monkeypatch.setattr(sd, "query_hostapis",
                        lambda *a: [{"name": "MME"}, {"name": "Windows WASAPI"}])
    assert audio.find_device("Scarlett|Focusrite", "output", "WASAPI") == 1
    assert audio.find_device("Focusrite|Scarlett", "input", "WASAPI") == 2
    with pytest.raises(RuntimeError):
        audio.find_device("Focusrite", "output")
