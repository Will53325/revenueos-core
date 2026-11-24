"""The only way Caraway talks to the internet.

* Host allowlist enforced in code (config.ALLOWED_HOSTS).
* Per-host rate limiting, bounded retries with backoff on 429/5xx.
* Response size cap. Secrets are passed in headers and never logged.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import config

_last_call: dict = {}


class NetError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class HostNotAllowed(NetError):
    pass


def _check_host(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in config.ALLOWED_HOSTS:
        raise HostNotAllowed(f"host not on allowlist: {host}")
    if parsed.scheme != "https" and host not in ("127.0.0.1", "localhost", "ollama", "caraway-ollama"):
        raise HostNotAllowed(f"refusing non-https url for {host}")
    return host


def _throttle(host: str) -> None:
    gap = config.ALLOWED_HOSTS.get(host, 2.0)
    wait = _last_call.get(host, 0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def request(url: str, *, method: str = "GET", body: Any = None, headers: Optional[dict] = None,
            timeout: float = 45, retries: int = 3, data: Optional[bytes] = None) -> bytes:
    host = _check_host(url)
    hdrs = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        _throttle(host)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(config.MAX_RESPONSE_BYTES + 1)
                if len(raw) > config.MAX_RESPONSE_BYTES:
                    raise NetError(f"response from {host} exceeded size cap")
                return raw
        except urllib.error.HTTPError as e:
            last = NetError(f"HTTP {e.code} from {host}", e.code)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
                time.sleep(min(delay, 60))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = NetError(f"network error contacting {host}: {getattr(e, 'reason', e)}")
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last
    raise last or NetError("request failed")


def get_json(url: str, params: Optional[dict] = None, **kw) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return json.loads(request(url, **kw))


def post_json(url: str, body: Any, **kw) -> Any:
    return json.loads(request(url, method="POST", body=body, **kw))
