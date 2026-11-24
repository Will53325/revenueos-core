"""One-time Gmail authorization (installed-app OAuth with a loopback redirect).

The owner creates an OAuth client of type "Desktop app" in their own Google
Cloud project, downloads client_secret.json, then runs:
    python3 -m caraway mail connect --client-secret ~/Downloads/client_secret.json
A browser opens on Google's consent screen; the refresh token is stored in
the macOS Keychain (service 'caraway-gmail'), or on Linux in CARAWAY_SECRETS_DIR/gmail-oauth.json
(mode 600). Nothing is stored in the repo.
Connecting does not enable sending; that still needs the policy and control.
"""
from __future__ import annotations

import json
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .. import net
from .transport import keychain_set

SCOPES = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def connect(client_secret_path: str, port: int = 0, open_browser: bool = True) -> str:
    """On a headless server: `mail connect --client-secret F --port 8788 --no-browser`, then from your
    laptop `ssh -L 8788:127.0.0.1:8788 <vm>` and open the printed URL in your laptop's browser."""
    cfg = json.load(open(client_secret_path))
    c = cfg.get("installed") or cfg.get("web") or {}
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Caraway: Gmail authorization received. You can close this tab.")

    srv = HTTPServer(("127.0.0.1", port), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state})
    print("Open Google's consent page" + ("" if open_browser else " in your laptop browser (SSH tunnel to this port)") + ":\n" + url)
    if open_browser:
        webbrowser.open(url)
    srv.handle_request()
    if result.get("state") != state or "code" not in result:
        raise RuntimeError(f"authorization failed: {result.get('error', 'state mismatch')}")
    body = urllib.parse.urlencode({"code": result["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    tok = json.loads(net.request("https://oauth2.googleapis.com/token", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body))
    keychain_set("oauth", json.dumps({"client_id": c["client_id"], "client_secret": c["client_secret"],
                                      "refresh_token": tok["refresh_token"]}))
    return "Gmail connected. Sending stays off until comms_policy.json enabled=true and `caraway mail enable`."
