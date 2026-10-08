"""The draft target group request against the schema extracted from the pinned spec."""

import copy
import json
from pathlib import Path

from cint_ref.audience_fixture import draft_target_group
from cint_ref.contract import CONTRACTS, contract_errors
from cint_ref.demand_client import DemandClient, DemandError
from fake_cint.demand import FakeDemand

LIVE_URL = "https://surveys.example.org/cint/s/x/entry?rid=[%RID%]"
PINNED_SHA256 = "008c54ee13d77341026cb9d5cdd06d2493350da66923b243e6d70f79ee904940"


def previous_fixture():
    """The fixture as published at 07d80e4: string client_id, no allocation."""
    body = copy.deepcopy(draft_target_group(LIVE_URL, 1234, 200))
    body["client_id"] = "placeholder-security-client-id"
    del body["allocations"]
    del body["filling_strategy"]
    return body


def test_contract_file_comes_from_the_pinned_spec():
    meta = json.loads((CONTRACTS / "create_draft_target_group.json").read_text())["x-source"]
    assert meta["sha256"] == PINNED_SHA256 and meta["root"] == "CreateDraftTargetGroupRequest"


def test_fixture_satisfies_the_pinned_schema():
    assert contract_errors("create_draft_target_group", draft_target_group(LIVE_URL, 1234, 200)) == []


def test_previous_fixture_fails_with_two_errors():
    errors = contract_errors("create_draft_target_group", previous_fixture())
    assert len(errors) == 2
    assert any(e.startswith("<root>: fails 'oneOf'") for e in errors)
    assert "client_id: 'placeholder-security-client-id' is not of type 'integer'" in errors


def test_validator_applies_nested_types():
    body = draft_target_group(LIVE_URL, 1234, 200)
    body["allocations"]["open_exchange_allocations"]["exchange_max_percentage"] = "0"
    assert contract_errors("create_draft_target_group", body)


def test_fake_cint_rejects_what_the_schema_rejects():
    fake = FakeDemand()
    demand = DemandClient("https://api.cint.com/v1", "placeholder-jwt", transport=fake.transport)
    try:
        demand.create_draft_target_group(101, "p", previous_fixture())
    except DemandError as e:
        assert e.status == 400 and len(e.body["detail"]) == 2
    else:
        raise AssertionError("the fake accepted an invalid request")
