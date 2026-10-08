"""FastAPI router for the respondent flow and the webhook receiver."""

from __future__ import annotations

from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .config import Settings
from .db import Database
from .respondent_flow import (
    AdmissionRefused,
    NoSavedResponse,
    RespondentFlow,
    ResponseConflict,
    SurveyBinding,
    parse_rid,
)
from .s2s_client import S2SClient
from .webhooks import SignatureError, WebhookInbox, decoded_body, verify_signature


class ResponseIn(BaseModel):
    rid: str
    response_uuid: str
    entries: dict[str, Any]
    scenario: Optional[dict[str, Any]] = None


def create_app(
    settings: Settings,
    bindings: dict[str, SurveyBinding],
    s2s_transport: Optional[httpx.BaseTransport] = None,
) -> FastAPI:
    db = Database(settings.database_url)
    flow = RespondentFlow(
        db, S2SClient(settings.s2s_base_url, settings.s2s_api_key, s2s_transport), settings, bindings
    )
    inbox = WebhookInbox(db)
    app = FastAPI(title="Cint integration reference for EDSL human surveys")
    app.state.flow = flow

    @app.get("/cint/s/{human_survey_uuid}/entry")
    def entry(human_survey_uuid: str, rid: str = ""):
        # Attachment point 1: the existing survey entry renders after admission.
        try:
            row = flow.admit(human_survey_uuid, rid)
        except AdmissionRefused as e:
            return JSONResponse({"admitted": False, "reason": str(e)}, status_code=403)
        return {"admitted": True, "rid": str(row["rid"]), "survey": human_survey_uuid}

    @app.post("/cint/s/{human_survey_uuid}/responses")
    def save(human_survey_uuid: str, body: ResponseIn):
        # Attachment point 2: authorized response persistence.
        try:
            row = flow.save_response(human_survey_uuid, body.rid, body.response_uuid,
                                     body.entries, body.scenario)
        except AdmissionRefused as e:
            return JSONResponse({"saved": False, "reason": str(e)}, status_code=403)
        except ResponseConflict as e:
            return JSONResponse({"saved": False, "reason": str(e)}, status_code=409)
        return {"saved": True, "outcome": row["outcome"]}

    @app.post("/cint/s/{human_survey_uuid}/finish")
    def finish(human_survey_uuid: str, rid: str, request: Request):
        # Attachment point 3: outcome finalization. Any status sent by the browser
        # (e.g. ?status=complete) is ignored; the outcome comes from server state.
        try:
            rid = parse_rid(rid)
        except AdmissionRefused:
            return JSONResponse({"state": "refused", "reason": "malformed rid"}, status_code=403)
        row = flow.session_state(rid)
        if row is None or row["human_survey_uuid"] != human_survey_uuid:
            return JSONResponse({"state": "refused", "reason": "unknown session"}, status_code=403)
        try:
            result = flow.finalize(rid)
        except NoSavedResponse as e:
            return JSONResponse({"state": "refused", "reason": str(e)}, status_code=409)
        if result.redirect_url:
            return RedirectResponse(result.redirect_url, status_code=303)
        # No redirect without a confirmed transition (guide: wait for 200).
        return JSONResponse(
            {"state": result.state, "response_saved": True, "detail": result.detail},
            status_code=202,
        )

    @app.get("/cint/sessions/{rid}")
    def session(rid: str):
        # Attachment point 4: status reporting for operators.
        try:
            row = flow.session_state(parse_rid(rid))
        except AdmissionRefused:
            row = None
        if row is None:
            return JSONResponse({"found": False}, status_code=404)
        keep = ("rid", "human_survey_uuid", "outcome", "outcome_conflicts", "transition_state",
                "transition_attempts", "last_transition_http", "last_transition_error",
                "confirmed_by", "observed_s2s_status", "observed_client_status", "observed_seq")
        return {k: (str(row[k]) if k == "rid" else row[k]) for k in keep}

    @app.post("/cint/webhooks")
    async def webhook(request: Request):
        raw = await request.body()
        try:
            body = decoded_body(raw, request.headers.get("content-encoding"))
            t = verify_signature(settings.webhook_secret, request.headers.get("cint-signature", ""),
                                 body, settings.webhook_max_age_seconds)
        except (SignatureError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        # Durable first: the acknowledgment is returned only after the commit.
        event_id, _ = await run_in_threadpool(inbox.accept, body, t)
        return {"event_id": event_id}

    return app
