"""Analyse des raccourcis clavier (codes virtuels Windows). Module pur, testable partout."""

from __future__ import annotations

from dataclasses import dataclass

VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

MOD_VKS = {
    "ctrl": (VK_LCONTROL, VK_RCONTROL),
    "alt": (VK_LMENU, VK_RMENU),
    "shift": (VK_LSHIFT, VK_RSHIFT),
    "win": (VK_LWIN, VK_RWIN),
}
MOD_FLAGS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
MOD_ALIASES = {"control": "ctrl", "altgr": "alt", "menu": "alt", "maj": "shift",
               "windows": "win", "super": "win", "cmd": "win"}

KEY_NAMES: dict[str, int] = {
    "space": 0x20, "espace": 0x20, "enter": 0x0D, "entree": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "gauche": 0x25, "haut": 0x26, "droite": 0x27, "bas": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E, "scrolllock": 0x91, "pause": 0x13,
    "printscreen": 0x2C,
}
KEY_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})
KEY_NAMES.update({f"numpad{i}": 0x60 + i for i in range(10)})
KEY_NAMES.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
KEY_NAMES.update({str(d): 0x30 + d for d in range(10)})


# Code virtuel Windows -> scancode PC (position QWERTY), pour macOS et Linux
_LETTERS_SC = dict(zip("qwertyuiop", range(0x10, 0x1A)))
_LETTERS_SC.update(zip("asdfghjkl", range(0x1E, 0x27)))
_LETTERS_SC.update(zip("zxcvbnm", range(0x2C, 0x33)))
VK_TO_SC: dict[int, int] = {ord(c.upper()): sc for c, sc in _LETTERS_SC.items()}
VK_TO_SC.update({0x31 + i: 0x02 + i for i in range(9)})
VK_TO_SC[0x30] = 0x0B
VK_TO_SC.update({0x70 + i: 0x3B + i for i in range(10)})  # F1..F10
VK_TO_SC.update({0x7A: 0x57, 0x7B: 0x58})                   # F11, F12
VK_TO_SC.update({0x7C + i: 0x64 + i for i in range(7)})    # F13..F19
VK_TO_SC.update({
    0x20: 0x39, 0x0D: 0x1C, 0x09: 0x0F, 0x1B: 0x01, 0x08: 0x0E,
    0x25: 0xE04B, 0x26: 0xE048, 0x27: 0xE04D, 0x28: 0xE050,
    0x24: 0xE047, 0x23: 0xE04F, 0x21: 0xE049, 0x22: 0xE051, 0x2D: 0xE052, 0x2E: 0xE053,
    0x91: 0x46, 0x13: 0xE046, 0x2C: 0xE037,
})
VK_TO_SC.update(dict(zip(range(0x60, 0x6A), (0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D,
                                              0x47, 0x48, 0x49))))
MOD_SCS = {
    "ctrl": (0x1D, 0xE01D),
    "alt": (0x38, 0xE038),
    "shift": (0x2A, 0x36),
    "win": (0xE05B, 0xE05C),
}
MOD_SC_ALL = {sc for pair in MOD_SCS.values() for sc in pair}


@dataclass(frozen=True)
class Hotkey:
    action: str          # "toggle", "remote" ou "local"
    mods: frozenset      # {"ctrl", "alt", ...}
    vk: int
    text: str

    @property
    def sc(self) -> int | None:
        return VK_TO_SC.get(self.vk)

    def matches(self, vk: int, down_vks: set[int]) -> bool:
        """Version Windows : codes virtuels."""
        if vk != self.vk:
            return False
        return all(any(v in down_vks for v in MOD_VKS[m]) for m in self.mods)

    def matches_sc(self, sc: int, down_scs: set[int]) -> bool:
        """Version macOS/Linux : scancodes PC. Les modificateurs en trop sont refusés."""
        if sc != self.sc:
            return False
        held = {m for m, pair in MOD_SCS.items() if any(c in down_scs for c in pair)}
        return held == set(self.mods)


def parse_hotkey(text: str, action: str) -> Hotkey:
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("raccourci vide")
    mods, key = set(), None
    for p in parts:
        p = MOD_ALIASES.get(p, p)
        if p in MOD_VKS:
            mods.add(p)
        elif p in KEY_NAMES:
            if key is not None:
                raise ValueError(f"raccourci « {text} » : une seule touche non modificatrice")
            key = KEY_NAMES[p]
        else:
            raise ValueError(f"raccourci « {text} » : touche inconnue « {p} »")
    if key is None:
        raise ValueError(f"raccourci « {text} » : il manque une touche")
    return Hotkey(action, frozenset(mods), key, text)


_PRETTY = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Maj", "win": "Win", "space": "Espace",
           "right": "→", "left": "←", "up": "↑", "down": "↓", "enter": "Entrée",
           "tab": "Tab", "esc": "Échap"}


def pretty(text: str) -> str:
    """« ctrl+alt+space » -> « Ctrl + Alt + Espace » (pour l'interface)."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    return " + ".join(_PRETTY.get(p, p.upper() if len(p) <= 3 else p.capitalize())
                      for p in parts)
