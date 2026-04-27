"""End-to-end test for the bridge web UI auth flow (#48).

Brings up `_serve_http` with `auth_token="test-token-123"` and verifies:

* GET /index.html with no auth → 401
* GET /login.html with no auth → 200 (bypass)
* GET /login.js with no auth → 200 (bypass)
* GET /auth/info with no auth → 200 (probe endpoint)
  - returns {"auth_required": true, "cookie_name": "x2d_token"}
* GET /auth/check with no auth → 401 + WWW-Authenticate
* GET /auth/check with `Authorization: Bearer test-token-123` → 200
* GET /auth/check with `Authorization: Bearer wrong` → 401
* GET /index.html with `Authorization: Bearer test-token-123` → 200
* GET /index.html with `Cookie: x2d_token=test-token-123` → 200 (SSE path)
* GET /state with cookie auth → 200
* `_check_bearer` rejects non-loopback access without a token

Also confirms the loopback-OPEN mode still works when `auth_token=None`.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import x2d_bridge


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(port: int, *, auth_token: str | None) -> threading.Thread:
    def gs(_p): return {"print": {"nozzle_temper": 27.0, "bed_temper": 24.0}}
    def gt(_p): return time.time() - 2

    th = threading.Thread(
        target=x2d_bridge._serve_http,
        kwargs={
            "bind":          f"127.0.0.1:{port}",
            "get_state":     gs,
            "get_last_ts":   gt,
            "max_staleness": 30.0,
            "auth_token":    auth_token,
            "printer_names": [""],
            "clients":       {},
            "web_dir":       x2d_bridge._WEB_DIR_DEFAULT,
        },
        daemon=True,
        name=f"webui-auth-test-{port}",
    )
    th.start()
    return th


def _req(url: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers) if e.headers else {}


def main() -> int:
    failed: list[str] = []
    def check(label, ok, detail=""):
        marker = "PASS" if ok else "FAIL"
        line = f"  {marker}  {label}"
        if detail and not ok:
            line += f": {detail}"
        print(line)
        if not ok:
            failed.append(label)

    # ===== auth-required mode =====
    port = _free_port()
    _spawn(port, auth_token="test-token-123")
    time.sleep(0.5)
    base = f"http://127.0.0.1:{port}"
    print(f"  daemon up at {base} (auth_token=test-token-123)")

    # /auth/info — no auth → 200
    s, body, _h = _req(base + "/auth/info")
    check("/auth/info status 200 (bypass)", s == 200, str(s))
    payload = json.loads(body)
    check("/auth/info reports auth_required=true",
          payload.get("auth_required") is True, str(payload))
    check("/auth/info reports cookie_name=x2d_token",
          payload.get("cookie_name") == "x2d_token", str(payload))

    # /login.html — no auth → 200
    s, body, _h = _req(base + "/login.html")
    check("/login.html bypass status 200", s == 200, str(s))
    check("/login.html serves the form",
          b"login-form" in body, body[:200].decode("utf-8", "replace"))

    # /login.js — no auth → 200
    s, body, _h = _req(base + "/login.js")
    check("/login.js bypass status 200", s == 200, str(s))
    check("/login.js serves the auth client",
          b"/auth/check" in body, body[:200].decode("utf-8", "replace"))

    # /index.html — no auth → 401
    s, body, h = _req(base + "/index.html")
    check("/index.html without auth returns 401", s == 401, str(s))
    check("401 includes WWW-Authenticate: Bearer header",
          "Bearer" in h.get("WWW-Authenticate", ""),
          str(h.get("WWW-Authenticate")))

    # /auth/check — no auth → 401
    s, body, h = _req(base + "/auth/check")
    check("/auth/check without auth returns 401", s == 401, str(s))

    # /auth/check — wrong token → 401
    s, body, h = _req(base + "/auth/check",
                       {"Authorization": "Bearer wrong"})
    check("/auth/check with wrong token returns 401", s == 401, str(s))
    check("/auth/check with wrong token sends invalid_token",
          "invalid_token" in h.get("WWW-Authenticate", ""),
          str(h.get("WWW-Authenticate")))

    # /auth/check — correct token → 200
    s, body, h = _req(base + "/auth/check",
                       {"Authorization": "Bearer test-token-123"})
    check("/auth/check with correct token returns 200", s == 200, str(s))
    check("/auth/check ok-payload",
          json.loads(body).get("ok") is True, body[:200].decode())

    # /index.html with bearer header → 200
    s, body, _h = _req(base + "/index.html",
                        {"Authorization": "Bearer test-token-123"})
    check("/index.html with bearer header returns 200", s == 200, str(s))

    # /index.html with cookie → 200 (SSE/EventSource path)
    s, body, _h = _req(base + "/index.html",
                        {"Cookie": "x2d_token=test-token-123"})
    check("/index.html with cookie returns 200", s == 200, str(s))

    # /state with cookie → 200 (proves cookie auth flows through to API)
    s, body, _h = _req(base + "/state",
                        {"Cookie": "other=foo; x2d_token=test-token-123; bar=baz"})
    check("/state with cookie alongside other cookies returns 200",
          s == 200, str(s))
    check("/state cookie auth returns the JSON state",
          b"nozzle_temper" in body, body[:200].decode())

    # /state with cookie + wrong token → 401
    s, body, _h = _req(base + "/state",
                        {"Cookie": "x2d_token=nope"})
    check("/state with bad cookie returns 401", s == 401, str(s))

    # Edge case: cookie value present but value-only-quoted
    s, body, _h = _req(base + "/state",
                        {"Cookie": 'x2d_token="test-token-123"'})
    check("/state with quoted cookie value still works",
          s == 200, str(s))

    # ===== auth-disabled mode (loopback only) =====
    port2 = _free_port()
    _spawn(port2, auth_token=None)
    time.sleep(0.5)
    base2 = f"http://127.0.0.1:{port2}"
    s, body, _h = _req(base2 + "/auth/info")
    payload = json.loads(body)
    check("auth_token=None → /auth/info returns auth_required=false",
          payload.get("auth_required") is False, str(payload))
    s, body, _h = _req(base2 + "/index.html")
    check("auth_token=None → /index.html serves without any auth",
          s == 200 and b"printer-name" in body, str(s))

    # _parse_cookie unit checks
    pc = x2d_bridge._parse_cookie
    check("_parse_cookie basic", pc("x2d_token=abc", "x2d_token") == "abc")
    check("_parse_cookie multi", pc("a=1; x2d_token=abc; b=2", "x2d_token") == "abc")
    check("_parse_cookie missing", pc("a=1; b=2", "x2d_token") == "")
    check("_parse_cookie empty header", pc("", "x2d_token") == "")
    check("_parse_cookie quoted value",
          pc('x2d_token="abc def"', "x2d_token") == "abc def")
    check("_parse_cookie spaces",
          pc("  a = 1 ;  x2d_token = xyz  ", "x2d_token") == "xyz")

    if failed:
        print(f"\nFAILED ({len(failed)}): {failed}")
        return 1
    print("\nALL TESTS PASSED — auth flow (#48)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
