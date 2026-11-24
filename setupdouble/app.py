"""Ouvre la fenêtre SetupDouble (tableau de bord).

    python -m setupdouble.app [--config config.toml] [--port N] [--browser]

Sur le Mac : fenêtre native (WebKit). Sous Windows : Edge en mode
application, sinon le navigateur par défaut. La fenêtre affiche la page
servie par l'agent (Mac) ou le serveur (PC) qui tourne déjà.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config as config_mod

DEFAULT_PORT = {"darwin": 47811, "win32": 47810}
LAUNCH_AGENT = "com.setupdouble.agent"


def ui_port(config_path: str | None) -> int:
    section = "client" if sys.platform == "darwin" else "server"
    try:
        cfg = config_mod.load(config_path)
        return int(cfg[section]["ui_port"])
    except Exception:  # config absente ou invalide : port par défaut
        return DEFAULT_PORT.get(sys.platform, 47811)


def reachable(url: str, timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(url + "ping.gif", timeout=timeout) as r:
            return r.status == 200
    except OSError:
        return False


def start_backend() -> None:
    """Relance l'agent (Mac) ou la tâche planifiée (Windows) s'il ne tourne pas."""
    try:
        if sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT}.plist"
            if plist.exists():
                uid = os.getuid()
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                               capture_output=True, timeout=5)
                subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{LAUNCH_AGENT}"],
                               capture_output=True, timeout=5)
        elif sys.platform == "win32":
            subprocess.run(["schtasks", "/Run", "/TN", "SetupDouble"], capture_output=True,
                           timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def offline_page(url: str) -> str:
    """Page affichée tant que l'agent ne répond pas : elle bascule toute seule dès qu'il revient."""
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>SetupDouble</title>
<style>
html,body{{margin:0;height:100%}}
body{{display:grid;place-items:center;font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:#f5f6fa;background:radial-gradient(900px 520px at 8% -10%,rgba(122,92,255,.45),transparent 60%),
linear-gradient(160deg,#1c1538,#0d1a36);-webkit-user-select:none;user-select:none}}
.box{{text-align:center;max-width:360px}}
.s{{width:26px;height:26px;margin:0 auto;border-radius:50%;border:2.5px solid rgba(255,255,255,.15);
border-top-color:#8ea8ff;animation:r 1s linear infinite}}@keyframes r{{to{{transform:rotate(360deg)}}}}
h2{{margin:14px 0 4px;font-size:15px}}p{{margin:0;color:rgba(235,238,255,.64)}}
</style></head><body><div class="box"><div class="s"></div><h2>SetupDouble ne répond pas</h2>
<p>L'agent ne tourne pas encore. Cette fenêtre se met à jour toute seule dès qu'il démarre.</p></div>
<script>
setInterval(function(){{var i=new Image();i.onload=function(){{location.href="{url}"}};
i.src="{url}ping.gif?"+Date.now()}},1500);
</script></body></html>"""


def find_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


def open_windows(url: str, online: bool) -> None:
    target = url
    if not online:
        page = Path(os.environ.get("TEMP", ".")) / "setupdouble-offline.html"
        page.write_text(offline_page(url), encoding="utf-8")
        target = page.as_uri()
    edge = find_edge()
    if edge:
        profile = Path(os.environ.get("LOCALAPPDATA", ".")) / "SetupDouble" / "ui-profile"
        subprocess.Popen([edge, f"--app={target}", "--window-size=900,720",
                          f"--user-data-dir={profile}", "--no-first-run",
                          "--disable-features=Translate"])
    else:
        webbrowser.open(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SetupDouble – fenêtre de statut")
    parser.add_argument("--config", help="chemin du config.toml")
    parser.add_argument("--port", type=int, help="port du tableau de bord")
    parser.add_argument("--browser", action="store_true", help="ouvrir dans le navigateur")
    args = parser.parse_args(argv)

    port = args.port or ui_port(args.config)
    url = f"http://127.0.0.1:{port}/"
    online = reachable(url)
    if not online:
        start_backend()
        for _ in range(8):
            time.sleep(0.5)
            if reachable(url):
                online = True
                break

    if args.browser:
        webbrowser.open(url)
        return 0
    if sys.platform == "darwin":
        try:
            from .mac_window import run_window
        except ImportError as exc:  # pyobjc-framework-WebKit absent
            print(f"Fenêtre native indisponible ({exc}), ouverture dans le navigateur.")
            webbrowser.open(url)
            return 0
        run_window(url, None if online else offline_page(url))
        return 0
    if sys.platform == "win32":
        open_windows(url, online)
        return 0
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
