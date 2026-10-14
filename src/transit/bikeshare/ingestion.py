"""Collect or replay a logical collection under a PostgreSQL advisory lock."""

from datetime import UTC, datetime
from uuid import uuid4

from transit.bikeshare.parsing import FeedValidationError, discover
from transit.bikeshare.store import LOCK_ID, load_raw, normalize, store_raw
from transit.db import connect
from transit.http import FeedClient, FetchError


class CollectionBusy(RuntimeError):
    pass


def run(settings, collection_id=None, *, replay=False, client=None):
    collection_id = collection_id or str(uuid4())
    if not collection_id.strip() or len(collection_id) > 200:
        raise ValueError("collection_id must contain 1-200 characters")
    with connect(settings.database_url) as conn:
        acquired = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_ID,))
        if not acquired.fetchone()["locked"]:
            raise CollectionBusy("Another collection/replay is running; retry later")
        try:
            existing = conn.execute(
                "SELECT * FROM ops.ingestion_runs WHERE collection_id=%s", (collection_id,)
            ).fetchone()
            if replay and not existing:
                raise ValueError("Unknown collection_id; replay never fetches data")
            # A process killed mid-collection cannot update its attempt. The next holder
            # of this global lock can safely mark any abandoned attempt as failed.
            conn.execute(
                """UPDATE ops.ingestion_attempts SET status='failed', finished_at=now(),
                   error='Process interrupted; recovered by next lock holder'
                   WHERE status='running'"""
            )
            conn.execute(
                """UPDATE ops.ingestion_runs SET status='failed', completed_at=now(),
                   error='Process interrupted; retry this collection ID' WHERE status='running'"""
            )
            if existing and existing["status"] == "succeeded" and not replay:
                return {"collection_id": collection_id, "status": "already_succeeded"}
            preserve_success = bool(replay and existing and existing["status"] == "succeeded")
            if not preserve_success:
                conn.execute(
                    """INSERT INTO ops.ingestion_runs (collection_id,collected_at,status)
                   VALUES (%s,%s,'running') ON CONFLICT (collection_id)
                   DO UPDATE SET status='running', error=NULL, completed_at=NULL""",
                    (collection_id, datetime.now(UTC)),
                )
            attempt_id = conn.execute(
                """INSERT INTO ops.ingestion_attempts (collection_id,operation,status)
                   VALUES (%s,%s,'running') RETURNING attempt_id""",
                (collection_id, "replay" if replay else "collect"),
            ).fetchone()["attempt_id"]
            owned_client = client is None and not replay
            try:
                if not replay:
                    backoff = conn.execute(
                        "SELECT retry_not_before FROM ops.source_backoff WHERE discovery_url=%s",
                        (settings.discovery_url,),
                    ).fetchone()
                    if backoff and backoff["retry_not_before"] > datetime.now(UTC):
                        raise FetchError(
                            "Source Retry-After is still active; no HTTP request made",
                            retry_not_before=backoff["retry_not_before"],
                        )
                    client = client or FeedClient()
                    raw = load_raw(conn, collection_id)
                    if "gbfs" not in raw:
                        store_raw(
                            conn,
                            collection_id,
                            "gbfs",
                            settings.discovery_url,
                            client.fetch(settings.discovery_url),
                        )
                        raw = load_raw(conn, collection_id)
                    endpoints = discover(raw["gbfs"]["payload"])
                    for name in ("station_information", "station_status"):
                        if name not in raw:
                            store_raw(
                                conn,
                                collection_id,
                                name,
                                endpoints[name],
                                client.fetch(endpoints[name]),
                            )
                rows = normalize(conn, collection_id)
                conn.execute(
                    """UPDATE ops.ingestion_attempts SET status='succeeded', finished_at=now()
                       WHERE attempt_id=%s""",
                    (attempt_id,),
                )
                return {"collection_id": collection_id, "status": "succeeded", "observations": rows}
            except Exception as exc:
                # Public validation/fetch messages are safe; never store a database DSN
                # or arbitrary exception text that might contain credentials.
                message = (
                    str(exc)
                    if isinstance(exc, (FeedValidationError, FetchError))
                    else type(exc).__name__
                )
                if isinstance(exc, FetchError) and exc.retry_not_before:
                    conn.execute(
                        """INSERT INTO ops.source_backoff (discovery_url,retry_not_before)
                           VALUES (%s,%s) ON CONFLICT (discovery_url) DO UPDATE
                           SET retry_not_before=greatest(ops.source_backoff.retry_not_before,
                                                        excluded.retry_not_before)""",
                        (settings.discovery_url, exc.retry_not_before),
                    )
                if not preserve_success:
                    conn.execute(
                        """UPDATE ops.ingestion_runs SET status='failed',completed_at=now(),error=%s
                           WHERE collection_id=%s""",
                        (message[:1000], collection_id),
                    )
                conn.execute(
                    """UPDATE ops.ingestion_attempts SET status='failed',finished_at=now(),error=%s
                       WHERE attempt_id=%s""",
                    (message[:1000], attempt_id),
                )
                raise
            finally:
                if owned_client and client:
                    client.close()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
