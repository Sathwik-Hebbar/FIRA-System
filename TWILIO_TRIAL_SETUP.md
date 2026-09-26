# Connect a Twilio trial number to FIRA

FIRA has an inbound Twilio webhook and bidirectional Media Streams handler. A trial account is enough to try an inbound call if Twilio lets the account provision a Voice number for your country. Trial account eligibility, available numbers, verification requirements, and remaining trial balance are controlled by Twilio and can vary by country/account. A trial is not a guaranteed free toll-free number.

## 1. Expose the local backend over HTTPS using ngrok

Twilio must reach your FastAPI backend over the public internet. Run the backend (port 8000) and expose it using **ngrok** with your permanent static domain:

### Start the ngrok Tunnel

Run the following command in a terminal window (or execute `start_ngrok_tunnel.bat` / `.\start_ngrok_tunnel.ps1`):

```powershell
ngrok http --url=baggy-boogieman-waggle.ngrok-free.dev 8000
```

> **Tip (Web Inspect UI):** Open `http://localhost:4040` in your browser to inspect incoming Twilio HTTP requests, webhook payloads, and live WebSocket streams in real-time.

### Configure PUBLIC_URL

In the project-root `.env`, set:

```dotenv
PUBLIC_URL=https://baggy-boogieman-waggle.ngrok-free.dev
AGENT_LANGUAGE=kn-IN
```

Because this is a permanent static domain, your URL never changes on restart! The WebSocket URL is automatically derived from it as `wss://baggy-boogieman-waggle.ngrok-free.dev/ws`.

## 2. Configure the Twilio number

In Twilio Console, open **Phone Numbers → Manage → Active numbers**, select the Voice-capable number, and set **A call comes in** to **Webhook**, method **HTTP POST**, URL:

```text
https://baggy-boogieman-waggle.ngrok-free.dev/twilio/voice
```

Save the configuration. The route returns TwiML that connects the call to FIRA's bidirectional `/ws` Media Stream. The app also exposes `/api/twilio/voice`, but the root `/twilio/voice` route is the URL above.

## 3. Place a trial call

Call the Twilio number from a caller number permitted by your trial account. Twilio trial accounts can restrict inbound calls to verified caller IDs; check the account's Voice trial panel and verify the phone you will call from. Trial calls may play a trial notice and are subject to current trial balance, duration, and country rules. The provider number, inbound minutes, and a public tunnel may not remain free.

## 4. FIRA speech services

Twilio carries the phone audio; it does not provide FIRA's Kannada recognition, translation, extraction, or synthesized response. The current FIRA stream pipeline calls the configured Sarvam services for speech and Gemini for extraction. Configure their keys privately in `.env` if available. Do not place credentials in frontend files or commit `.env`. Without working speech-service credentials, the call can connect but the complete Kannada conversation will not work.

The current endpoint accepts incoming webhook requests without checking `X-Twilio-Signature`. Use it for a controlled demo tunnel only; before exposing it as a public service, add Twilio request-signature validation (including validation for the WebSocket handshake).

## Troubleshooting

- **Call does not reach FIRA:** inspect Twilio's call log and confirm the incoming webhook URL/method and tunnel URL.
- **Stream fails to connect:** the backend must be publicly reachable through valid HTTPS/WSS on port 443, with `/ws` forwarded to FastAPI.
- **Call connects but agent is silent:** inspect backend logs and confirm Sarvam STT/TTS configuration and trial/service quota.
- **No Kannada response:** set `AGENT_LANGUAGE=kn-IN`; check that the STT and TTS services accept this locale.
- **Caller rejected:** verify the caller number and check the current country and trial restrictions in Twilio Console.
