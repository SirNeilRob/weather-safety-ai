# SharkByte — offline-first weather alert backend (Python)

A backend that keeps issuing weather warnings when the internet does not.

* **Online** → the Swarm API drafts the public copy (the `communication` stage).
* **Offline** → a deterministic local pipeline classifies the hazard, picks the
  safety playbook and writes the EN/ES messages itself, in milliseconds.
* **Back online** → locally-produced alerts are reconciled upstream (if a sync
  endpoint is configured) and the backlog is reported honestly in `/v1/health`.

The safety advice is **never** produced by a language model, online or offline:
the deterministic Safety agent owns it in both modes. That is the design
decision the whole system rests on.

Status: **runs and is tested** — the test suite passes and the online→offline
drill (`scripts/offline_drill.py`) runs a live NWS alert against the real Swarms API.

Companion docs:

| Doc | Audience |
|---|---|
| [`docs/FRONTEND_CONTRACT.md`](docs/FRONTEND_CONTRACT.md) | frontend engineer — every field the dashboard renders |
| [`docs/SWARM_AGENTS.md`](docs/SWARM_AGENTS.md) | AI/Swarm engineer — agent prompts + JSON contract |
| [`docs/TEAM_SPLIT.md`](docs/TEAM_SPLIT.md) | who owns what this weekend |

---

## 1. Quick start

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in SWARM_BASE_URL / SWARM_API_KEY if you have them
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open <http://127.0.0.1:8000/docs> for interactive API docs.

`.env` (gitignored) holds `SWARMS_API_KEY` and points the backend at the real
Swarms API (`https://api.swarms.world`, `POST /v1/agent/completions`). With no
key the backend simply runs fully offline.

---

## 2. The alert pipeline

`POST /v1/alerts` runs three agents in order, and each hop is recorded in
`trace` so the dashboard can show the handoff:

```
raw CAP alert
   └─ Weather agent      normalise → hazard → severity (+ escalation) → confidence
        └─ Safety agent  severity tier → immediate / avoid / prepare playbook (EN + ES)
             └─ Communication agent → SMS (GSM-7 segments), push, bulletin, voice
```

**Weather agent** (`app/agents/weather.py`, rules in `app/knowledge/hazards.py`)

* Maps the feed's `event` / `product_type` onto a fixed hazard taxonomy
  (tornado, flash flood, heat, winter storm, wildfire, …), with a hazard legend
  available at `GET /v1/hazards`.
* Derives severity from the CAP fields but applies a **safety-first escalation
  rule**: a Tornado Warning is `extreme` even if the feed says `moderate`, and
  a heat alert with no stated severity is raised above `moderate` when the
  wording supports it. Every escalation is explained in
  `classification.rationale` and flagged with `escalated: true`, so nothing is
  silently inflated.
* Reports `confidence` and `signals` (which fields agreed), plus `conflicts`
  when the feed contradicts itself.

**Safety agent** (`app/agents/safety.py`)

* Picks a tier (`extreme` → `severe` → `moderate` → `minor` → `info`) and the
  matching playbook: `immediate`, `avoid`, `prepare`, `first_action`,
  `evacuation_recommended`, `shelter_recommended`.
* Playbooks are written natively in **English and Spanish** — no machine
  translation of life-safety instructions. If the alert carries an official
  instruction (`instruction`), it is surfaced as `official_instruction` and
  marked `official_reliable` only when the source is trusted.

**Communication agent** (`app/agents/communication.py`)

* Builds SMS with real GSM-7 accounting (`chars`, `segments`, unicode fallback),
  a push payload, a long-form bulletin and a voice script.
* Spanish free text goes through a curated phrasebook
  (`app/i18n/terms.py`) with an honest coverage signal in
  `communication.translation` — it reports which phrases it knew rather than
  pretending the output is fully translated.

`engine` is `local`, `swarm` or `mixed` — derived from what actually served the
stages, not from whether the device happened to be online.

### Live data only

Alerts come from the NOAA/NWS feed (`/v1/noaa`, polled for the configured
location) or from `POST /v1/alerts`. Nothing canned is served. The hand-written
CAP alerts used to test classification live in `tests/fixtures/samples.py` and
are never imported by `app/`.

---

## 3. API contract (for the frontend)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/alerts` | submit a weather alert → job id, or finished result with `wait_seconds` |
| `GET` | `/v1/hazards` | severity levels, hazard legend, products, channels, languages |
| `GET` | `/v1/health` | link state, queue depth, sync backlog |
| `POST` | `/v1/jobs` | generic job submit (kept for the non-alert kinds) |
| `GET` | `/v1/jobs/{id}` | poll status / result (`?wait=5` long-polls) |
| `GET` | `/v1/jobs` | recent jobs (`?limit=`, `?status=`) |
| `GET` | `/v1/queue/summary` | queue + sync counters for the status badge |
| `GET` | `/v1/capabilities` | what the UI may show as available |
| `GET` | `/v1/alerts/active` | processed alerts to display (NOAA ones only while NWS still lists them) |
| `GET` | `/v1/noaa` | NOAA location, last poll, active count, latest observation |
| `POST` | `/v1/noaa/location` | set the NOAA point (`latitude`, `longitude`, `label`) |
| `POST` | `/v1/noaa/poll` | poll NOAA now |
| `POST` | `/v1/demo/offline` | force offline on/off (endpoint name kept for compatibility) |
| `POST` | `/v1/sync/run` | force one reconciliation pass |

Submit an alert:

