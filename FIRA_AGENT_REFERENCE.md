FIRA — Project Reference (for AI Coding Agent)
This file is the single source of truth for building FIRA. Read this before generating or editing any code. If a later instruction conflicts with this file, ask before deviating from the schema, API contract, or model approach below — they are fixed decisions, not suggestions.

FIRA is being built as a small, legitimate MVP — a real product slice, not a scripted demo. Every screen must do genuine work against genuine data flowing through the system. No hardcoded outputs, no fake buttons, no screens that only exist to be clicked once in front of an audience.

1. Project Summary
FIRA (Flood Intelligence & Response Assistant) is a flood-risk-and-SOS product. It gives citizens a live, computed flood-risk view of their area, lets them submit SOS reports, and gives responders a priority-sorted dashboard with automatic shelter recommendations.

Core product loop: Conditions in a zone change (rainfall/river level) → the risk model recomputes that zone's risk in real time → a citizen sees the updated risk and, if needed, submits an SOS → the backend scores its priority and attaches the nearest shelter → the emergency dashboard reflects it immediately, correctly sorted above lower-priority incidents.

Non-negotiable constraints:

No authentication, no user accounts, no roles, at this stage.
No real road-network routing engine, no live IoT ingestion, no computer vision. These are roadmap items — do not attempt partial implementations of them.
The app must run fully functional against seed/synthetic data with zero external dependencies. Any live third-party API is an optional enhancement layer, never a hard requirement for the app to work.
Every score or recommendation the system shows must come from an actual computation over real request data — never a value invented for effect.
2. Tech Stack (fixed — do not substitute)
Layer	Choice	Notes
Frontend	React (Vite)	Component-based: Map view, Report form, Dashboard as separate routed pages
Map	react-leaflet + OpenStreetMap tiles	No API key required
Backend	FastAPI (Python)	Async, auto-docs at /docs
Database	SQLite via SQLAlchemy	File-based, fira.db; swappable for Postgres later with no schema change
Risk model	Trained ML classifier (scikit-learn), with a rule-based formula as fallback/baseline	See §5
SOS text understanding	Google Gemini API (gemini-1.5-flash or current equivalent), with a keyword rule-engine as fallback	See §6
3. Folder Structure
fira/
├── backend/
│   ├── main.py                 # FastAPI app + all routes
│   ├── risk_engine.py          # loads ML model, compute_risk(), rule-based fallback, bump_rainfall()
│   ├── priority_engine.py      # compute_priority() — Gemini call + keyword fallback
│   ├── models.py               # SQLAlchemy models: Zone, Report, Shelter
│   ├── database.py             # SQLite engine + session dependency
│   ├── seed_data.py            # seed_if_empty() loader
│   ├── gemini_client.py        # thin wrapper around the Gemini API call, with try/except fallback
│   └── requirements.txt
├── ml/
│   ├── generate_training_data.py   # produces synthetic labeled dataset from domain rules + noise
│   ├── train_risk_model.py         # trains + evaluates + saves the model
│   ├── training_data.csv           # generated dataset (checked in for reproducibility)
│   └── risk_model.joblib           # trained model artifact, loaded by risk_engine.py
├── frontend/
│   ├── index.html
│   ├── package.json
│   ├── vite.config.js
│   └── src/
│       ├── main.jsx
│       ├── App.jsx                 # router: /, /report, /dashboard
│       ├── api.js                  # fetch wrappers for all backend calls
│       ├── pages/
│       │   ├── RiskMapPage.jsx
│       │   ├── ReportPage.jsx
│       │   └── DashboardPage.jsx
│       ├── components/
│       │   ├── ZoneMap.jsx         # react-leaflet map, zones + shelters + incidents
│       │   ├── RiskBanner.jsx
│       │   ├── ReportForm.jsx
│       │   └── IncidentTable.jsx
│       └── styles/
│           └── index.css
├── data/
│   ├── seed_zones.json
│   └── seed_shelters.json
├── README.md
└── .env.example                # GEMINI_API_KEY=...
Keep all scoring logic in risk_engine.py / priority_engine.py and all model training in ml/ — routes in main.py only call these modules, they never contain scoring logic inline.

