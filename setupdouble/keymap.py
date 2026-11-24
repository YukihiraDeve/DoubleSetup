"""Correspondance scancodes PC (set 1) -> keycodes macOS (kVK_*).

Les deux systèmes de codes sont *positionnels* : on transmet la position
physique de la touche, et c'est la disposition du Mac (idéalement
« Français – PC ») qui produit le caractère. AZERTY, AltGr, etc. se
comportent donc comme sur Windows.

Les scancodes étendus (préfixe E0) sont notés 0xE0xx.
"""

from __future__ import annotations

# kVK_* (HIToolbox/Events.h)
SCANCODE_TO_MAC: dict[int, int] = {
    0x01: 0x35,  # Échap
    0x02: 0x12, 0x03: 0x13, 0x04: 0x14, 0x05: 0x15, 0x06: 0x17,  # 1..5
    0x07: 0x16, 0x08: 0x1A, 0x09: 0x1C, 0x0A: 0x19, 0x0B: 0x1D,  # 6..0
    0x0C: 0x1B,  # - (°)
    0x0D: 0x18,  # = (+)
    0x0E: 0x33,  # Retour arrière
    0x0F: 0x30,  # Tab
    0x10: 0x0C, 0x11: 0x0D, 0x12: 0x0E, 0x13: 0x0F, 0x14: 0x11,  # Q W E R T
    0x15: 0x10, 0x16: 0x20, 0x17: 0x22, 0x18: 0x1F, 0x19: 0x23,  # Y U I O P
    0x1A: 0x21,  # [
    0x1B: 0x1E,  # ]
    0x1C: 0x24,  # Entrée
    0x1D: 0x3B,  # Ctrl gauche (remappé ensuite)
    0x1E: 0x00, 0x1F: 0x01, 0x20: 0x02, 0x21: 0x03, 0x22: 0x05,  # A S D F G
    0x23: 0x04, 0x24: 0x26, 0x25: 0x28, 0x26: 0x25,              # H J K L
    0x27: 0x29,  # ;
    0x28: 0x27,  # '
    0x29: 0x32,  # ` (² en AZERTY)
    0x2A: 0x38,  # Maj gauche
    0x2B: 0x2A,  # \ (* en AZERTY)
    0x2C: 0x06, 0x2D: 0x07, 0x2E: 0x08, 0x2F: 0x09, 0x30: 0x0B,  # Z X C V B
    0x31: 0x2D, 0x32: 0x2E,                                      # N M
    0x33: 0x2B,  # ,
    0x34: 0x2F,  # .
    0x35: 0x2C,  # /
    0x36: 0x3C,  # Maj droite
    0x37: 0x43,  # pavé *
    0x38: 0x3A,  # Alt gauche
    0x39: 0x31,  # Espace
    0x3A: 0x39,  # Verr. Maj
    0x3B: 0x7A, 0x3C: 0x78, 0x3D: 0x63, 0x3E: 0x76, 0x3F: 0x60,  # F1..F5
    0x40: 0x61, 0x41: 0x62, 0x42: 0x64, 0x43: 0x65, 0x44: 0x6D,  # F6..F10
    0x45: 0x47,  # Verr. Num -> Clear
    0x47: 0x59, 0x48: 0x5B, 0x49: 0x5C,  # pavé 7 8 9
    0x4A: 0x4E,  # pavé -
    0x4B: 0x56, 0x4C: 0x57, 0x4D: 0x58,  # pavé 4 5 6
    0x4E: 0x45,  # pavé +
    0x4F: 0x53, 0x50: 0x54, 0x51: 0x55,  # pavé 1 2 3
    0x52: 0x52,  # pavé 0
    0x53: 0x41,  # pavé .
    0x56: 0x0A,  # touche ISO < >
    0x57: 0x67,  # F11
    0x58: 0x6F,  # F12
    0x64: 0x69, 0x65: 0x6B, 0x66: 0x71,  # F13 F14 F15
    0x67: 0x6A, 0x68: 0x40, 0x69: 0x4F, 0x6A: 0x50,  # F16..F19
    0x46: 0x6B,  # Arrêt défil -> F14 (luminosité - sur Mac)
    0xE046: 0x71,  # Pause -> F15 (luminosité + sur Mac)
    0xE01C: 0x4C,  # pavé Entrée
    0xE01D: 0x3E,  # Ctrl droit
    0xE035: 0x4B,  # pavé /
    0xE037: 0x69,  # Impr écran -> F13
    0xE038: 0x3D,  # Alt droit / AltGr
    0xE047: 0x73,  # Début
    0xE048: 0x7E,  # Haut
    0xE049: 0x74,  # Page préc.
    0xE04B: 0x7B,  # Gauche
    0xE04D: 0x7C,  # Droite
    0xE04F: 0x77,  # Fin
    0xE050: 0x7D,  # Bas
    0xE051: 0x79,  # Page suiv.
    0xE052: 0x72,  # Inser -> Help
    0xE053: 0x75,  # Suppr
    0xE05B: 0x37,  # Windows gauche (remappé ensuite)
    0xE05C: 0x36,  # Windows droite
    0xE05D: 0x6E,  # Menu contextuel
    # Touches multimédia -> pseudo-codes (événements « système » sur Mac).
    # Le volume n'est pas transmis : il reste sur Windows, qui pilote le casque.
    0xE022: 0x1000 + 16,  # Lecture/Pause
    0xE019: 0x1000 + 17,  # Piste suivante
    0xE010: 0x1000 + 18,  # Piste précédente
}

