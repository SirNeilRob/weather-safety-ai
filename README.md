# SharkByte — offline-first weather alert backend (Python)

A backend that keeps issuing weather warnings when the internet does not.

* **Online** → the Swarm API drafts the public copy (the `communication` stage).
* **Offline** → a deterministic local pipeline classifies the hazard, picks the
  safety playbook and writes the EN/ES messages itself, in milliseconds.
* **Back online** → locally-produced alerts are reconciled upstream (if a sync
  endpoint is configured) and the backlog is reported honestly in `/v1/health`.

The safety advice is **never** produced by a language model, online or offline:
the deterministic Safety agent owns it in both modes. That is the design
decision the whole demo rests on.

Status: **runs, tested, demo-verified** — 123 tests pass and the live
online→offline→sync drill passes 21/21 checks against the stand-in Swarm.

Companion docs:

| Doc | Audience |
|---|---|
| [`docs/FRONTEND_CONTRACT.md`](docs/FRONTEND_CONTRACT.md) | frontend engineer — every field the dashboard renders |
| [`docs/SWARM_AGENTS.md`](docs/SWARM_AGENTS.md) | AI/Swarm engineer — agent prompts + JSON contract |
| [`docs/TEAM_SPLIT.md`](docs/TEAM_SPLIT.md) | who owns what this weekend |

---

## 1. Quick start

```bash
git clone https://github.com/SirNeilRob/weather-safety-ai.git
cd weather-safety-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in SWARM_BASE_URL / SWARM_API_KEY if you have them
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open <http://127.0.0.1:8000/docs> for interactive API docs.

No Swarm key yet? Run the stand-in API in a second terminal:

```bash
uvicorn scripts.fake_swarm:app --port 8899
```

and set in `.env`:

```
SWARM_BASE_URL=http://127.0.0.1:8899
SWARM_SYNC_ENDPOINT=/v1/sync/ingest
PROBE_URLS=http://127.0.0.1:8899/v1/health
```

That gives you the **online path and the sync path** without internet or
credits. Everything below also works with `SWARM_BASE_URL` empty — that is just
"always offline", which is the state we demo in.

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

Sample output for a tornado warning while offline:

```json
{
  "engine": "local",
  "classification": { "hazard": "tornado", "severity": "extreme", "escalated": true },
  "trace": [
    {"agent": "weather", "engine": "local"},
    {"agent": "safety", "engine": "local"},
    {"agent": "communication", "engine": "local"}
  ],
  "communication": {
    "sms": {
      "en": {"text": "Tornado Warning. EXTREME. Cedar Rapids, …", "chars": 145, "segments": 1},
      "es": {"segments": 3}
    }
  }
}
```

`engine` is `local`, `swarm` or `mixed` — derived from what actually served the
stages, not from whether the device happened to be online.

### Demo fixtures

`GET /v1/alerts/samples` returns 10 CAP-shaped alerts, each with an `expected`
block (hazard, severity, whether escalation is required). Two of them are
deliberately mislabelled by the feed (`heat_understated`,
`thunderstorm_minor`) so the escalation rule is visible in the demo instead of
being a claim in a slide.

---

## 3. The sensor bridge (accelerometer to alert)

Part three of the ResQ chain. The microcontroller measures how hard and how
long the ground shook; `scripts/sensor_bridge.py` turns that pair into the same
CAP-shaped alert the pipeline already ingests. Nothing downstream is special-cased:
the weather agent classifies it, the safety agent attaches the earthquake
playbook, the communication agent writes the bilingual messages.

```
ESP32 --serial--> sensor_bridge.py --HTTP--> POST /v1/alerts --> weather -> safety -> communication
```

### Wire format

The firmware writes one line per detected event. Three formats are accepted, so
the firmware can change without touching Python:

```
PEAK=0.21 DUR=2.4          # g and seconds
PEAK=210mg DUR=2400ms      # units honoured either way
0.21,2.4                   # bare CSV, peak first
{"peak_g": 0.21, "duration_s": 2.4}
```

### Why 0.21 g becomes "Severe"

Peak ground acceleration is not a severity word. The bridge converts it with
the PGA-to-intensity relation of Worden et al. (2012),

```
MMI = 3.66 * log10(PGA[cm/s^2]) - 1.66
```

which reproduces the familiar Wald et al. (1999) shaking table: 0.05 g is
about MMI V, 0.1 g is VI, 0.2 g is VII, 0.4 g is VIII. The pipeline receives
the severity word derived from that intensity, so the sensor and the alert
vocabulary cannot drift apart.

### The two gates

A single MEMS sensor on a table is an earthquake detector *and* a box that can
be knocked over. Three rules keep the second case from paging the venue:

| Option | Default | Rejects |
|---|---|---|
| `--min-peak` | 0.03 g | background noise |
| `--min-duration` | 1.0 s | a knock: 2.4 g that lasts 40 ms |
| `--cooldown` | 120 s | the same earthquake twice; a stronger reading overrides |

If the API is unreachable, the reading is spooled to `data/bridge_spool.jsonl`
and replayed on the next successful contact, so a dropped link loses no event.

### Running it

```bash
# no hardware: a fake node on a pseudo-terminal, then the bridge against it
python3 scripts/sensor_sim.py --pty --scenario sequence      # prints /dev/pts/N
python3 scripts/sensor_bridge.py --port /dev/pts/N --once --dry-run

# replay a capture against a running backend
python3 scripts/sensor_bridge.py --port tests/data/shake.log --once

# real hardware
python3 scripts/sensor_bridge.py --port /dev/ttyACM0
```

A clean run prints one line per skipped reading with the reason, then the job
result:

```
  skipped: peak 0.01 g is below the 0.03 g sensor floor
  skipped: 0.04 s of shaking is under the 1.0 s gate; treated as a knock, not a quake
