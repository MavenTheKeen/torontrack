"""Browser notifications when a subway line's reported status changes (docs/alerts.md).

A browser that asks for them hands the site a push subscription: an address at its
maker's push service and two keys. The site keeps that and the lines chosen, nothing
else. Once a minute the alerts process compares each line's reported status with the one
it last saw and sends a short message to the subscriptions for a line that changed.

Messages are encrypted for the one browser (RFC 8291, aes128gcm) and signed with this
site's key (VAPID, RFC 8292), so the push service can neither read nor forge them. The
private key stays in the alerts process; the public site only records subscriptions,
through the functions in the `push` schema.
"""

import base64
import json
import os
import struct
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from transit.web import queries

# Only the browser makers' push services: a subscription is an address this server will
# send requests to, so anything else is refused when it is offered and again when used.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com")
PUSH_HOST_SUFFIXES = (".notify.windows.com", ".push.apple.com", ".push.services.mozilla.com")
MAX_LINES = 12
MAX_FAILURES = 5
TTL_SECONDS = 3600  # A status message an hour late is no longer worth showing.
RECORD_SIZE = 4096
CONTACT = "https://github.com/MavenTheKeen/torontrack/security/advisories/new"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_endpoint(endpoint: str) -> bool:
    try:
        url = urlsplit(endpoint)
    except ValueError:
        return False
    host = url.hostname or ""
    return (
        url.scheme == "https"
        and url.port in (None, 443)
        and not url.username
        and len(endpoint) <= 1000
        and (host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES))
    )


def valid_keys(p256dh: str, auth: str) -> bool:
    """A subscription's keys: an uncompressed P-256 point and a 16-byte secret."""
    try:
        point, secret = unb64url(p256dh), unb64url(auth)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (ValueError, TypeError):
        return False
    return len(point) == 65 and len(secret) == 16


def load_key(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("VAPID_PRIVATE_KEY must be a P-256 private key in PEM form")
    return key


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def public_key(key: ec.EllipticCurvePrivateKey) -> str:
    """The key browsers subscribe with (applicationServerKey)."""
    return b64url(_point(key.public_key()))


def encrypt(message: bytes, p256dh: str, auth: str, sender=None, salt: bytes | None = None):
    """The message as one aes128gcm record for the subscription's keys (RFC 8291).
    `sender` and `salt` are fresh for every message; tests pass fixed ones."""
    receiver_point = unb64url(p256dh)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_point)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    sender_point = _point(sender.public_key())
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        hashes.SHA256(), 32, unb64url(auth), b"WebPush: info\x00" + receiver_point + sender_point
    ).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    # 0x02 marks the last (here the only) record.
    sealed = AESGCM(key).encrypt(nonce, message + b"\x02", None)
    header = salt + struct.pack(">IB", RECORD_SIZE, len(sender_point)) + sender_point
    return header + sealed


def vapid(endpoint: str, key: ec.EllipticCurvePrivateKey, now: float | None = None) -> str:
    """The Authorization header that proves a message comes from this site (RFC 8292)."""
    url = urlsplit(endpoint)
    claims = {
        "aud": f"{url.scheme}://{url.netloc}",
        "exp": int(now or time.time()) + 12 * 3600,
        "sub": CONTACT,
    }
    signed = ".".join(
        b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in ({"typ": "JWT", "alg": "ES256"}, claims)
    )
    r, s = decode_dss_signature(key.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={signed}.{signature}, k={public_key(key)}"


def send(client: httpx.Client, key, subscription: dict, message: dict) -> int:
    """Deliver one message; the push service's status code (201 accepted, 404 or 410 when
    the subscription has ended)."""
    endpoint = subscription["endpoint"]
    if not allowed_endpoint(endpoint):
        return 410
    body = encrypt(json.dumps(message).encode(), subscription["p256dh"], subscription["auth"])
    response = client.post(
        endpoint,
        content=body,
        headers={
            "Authorization": vapid(endpoint, key),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_SECONDS),
            "Urgency": "normal",
        },
    )
    return response.status_code


