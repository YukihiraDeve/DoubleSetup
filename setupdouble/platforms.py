"""Tout ce qui dépend du système, rassemblé ici : Windows, macOS ou Linux.

Le reste du code (serveur, client, réseau, audio) est commun.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

WINDOWS, MAC, LINUX = "windows", "mac", "linux"


def current_os() -> str:
    if sys.platform == "win32":
        return WINDOWS
    if sys.platform == "darwin":
        return MAC
    return LINUX


OS = current_os()
OS_LABELS = {WINDOWS: "Windows", MAC: "macOS", LINUX: "Linux"}


def log_dir() -> Path:
    if OS == WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SetupDouble"
    if OS == MAC:
        return Path.home() / "Library" / "Logs" / "SetupDouble"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "setupdouble"


def computer_name() -> str:
    """Nom lisible de la machine (« MacBook Pro de … », nom du PC…)."""
    if OS == MAC:
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                                 text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if OS == WINDOWS and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    host = socket.gethostname()
    for suffix in (".local", ".home", ".lan"):
        host = host.removesuffix(suffix)
    return host


def open_path(path: str | os.PathLike) -> None:
    path = str(path)
    if OS == WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif OS == MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# --- Presse-papiers ------------------------------------------------------------------

class CommandClipboard:
    """Presse-papiers via des outils en ligne de commande (pbcopy, wl-copy, xclip…)."""

    def __init__(self, get_cmd: list[str], set_cmd: list[str]):
        self.get_cmd, self.set_cmd = get_cmd, set_cmd
        self.env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")

    def get_text(self) -> str | None:
        try:
            out = subprocess.run(self.get_cmd, capture_output=True, env=self.env, timeout=2)
            return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def set_text(self, text: str) -> bool:
        try:
            subprocess.run(self.set_cmd, input=text.encode("utf-8"), env=self.env, timeout=2,
                           check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def linux_clipboard_commands(env: dict | None = None,
                             which=shutil.which) -> tuple[list[str], list[str]] | None:
    env = os.environ if env is None else env
    if env.get("WAYLAND_DISPLAY") and which("wl-copy") and which("wl-paste"):
        return ["wl-paste", "--no-newline"], ["wl-copy"]
    if which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard"]
    if which("xsel"):
        return ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
    return None


def make_clipboard():
    if OS == WINDOWS:
        from . import win_clipboard

        class _Win:
            get_text = staticmethod(win_clipboard.get_text)
            set_text = staticmethod(win_clipboard.set_text)
        return _Win()
    if OS == MAC:
        return CommandClipboard(["pbpaste"], ["pbcopy"])
    cmds = linux_clipboard_commands()
    if cmds is None:
        log.warning("Presse-papiers indisponible : installe wl-clipboard (Wayland) ou xclip (X11)")
        return None
    return CommandClipboard(*cmds)


# --- Machine pilotée ----------------------------------------------------------------------

def make_injector(ccfg: dict, system_ballistics: bool = False):
    """``system_ballistics`` : laisser l'OS appliquer son accélération (profil « local »).
    La vitesse de la souris est appliquée en amont par pointer.PointerShaper."""
    speed, scroll, invert = 1.0, float(ccfg["scroll_speed"]), bool(ccfg["invert_scroll"])
    if OS == MAC:
        from . import keymap
        from .mac_inject import MacInjector
        return MacInjector(keymap.KeyMapper(ccfg["modifier_layout"], ccfg["swap_iso_keys"]),
                           speed, scroll, invert)
    if OS == WINDOWS:
        from .win_inject import WinInjector
        return WinInjector(speed, scroll, invert, absolute=not system_ballistics)
    from .linux_input import LinuxInjector
    return LinuxInjector(speed, scroll, invert)


def input_permission():
    """Fonction qui dit si l'injection est autorisée (None = rien à vérifier)."""
    if OS == MAC:
        from .mac_inject import accessibility_granted
        return accessibility_granted
    if OS == LINUX:
        return lambda: os.access("/dev/uinput", os.W_OK)
    return None


def request_input_permission() -> bool:
    if OS == MAC:
        from .mac_inject import request_accessibility
        return request_accessibility()
    check = input_permission()
    return True if check is None else check()


def permission_help() -> tuple[str, str]:
    """(libellé, aide) pour la vérification « droits » de la fenêtre de statut."""
    if OS == MAC:
        return ("Accessibilité",
                "À autoriser : Réglages Système → Confidentialité et sécurité → "
                "Accessibilité → " + os.path.realpath(sys.executable))
    if OS == LINUX:
        return ("Accès à /dev/uinput",
                "Relance l'installation (règle udev + groupe input), puis reconnecte-toi")
    return ("Contrôle", "")


