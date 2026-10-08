"""Settings. Every credential here is a placeholder; nothing talks to Cint."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    database_url: str
    # S2S lives on its own host and takes the raw key, no "Bearer", no version header.
    # Source: how-to-server-to-server-api (2025-12-18).
    s2s_base_url: str = "https://s2s.cint.com"
    s2s_api_key: str = "placeholder-s2s-key"
    # Demand API: bearer JWT plus Cint-API-Version. Source: spec info.description.
    demand_base_url: str = "https://api.cint.com/v1"
    demand_api_version: str = "2025-12-18"
    demand_token: str = "placeholder-jwt"
    # Webhook signing secret, used as given (see tests/test_webhooks.py).
    webhook_secret: str = "placeholder-webhook-secret"
    # Local policy, off by default: Cint retries for up to 7 days and does not say
    # whether t is re-signed on each attempt (question 5 in the README).
    webhook_max_age_seconds: Optional[int] = None
    # Where the survey host serves Cint entries. Placeholder domain.
    public_base_url: str = "https://surveys.example.org"
    # Return URL after a confirmed transition. Source: how-to-server-to-server-api step 3.
    callback_url_template: str = "https://samplicio.us/s/ClientCallBack.aspx?RID={rid}"
    # A row stuck in 'in_flight' longer than this is treated as a crashed attempt.
    in_flight_lease_seconds: int = 60
    max_transition_attempts: int = 3

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql:///edsl_cint_reference_test"
            )
        )