def line_states(conn, now: datetime) -> dict[str, dict] | None:
    """Each line with an active service alert: its most severe status and that alert.
    None while TTC's alert feed is stale, when no change can be told from silence."""
    state = queries.feed_state(conn)
    if queries.freshness(state, "alerts_subway", now, queries.ALERTS_STALE)["stale"]:
        return None
    current: dict[str, dict] = {}
    for alert in queries.current_alerts(conn, now):
        if alert["kind"] != "service" or alert["timing"] != "active":
            continue
        if alert["status"] not in queries.STATUS_ORDER:
            continue
        rank = queries.STATUS_ORDER.index(alert["status"])
        for line in alert["lines"]:
            if line not in current or rank < current[line]["rank"]:
                current[line] = {
                    "rank": rank,
                    "status": alert["status"],
                    "summary": alert["header"],
                }
    return current


def changes(conn, now: datetime) -> list[dict]:
    """Messages for lines whose reported status changed since the last check, recording
    the new state. The first check only records, so a restart never announces old news."""
    current = line_states(conn, now)
    if current is None:
        return []
    known = {
        r["line"]: r["status"]
        for r in conn.execute("SELECT line, status FROM push.line_state").fetchall()
    }
    first = not conn.execute("SELECT 1 FROM push.line_state LIMIT 1").fetchone()
    messages = []
    for line in sorted(set(current) | set(known)):
        new = current[line]["status"] if line in current else "normal"
        if known.get(line, "normal") == new and line in known:
            continue
        conn.execute(
            """INSERT INTO push.line_state (line, status, changed_at) VALUES (%s, %s, %s)
               ON CONFLICT (line) DO UPDATE
               SET status = excluded.status, changed_at = excluded.changed_at""",
            (line, new, now),
        )
        if first or known.get(line, "normal") == new:
            continue
        messages.append(
            {
                "line": line,
                "title": f"Line {line}: {queries.STATUS_LABELS.get(new, 'Normal service')}",
                "body": current[line]["summary"]
                if line in current
                else "Service is back to normal.",
                "url": f"/#/line/{line}",
                "tag": f"line-{line}",
            }
        )
    return messages


def _deliver(conn, client, key, subscription: dict, message: dict) -> bool:
    try:
        status = send(client, key, subscription, message)
    except httpx.HTTPError:
        status = 0
    if status in (404, 410):
        conn.execute(
            "DELETE FROM push.subscriptions WHERE endpoint = %s", (subscription["endpoint"],)
        )
    elif status in (200, 201, 202):
        conn.execute(
            "UPDATE push.subscriptions SET failures = 0 WHERE endpoint = %s AND failures > 0",
            (subscription["endpoint"],),
        )
    else:
        # A subscription that keeps failing is dropped; the browser can subscribe again.
        conn.execute(
            """WITH failed AS (
                   UPDATE push.subscriptions SET failures = failures + 1
                   WHERE endpoint = %s RETURNING endpoint, failures)
               DELETE FROM push.subscriptions s USING failed f
               WHERE s.endpoint = f.endpoint AND f.failures >= %s""",
            (subscription["endpoint"], MAX_FAILURES),
        )
    return status in (200, 201, 202)


def run_once(conn, client: httpx.Client, key, now: datetime) -> dict:
    """One round: publish the public key, confirm new subscriptions, announce changes."""
    conn.execute(
        """INSERT INTO push.config (name, value) VALUES ('public_key', %s)
           ON CONFLICT (name) DO UPDATE SET value = excluded.value""",
        (public_key(key),),
    )
    sent = 0
    new = conn.execute(
        "SELECT endpoint, p256dh, auth, lines FROM push.subscriptions WHERE confirm_due"
    ).fetchall()
    for subscription in new:
        lines = ", ".join(subscription["lines"])
        message = {
            "title": "Notifications are on",
            "body": f"You will hear when the status of Line {lines} changes."
            if len(subscription["lines"]) == 1
            else f"You will hear when the status of Lines {lines} changes.",
            "url": "/#/",
            "tag": "welcome",
        }
        conn.execute(
            "UPDATE push.subscriptions SET confirm_due = false WHERE endpoint = %s",
            (subscription["endpoint"],),
        )
        sent += _deliver(conn, client, key, subscription, message)
    for message in changes(conn, now):
        line = message.pop("line")
        for subscription in conn.execute(
            "SELECT endpoint, p256dh, auth FROM push.subscriptions WHERE %s = ANY(lines)", (line,)
        ).fetchall():
            sent += _deliver(conn, client, key, subscription, message)
    return {"sent": sent}