[23:15:30] peak 0.21 g / 2.4 s = MMI 7 -> severe
  classified hazard=earthquake severity=severe product=warning confidence=0.85
  sms en (1 seg, 129 chars): Earthquake Warning. SEVERE. ResQ node - Hackathon venue. Drop, cover, and hold on under sturdy furniture. - ResQ on-device sensor
  sms es (2 seg, 117 chars): Aviso de terremoto. SEVERA. ResQ node - Hackathon venue. Agáchese, cúbrase y agárrese debajo de un mueble resistente.
```

---

## 4. API contract (for the frontend)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/alerts` | submit a weather alert → job id, or finished result with `wait_seconds` |
| `GET` | `/v1/alerts/samples` | 10 demo alerts + expected classification |
| `GET` | `/v1/hazards` | severity levels, hazard legend, products, channels, languages |
| `GET` | `/v1/health` | link state, queue depth, sync backlog |
| `POST` | `/v1/jobs` | generic job submit (kept for the non-alert kinds) |
| `GET` | `/v1/jobs/{id}` | poll status / result (`?wait=5` long-polls) |
| `GET` | `/v1/jobs` | recent jobs (`?limit=`, `?status=`) |
| `GET` | `/v1/queue/summary` | queue + sync counters for the status badge |
| `GET` | `/v1/capabilities` | what the UI may show as available |
| `POST` | `/v1/demo/offline` | force offline on/off (stage switch) |
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

## 5. How it decides what to use

Every job goes through the same decision order (`app/core/router.py`):

| # | Condition | Result |
|---|---|---|
| 1 | request says `prefer_local`, or device is offline | local engine |
| 2 | Swarm circuit breaker is open (3 consecutive failures) | local engine |
| 3 | `SWARM_BASE_URL` not configured | local engine |
| 4 | otherwise | try Swarm → on any failure, fall back to local |

Two details that matter in a live demo:

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

## 6. Run the drill

With the backend (and optionally the stand-in Swarm) running:

```bash
python3 scripts/offline_drill.py --base-url http://127.0.0.1:8000
python3 scripts/offline_drill.py --base-url http://127.0.0.1:8000 \
        --swarm-url http://127.0.0.1:8899     # also checks the sync inbox
```

It walks online → forced offline → alert panel while offline → restored →
reconciled, and exits non-zero if any guarantee broke. Section *3b* is the
weather pipeline: samples published, tornado classified, severity escalated,
local engine, agent handoff chain, SMS segment math, Spanish translation,
bilingual safety. That is the 21/21 run.

Tests:

```bash
python3 -m pytest -q        # 123 passed
```

---

## 7. Repo layout

```
app/
  main.py              FastAPI factory, lifespan, CORS
  context.py           owns every long-lived service
  config.py            env-driven settings (no network at import time)
  api/routes.py        the HTTP contract above
  domain/alert.py      CAP 1.2 normalisation → NormalizedAlert
  knowledge/
    hazards.py         hazard taxonomy, severity rules, EN/ES playbooks
    samples.py         10 demo alerts with expected classifications
  agents/
    weather.py         classification + escalation + confidence
    safety.py          severity tier → bilingual safety plan
    communication.py   SMS/push/bulletin/voice, GSM-7 segment math
    cloud.py           optional Swarm drafting for the communication stage
    pipeline.py        weather -> safety -> communication, with trace
  i18n/terms.py        curated EN/ES phrasebook + coverage signal
  core/
    db.py              SQLite job store (WAL, survives restarts)
    connectivity.py    background probe loop + hysteresis + demo switch
    router.py          Swarm vs local decision + circuit breaker
    worker.py          bounded worker pool draining the queue
    sync.py            pushes offline results upstream on reconnect
    processors/
      base.py          processor contract + text extraction
      local.py         offline engine (summarize/classify/extract + Ollama)
      swarm.py         Swarm client (timeouts, retries, response normalisation)
scripts/
  fake_swarm.py        local stand-in for the Swarm API
  offline_drill.py     repeatable online/offline proof
  sensor_bridge.py     accelerometer (peak, duration) -> CAP alert -> /v1/alerts
  sensor_sim.py        fake node on stdout or a pty, for demos without hardware
tests/                 123 tests: store, connectivity, router, processors,
                       agents, translation, API, sensor bridge
docs/                  frontend contract, Swarm agent prompts, team split
```

---

## 8. What is still an assumption

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

## 9. Demo script (3 minutes)

1. `GET /v1/health` → `mode: online`, Swarm configured, `pending_sync: 0`.
2. `GET /v1/alerts/samples` → 10 alerts; point at `expected.escalated: true`
   on `heat_understated` (the feed calls it moderate; we do not).
3. `POST /v1/alerts` with `tornado_emergency` → `hazard: tornado`,
   `severity: extreme`, `engine: local`, 3-step trace with each handoff line.
4. Show `sms.en` (145 chars, 1 segment) next to `sms.es` (123 chars, 2
   segments) and the bilingual `safety.immediate` list. Point out that the
   Spanish costs a second segment because accented text leaves GSM-7, and that
   attribution is dropped before the instruction is ever cut.
5. `POST /v1/demo/offline {"enabled": true}` → health flips to `offline`.
6. Re-submit the same alert → identical classification, `offline: true`,
   `sync_state: pending`, `queue.pending_sync: 1`.
7. `POST /v1/demo/offline {"enabled": false}` → `POST /v1/sync/run` →
   `{"status":"ok","sent":1}`, then `GET /v1/sync/inbox` on the stand-in.
8. Close with: *"Classification and safety advice are deterministic and
   identical in both modes; the network only changes who writes the prose. And
   the queue is SQLite, so a restart does not lose the warning."*