MEDIA_BASE = 0x1000  # keycode >= MEDIA_BASE : touche média, type NX = code - MEDIA_BASE

# Keycodes Mac des modificateurs
MAC_CMD, MAC_SHIFT, MAC_CAPS, MAC_OPT, MAC_CTRL = 0x37, 0x38, 0x39, 0x3A, 0x3B
MAC_RSHIFT, MAC_ROPT, MAC_RCTRL, MAC_RCMD, MAC_FN = 0x3C, 0x3D, 0x3E, 0x36, 0x3F

# Scancodes PC des modificateurs
SC_LCTRL, SC_RCTRL = 0x1D, 0xE01D
SC_LALT, SC_RALT = 0x38, 0xE038
SC_LWIN, SC_RWIN = 0xE05B, 0xE05C

# Dispositions des modificateurs.
#  "pc"  : Ctrl = Cmd (Ctrl+C copie sur le Mac comme sur le PC),
#          Windows = Ctrl, Alt = Option, AltGr = Option droite.
#  "mac" : Windows = Cmd, Alt = Option, Ctrl = Ctrl (disposition Apple).
MODIFIER_LAYOUTS: dict[str, dict[int, int]] = {
    "pc": {
        SC_LCTRL: MAC_CMD, SC_RCTRL: MAC_RCMD,
        SC_LWIN: MAC_CTRL, SC_RWIN: MAC_RCTRL,
        SC_LALT: MAC_OPT, SC_RALT: MAC_ROPT,
    },
    "mac": {
        SC_LCTRL: MAC_CTRL, SC_RCTRL: MAC_RCTRL,
        SC_LWIN: MAC_CMD, SC_RWIN: MAC_RCMD,
        SC_LALT: MAC_OPT, SC_RALT: MAC_ROPT,
    },
}

# Masques CGEventFlags + bits « device dependent » (IOLLEvent.h)
FLAG_CAPS = 0x00010000
FLAG_SHIFT = 0x00020000
FLAG_CTRL = 0x00040000
FLAG_ALT = 0x00080000
FLAG_CMD = 0x00100000
FLAG_NUMPAD = 0x00200000
FLAG_FN = 0x00800000

MODIFIER_FLAGS: dict[int, tuple[int, int]] = {
    # keycode: (masque générique, bit gauche/droite)
    MAC_CTRL: (FLAG_CTRL, 0x0001),
    MAC_SHIFT: (FLAG_SHIFT, 0x0002),
    MAC_RSHIFT: (FLAG_SHIFT, 0x0004),
    MAC_CMD: (FLAG_CMD, 0x0008),
    MAC_RCMD: (FLAG_CMD, 0x0010),
    MAC_OPT: (FLAG_ALT, 0x0020),
    MAC_ROPT: (FLAG_ALT, 0x0040),
    MAC_RCTRL: (FLAG_CTRL, 0x2000),
    MAC_FN: (FLAG_FN, 0),
}

