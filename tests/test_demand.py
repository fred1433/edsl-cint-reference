"""Draft creation, asynchronous launch, ETag-guarded pause."""

import pytest

from cint_ref.audience_fixture import draft_target_group
from cint_ref.demand_client import DemandClient, DemandError
from cint_ref.launch import LaunchTracker
from cint_ref.respondent_flow import SurveyBinding
from fake_cint.demand import FakeDemand
from tests.conftest import SURVEY

ACCOUNT = 101


@pytest.fixture
def fake_demand():
    return FakeDemand()


@pytest.fixture
def demand(settings, fake_demand):
    return DemandClient(settings.demand_base_url, settings.demand_token,
                        settings.demand_api_version, fake_demand.transport)


@pytest.fixture
def tracker(db, demand):
    return LaunchTracker(db, demand)


def draft(settings, demand):
    project = demand.create_project(ACCOUNT, "EDSL study", "00000000-0000-0000-0000-000000000000")
    live_url = SurveyBinding(SURVEY).live_url(settings.public_base_url)
    tg = demand.create_draft_target_group(
        ACCOUNT, project, draft_target_group(live_url, 1234, 200))
    return project, tg


def test_project_creation_is_idempotent(demand):
    a = demand.create_project(ACCOUNT, "x", "pm", idempotency_key="k-1")
    b = demand.create_project(ACCOUNT, "x", "pm", idempotency_key="k-1")
    assert a == b


def test_draft_payload_has_the_required_fields(settings, demand, fake_demand):
    _, tg = draft(settings, demand)
    assert fake_demand.target_groups[tg]["status"] == "draft"
    assert fake_demand.target_groups[tg]["live_url"].endswith("?rid=[%RID%]")
    payload = draft_target_group("https://x", 1234, 1)
    del payload["completes_goal"]
    with pytest.raises(DemandError):
        demand.create_draft_target_group(ACCOUNT, "p", payload)


def test_accepted_launch_job_that_fails_is_never_live(settings, demand, fake_demand, tracker):
    fake_demand.next_launch_outcome = "Failed"
    project, tg = draft(settings, demand)
    seen = [tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")["state"]]
    seen.append(tracker.poll(tg)["state"])   # Processing
    seen.append(tracker.poll(tg)["state"])   # Failed
    assert seen == ["launching", "launching", "failed"]
    assert tracker.get(tg)["failure_code"] == "TargetGroupInvalidState"


def test_launch_is_live_only_after_job_completes(settings, demand, tracker):
    project, tg = draft(settings, demand)
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    assert tracker.poll(tg)["state"] == "launching"
    row = tracker.poll(tg)
    assert row["state"] == "live" and row["fielding_run_id"]


def test_launch_request_is_not_duplicated(settings, demand, fake_demand, tracker):
    project, tg = draft(settings, demand)
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    posts = [c for c in fake_demand.calls if c[0] == "POST" and c[1].endswith("launch-from-draft")]
    assert len(posts) == 1


def live(settings, demand, tracker):
    project, tg = draft(settings, demand)
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    tracker.poll(tg)
    return tg, tracker.poll(tg)["fielding_run_id"]


def change_after_first_read(monkeypatch, demand, fake_demand, run_id, status=None):
    original, reads = demand.get_fielding_run, []

    def read(*args):
        out = original(*args)
        if not reads:
            fake_demand.concurrent_change(run_id, status)
        reads.append(out)
        return out

    monkeypatch.setattr(demand, "get_fielding_run", read)
    return reads


def pause_posts(fake_demand):
    return [c for c in fake_demand.calls if c[1].endswith("/pause")]


def test_stale_etag_triggers_reread_then_pause(settings, demand, fake_demand, tracker, monkeypatch):
    tg, run_id = live(settings, demand, tracker)
    reads = change_after_first_read(monkeypatch, demand, fake_demand, run_id)
    assert tracker.pause(tg) == "paused"
    assert len(reads) == 2 and len(pause_posts(fake_demand)) == 2   # 412, reread, 204
    assert fake_demand.runs[run_id].status == "paused"


def test_stale_etag_after_someone_else_paused_is_not_retried(settings, demand, fake_demand,
                                                             tracker, monkeypatch):
    tg, run_id = live(settings, demand, tracker)
    change_after_first_read(monkeypatch, demand, fake_demand, run_id, status="paused")
    assert tracker.pause(tg) == "already_paused"
    assert len(pause_posts(fake_demand)) == 1


def test_delayed_launch_poll_cannot_undo_a_later_pause(settings, demand, fake_demand, tracker,
                                                       db, monkeypatch):
    from cint_ref.demand_client import DemandClient

    fake_demand.polls_before_terminal = 0
    project, tg = draft(settings, demand)
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    demand_b = DemandClient(settings.demand_base_url, settings.demand_token,
                            settings.demand_api_version, fake_demand.transport)
    tracker_b = LaunchTracker(db, demand_b)
    original = demand.get_launch_job

    def a_reads_then_b_acts(*args):
        job = original(*args)                  # A has read 'Completed'...
        assert tracker_b.poll(tg)["state"] == "live"   # ...B records live
        assert tracker_b.pause(tg) == "paused"         # ...and B pauses
        return job                             # then A's reply is applied

    monkeypatch.setattr(demand, "get_launch_job", a_reads_then_b_acts)
    assert tracker.poll(tg)["state"] == "paused"
    assert tracker.get(tg)["state"] == "paused"


def test_launch_retry_with_a_different_request_is_refused(settings, demand, tracker):
    from cint_ref.launch import LaunchConflict

    project, tg = draft(settings, demand)
    tracker.start(ACCOUNT, project, tg, "2026-11-16T23:59:59.000Z")
    with pytest.raises(LaunchConflict):
        tracker.start(ACCOUNT, project, tg, "2026-12-31T23:59:59.000Z")
