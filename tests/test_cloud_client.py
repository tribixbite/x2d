#!/usr/bin/env python3
"""Unit tests for cloud_client.py — the OAuth + cloud-MQTT-creds + OSS-upload
plumbing behind item #67. No real Bambu account needed; HTTP traffic is
mocked via urllib's request hooks.

Runs offline on the GitHub Actions runner. Validates:
  - Session round-trip (to_json / from_json idempotent).
  - mqtt_credentials() raises CloudError when not logged in.
  - mqtt_credentials() returns ("u_<id>", "<token>") when logged in.
  - mqtt_broker() picks the right host per region.
  - cloud_upload_file() shape A: presigned-URL → PUT → cloud:// URL.
  - cloud_upload_file() shape B: STS creds → HMAC-SHA1 signed PUT.

Live-network tests against api.bambulab.com would require a real
account; those are deliberately out of scope here.
"""
from __future__ import annotations

import io
import sys
import time
import json
import unittest
import unittest.mock as mock
from pathlib import Path

# Add repo root to sys.path so `import cloud_client` works regardless of
# where pytest / unittest is invoked from.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import cloud_client


class TestSessionRoundTrip(unittest.TestCase):
    def test_to_from_json_roundtrip(self):
        s = cloud_client.Session(
            access_token="AT_x",
            refresh_token="RT_y",
            expires_at=1234567890.0,
            user_id="42",
            region="us",
            extra={"k": "v"},
        )
        s2 = cloud_client.Session.from_json(s.to_json())
        self.assertEqual(s2.access_token, "AT_x")
        self.assertEqual(s2.refresh_token, "RT_y")
        self.assertEqual(s2.expires_at, 1234567890.0)
        self.assertEqual(s2.user_id, "42")
        self.assertEqual(s2.region, "us")
        self.assertEqual(s2.extra, {"k": "v"})

    def test_empty_session(self):
        s = cloud_client.Session()
        self.assertTrue(s.empty)
        self.assertTrue(s.expired)  # no token → treated as expired

    def test_expired(self):
        s = cloud_client.Session(access_token="x", expires_at=time.time() - 10)
        self.assertTrue(s.expired)
        s2 = cloud_client.Session(access_token="x", expires_at=time.time() + 9999)
        self.assertFalse(s2.expired)


class TestMQTTCredentials(unittest.TestCase):
    def test_raises_when_not_logged_in(self):
        cli = cloud_client.CloudClient(cloud_client.Session())
        with self.assertRaises(cloud_client.CloudError):
            cli.mqtt_credentials()

    def test_returns_user_and_token(self):
        s = cloud_client.Session(access_token="JWT_HERE",
                                 refresh_token="rt",
                                 expires_at=time.time() + 9999,
                                 user_id="3737485665",
                                 region="us")
        cli = cloud_client.CloudClient(s)
        user, pw = cli.mqtt_credentials()
        self.assertEqual(user, "u_3737485665")
        self.assertEqual(pw, "JWT_HERE")

    def test_broker_routing(self):
        for region, expected in [("us", "us.mqtt.bambulab.com"),
                                 ("cn", "cn.mqtt.bambulab.com")]:
            s = cloud_client.Session(access_token="x",
                                     expires_at=time.time() + 9999,
                                     user_id="1", region=region)
            cli = cloud_client.CloudClient(s)
            self.assertEqual(cli.mqtt_broker(), expected)


class _FakeUrlopenResp:
    """Minimal stub of the object urllib.request.urlopen returns. Just
    enough to satisfy CloudClient + cloud_upload_file."""
    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body
    def read(self, *a, **k):
        return self._body
    def getcode(self):
        return self.status
    def __enter__(self): return self
    def __exit__(self, *a, **k): return False


class TestCloudUploadFile(unittest.TestCase):
    """Validates the OSS upload helper handles BOTH response shapes
    Bambu's API has been observed to return."""

    def setUp(self):
        # Make a tiny temp file to "upload"
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.file = Path(self.tmpdir) / "rumi_frame.gcode.3mf"
        self.file.write_bytes(b"PK\x03\x04fake-3mf-body")
        self.s = cloud_client.Session(
            access_token="JWT", refresh_token="RT",
            expires_at=time.time() + 9999,
            user_id="42", region="us",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_shape_A_presigned_url(self):
        """Shape A: response carries a signed PUT URL; we just PUT to it."""
        cli = cloud_client.CloudClient(self.s)
        token = {
            "url": ("https://my-bucket.oss-cn-shanghai.aliyuncs.com/"
                    "users/42/abc.3mf?Signature=DEADBEEF&Expires=99"),
            "fileName": "users/42/abc.3mf",
            "expireAt": int(time.time()) + 60,
        }
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value = _FakeUrlopenResp(status=200)
            out = cli.cloud_upload_file(self.file, token=token)
        self.assertTrue(out["url"].startswith("cloud://my-bucket/"))
        self.assertIn("users/42/abc.3mf", out["url"])
        self.assertEqual(out["size"], self.file.stat().st_size)
        self.assertEqual(len(out["md5"]), 32)
        # Confirms a PUT request was issued.
        called_req = mu.call_args[0][0]
        self.assertEqual(called_req.get_method(), "PUT")

    def test_shape_B_sts_credentials_signs_correctly(self):
        """Shape B: STS creds + bucket/region/path; we have to HMAC-sign."""
        cli = cloud_client.CloudClient(self.s)
        token = {
            "accessKeyId":     "STS_AK",
            "accessKeySecret": "STS_SECRET",
            "securityToken":   "STS_TOKEN",
            "expiration":      "2099-01-01T00:00:00Z",
            "bucket":          "bbl-prod",
            "region":          "cn-shanghai",
            "fileSavePath":    "users/42/upload/abc.3mf",
        }
        with mock.patch("urllib.request.urlopen") as mu:
            mu.return_value = _FakeUrlopenResp(status=200)
            out = cli.cloud_upload_file(self.file, token=token)
        self.assertEqual(out["url"], "cloud://bbl-prod/users/42/upload/abc.3mf")
        self.assertEqual(out["size"], self.file.stat().st_size)
        # Confirm the request had HMAC-signed Authorization + STS token header.
        called_req = mu.call_args[0][0]
        self.assertEqual(called_req.get_method(), "PUT")
        auth = called_req.get_header("Authorization") or ""
        self.assertTrue(auth.startswith("OSS STS_AK:"))
        self.assertEqual(called_req.get_header("X-oss-security-token"), "STS_TOKEN")

    def test_unrecognised_token_shape_raises(self):
        cli = cloud_client.CloudClient(self.s)
        token = {"weird": "shape", "missing": "bucket"}
        with self.assertRaises(cloud_client.CloudError):
            cli.cloud_upload_file(self.file, token=token)


class TestRegionsHaveMQTT(unittest.TestCase):
    def test_all_regions_have_mqtt_key(self):
        for region, info in cloud_client.REGIONS.items():
            self.assertIn("mqtt", info, f"region {region} missing mqtt host")
            self.assertTrue(info["mqtt"].endswith(".bambulab.com"),
                            f"region {region} mqtt host: {info['mqtt']}")
        self.assertEqual(cloud_client.MQTT_PORT, 8883)


if __name__ == "__main__":
    unittest.main(verbosity=2)
