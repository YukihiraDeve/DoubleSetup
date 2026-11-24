"""Serveur : la machine où sont branchés clavier, souris et carte son.

Windows, macOS ou Linux. Icône dans la barre des tâches / de menus quand
c'est possible, sinon fonctionnement sans icône (la fenêtre de statut suffit).

    python -m setupdouble.server [--config config.toml] [--no-tray]
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as config_mod
from . import dashboard
from . import hotkeys as hotkeys_mod
from . import platforms
from .server_core import Server

log = logging.getLogger("setupdouble")

LOG_DIR = platforms.log_dir()
OS_SHORT = {"windows": "WIN", "mac": "MAC", "linux": "LNX"}


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
    """Windows : coordonnées en pixels physiques, quel que soit le zoom."""
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def parse_hotkeys(scfg: dict) -> list:
    hotkeys = []
    for key, action in (("hotkey_toggle", "toggle"), ("hotkey_remote", "remote"),
                        ("hotkey_local", "local")):
        if scfg.get(key):
            hotkeys.append(hotkeys_mod.parse_hotkey(scfg[key], action))
    return hotkeys


def hotkey_check(capture, hotkeys) -> dict:
    text = ", ".join(hotkeys_mod.pretty(h.text) for h in hotkeys)
    if getattr(capture, "_fallback_kb_hook", None):
        return dashboard.check("hotkey", "Raccourci clavier", "warn",
                               f"{text} : déjà pris par une autre appli, mode de secours")
    return dashboard.check("hotkey", "Raccourci clavier", "ok", text)


def make_icon(label: str, remote: bool, connected: bool):
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (104, 72, 230, 255) if remote else (64, 64, 72, 255)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=14, fill=bg)
    font = None
    for name in ("segoeuib.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, 22)
            break
        except Exception:
            continue
    d.text((size / 2, size / 2), label, fill="white", font=font, anchor="mm")
    dot = (61, 220, 151, 255) if connected else (255, 95, 126, 255)
    d.ellipse((size - 22, size - 22, size - 4, size - 4), fill=dot, outline="white", width=2)
    return img


def open_dashboard(config_path: str | None = None) -> None:
    """Ouvre la fenêtre de statut dans un processus séparé."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.exists() else exe), "-m", "setupdouble.app"]
    if config_path:
        cmd += ["--config", config_path]
    subprocess.Popen(cmd, cwd=str(config_mod.ROOT),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_tray(server: Server, capture, log_path: Path, hotkey_text: str,
             restart, stop_all) -> None:
    import pystray
    from pystray import Menu, MenuItem as Item

    def client_name() -> str:
        return server.session.name if server.session else "la machine pilotée"

    def quit_app(icon, item):
        stop_all()
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
                    icon.stop()
                    restart()
            except Exception as exc:  # ne jamais faire tomber l'icône
                log.exception("mise à jour")
                notify(f"Mise à jour impossible : {exc}")
            finally:
                updating.release()

        threading.Thread(target=work, name="update", daemon=True).start()

    menu = Menu(
        Item(lambda i: server.status_text(), None, enabled=False),
        Item("Ouvrir SetupDouble…", lambda i, it: open_dashboard(server.cfg.get("_path"))),
        Item(f"Basculer ({hotkey_text})", lambda i, it: server.request_mode(not server.remote),
             default=True, visible=False),
        Menu.SEPARATOR,
        Item(lambda i: f"Contrôler {client_name()}", lambda i, it: server.request_mode(True),
             checked=lambda i: server.remote, radio=True,
             enabled=lambda i: server.connected or server.can_wake),
        Item("Contrôler cet ordinateur", lambda i, it: server.request_mode(False),
             checked=lambda i: not server.remote, radio=True),
        Menu.SEPARATOR,
        Item("Son de la machine pilotée", lambda i, it: server.set_sound(not server.sound_on),
             checked=lambda i: server.sound_on, enabled=lambda i: server.acfg["enabled"]),
        Item("Micro partagé", lambda i, it: server.set_mic(not server.mic_on),
             checked=lambda i: server.mic_on, enabled=lambda i: server.acfg["enabled"]),
        Item(lambda i: f"Audio : {server.audio_status}", None, enabled=False),
        Menu.SEPARATOR,
        Item("Ouvrir le journal", lambda i, it: platforms.open_path(log_path)),
        Item("Mettre à jour", update),
        Item("Quitter", quit_app),
    )

    def label() -> str:
        if server.remote:
            peer_os = (server.session.peer_info.get("os") if server.session else "") or ""
            return OS_SHORT.get(peer_os, "→")
        return OS_SHORT.get(platforms.OS, "ICI")

    icon = pystray.Icon("SetupDouble", make_icon(label(), False, False), "SetupDouble", menu)

    def refresh():
        icon.icon = make_icon(label(), server.remote, server.connected)
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


def show_error(message: str) -> None:
    print(message, file=sys.stderr)
    if platforms.OS == platforms.WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "SetupDouble", 0x10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SetupDouble – serveur")
    parser.add_argument("--config", help="chemin du config.toml")
    parser.add_argument("--no-tray", action="store_true", help="sans icône")
    parser.add_argument("--wait-instance", action="store_true",
                        help="attend la fin de l'instance précédente (redémarrage)")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as exc:
        show_error(str(exc))
        return 2
    log_path = setup_logging(cfg["log_level"])

    if not platforms.single_instance("Server", wait=20.0 if args.wait_instance else 0.0):
        log.error("SetupDouble (serveur) tourne déjà")
        return 1
    if platforms.OS == platforms.WINDOWS:
        set_dpi_aware()
    elif platforms.OS == platforms.MAC:
        platforms.request_input_permission()  # la capture demande l'Accessibilité

    s = cfg["server"]
    try:
        hotkeys = parse_hotkeys(s)
    except ValueError as exc:
        show_error(f"Raccourci invalide : {exc}")
        return 2
    if not hotkeys:
        log.error("Aucun raccourci configuré")
        return 2

    server = Server(cfg, clipboard=platforms.make_clipboard())
    capture = platforms.make_capture(hotkeys, server.on_hotkey, server.sink, server.on_mode, s)
    server.input = capture
    server.hotkey_check = lambda: hotkey_check(capture, hotkeys)
    server.open_log = lambda: platforms.open_path(log_path)
    server.wake_store = LOG_DIR / "mac.json"
    try:
        capture.start()
    except PermissionError as exc:
        show_error(str(exc))
        return 3
    server.start()

    def stop_all():
        server.stop()
        capture.stop()

    def restart():
        stop_all()
        platforms.restart_self("setupdouble.server", ["--config", cfg["_path"]]
                               + (["--no-tray"] if args.no_tray else []))

    server.restart = restart
    dashboard.start(server, int(s["ui_port"]))
    log.info("SetupDouble prêt – raccourci : %s", ", ".join(h.text for h in hotkeys))

    tray = not args.no_tray
    if tray:
        try:
            run_tray(server, capture, log_path, hotkeys[0].text, restart, stop_all)
            return 0
        except Exception as exc:  # pas de barre système (Linux sans AppIndicator…)
            log.warning("Icône indisponible (%s) : fonctionnement sans icône", exc)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
