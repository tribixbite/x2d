"""cloud_client — minimal Bambu Lab cloud REST client.

Wraps the public-knowledge endpoints (login, refresh, bind list, profile,
print history) so the shim's currently-stubbed cloud entry points
(`bambu_network_is_user_login`, `get_user_id`, `get_user_presets`,
`get_user_tasks`) can return real data when the user has logged in.

Endpoints reverse-engineered from open-source consumers of the same
API: pybambu (Home Assistant), bambu-farm-manager, bambu-node, OrcaSlicer.
None of this is from Bambu's own SDK — they don't publish one. If
Bambu rotates an endpoint, all of those projects break too; rotate
this module in lockstep.

Tokens live in ~/.x2d/cloud_session.json (chmod 600). The file holds
{access_token, refresh_token, expires_at, user_id, region}. We
auto-refresh when the access_token's `expires_at` is within 5 minutes.

Usage from CLI:
    x2d_bridge.py cloud-login --email me@x.com --password '…'
    x2d_bridge.py cloud-status

Usage from the bridge (dispatch):
    cli = CloudClient.load_or_anonymous()
    cli.is_logged_in()              # bool
    cli.get_user_id()               # str | None
    cli.get_user_presets()          # dict[name, dict]
    cli.get_user_tasks(limit=20)    # list[dict]
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bambu has two regional clouds. The login endpoint disambiguates from
# the email's TLD; users can also force one via X2D_REGION=us|cn.
REGIONS: dict[str, dict[str, str]] = {
    "us": {
        "api":  "https://api.bambulab.com",
        "iot":  "https://api.bambulab.com",
    },
    "cn": {
        "api":  "https://api.bambulab.cn",
        "iot":  "https://api.bambulab.cn",
    },
}

SESSION_PATH = Path.home() / ".x2d" / "cloud_session.json"
DEFAULT_TIMEOUT = 15
USER_AGENT = "BambuStudio/02.06.00.51 x2d-cloud-client/0.1"


@dataclass
class Session:
    access_token: str = ""
    refresh_token: str = ""
    # Unix-epoch seconds; 0 means "unknown / treat as expired".
    expires_at: float = 0.0
    user_id: str = ""
    region: str = "us"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        # 5-minute safety margin so a long-running call doesn't 401 mid-way.
        return self.access_token == "" or time.time() >= (self.expires_at - 300)

    @property
    def empty(self) -> bool:
        return not self.access_token

    def to_json(self) -> str:
        return json.dumps({
            "access_token":  self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at":    self.expires_at,
            "user_id":       self.user_id,
            "region":        self.region,
            "extra":         self.extra,
        }, indent=2)

    @classmethod
    def from_json(cls, blob: str) -> "Session":
        d = json.loads(blob)
        return cls(
            access_token=d.get("access_token", ""),
            refresh_token=d.get("refresh_token", ""),
            expires_at=float(d.get("expires_at", 0.0)),
            user_id=d.get("user_id", ""),
            region=d.get("region", "us"),
            extra=d.get("extra", {}),
        )


class CloudError(RuntimeError):
    """HTTP / API failure. Carries the status code so callers can branch
    on 401 (re-login) vs 5xx (transient)."""

    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def _request(method: str,
             url: str,
             *,
             body: dict | None = None,
             headers: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One HTTP round-trip returning parsed JSON. Raises CloudError on
    non-2xx or unparseable response."""
    h = {
        "User-Agent":   USER_AGENT,
        "Accept":       "application/json",
    }
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)

    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, method=method, data=data, headers=h)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            payload = r.read().decode("utf-8", errors="replace")
            try:
                return json.loads(payload)
            except json.JSONDecodeError as e:
                raise CloudError(f"non-JSON response: {payload[:200]}", r.status, payload) from e
    except urllib.error.HTTPError as e:
        body_str = ""
        try:
            body_str = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # Bambu returns JSON error bodies; pull the message if present.
        msg = body_str
        try:
            j = json.loads(body_str)
            msg = j.get("message") or j.get("error_msg") or body_str
        except Exception:
            pass
        raise CloudError(f"HTTP {e.code} on {method} {url}: {msg}",
                         status=e.code, body=body_str) from e
    except urllib.error.URLError as e:
        raise CloudError(f"network failure on {method} {url}: {e.reason}") from e


