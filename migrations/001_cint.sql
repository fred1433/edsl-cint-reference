-- Minimal durable state for the behaviour this reference implements.
-- Each constraint says what it protects; see docs/TECHNICAL_NOTES.md "State in PostgreSQL".

CREATE TABLE IF NOT EXISTS cint_sessions (
    -- One row per Cint RID: a RID can be admitted once and is bound to one survey.
    rid                     uuid PRIMARY KEY,
    human_survey_uuid       text        NOT NULL,
    entry_href              text        NOT NULL,   -- href returned by S2S at admission
    admitted_at             timestamptz NOT NULL DEFAULT now(),

    -- The recorded response lives in the same row as the outcome it justifies, so
    -- "an outcome without a saved response" cannot be written in one transaction.
    response_uuid           text UNIQUE,
    response_entries        jsonb,                  -- {question_name: HumanResponseEntry}
    scenario                jsonb,
    response_saved_at       timestamptz,

    outcome                 text CHECK (outcome IN ('complete','screenout','quality_terminate','quota_full')),
    outcome_decided_by      text,
    outcome_conflicts       jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- Transition intent and its fate. 'in_flight' is committed BEFORE the S2S call,
    -- so a crash leaves evidence that a request may have left the process.
    transition_state        text        NOT NULL DEFAULT 'not_ready'
        CHECK (transition_state IN ('not_ready','pending','in_flight','confirmed','unknown','failed')),
    transition_attempts     integer     NOT NULL DEFAULT 0,
    in_flight_since         timestamptz,
    last_transition_http    integer,
    last_transition_error   text,
    confirmed_at            timestamptz,
    confirmed_by            text,       -- 's2s_200' or 'operator:<name>'

    -- Evidence from Cint that is NOT an acknowledgment (S2S GET, session webhooks).
    observed_s2s_status     integer,
    observed_s2s_at         timestamptz,
    observed_client_status  integer,
    observed_seq            integer,

    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT outcome_needs_response CHECK (outcome IS NULL OR response_saved_at IS NOT NULL),
    CONSTRAINT transition_needs_outcome CHECK (transition_state = 'not_ready' OR outcome IS NOT NULL)
);

-- Recovery scans look for unfinished work by state.
CREATE INDEX IF NOT EXISTS cint_sessions_unfinished
    ON cint_sessions (transition_state)
    WHERE transition_state IN ('pending','in_flight','unknown');

CREATE TABLE IF NOT EXISTS cint_webhook_inbox (
    -- Cint delivers at least once: the CloudEvents id makes a redelivery a no-op.
    event_id        text PRIMARY KEY,
    event_type      text        NOT NULL,
    event_time      timestamptz,             -- CloudEvents "time": when it happened
    signature_t     bigint      NOT NULL,    -- Cint-Signature t: when it was signed
    payload         jsonb       NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cint_quota_progress (
    -- Observed progress only; never used to change an in-progress respondent.
    target_group_id     text        NOT NULL,
    profile_quota_id    text        NOT NULL DEFAULT '',
    screens             integer     NOT NULL,
    completes           integer     NOT NULL,
    event_time          timestamptz NOT NULL,
    PRIMARY KEY (target_group_id, profile_quota_id)
);

CREATE TABLE IF NOT EXISTS cint_launches (
    -- One launch per target group; the idempotency key is stored before the request
    -- so a retry after a crash reuses it and cannot create a second job.
    target_group_id         text PRIMARY KEY,
    account_id              integer     NOT NULL,
    project_id              text        NOT NULL,
    idempotency_key         uuid        NOT NULL,
    job_id                  text,
    state                   text        NOT NULL
        CHECK (state IN ('requested','launching','live','paused','failed')),
    fielding_run_id         text,
    failure_code            text,
    updated_at              timestamptz NOT NULL DEFAULT now()
);