def make_sleep_guard(mode: str):
    if OS == MAC:
        from .mac_system import SleepGuard
        return SleepGuard(mode)
    if OS == WINDOWS:
        from .win_inject import WinSleepGuard
        return WinSleepGuard(mode)
    return InhibitGuard(mode)


class InhibitGuard:
    """Linux : systemd-inhibit empêche la mise en veille tant que le serveur est connecté."""

    def __init__(self, mode: str = "power"):
        if mode not in ("power", "always", "never"):
            raise ValueError(f"prevent_sleep inconnu : {mode!r}")
        self.mode = mode
        self._proc: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.mode == "never" or self.active or not shutil.which("systemd-inhibit"):
            return
        self._proc = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:idle", "--who=SetupDouble",
             "--why=Machine pilotée par SetupDouble", "--mode=block", "sleep", "infinity"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc = None


def network_identity() -> list[dict]:
    """Adresses MAC/IP de cette machine, pour que le serveur puisse la réveiller."""
    if OS == MAC:
        from .mac_system import network_identity as mac_identity
        return mac_identity()
    if OS == LINUX:
        return linux_identity()
    node = uuid.getnode()
    if node >> 40 & 1:  # adresse aléatoire : uuid n'a pas trouvé de carte réseau
        return []
    mac = ":".join(f"{(node >> s) & 0xFF:02x}" for s in range(40, -1, -8))
    return [{"iface": "", "mac": mac, "ip": "", "broadcast": ""}]


def linux_identity(sys_net: Path = Path("/sys/class/net")) -> list[dict]:
    result = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == "lo":
            continue
        iface, ip = parts[1], parts[3].split("/")[0]
        brd = parts[parts.index("brd") + 1] if "brd" in parts else ""
        try:
            mac = (sys_net / iface / "address").read_text().strip().lower()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            result.append({"iface": iface, "mac": mac, "ip": ip, "broadcast": brd})
    return result


def wake_setting_check():
    """Fonction qui dit si le réveil par le réseau est activé (None = inconnu)."""
    if OS == MAC:
        from .mac_system import wake_on_lan_enabled
        return wake_on_lan_enabled
    return None


def make_audio_router(acfg: dict):
    if OS == MAC and acfg["client_auto_route"]:
        from .mac_system import AudioRouter
        return AudioRouter(acfg["client_capture_device"], acfg["client_mic_device"])
    return None


# --- Machine source (serveur) ---------------------------------------------------------

def make_capture(hotkeys, on_hotkey, sink, on_mode, scfg: dict):
    if OS == WINDOWS:
        from .win_input import InputCapture
        return InputCapture(hotkeys, on_hotkey, sink, on_mode)
    if OS == MAC:
        from .mac_capture import MacCapture
        return MacCapture(hotkeys, on_hotkey, sink, on_mode, scfg.get("modifier_layout", "pc"))
    from .linux_input import LinuxCapture
    return LinuxCapture(hotkeys, on_hotkey, sink, on_mode)


# --- Une seule instance, redémarrage ---------------------------------------------------------

_lock_handle = None


def single_instance(name: str, wait: float = 0.0) -> bool:
    """Vrai si aucune autre instance ``name`` ne tourne. ``wait`` : secondes d'attente."""
    import time

    deadline = time.monotonic() + wait
    while True:
        if _try_lock(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def _try_lock(name: str) -> bool:
    global _lock_handle
    if OS == WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, f"Local\\SetupDouble{name}")
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            _lock_handle = handle
            return True
        if handle:
            kernel32.CloseHandle(handle)  # sinon le mutex de l'ancienne instance survivrait
        return False
    import fcntl
    path = log_dir() / f"{name.lower()}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_handle = f
    return True


def restart_self(module: str, extra_args: list[str]) -> None:
    """Relance ce programme (après une mise à jour ou un changement de réglages)."""
    exe = Path(sys.executable)
    if OS == WINDOWS:
        pythonw = exe.with_name("pythonw.exe")
        cmd = [str(pythonw if pythonw.exists() else exe), "-m", module, "--wait-instance",
               *extra_args]
        subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent.parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        os._exit(0)
    # macOS/Linux : on se remplace (même PID, launchd/systemd ne voient rien)
    os.execv(str(exe), [str(exe), "-m", module, "--wait-instance", *extra_args])
