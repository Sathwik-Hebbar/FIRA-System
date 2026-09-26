# Sarvam Voice Agent → FIRA SOS

FIRA exposes an authenticated endpoint for Sarvam's Voice Agent API tool. The tool posts the caller's transcript to the existing FIRA voice SOS pipeline; FIRA then extracts emergency details, computes priority, and saves the incident and call session in SQLite.

## 1. Configure the backend secret

Add a long, random value to the project-root `.env` (the same file used for `PUBLIC_URL`):

```dotenv
SARVAM_FIRA_WEBHOOK_TOKEN=replace-with-a-long-random-secret
```

Use the same value as the Sarvam API tool's Bearer credential. Keep it out of source control and frontend code. Restart FastAPI after changing `.env`.

## 2. Expose FIRA to Sarvam with ngrok
 
For local development, run FastAPI and expose port 8000 using **ngrok**:

**Terminal 1 (Start FastAPI):**
```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 (Start ngrok Tunnel):**
```powershell
ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000
```

*(Or run `start_ngrok_tunnel.bat` / `.\start_ngrok_tunnel.ps1`)*

The route for the Sarvam Voice SOS API tool is:

```text
POST https://baggy-boogieman-waggle.ngrok-free.dev/api/sarvam/voice-sos
```

Keep the ngrok tunnel running while calls are being tested.

## 3. Configure the Sarvam API tool

In the Voice Agent, create an API tool named `submit_fira_sos`:

- Method: `POST`
- URL: `https://baggy-boogieman-waggle.ngrok-free.dev/api/sarvam/voice-sos`
- Authentication: `Bearer`; enter the same secret as `SARVAM_FIRA_WEBHOOK_TOKEN` in the secure credential field.
- Run: **on_end** (after the conversation)
- Header: `Content-Type: application/json`
- Body:

```json
{
  "caller_phone": "@User Identifier",
  "transcript": "@Call Transcript",
  "session_id": "@Interaction ID",
  "language": "kn-IN"
}
```

Insert the three `@` values with Sarvam's variable picker; they are tool-context variables, not literal strings. Because this is an **on_end** lifecycle tool, Sarvam calls it automatically when the conversation ends; it does not need to be invoked by the agent in its instructions. Configure the agent to collect and confirm location, people affected, whether anyone is trapped or injured, and water level before ending an emergency call.

## 4. Verify the flow

Place a test call and check the API tool result plus FIRA's backend logs. The route returns the saved SOS ID and session ID. Repeated delivery of the same Sarvam interaction ID reuses the existing incident instead of creating another one.

The endpoint returns `503` if `SARVAM_FIRA_WEBHOOK_TOKEN` is missing and `401` if Sarvam sends the wrong Bearer token. It accepts the caller, transcript, session ID, language, and optional coordinates as defined by FIRA's `VoiceProcessRequest`.

## Location accuracy

Include `lat` and `lng` only if they come from a reliable source. A spoken landmark is retained in the transcript and extraction, but FIRA currently falls back to a regional default coordinate when neither caller-profile coordinates nor explicit coordinates are available. Do not use that map marker as a verified caller location.