class CloudClient:
    """Thin wrapper around Session + the few endpoints we actually call.

    All methods return Python primitives (no SDK objects) so the bridge
    can JSON-serialize the responses straight back to the shim."""

    def __init__(self, session: Session | None = None):
        self.session = session or Session()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load_or_anonymous(cls, path: Path = SESSION_PATH) -> "CloudClient":
        """Load session from disk if present, otherwise return an empty
        client (every getter will report "not logged in")."""
        if path.exists():
            try:
                return cls(Session.from_json(path.read_text()))
            except (json.JSONDecodeError, KeyError, ValueError):
                # Corrupt session file — leave the file alone (user can
                # inspect it) and run anonymous. Don't crash the bridge.
                return cls(Session())
        return cls(Session())

    def save(self, path: Path = SESSION_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then atomically rename so a crash mid-write
        # doesn't leave an unparseable JSON.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.session.to_json())
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def logout(self, path: Path = SESSION_PATH) -> None:
        self.session = Session()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @staticmethod
    def dry_run_check(region: str = "us") -> dict:
        """Validate that the cloud endpoint is reachable WITHOUT
        sending credentials. Hits the login URL with a GET (instead
        of the required POST) — a 405 response proves DNS, TLS, and
        the API route all resolve. Useful as a smoke test from CI or
        during install.sh validation. Returns:
            {'ok': bool,
             'status': int (HTTP code, 0 on transport error),
             'region': str,
             'endpoint': str,
             'message': str}"""
        if region not in REGIONS:
            return {"ok": False, "status": 0, "region": region,
                    "endpoint": "",
                    "message": f"unknown region {region!r}; expected us|cn"}
        url = REGIONS[region]["api"] + "/v1/user-service/user/login"
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": USER_AGENT,
            "Accept":     "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT,
                                        context=ssl.create_default_context()) as r:
                # 200 here would be unexpected (login should require POST)
                # but still proves reachability.
                return {"ok": True, "status": r.status, "region": region,
                        "endpoint": url,
                        "message": f"unexpected success (status {r.status}); "
                                   "endpoint reached"}
        except urllib.error.HTTPError as e:
            # 405 (Method Not Allowed) is the EXPECTED response — proves
            # the path exists. 404 means Bambu rotated the route.
            ok = e.code in (405, 400, 401, 403)
            return {"ok": ok, "status": e.code, "region": region,
                    "endpoint": url,
                    "message": f"HTTP {e.code} from {url} — "
                               f"{'endpoint reachable' if ok else 'endpoint missing or moved'}"}
        except urllib.error.URLError as e:
            return {"ok": False, "status": 0, "region": region,
                    "endpoint": url,
                    "message": f"network error: {e.reason}"}

    def login(self, email: str, password: str, region: str | None = None) -> None:
        """Exchange email + password for an access_token / refresh_token
        pair. Region defaults to "us" unless email ends with .cn or
        the user passes region="cn"."""
        if not region:
            region = "cn" if email.lower().endswith(".cn") else "us"
        if region not in REGIONS:
            raise ValueError(f"unknown region {region!r}; expected us|cn")
        url = REGIONS[region]["api"] + "/v1/user-service/user/login"
        # Bambu's login API accepts both account/password (legacy) and
        # email/password. We send the email under both keys to maximise
        # compatibility across endpoint revisions.
        body = {
            "account":  email,
            "email":    email,
            "password": password,
            "apiError": "",
        }
        r = _request("POST", url, body=body)
        access  = r.get("accessToken")  or r.get("access_token")  or ""
        refresh = r.get("refreshToken") or r.get("refresh_token") or ""
        # Bambu's response sometimes carries `expiresIn` (seconds) and
        # sometimes `expiresAt` (epoch). Handle both, default to 1h ttl
        # if missing entirely.
        expires_in = r.get("expiresIn") or r.get("expires_in")
        expires_at = r.get("expiresAt") or r.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            expiry = float(expires_at)
        elif isinstance(expires_in, (int, float)) and expires_in > 0:
            expiry = time.time() + float(expires_in)
        else:
            expiry = time.time() + 3600
        if not access:
            raise CloudError("login response missing access_token", body=str(r))
        self.session = Session(
            access_token=access,
            refresh_token=refresh,
            expires_at=expiry,
            user_id=str(r.get("userId") or r.get("uid") or ""),
            region=region,
            extra={k: v for k, v in r.items()
                   if k not in {"accessToken", "access_token",
                                "refreshToken", "refresh_token",
                                "expiresIn", "expires_in",
                                "expiresAt", "expires_at"}},
        )
        self.save()

    def refresh(self) -> None:
        """Exchange the refresh_token for a new access_token. Raises
        CloudError if the refresh token is rejected — caller must
        re-login interactively."""
        if not self.session.refresh_token:
            raise CloudError("no refresh_token; need to login first")
        url = REGIONS[self.session.region]["api"] + "/v1/user-service/user/refreshtoken"
        r = _request("POST", url, body={"refreshToken": self.session.refresh_token})
        self.session.access_token  = r.get("accessToken")  or r.get("access_token")  or self.session.access_token
        self.session.refresh_token = r.get("refreshToken") or r.get("refresh_token") or self.session.refresh_token
        expires_in = r.get("expiresIn") or r.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            self.session.expires_at = time.time() + float(expires_in)
        else:
            self.session.expires_at = time.time() + 3600
        self.save()

    def is_logged_in(self) -> bool:
        return not self.session.empty

    def _ensure_fresh(self) -> None:
        """Refresh the access_token if it's about to expire. Idempotent."""
        if self.session.empty:
            raise CloudError("not logged in")
        if self.session.expired:
            self.refresh()

    def _authed_get(self, path: str) -> dict:
        self._ensure_fresh()
        url = REGIONS[self.session.region]["iot"] + path
        return _request("GET", url, headers={
            "Authorization": f"Bearer {self.session.access_token}",
        })

    # ------------------------------------------------------------------
    # Endpoints — only the few the shim actually consumes
    # ------------------------------------------------------------------

    def get_user_id(self) -> str:
        if self.session.user_id:
            return self.session.user_id
        # Fallback: hit /my/profile to derive it.
        r = self._authed_get("/v1/design-user-service/my/profile")
        uid = str(r.get("uidStr") or r.get("uid") or r.get("userId") or "")
        if uid:
            self.session.user_id = uid
            self.save()
        return uid

    def get_bound_devices(self) -> list[dict]:
        """List the printers tied to this account. Each entry has
        dev_id, dev_name, online, online_status. The shim doesn't
        currently consume this but it's the basis for any future
        cloud-side print monitoring."""
        r = self._authed_get("/v1/iot-service/api/user/bind")
        return r.get("devices") or r.get("data") or []

    def get_user_presets(self) -> dict:
        """Cloud-synced filament + print + printer presets for the
        logged-in user. Shape mirrors Slic3r::PresetCollection: a dict
        with three keys (filament, print, printer), each a dict from
        preset name → preset settings."""
        r = self._authed_get("/v1/iot-service/api/user/preset")
        # Bambu's response groups by preset type; we re-key so the shim
        # can pass it straight to PresetCollection::load_user_presets.
        out: dict[str, dict] = {"filament": {}, "print": {}, "printer": {}}
        for item in r.get("presets") or r.get("data") or []:
            kind = (item.get("type") or "").lower()
            name = item.get("name") or item.get("setting_name") or ""
            if kind in out and name:
                out[kind][name] = item.get("setting") or item
        return out

    def get_user_tasks(self, limit: int = 20) -> list[dict]:
        """Recent print history (cloud queue + ad-hoc). Each entry has
        task_id, design_id, plate, start_time, end_time, status, etc."""
        path = f"/v1/iot-service/api/user/print?limit={int(limit)}"
        r = self._authed_get(path)
        return r.get("tasks") or r.get("hits") or r.get("data") or []
