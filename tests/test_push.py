"""Browser notifications and the site's availability record."""

import base64
import json
import struct
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from transit import push, uptime
from transit.db import connect
from transit.web.app import create_app

T0 = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


class Browser:
    """The receiving side of a push subscription."""

    def __init__(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.secret = b"0123456789abcdef"
        self.point = self.private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.p256dh, self.auth = push.b64url(self.point), push.b64url(self.secret)

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8291 from the receiver's side, written apart from transit.push."""
        salt, (size, key_length) = body[:16], struct.unpack(">IB", body[16:21])
        sender_point = body[21 : 21 + key_length]
        sealed = body[21 + key_length :]
        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_point)
        shared = self.private.exchange(ec.ECDH(), sender)
        info = b"WebPush: info\x00" + self.point + sender_point
        ikm = HKDF(hashes.SHA256(), 32, self.secret, info).derive(shared)
        key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(key).decrypt(nonce, sealed, None)
        assert size == 4096 and key_length == 65 and plain.endswith(b"\x02")
        return plain[:-1]


def test_rfc_8291_example_message():
    """The worked example in RFC 8291, section 5 and appendix A."""
    sender = ec.derive_private_key(
        int.from_bytes(push.unb64url("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
        ec.SECP256R1(),
    )
    body = push.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender=sender,
        salt=push.unb64url("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert push.b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tl"
        "zAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_a_message_round_trips_and_is_signed():
    browser = Browser()
    message = json.dumps({"title": "Line 1: Delays"}).encode()
    assert browser.decrypt(push.encrypt(message, browser.p256dh, browser.auth)) == message

    key = ec.generate_private_key(ec.SECP256R1())
    header = push.vapid(ENDPOINT, key, now=T0.timestamp())
    token, public = header.removeprefix("vapid t=").split(", k=")
    head, claims, signature = token.split(".")
    raw = push.unb64url(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(push.unb64url(claims))["aud"] == "https://fcm.googleapis.com"
    assert public == push.public_key(key) and len(push.unb64url(public)) == 65
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    assert push.public_key(push.load_key(pem)) == public


def test_only_push_services_are_accepted():
    browser = Browser()
    assert push.allowed_endpoint(ENDPOINT)
    assert push.allowed_endpoint("https://wns2-by3p.notify.windows.com/w/?token=abc")
    for bad in (
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com.evil.example/x",
        "https://127.0.0.1/x",
        "https://user@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "not a url",
    ):
        assert not push.allowed_endpoint(bad), bad
    assert push.valid_keys(browser.p256dh, browser.auth)
    assert not push.valid_keys(base64.b64encode(b"x" * 65).decode(), browser.auth)
    assert not push.valid_keys(browser.p256dh, push.b64url(b"short"))


@pytest.mark.integration
def test_changes_are_announced_once_to_the_lines_subscribers(settings, monkeypatch):
    browser, key = Browser(), ec.generate_private_key(ec.SECP256R1())
    states = {}
    monkeypatch.setattr(push, "line_states", lambda conn, now: states)
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(410 if "gone" in str(request.url) else 201)

    client = TestClient(create_app(settings))
    assert client.get("/api/push").json() == {"available": False, "key": None}
    with (
        connect(settings.database_url) as conn,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        assert push.run_once(conn, http, key, T0) == {"sent": 0}  # Records the first state.
        conn.commit()
        assert client.get("/api/push").json()["key"] == push.public_key(key)
        keys = {"p256dh": browser.p256dh, "auth": browser.auth}
        body = {"endpoint": ENDPOINT, "keys": keys, "lines": ["1", "nope"]}
        assert client.post("/api/push", json=body).json()["lines"] == ["1"]
        gone = {"endpoint": ENDPOINT.replace("abc123", "gone"), "keys": keys, "lines": ["1"]}
        assert client.post("/api/push", json=gone).status_code == 200
        assert client.post("/api/push", json={"endpoint": ENDPOINT}).json()["lines"] == ["1"]
        bad = {"endpoint": "https://evil.example/x", "keys": keys, "lines": ["1"]}
        assert client.post("/api/push", json=bad).status_code == 422

        # Each new subscription is confirmed once; the ended one is removed.
        assert push.run_once(conn, http, key, T0 + timedelta(minutes=1)) == {"sent": 1}
        welcome = json.loads(browser.decrypt(posts[0].content))
        assert welcome["title"] == "Notifications are on" and "Line 1" in welcome["body"]
        assert posts[0].headers["content-encoding"] == "aes128gcm"
        count = conn.execute("SELECT count(*) AS n FROM push.subscriptions").fetchone()["n"]
        assert count == 1

        states["1"] = {"rank": 2, "status": "delays", "summary": "Delays at Bloor-Yonge"}
        states["2"] = {"rank": 0, "status": "no_service", "summary": "No service"}
        posts.clear()
        assert push.run_once(conn, http, key, T0 + timedelta(minutes=2)) == {"sent": 1}
        delay = json.loads(browser.decrypt(posts[0].content))
        assert delay == {
            "title": "Line 1: Delays",
            "body": "Delays at Bloor-Yonge",
            "url": "/#/line/1",
            "tag": "line-1",
        }
        assert push.run_once(conn, http, key, T0 + timedelta(minutes=3)) == {"sent": 0}
        del states["1"]
        assert push.run_once(conn, http, key, T0 + timedelta(minutes=4)) == {"sent": 1}
        assert json.loads(browser.decrypt(posts[1].content))["title"] == "Line 1: Normal service"

        # No lines ends the subscription.
        conn.commit()
        assert client.post("/api/push", json={"endpoint": ENDPOINT, "lines": []}).json() == {
            "available": True,
            "key": push.public_key(key),
            "lines": [],
        }
        assert not conn.execute("SELECT 1 FROM push.subscriptions").fetchone()


@pytest.mark.integration
def test_availability_counts_failures_and_silences_as_down(settings):
    answers = iter([200, 200, 503, 200, 200])
    transport = httpx.MockTransport(lambda request: httpx.Response(next(answers)))
    with connect(settings.database_url) as conn, httpx.Client(transport=transport) as http:
        # Minutes 0 to 3, then a 30 minute silence before the last check.
        for minute in (0, 1, 2, 3, 33):
            uptime.record(conn, http, "https://site.example/", T0 + timedelta(minutes=minute))
        result = uptime.daily(conn, T0 + timedelta(hours=1))
    # Up: minutes 0-1 and 2-3. Down: the failed check's minute and the silence.
    assert result["available"] and result["since"] == "2026-11-16"
    day = result["days"][0]
    assert (day["day"], day["pct"], day["down_minutes"]) == ("2026-11-16", 6.06, 31)
    assert len(result["days"]) == 1 and result["pct"] == 6.06
    assert TestClient(create_app(settings)).get("/api/uptime").json()["uptime"]["available"]
