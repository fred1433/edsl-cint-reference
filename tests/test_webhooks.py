"""Webhook signature against Cint's published vector, and the delivery contract."""

import gzip
import json
import time
import uuid

import pytest

from cint_ref.webhooks import SignatureError, WebhookInbox, sign, verify_signature
from tests.conftest import PUBLISHED_SECRET

# Published in the spec (components.schemas.WebhookSecret), OpenSSL example.
PUBLISHED_T = 1732526080
PUBLISHED_BODY = b'{"foo":"bar"}'
PUBLISHED_V1 = "6532d4777122acc32cd388763fc0c0ba8732698d09cf99eea086e5a86f8d3ebe"


def test_published_vector_verifies_with_secret_used_as_given():
    header = f"t={PUBLISHED_T},v1={PUBLISHED_V1}"
    assert verify_signature(PUBLISHED_SECRET, header, PUBLISHED_BODY) == PUBLISHED_T


def test_base64_decoding_the_secret_does_not_match_the_vector():
    import base64, hashlib, hmac

    key = base64.b64decode(PUBLISHED_SECRET)
    digest = hmac.new(key, f"{PUBLISHED_T}.".encode() + PUBLISHED_BODY, hashlib.sha256).hexdigest()
    assert digest != PUBLISHED_V1


def test_reserialized_body_fails():
    reserialized = json.dumps(json.loads(PUBLISHED_BODY)).encode()  # '{"foo": "bar"}'
    with pytest.raises(SignatureError):
        verify_signature(PUBLISHED_SECRET, f"t={PUBLISHED_T},v1={PUBLISHED_V1}", reserialized)


def session_event(rid, seq, client_status=None, event_id=None, time_="2026-10-08T10:00:00.000Z"):
    changes = [{"object": "respondent_update_date", "new_value": time_}]
    if client_status is not None:
        changes.append({"object": "client_status_code_change", "new_value": client_status})
    return {"id": event_id or str(uuid.uuid4()), "specversion": "1.0",
            "source": "https://api.cint.com", "type": "com.cint.session.updated",
            "time": time_, "datacontenttype": "application/json",
            "data": {"account_id": 1, "response_session_id": rid, "sequence_number": seq,
                     "target_group_id": "01BX5ZZKBKACTAV9WEVGEMMVS1", "changes": changes}}


def quota_event(completes, time_):
    return {"id": str(uuid.uuid4()), "specversion": "1.0", "source": "https://api.cint.com",
            "type": "com.cint.quota.fill.registered", "time": time_,
            "datacontenttype": "application/json",
            "data": {"account_id": 1, "target_group_id": "01JPSECBPA847DD89PN9ZFFBE1",
                     "screens": completes + 40, "completes": completes,
                     "profile_quota_id": "01JPSECY0CC2XAJEP5EV18SD9F"}}


def post(h, event, t=None, compress=False, body=None):
    body = body if body is not None else json.dumps(event, separators=(",", ":")).encode()
    t = t or int(time.time())
    headers = {"Cint-Signature": f"t={t},v1={sign(PUBLISHED_SECRET, t, body)}",
               "Content-Type": "application/json"}
    if compress:
        headers["Content-Encoding"] = "gzip"
        body = gzip.compress(body)
    return h.client.post("/cint/webhooks", content=body, headers=headers)


def count(db, table):
    with db.tx() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_duplicate_delivery_has_one_effect_and_two_acks(h, db, monkeypatch):
    applied = []
    original = WebhookInbox._apply
    monkeypatch.setattr(WebhookInbox, "_apply",
                        lambda self, conn, ev: (applied.append(ev["id"]), original(self, conn, ev)))
    rid = h.new_respondent()
    h.admit(rid)
    event = session_event(rid, seq=4, client_status=10)
    r1, r2 = post(h, event), post(h, event)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"event_id": event["id"]}
    assert applied == [event["id"]]
    assert count(db, "cint_webhook_inbox") == 1


def test_older_session_update_does_not_overwrite_newer(h):
    rid = h.new_respondent()
    h.admit(rid)
    post(h, session_event(rid, seq=5, client_status=10))
    post(h, session_event(rid, seq=3, client_status=1))
    row = h.flow.session_state(rid)
    assert (row["observed_seq"], row["observed_client_status"]) == (5, 10)
    # Observation only: the S2S transition state is untouched.
    assert row["transition_state"] == "not_ready"


def test_freshness_is_a_policy_on_signature_time_not_event_time(h):
    h.start(webhook_max_age_seconds=300)
    rid = h.new_respondent()
    h.admit(rid)
    old_event = session_event(rid, seq=1, time_="2026-10-01T00:00:00.000Z")
    assert post(h, old_event).status_code == 200            # old event, fresh signature
    stale = session_event(rid, seq=2)
    assert post(h, stale, t=int(time.time()) - 3600).status_code == 401


def test_gzip_body_is_verified_on_uncompressed_bytes(h):
    assert post(h, quota_event(10, "2026-10-08T10:00:00.000Z"), compress=True).status_code == 200


def test_bad_signature_is_rejected(h):
    body = json.dumps(quota_event(1, "2026-10-08T10:00:00.000Z")).encode()
    r = h.client.post("/cint/webhooks", content=body,
                      headers={"Cint-Signature": f"t={int(time.time())},v1={'0' * 64}"})
    assert r.status_code == 401


def test_failed_processing_is_not_acknowledged(h, db):
    broken = session_event(str(uuid.uuid4()), seq=1)
    del broken["data"]["sequence_number"]
    r = post(h, broken)
    assert r.status_code == 500 and "event_id" not in r.text
    assert count(db, "cint_webhook_inbox") == 0  # rolled back: a redelivery will be processed


def test_quota_fill_updates_progress_and_leaves_respondents_alone(h, db):
    rid = h.new_respondent()
    h.admit(rid)                                   # in progress, no response yet
    post(h, quota_event(73, "2026-10-08T10:05:00.000Z"))
    post(h, quota_event(70, "2026-10-08T10:04:00.000Z"))  # late, older
    with db.tx() as conn:
        progress = conn.execute("SELECT completes FROM cint_quota_progress").fetchone()
    assert progress["completes"] == 73
    assert h.flow.session_state(rid)["outcome"] is None
    assert h.save(rid).json()["outcome"] == "complete"
