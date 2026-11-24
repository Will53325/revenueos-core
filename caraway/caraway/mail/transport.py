"""Mail transports.

DryRunTransport writes RFC 5322 .eml files to var/outbox (used for tests and
for reviewing exactly what would be sent). GmailTransport uses the Gmail API
with an OAuth refresh token kept in the macOS Keychain, never in files or the
database. Neither is used unless compliance authorizes the send.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.parse
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from .. import config, net

KEYCHAIN_SERVICE = "caraway-gmail"


def build_mime(sender: dict, to: str, subject: str, text: str, html: str,
               in_reply_to: Optional[str] = None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = f"{sender.get('name') or 'Caraway Housing LLC'} <{sender.get('email') or 'unset@invalid'}>"
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=(sender.get("email") or "caraway.invalid").split("@")[-1])
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m["List-Unsubscribe"] = f"<mailto:{sender.get('email') or 'unset@invalid'}?subject=remove>"
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    return m


class DryRunTransport:
    name = "dry_run"

    def __init__(self, outbox: Optional[Path] = None):
        self.outbox = Path(outbox or config.HOME / "outbox")

    def configured(self) -> bool:
        return True

    def send(self, mime: EmailMessage) -> dict:
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"{mime['Message-ID'].strip('<>').replace('@', '_at_')}.eml"
        path.write_bytes(bytes(mime))
        return {"transport": self.name, "id": mime["Message-ID"], "path": str(path)}


# ---------------------------------------------------------------- Gmail

def keychain_get(account: str) -> Optional[str]:
    if sys.platform != "darwin":
        return None
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def keychain_set(account: str, secret: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Keychain storage requires macOS")
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", secret],
                   check=True, capture_output=True)


class GmailTransport:
    """Gmail API (gmail.send + gmail.readonly scopes). Configure with `python3 -m caraway mail connect`."""
    name = "gmail"
    API = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self):
        self._token: Optional[str] = None

    def _creds(self) -> Optional[dict]:
        raw = keychain_get("oauth")
        return json.loads(raw) if raw else None

    def configured(self) -> bool:
        return bool(self._creds())

    def _access_token(self) -> str:
        if self._token:
            return self._token
        c = self._creds()
        if not c:
            raise RuntimeError("Gmail is not connected")
        body = urllib.parse.urlencode({"client_id": c["client_id"], "client_secret": c["client_secret"],
                                       "refresh_token": c["refresh_token"], "grant_type": "refresh_token"}).encode()
        resp = json.loads(net.request("https://oauth2.googleapis.com/token", method="POST", body=None,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body))
        self._token = resp["access_token"]
        return self._token

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def send(self, mime: EmailMessage) -> dict:
        raw = base64.urlsafe_b64encode(bytes(mime)).decode()
        resp = net.post_json(f"{self.API}/messages/send", {"raw": raw}, headers=self._auth())
        return {"transport": self.name, "id": mime["Message-ID"], "gmail_id": resp.get("id"), "thread": resp.get("threadId")}

    def list_inbound(self, query: str = "newer_than:14d -from:me") -> list:
        data = net.get_json(f"{self.API}/messages", {"q": query, "maxResults": 100}, headers=self._auth())
        return [m["id"] for m in data.get("messages", [])]

    def fetch_raw(self, gmail_id: str) -> bytes:
        data = net.get_json(f"{self.API}/messages/{gmail_id}", {"format": "raw"}, headers=self._auth())
        return base64.urlsafe_b64decode(data["raw"] + "==")


def get_transport(name: Optional[str] = None):
    if name == "dry_run":
        return DryRunTransport()
    g = GmailTransport()
    return g if g.configured() else None
