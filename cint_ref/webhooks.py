"""Webhook receiver: signature, durable acceptance, deduplication, ordering.

Contract (spec WebhookSecret; deep-dives/understanding-webhooks-concept):
* Cint-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, f"{t}.{body}")>
* body is a CloudEvents 1.0 envelope; delivery is at least once;
* an event counts as delivered on HTTP 200 with a JSON body containing event_id.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .db import Database


# Local policy: clock skew tolerated for a signature dated in the future.
FUTURE_SKEW_SECONDS = 60


class SignatureError(Exception):
    pass


def parse_signature_header(header: str) -> tuple[int, list[str]]:
    t: Optional[int] = None
    v1: list[str] = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                t = int(value)
            except ValueError:
                raise SignatureError("bad t")
        elif key == "v1":
            v1.append(value)
    if t is None or not v1:
        raise SignatureError("missing t or v1")
    return t, v1


def sign(secret: str, t: int, body: bytes) -> str:
    # The secret string is the HMAC key as given: this is what reproduces the
    # published OpenSSL vector (decoding it as base64 does not).
    return hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    max_age_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> int:
    """Verify against the original bytes (never a re-serialized JSON). Returns t."""
    t, candidates = parse_signature_header(header)
    expected = sign(secret, t, body)
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("signature mismatch")
    if max_age_seconds is not None:
        age = (now if now is not None else time.time()) - t
        if age > max_age_seconds:
            raise SignatureError(f"signature older than {max_age_seconds}s")
        if age < -FUTURE_SKEW_SECONDS:
            raise SignatureError("signature timestamp in the future")
    return t


def decoded_body(raw: bytes, content_encoding: Optional[str]) -> bytes:
    """The signature covers the uncompressed JSON whatever the encoding."""
    if (content_encoding or "").lower() == "gzip":
        return gzip.decompress(raw)
    return raw


class WebhookInbox:
    def __init__(self, db: Database):
        self.db = db

    def accept(self, body: bytes, signature_t: int) -> tuple[str, bool]:
        """Store the event and apply its effect in one transaction, then acknowledge.

        Returns (event_id, first_delivery). A redelivery is acknowledged again but
        has no further effect."""
        event = json.loads(body)
        event_id = str(event["id"])
        with self.db.tx() as conn:
            inserted = conn.execute(
                """INSERT INTO cint_webhook_inbox (event_id, event_type, event_time, signature_t, payload)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                (event_id, event.get("type", ""), event.get("time"), signature_t, Jsonb(event)),
            ).fetchone()
            if inserted is None:
                return event_id, False
            projection = apply_event(conn, event)
            conn.execute("UPDATE cint_webhook_inbox SET projection = %s WHERE event_id = %s",
                         (projection, event_id))
        return event_id, True


def apply_event(conn, event: dict[str, Any]) -> str:
    """Project one event. Returns 'applied', 'unmatched' or 'ignored'."""
    data = event.get("data") or {}
    kind = event.get("type")
    if kind == "com.cint.session.updated":
        return _session_updated(conn, data)
    if kind == "com.cint.quota.fill.registered":
        _quota_fill(conn, data, event["time"])
        return "applied"
    return "ignored"  # target-group.updated and unknown types: kept in the inbox only


def _session_updated(conn, data: dict[str, Any]) -> str:
    # Assumption to confirm: response_session_id is the RID received on entry.
    # If it is not a UUID, the event stays in the inbox without effect and is still
    # acknowledged: refusing it would block the destination for up to 7 days.
    seq = int(data["sequence_number"])
    try:
        rid = str(uuid.UUID(str(data.get("response_session_id"))))
    except ValueError:
        return "ignored"
    status = None
    for change in data.get("changes") or []:
        if change.get("object") == "client_status_code_change":
            status = change.get("new_value")
    # Events carry changes, not snapshots: each projected field keeps the sequence of
    # the event that last set it, so a later event without a status change cannot
    # hide an earlier status. Observation only: never confirms an S2S transition.
    row = conn.execute(
        """UPDATE cint_sessions SET
               observed_seq = GREATEST(COALESCE(observed_seq, %(seq)s), %(seq)s),
               observed_client_status = CASE
                   WHEN %(st)s::int IS NOT NULL AND COALESCE(observed_client_status_seq, -1) < %(seq)s
                   THEN %(st)s::int ELSE observed_client_status END,
               observed_client_status_seq = CASE
                   WHEN %(st)s::int IS NOT NULL AND COALESCE(observed_client_status_seq, -1) < %(seq)s
                   THEN %(seq)s ELSE observed_client_status_seq END,
               updated_at = now()
           WHERE rid = %(rid)s RETURNING rid""",
        {"seq": seq, "st": status, "rid": rid},
    ).fetchone()
    return "applied" if row else "unmatched"


def replay_unmatched_session_events(conn, rid: str) -> int:
    """Called at admission: project session events that arrived before the session."""
    events = conn.execute(
        """SELECT event_id, payload FROM cint_webhook_inbox
           WHERE projection = 'unmatched' AND event_type = 'com.cint.session.updated'
             AND lower(payload->'data'->>'response_session_id') = %s
           ORDER BY (payload->'data'->>'sequence_number')::int""",
        (rid,),
    ).fetchall()
    for e in events:
        projection = apply_event(conn, e["payload"])
        conn.execute("UPDATE cint_webhook_inbox SET projection = %s WHERE event_id = %s",
                     (projection, e["event_id"]))
    return len(events)


def _quota_fill(conn, data: dict[str, Any], event_time: str) -> None:
    # Progress view only; in-progress respondents are not converted to quota full.
    conn.execute(
        """INSERT INTO cint_quota_progress
               (target_group_id, profile_quota_id, screens, completes, event_time)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (target_group_id, profile_quota_id) DO UPDATE
              SET screens = EXCLUDED.screens, completes = EXCLUDED.completes,
                  event_time = EXCLUDED.event_time
            WHERE cint_quota_progress.event_time < EXCLUDED.event_time""",
        (
            data["target_group_id"],
            data.get("profile_quota_id") or "",
            int(data["screens"]),
            int(data["completes"]),
            event_time,
        ),
    )
