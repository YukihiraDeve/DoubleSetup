"""Tableau de bord local : sert l'interface et l'état en JSON sur 127.0.0.1.

Le serveur Windows et l'agent Mac en lancent chacun un. La fenêtre
(``python -m setupdouble.app``) se contente d'afficher la page.

Protection :
  * écoute uniquement sur 127.0.0.1 ;
  * l'en-tête ``Host`` doit être 127.0.0.1/localhost (anti DNS rebinding) ;
  * chaque appel ``/api`` exige un jeton aléatoire, inclus dans la page
    servie. Un autre site ouvert dans un navigateur ne peut ni lire la page
    (même origine) ni envoyer cet en-tête sans pré-vérification CORS, que
    l'on ne valide jamais.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent / "ui"
TOKEN_HEADER = "X-SD-Token"
_PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
          b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class Provider(Protocol):
    def status(self) -> dict[str, Any]: ...
    def action(self, name: str, value: Any) -> tuple[bool, str]: ...


def check(cid: str, label: str, state: str, detail: str = "") -> dict[str, str]:
    """Une ligne de vérification. ``state`` : ok, warn, error ou off."""
    return {"id": cid, "label": label, "state": state, "detail": detail}


class Dashboard:
    def __init__(self, provider: Provider, port: int, bind: str = "127.0.0.1"):
        self.provider = provider
        self.token = secrets.token_urlsafe(24)
        self._httpd = ThreadingHTTPServer((bind, port), self._handler_class())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> "Dashboard":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="dashboard",
                                        daemon=True)
        self._thread.start()
        log.info("Tableau de bord : %s", self.url)
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def page(self) -> bytes:
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        return html.replace("{{TOKEN}}", self.token).encode("utf-8")

    def _handler_class(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SetupDouble"

            def log_message(self, fmt, *args):  # silence : la page interroge chaque seconde
                log.debug("ui %s", fmt % args)

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                return host in _ALLOWED_HOSTS

            def _token_ok(self) -> bool:
                return secrets.compare_digest(self.headers.get(TOKEN_HEADER, ""), dash.token)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if ctype.startswith("text/html"):
                    self.send_header("Content-Security-Policy",
                                     "default-src 'self'; script-src 'unsafe-inline'; "
                                     "style-src 'unsafe-inline'; img-src 'self' data:; "
                                     "frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self):  # noqa: N802
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    return self._send(200, dash.page(), "text/html; charset=utf-8")
                if path == "/ping.gif":  # sonde utilisée par la page « agent arrêté »
                    return self._send(200, _PIXEL, "image/gif")
                if path == "/api/status":
                    if not self._token_ok():
                        return self._json(401, {"error": "jeton invalide"})
                    try:
                        return self._json(200, dash.provider.status())
                    except Exception as exc:  # pragma: no cover
                        log.exception("status")
                        return self._json(500, {"error": str(exc)})
                self._send(404, b"not found", "text/plain")

            def do_POST(self):  # noqa: N802
                if not self._host_ok() or self.path != "/api/action":
                    return self._send(403, b"forbidden", "text/plain")
                if not self._token_ok():
                    return self._json(401, {"error": "jeton invalide"})
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 4096)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    name = str(body.get("action", ""))
                except (ValueError, AttributeError):
                    return self._json(400, {"ok": False, "message": "requête invalide"})
                try:
                    ok, message = dash.provider.action(name, body.get("value"))
                except Exception as exc:
                    log.exception("action %s", name)
                    ok, message = False, str(exc)
                self._json(200 if ok else 409, {"ok": ok, "message": message})

        return Handler


def start(provider: Provider, port: int) -> Dashboard | None:
    """Démarre le tableau de bord, sans jamais empêcher l'appli de tourner."""
    if not port:
        return None
    try:
        return Dashboard(provider, int(port)).start()
    except OSError as exc:
        log.warning("Tableau de bord indisponible sur le port %s : %s", port, exc)
        return None
