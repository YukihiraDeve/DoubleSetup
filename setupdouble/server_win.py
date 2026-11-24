"""Point d'entrée Windows : icône dans la barre des tâches + raccourcis.

    pythonw -m setupdouble.server_win [--config chemin\\config.toml]
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as config_mod
from . import dashboard
from . import hotkeys as hotkeys_mod
from . import win_clipboard, win_input
from .server_core import Server

log = logging.getLogger("setupdouble")

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SetupDouble"


def setup_logging(level: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "server.log"
    handlers: list[logging.Handler] = [RotatingFileHandler(path, maxBytes=1_000_000,
                                                           backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:  # absent sous pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return path


def set_dpi_aware() -> None:
    """Coordonnées en pixels physiques, quel que soit le zoom Windows."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def single_instance(wait: float = 0.0) -> bool:
    """Vrai si aucune autre instance ne tourne. ``wait`` : secondes à attendre
    qu'une ancienne instance se ferme (redémarrage après mise à jour)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    deadline = time.monotonic() + wait
    while True:
        handle = kernel32.CreateMutexW(None, False, "Local\\SetupDoubleServer")
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            single_instance.handle = handle  # garder le mutex en vie
            return True
        if handle:
            kernel32.CloseHandle(handle)  # sinon le mutex de l'ancienne instance survivrait
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def restart_command(config_path: str | None) -> list[str]:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.exists() else exe), "-m", "setupdouble.server_win",
           "--wait-instance"]
    if config_path:
        cmd += ["--config", config_path]
    return cmd


class Clip:
    get_text = staticmethod(win_clipboard.get_text)
    set_text = staticmethod(win_clipboard.set_text)


def make_icon(remote: bool, connected: bool):
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (37, 99, 235, 255) if remote else (64, 64, 72, 255)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=14, fill=bg)
    label = "MAC" if remote else "PC"
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("segoeuib.ttf", 26 if remote else 30)
    except Exception:
        font = None
    d.text((size / 2, size / 2), label, fill="white", font=font, anchor="mm")
    dot = (34, 197, 94, 255) if connected else (239, 68, 68, 255)
    d.ellipse((size - 22, size - 22, size - 4, size - 4), fill=dot, outline="white", width=2)
    return img


def open_dashboard(config_path: str | None = None) -> None:
    """Ouvre la fenêtre du tableau de bord dans un processus séparé."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.exists() else exe), "-m", "setupdouble.app"]
    if config_path:
        cmd += ["--config", config_path]
    subprocess.Popen(cmd, cwd=str(config_mod.ROOT),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def hotkey_check(capture: win_input.InputCapture, hotkeys) -> dict:
    text = ", ".join(hotkeys_mod.pretty(h.text) for h in hotkeys)
    if capture._fallback_kb_hook:
        return dashboard.check("hotkey", "Raccourci clavier", "warn",
                               f"{text} : déjà pris par une autre appli, mode de secours")
    return dashboard.check("hotkey", "Raccourci clavier", "ok", text)


def run_tray(server: Server, capture: win_input.InputCapture, log_path: Path,
             hotkey_text: str) -> None:
    import pystray
    from pystray import Menu, MenuItem as Item

    def show_dashboard(icon, item):
        open_dashboard(server.cfg.get("_path"))

    def toggle(icon, item):
        server.request_mode(not server.remote)

    def to_mac(icon, item):
        server.request_mode(True)

    def to_pc(icon, item):
        server.request_mode(False)

    def sound(icon, item):
        server.set_sound(not server.sound_on)

    def mic(icon, item):
        server.set_mic(not server.mic_on)

    def open_log(icon, item):
        os.startfile(log_path)  # type: ignore[attr-defined]

    def quit_app(icon, item):
        server.stop()
        capture.stop()
        icon.stop()

    updating = threading.Lock()

    def update(icon, item):
        if not updating.acquire(blocking=False):
            notify("Mise à jour déjà en cours…")
            return

        def work():
            from .updater import Updater
            try:
                notify("Recherche d'une mise à jour…")
                result = Updater().run()
                notify(result.message)
                if result.restart_needed:
                    time.sleep(1.5)  # laisse le temps de lire la notification
                    log.info("Redémarrage après mise à jour")
                    subprocess.Popen(restart_command(server.cfg.get("_path")),
                                     cwd=str(config_mod.ROOT),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                     | getattr(subprocess, "DETACHED_PROCESS", 0))
                    quit_app(icon, item)
            except Exception as exc:  # ne jamais faire tomber l'icône
                log.exception("mise à jour")
                notify(f"Mise à jour impossible : {exc}")
            finally:
                updating.release()

        threading.Thread(target=work, name="update", daemon=True).start()

    menu = Menu(
        Item(lambda i: server.status_text(), None, enabled=False),
        Item("Ouvrir SetupDouble…", show_dashboard),
        Item(f"Basculer ({hotkey_text})", toggle, default=True, visible=False),
        Menu.SEPARATOR,
        Item("Contrôler le Mac", to_mac, checked=lambda i: server.remote, radio=True,
             enabled=lambda i: server.connected or server.can_wake),
        Item("Contrôler le PC", to_pc, checked=lambda i: not server.remote, radio=True),
        Menu.SEPARATOR,
        Item("Son du Mac dans le casque", sound, checked=lambda i: server.sound_on,
             enabled=lambda i: server.acfg["enabled"]),
        Item("Micro vers le Mac", mic, checked=lambda i: server.mic_on,
             enabled=lambda i: server.acfg["enabled"]),
        Item(lambda i: f"Audio : {server.audio_status}", None, enabled=False),
        Menu.SEPARATOR,
        Item("Ouvrir le journal", open_log),
        Item("Mettre à jour", update),
        Item("Quitter", quit_app),
    )
    icon = pystray.Icon("SetupDouble", make_icon(False, False), "SetupDouble", menu)

    def refresh():
        icon.icon = make_icon(server.remote, server.connected)
        icon.title = f"SetupDouble – {server.status_text()}"
        icon.update_menu()

    def notify(msg: str):
        log.info("%s", msg)
        try:
            icon.notify(msg, "SetupDouble")
        except Exception:
            pass

    server.listeners.append(refresh)
    server.notify = notify
    icon.run(setup=lambda ic: (setattr(ic, "visible", True), refresh()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SetupDouble – serveur Windows")
    parser.add_argument("--config", help="chemin du config.toml")
    parser.add_argument("--no-tray", action="store_true", help="sans icône (console)")
    parser.add_argument("--wait-instance", action="store_true",
                        help="attend la fermeture de l'instance précédente (après mise à jour)")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "SetupDouble", 0x10)
        return 2
    log_path = setup_logging(cfg["log_level"])

    if not single_instance(wait=20.0 if args.wait_instance else 0.0):
        log.error("SetupDouble tourne déjà")
        return 1
    set_dpi_aware()

    s = cfg["server"]
    hotkeys = []
    for key, action in (("hotkey_toggle", "toggle"), ("hotkey_mac", "mac"), ("hotkey_pc", "pc")):
        if s.get(key):
            hotkeys.append(hotkeys_mod.parse_hotkey(s[key], action))
    if not hotkeys:
        log.error("Aucun raccourci configuré")
        return 2

    server = Server(cfg, clipboard=Clip())
    capture = win_input.InputCapture(hotkeys, server.on_hotkey, server.sink, server.on_mode)
    server.input = capture
    server.hotkey_check = lambda: hotkey_check(capture, hotkeys)
    server.open_log = lambda: os.startfile(log_path)  # type: ignore[attr-defined]
    server.wake_store = LOG_DIR / "mac.json"
    capture.start()
    server.start()
    dashboard.start(server, int(s["ui_port"]))
    log.info("SetupDouble prêt – raccourci : %s", ", ".join(h.text for h in hotkeys))

    if args.no_tray:
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.stop()
        capture.stop()
    else:
        run_tray(server, capture, log_path, hotkeys[0].text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