4. Database Schema
Zone
Field	Type	Notes
id	int, PK	
name	str	e.g. "Riverside Ward 4"
lat, lng	float	zone center point
rainfall_mm	float	last-24h rainfall
river_level_m	float	current level
danger_level_m	float	threshold level
elevation_m	float	ground elevation
drainage_quality	float, 0–1	1 = excellent drainage
historical_frequency	float, 0–1	past flood frequency
Report
Field	Type	Notes
id	int, PK	
name	str	reporter name
phone	str	reporter phone
lat, lng	float	incident location
severity	str	"Low" / "Medium" / "High" / "Critical"
description	str	free-text SOS message
priority_score	float	computed by priority_engine, 0–100
status	str	"New" / "Assigned" / "Resolved"
shelter_id	int, FK → Shelter.id	nearest recommended shelter
created_at	datetime	auto-set on insert
Shelter
Field	Type	Notes
id	int, PK	
name	str	
lat, lng	float	
capacity	int	
Do not add a Users table, auth fields, or an EmergencyResources table — out of scope at this stage.

5. Risk Model (risk_engine.py + ml/)
FIRA uses an actual trained ML classifier for risk level, not a hand-tuned formula presented as if it were a model. Because no real historical labeled flood dataset exists for the demo region, the model is trained on a synthetic dataset generated from documented domain rules plus randomized noise — this is stated openly in code comments and docs, never described as real historical training data.

5.1 Generate training data (ml/generate_training_data.py)
Procedurally generate several thousand rows by sampling rainfall_mm, river_level_m, danger_level_m, elevation_m, drainage_quality, historical_frequency from realistic ranges.
Label each row's risk level using the same weighted domain formula from the original rule-based design, plus random noise, so the model learns a generalizable boundary rather than memorizing the exact formula:
raw_score =
    0.35 * normalized(rainfall_mm) +
    0.30 * normalized(river_level_m - danger_level_m) +
    0.15 * normalized(historical_frequency) +
    0.10 * normalized(1 / elevation_m) +
    0.10 * normalized(1 - drainage_quality)
    + gaussian_noise(0, 0.05)

label = "Low" if raw_score < 0.25
        "Moderate" if raw_score < 0.50
        "High" if raw_score < 0.75
        else "Severe"
Save as ml/training_data.csv.
5.2 Train the model (ml/train_risk_model.py)
Model: RandomForestClassifier (scikit-learn) — robust to noisy/nonlinear feature interactions, gives predict_proba for a continuous-feeling score, and is trivial to explain to a non-ML audience ("a model trained to recognize the same rainfall/river-level/elevation patterns that cause flooding").
Features: the 6 raw zone fields (let the model learn interactions/normalization itself; do not hand-normalize inputs before training — pass raw values, use StandardScaler in a pipeline if needed).
Split: 80/20 train/test. Report accuracy and a confusion matrix in a comment/log — this is the model's validation, keep it visible.
Persist with joblib.dump() to ml/risk_model.joblib.
5.3 Serve predictions (risk_engine.py)
Function: compute_risk(zone) -> {score: float 0-1, level: str, reason: str}

Load risk_model.joblib once at startup.
score = the model's predicted probability of the predicted class (or max class probability), level = the predicted class label.
reason: derive from feature importances (model.feature_importances_) — report the top contributing feature by name in plain English (e.g. "Driven primarily by river level relative to danger threshold").
Fallback: if risk_model.joblib is missing or fails to load, fall back to the deterministic weighted formula in §5.1 directly (no noise) so the app never breaks due to a missing model file. Log clearly which path (model vs fallback-formula) served each request — this must be inspectable, never silent.
Also implement: bump_rainfall(db, zone_id, delta) — increases rainfall_mm on a zone and persists it, used to show the risk model responding to changing conditions in real time (via manual trigger or a scheduled/simulated feed — framed as a testing/ops control, not a demo gimmick).

6. Priority Engine (priority_engine.py + gemini_client.py)
Function: compute_priority(severity: str, description: str) -> dict {priority_score: float, signals: list[str], source: "gemini" | "keyword-fallback"}

Primary path — Gemini API: send the description to Gemini with a prompt instructing it to return structured JSON only: {urgent: bool, vulnerable_person: bool, hazard_type: str, signals: [str]}. Parse this JSON and use it to compute the boost (see below). Wrap the call in try/except with a strict timeout.

Fallback path — keyword rule-engine: if the Gemini call fails, times out, or returns unparseable output, scan description (case-insensitive) for keywords such as trapped, stuck, child, elderly, can't swim, water rising, no power, and derive the same signals list from keyword hits instead.

Score combination (same for both paths):

base = {Low: 20, Medium: 40, High: 70, Critical: 90}[severity]
boost = 10 points per distinct signal detected (urgent, vulnerable_person, or each item in signals), capped contribution at +30
priority_score = min(100, base + boost)
Store which source produced the signals on the Report record (or log it) — this must always be inspectable/debuggable, never presented as more certain than it is. Gemini is used for genuine language understanding of the free-text report, not decorative — if the call is never actually wired up and tested, do not claim it in the UI or docs.

