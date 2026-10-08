-- Second pass: host decisions, attempt ownership, return authorization, webhook
-- projection state, immutable launch requests. Idempotent.

ALTER TABLE cint_sessions
    -- Identity of the attempt that owns 'in_flight'; settlement must present it.
    ADD COLUMN IF NOT EXISTS attempt_token uuid,
    -- Replies that arrived after their attempt lost ownership: kept, never applied.
    ADD COLUMN IF NOT EXISTS late_replies jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Permission to send the respondent back to Cint, separate from classification.
    ADD COLUMN IF NOT EXISTS return_authorized boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS return_authorized_by text,
    -- Who resolved an unknown or failed transition by hand, and when.
    ADD COLUMN IF NOT EXISTS resolved_by text,
    ADD COLUMN IF NOT EXISTS resolved_at timestamptz,
    -- Sequence of the last event that carried a client status change.
    ADD COLUMN IF NOT EXISTS observed_client_status_seq integer;

DO $$ BEGIN
    ALTER TABLE cint_sessions ADD CONSTRAINT return_needs_confirmation
        CHECK (NOT return_authorized OR transition_state = 'confirmed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE cint_webhook_inbox
    -- applied: projected; unmatched: valid but no local session yet (replayed at
    -- admission); ignored: not projectable (e.g. session id is not a UUID).
    ADD COLUMN IF NOT EXISTS projection text NOT NULL DEFAULT 'applied'
        CHECK (projection IN ('applied','unmatched','ignored'));

ALTER TABLE cint_launches
    -- The exact launch request sent with the stored idempotency key.
    ADD COLUMN IF NOT EXISTS launch_request jsonb;