# Touches qui portent le flag « pavé numérique / fn » sur un vrai clavier Mac
NUMPAD_KEYS = {0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51,
               0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5B, 0x5C}
ARROW_KEYS = {0x7B, 0x7C, 0x7D, 0x7E}
NAV_KEYS = {0x72, 0x73, 0x74, 0x75, 0x77, 0x79}  # Help, Début, PgPréc, Suppr, Fin, PgSuiv


class KeyMapper:
    def __init__(self, modifier_layout: str = "pc", swap_iso_keys: bool = False):
        if modifier_layout not in MODIFIER_LAYOUTS:
            raise ValueError(f"modifier_layout inconnu : {modifier_layout!r} (pc ou mac)")
        self.table = dict(SCANCODE_TO_MAC)
        self.table.update(MODIFIER_LAYOUTS[modifier_layout])
        if swap_iso_keys:
            self.table[0x29], self.table[0x56] = self.table[0x56], self.table[0x29]

    def to_mac(self, scancode: int) -> int | None:
        return self.table.get(scancode)


def is_modifier(keycode: int) -> bool:
    return keycode in MODIFIER_FLAGS


def flags_for(pressed_modifiers: set[int], caps_on: bool) -> int:
    flags = 0x100  # NX_NONCOALESCEDMASK, présent sur les événements réels
    for kc in pressed_modifiers:
        generic, side = MODIFIER_FLAGS.get(kc, (0, 0))
        flags |= generic | side
    if caps_on:
        flags |= FLAG_CAPS
    return flags


# --- Linux (codes KEY_* de linux/input-event-codes.h) ------------------------------

SCANCODE_TO_LINUX: dict[int, int] = {sc: sc for sc in range(0x01, 0x59) if sc not in (0x54, 0x55)}
SCANCODE_TO_LINUX.update({0x64 + i: 183 + i for i in range(7)})  # F13..F19
SCANCODE_TO_LINUX.update({
    0xE01C: 96, 0xE01D: 97, 0xE035: 98, 0xE037: 99, 0xE038: 100,
    0xE047: 102, 0xE048: 103, 0xE049: 104, 0xE04B: 105, 0xE04D: 106,
    0xE04F: 107, 0xE050: 108, 0xE051: 109, 0xE052: 110, 0xE053: 111,
    0xE020: 113, 0xE02E: 114, 0xE030: 115, 0xE046: 119,
    0xE05B: 125, 0xE05C: 126, 0xE05D: 127,
    0xE019: 163, 0xE022: 164, 0xE010: 165, 0xE024: 166,
})
LINUX_TO_SCANCODE: dict[int, int] = {v: k for k, v in SCANCODE_TO_LINUX.items()}


# --- macOS comme machine source (keycodes Mac -> scancodes PC) ---------------------

def _mac_inverse() -> dict[int, int]:
    inv: dict[int, int] = {}
    for sc, kc in SCANCODE_TO_MAC.items():
        if kc >= MEDIA_BASE or sc in (0x46, 0xE046, 0xE037):  # alias de F13..F15
            continue
        inv.setdefault(kc, sc)
    return inv


MAC_TO_SCANCODE_BASE = _mac_inverse()


class MacToScancode:
    """Traduit un keycode Mac en scancode PC, modificateurs selon la disposition.

    "pc"  : Cmd -> Ctrl, Ctrl -> Windows (miroir de la disposition « pc » côté Mac pilote) ;
    "mac" : Cmd -> Windows, Ctrl -> Ctrl.
    """

    def __init__(self, modifier_layout: str = "pc"):
        if modifier_layout not in MODIFIER_LAYOUTS:
            raise ValueError(f"modifier_layout inconnu : {modifier_layout!r} (pc ou mac)")
        self.table = dict(MAC_TO_SCANCODE_BASE)
        for sc, kc in MODIFIER_LAYOUTS[modifier_layout].items():
            self.table[kc] = sc
        self.table[MAC_SHIFT], self.table[MAC_RSHIFT] = 0x2A, 0x36
        self.table[MAC_CAPS] = 0x3A

    def to_scancode(self, keycode: int) -> int | None:
        return self.table.get(keycode)