gemini_client.py reads GEMINI_API_KEY from environment (.env) and must never hardcode a key.

7. API Contract (fixed — frontend and backend both code against this)
Method	Endpoint	Body / Params	Returns
GET	/zones	—	list of all zones (full Zone fields)
GET	/risk/{zone_id}	—	{zone_id, score, level, reason, source}
POST	/simulate-rain	{zone_id, delta}	updated zone risk (same shape as /risk/{id})
POST	/report	{name, phone, lat, lng, severity, description}	full Report record incl. priority_score, signals, source, shelter_id, shelter_name
GET	/incidents	—	list of Reports, sorted by priority_score descending
PATCH	/incident/{id}	{status}	updated Report record
GET	/shelters	—	list of all shelters
CORS: allow the Vite dev origin (http://localhost:5173) explicitly rather than "*" once the frontend is React-based, to keep the setup closer to production-appropriate.

Nearest shelter for a report = minimum haversine distance from (lat, lng) to each Shelter, ignoring capacity unless explicitly asked to add capacity filtering.

8. Frontend (React)
Page/Component	File	Purpose	Calls
Risk map page	pages/RiskMapPage.jsx + components/ZoneMap.jsx, RiskBanner.jsx	Live risk-colored zones, shelters, link to report page, ops control to bump rainfall	GET /zones, GET /risk/{id}, GET /shelters, POST /simulate-rain
Report page	pages/ReportPage.jsx + components/ReportForm.jsx	Click-to-set location on map, severity select, description, submit	POST /report
Dashboard page	pages/DashboardPage.jsx + components/IncidentTable.jsx	Polls/refetches incidents, table sorted by priority, status control per row	GET /incidents, PATCH /incident/{id}
Routing: React Router (/, /report, /dashboard). State: local component state + fetch/useEffect is sufficient — no global state library needed at MVP size.

Color mapping (consistent across map + dashboard): Low = green, Moderate = yellow, High = orange, Severe = red.

Styling: plain CSS (or CSS modules) in src/styles/ — calming blue/teal for citizen-facing pages, a red/orange accent on the dashboard. No UI framework required, but Tailwind is acceptable if the team is already fluent in it.

9. Data & Model Artifacts
data/seed_zones.json: 6–8 zones with realistic, varied values across the risk spectrum — used to seed the live database (what the running app actually serves).

data/seed_shelters.json: 4 shelters with lat/lng near the seed zones and a capacity each.

ml/training_data.csv and ml/risk_model.joblib: the separate, larger synthetic dataset used only to train the risk model (§5) — not the same as the seed data above, which populates the live app's zones. Keep these clearly distinct in code and docs: seed data = what the product shows; training data = what taught the model to score it.

Loaded once at startup via seed_data.seed_if_empty(db), only if the tables are currently empty — never re-seed over existing data.

10. Explicitly Out of Scope (do not implement)
Authentication / login / roles
Real road-network routing (OSRM/GraphHopper) — if "safer route" is requested, implement only as a static "blocked road" flag on the map, not real pathfinding
Computer vision / image analysis on uploaded photos (storing a photo is fine; analyzing it is not)
Live IoT/sensor ingestion pipelines
Multi-language UI
Cloud deployment/CI setup
Any second LLM provider alongside Gemini — pick one and keep the fallback path simple
If asked to add any of the above, flag that it's outside the current MVP scope before implementing.

11. Definition of Done (end-to-end)
The MVP is complete when this full loop works with zero errors, twice in a row, entirely through genuine computation (no hardcoded responses anywhere in the path):

The React risk map page loads, fetches real zone data, and renders visibly different-colored zones based on the trained model's live predictions.
Triggering the rainfall-bump control updates a zone's underlying data, and the risk map reflects the model's new prediction without a full page reload.
Submitting a report with an urgency-laden description (e.g. "elderly parent trapped, water rising fast") is sent to Gemini, correctly classified, and returns a high priority_score with a sensible nearest shelter — and if Gemini is unreachable, the keyword fallback still produces a correct, distinguishable score.
That report appears at the top of the dashboard, correctly sorted above lower-priority incidents, from real data — not injected for the demo.
Changing a report's status via the dashboard persists correctly on refresh.
The whole flow — including risk scoring — still works with GEMINI_API_KEY unset or the network disabled, via the documented fallback paths in §5 and §6.