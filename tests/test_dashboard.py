"""Tableau de bord : état JSON, actions depuis le PC et depuis le Mac, sécurité HTTP."""

import http.client
import json
import re

import pytest

from setupdouble import dashboard
from setupdouble import protocol as P

from test_integration import wait_for


def get(dash, path, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=3)
    headers = {"X-SD-Token": dash.token} if token else {}
    if host:
        headers["Host"] = host
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    return r.status, r.read()


def post(dash, action, value=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-SD-Token"] = dash.token
    conn.request("POST", "/api/action", json.dumps({"action": action, "value": value}), headers)
    r = conn.getresponse()
    return r.status, json.loads(r.read() or b"{}")


@pytest.fixture
def dashes(pair):
    server, fake, client, *_ = pair
    client.accessibility = lambda: True
    d_pc = dashboard.Dashboard(server, 0).start()
    d_mac = dashboard.Dashboard(client, 0).start()
    yield server, client, d_pc, d_mac
    d_pc.stop()
    d_mac.stop()


def status(dash):
    code, body = get(dash, "/api/status")
    assert code == 200
    return json.loads(body)


def test_page_embeds_token_and_status_is_consistent(dashes):
    server, client, d_pc, d_mac = dashes
    code, page = get(d_pc, "/", token=False)
    assert code == 200 and d_pc.token.encode() in page and b"{{TOKEN}}" not in page

    pc = status(d_pc)
    assert pc["role"] == "pc" and pc["link"]["state"] == "connected" and pc["active"] == "pc"
    assert pc["machines"]["pc"]["here"] and not pc["machines"]["mac"]["here"]
    assert pc["hotkey"] == "Ctrl + Alt + Espace"

    # le Mac connaît le raccourci et l'état du PC ; le PC voit les vérifications du Mac
    assert wait_for(lambda: status(d_mac)["hotkey"] == "Ctrl + Alt + Espace")
    assert wait_for(lambda: any(c["machine"] == "mac" and c["id"] == "accessibility"
                                and c["state"] == "ok" for c in status(d_pc)["checks"]))
    mac = status(d_mac)
    assert mac["role"] == "mac" and mac["link"]["state"] == "connected"
    assert mac["machines"]["mac"]["here"]


def test_switch_from_pc_dashboard(dashes):
    server, client, d_pc, d_mac = dashes
    assert post(d_pc, "mac") == (200, {"ok": True, "message": ""})
    assert wait_for(lambda: client.active)
    assert status(d_mac)["active"] == "mac"
    post(d_pc, "pc")
    assert wait_for(lambda: not client.active)


def test_switch_and_audio_from_mac_dashboard(dashes):
    server, client, d_pc, d_mac = dashes
    assert post(d_mac, "mac")[0] == 200
    assert wait_for(lambda: server.remote and client.active)
    post(d_mac, "toggle")
    assert wait_for(lambda: not server.remote and not client.active)

    # audio désactivé dans la config de test : refus propre des deux côtés
    code, body = post(d_mac, "sound", False)
    assert code == 409 and "désactivé" in body["message"]
    code, body = post(d_pc, "mic", False)
    assert code == 409


def test_mac_audio_request_reaches_server(pair):
    server, fake, client, *_ = pair
    server.acfg["enabled"] = True  # sans carte son : seuls les drapeaux changent
    server.set_sound(True)
    server.set_mic(True)
    client._send(P.encode(P.REQ_AUDIO, bytes([P.AUDIO_MIC_ON])))
    assert wait_for(lambda: not server.sound_on and server.mic_on)
    assert wait_for(lambda: client.audio_flags == P.AUDIO_MIC_ON)


def test_refuses_without_token_or_foreign_host(dashes):
    server, client, d_pc, d_mac = dashes
    assert get(d_pc, "/api/status", token=False)[0] == 401
    assert post(d_pc, "mac", token=False)[0] == 401
    assert not server.remote
    assert get(d_pc, "/", host="evil.example:80")[0] == 403
    assert get(d_pc, "/api/status", host="evil.example")[0] == 403
    assert get(d_pc, "/ping.gif", token=False)[0] == 200


def test_mac_dashboard_without_connection():
    from test_integration import make_cfg
    from setupdouble.client_mac import Client, RecordingInjector

    client = Client(make_cfg(), RecordingInjector(), use_audio=False)
    d = dashboard.Dashboard(client, 0).start()
    try:
        s = status(d)
        assert s["link"]["state"] == "searching" and s["active"] == "pc"
        assert all(c["machine"] == "mac" for c in s["checks"])
        code, body = post(d, "mac")
        assert code == 409 and "pas connecté" in body["message"]
    finally:
        d.stop()


def test_ui_page_has_no_dynamic_innerhtml():
    """Les textes venant de l'autre machine passent par textContent, jamais innerHTML."""
    html = (dashboard.UI_DIR / "index.html").read_text(encoding="utf-8")
    assignments = re.findall(r"innerHTML\s*=\s*([^;]+);", html)
    assert assignments == ['"<i></i><i></i><i></i>"']