```bash
curl -s -X POST localhost:8000/v1/alerts \
  -H 'content-type: application/json' \
  -d '{
        "alert": {
          "event": "Tornado Warning",
          "severity": "Severe",
          "areaDesc": "Cedar Rapids, Linn County, Iowa",
          "headline": "Tornado Warning issued for Linn County",
          "description": "Take shelter now.",
          "instruction": "Move to a basement or an interior room on the lowest floor."
        },
        "languages": ["en", "es"],
        "wait_seconds": 15
      }' | jq '.result.classification | {hazard, severity, escalated}'
```

`languages` accepts `en`, `es` (default both). `audience` is `public` by
default. Job states: `pending → running → done | failed`, plus
`sync_state: not_needed | pending | sent | failed`.

The generic kinds (`analyze`, `summarize`, `classify`, `extract`) still work and
are unchanged — see `app/core/processors/local.py` if you need them.

---

## 4. How it decides what to use

Every job goes through the same decision order (`app/core/router.py`):

| # | Condition | Result |
|---|---|---|
| 1 | request says `prefer_local`, or device is offline | local engine |
| 2 | Swarm circuit breaker is open (3 consecutive failures) | local engine |
| 3 | `SWARM_BASE_URL` not configured | local engine |
| 4 | otherwise | try Swarm → on any failure, fall back to local |

Two details that matter in practice:

* **The request never waits on a network probe.** Connectivity is one background
  task; submitting a job reads the last known state.
* **Hysteresis.** 2 consecutive probe failures are needed to go offline, so a
  single dropped packet does not flip the UI. Tunable via env vars.

The alert pipeline degrades per stage rather than failing: only the
`communication` stage may call Swarm (`ALERT_CLOUD_STAGES`), and if the call
times out, errors or returns an unexpected shape, the local agent writes that
hop and a warning is attached to the result. Classification and safety are
always local.

---

## 5. Run the drill

With the backend running:

```bash
python3 scripts/offline_drill.py --base-url http://127.0.0.1:8000
```

It fetches one currently active alert from api.weather.gov, submits it online,
forces offline, submits it again, restores, and reconciles. It exits non-zero if
any guarantee broke: offline processing, local engine reported, agent handoff
chain intact, severity not below NWS, bilingual safety advice, no job lost.

Tests:

```bash
python3 -m pytest -q        # 91 passed
```

---

## 6. Repo layout

```
app/
  main.py              FastAPI factory, lifespan, CORS
  context.py           owns every long-lived service
  config.py            env-driven settings (no network at import time)
  api/routes.py        the HTTP contract above
  domain/alert.py      CAP 1.2 normalisation → NormalizedAlert
  knowledge/
    hazards.py         hazard taxonomy, severity rules, EN/ES playbooks
  agents/
    weather.py         classification + escalation + confidence
    safety.py          severity tier → bilingual safety plan
    communication.py   SMS/push/bulletin/voice, GSM-7 segment math
    cloud.py           optional Swarm drafting for the communication stage
    pipeline.py        weather -> safety -> communication, with trace
  i18n/terms.py        curated EN/ES phrasebook + coverage signal
  core/
    db.py              SQLite job store (WAL, survives restarts)
    connectivity.py    background probe loop + hysteresis + forced-offline switch
    router.py          Swarm vs local decision + circuit breaker
    worker.py          bounded worker pool draining the queue
    sync.py            pushes offline results upstream on reconnect
    processors/
      base.py          processor contract + text extraction
      local.py         offline engine (summarize/classify/extract + Ollama)
      swarm.py         Swarm client (timeouts, retries, response normalisation)
scripts/
  offline_drill.py     repeatable online/offline proof
tests/                 91 tests: store, connectivity, router, processors,
                       agents, translation, API
docs/                  frontend contract, Swarm agent prompts, team split
```

---

## 7. What is still an assumption

1. **Swarm request/response shape** — defaults to an OpenAI-compatible
   `POST /v1/chat/completions` and parses `choices[0].message.content`,
   `output_text`, or falls back to returning the raw JSON. The communication
   stage expects `{title, body, sms, push, voice}` in EN and ES and rejects any
   other shape (falling back to local). Change `SWARM_ENDPOINT` /
   `SwarmProcessor._build_body` if the real API differs.
2. **Sync endpoint** — I invented `POST /v1/sync/ingest` with a record of
   `{job_id, kind, mode, created_at, finished_at, result}`. If the real project
   does not need upstream reconciliation, leave `SWARM_SYNC_ENDPOINT` empty.
3. **Translation quality** — hazard names, severity words, safety playbooks and
   the common alert phrases are curated. Free-text description translation is
   phrasebook-based and reports its coverage; it is not a general translator.
   A missing phrase degrades to English with a visible signal, never to
   invented Spanish.
4. **Deployment target** — currently assumes the backend runs on the same device
   as the app (localhost). If it is a shared server, the offline path is on the
   wrong side of the network and we should move the queue/engine into the client
   or a desktop wrapper.

---

## 8. Walkthrough (live data)

1. `GET /v1/health` → `connectivity.probes` shows whether api.weather.gov is
   reachable; `swarm.last_call` shows the last Swarms call (ok/failed, ms).
2. On the dashboard tap **Find live alert**: it picks an active NWS alert,
   sets the NOAA point inside its area and polls. The card shows the NWS alert
   ID with a link to api.weather.gov.
3. Point at the card's "Written by" line: Swarms AI for the message text when
   online, local rules for classification and safety.
4. `POST /v1/demo/offline {"enabled": true}` → health flips to `offline`; new
   alerts are written by local rules only.
5. `POST /v1/demo/offline {"enabled": false}` → `POST /v1/sync/run`.
6. Close with: *"Classification and safety advice are deterministic and
   identical in both modes; the network only changes who writes the prose. And
   the queue is SQLite, so a restart does not lose the warning."*
