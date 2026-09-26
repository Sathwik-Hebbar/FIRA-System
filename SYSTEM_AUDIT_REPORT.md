# FIRA Incident Data-Flow Audit

## Existing architecture

The FastAPI backend accepts web reports in `backend/main.py` and voice SOS data in
`backend/routes_voice.py`. Voice calls pass through Sarvam STT/translation,
`GeminiExtractor`, citizen matching, and `merge_voice_sos`; reports are then stored
in Neon PostgreSQL and read by `/incidents` for the Command Center UI.

## Problems found and fixed

| Area | Problem | Fix |
| --- | --- | --- |
| Voice triage | Extracted fields were converted back into a keyword string for priority, so structured facts could be lost. | `services/incident_processor.py` scores normalized fields directly. |
| Persistence | Raw payload, normalized facts, risk and reasons were not persisted on the incident. | Added report audit columns and database migration. |
| Two input paths | Web and voice reports used unrelated scoring flows. | Both call `process_incident()`. |
| Explainability | Only one legacy priority reason was retained. | Canonical `priority.reason` is persisted as `priority_reasons`. |
| Gemini resilience | A dummy/local key could trigger outbound attempts. | Dummy keys are treated as unconfigured; deterministic fallback remains active. |

## Authoritative flow

`voice/web input -> extraction -> normalize_incident -> calculate_incident_risk -> calculate_incident_priority -> persist -> /incidents -> Command Center`

Sarvam webhook requests already deduplicate by voice `session_id`; the original
voice payload is now retained with the session, while a normalized incident is
retained with the report.

## API and database changes

No endpoint was removed. `/report` and `/api/voice-sos/process` now return records
whose backend values include `risk_score`, `risk_level`, `priority_reasons`, and
`normalized_incident` from `/incidents` and `/incident/{id}`.

The `reports` table gains `raw_input`, `normalized_data`, `risk_score`,
`risk_level`, `priority_reasons`, `ai_analysis`, and `updated_at`; `voice_sessions`
gains `raw_payload`. `ensure_schema_migrations()` validates database schemas.

## Environment

`.env` contains `GEMINI_API_KEY=dummy_gemini_api_key_for_local_development` and
`GEMINI_ASSISTED_REVIEW=false`. See `.env.example` for `SARVAM_API_KEY`,
`GEMINI_API_KEY`, `DATABASE_URL`, `VOICE_AGENT_ID`, and `CONNECTION_ID`.

## Tests and limitations

`tests/test_incident_processor.py` covers low-water, trapped/chest-water P1,
medical-injury P1, and stale-priority recalculation. The existing voice suite
continues to cover Gemini fallback and webhook duplicate handling.

This remains an MVP: command-center synchronization uses its existing client
refresh/polling behavior rather than server push, and the optional Gemini review
envelope is reserved for a configured provider; it never changes deterministic
priority.
