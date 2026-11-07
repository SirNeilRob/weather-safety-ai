# Frontend contract — SharkByte alert API

Everything below is served by the Python backend on `http://localhost:8000`
(`uvicorn app.main:app --port 8000`). All bodies are JSON, all timestamps are
ISO-8601 strings, and every endpoint answers while the machine is offline.
CORS is open (`*`) during the hackathon.

Rule of thumb for the dashboard: **the backend never returns an alert without
advice.** If a field is missing or the feed is malformed, you still get a
severity, a colour, and at least one instruction in both languages.

---

## 1. Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/health` | Online/offline state, engine in use, queue counts |
| `POST` | `/v1/alerts` | Submit one raw alert, get classification + safety + messages |
| `GET` | `/v1/alerts/active` | Processed alerts to display, newest first (NOAA ones only while NWS still lists them) |
| `GET` | `/v1/hazards` | Hazard legend, severity levels + colours, languages |
| `GET` | `/v1/jobs/{id}` | Fetch a job later (same shape as the POST response) |
| `GET` | `/v1/queue/summary` | Durable counts, including `pending_sync` |
| `GET` | `/v1/jobs?limit=10` | Most recent jobs |
| `GET` | `/v1/noaa` | NOAA point (`null` until a location is set), `last_poll_at`, `last_error`, `interval_seconds`, `observation` |
| `POST` | `/v1/noaa/location` | `{latitude, longitude, label?}` sets the NOAA point |
| `POST` | `/v1/noaa/poll` | Poll NOAA now → `{status: ok|skipped|error, …}` |
| `POST` | `/v1/demo/offline` | `{"enabled": true}` forces offline (endpoint name kept for compatibility) |
| `POST` | `/v1/sync/run` | Trigger a reconciliation pass by hand |
| `GET` | `/healthz` | Liveness only |

### POST /v1/alerts

```json
{
  "alert": { "...": "raw CAP 1.2 or flat JSON, see below" },
  "languages": ["en", "es"],
  "audience": "public",
  "wait_seconds": 5
}
```

`wait_seconds` is how long the request blocks waiting for the pipeline (a few
milliseconds locally). The default is 25s, which is enough for one Swarm round
trip; send `0` to get a job `id` and `status: "pending"` back immediately, then
poll `GET /v1/jobs/{id}`.

Response `201`:

```json
{
  "id": "7f3c…",
  "kind": "alert",
  "status": "done",
  "engine": "local",
  "sync_state": "pending",
  "result": { "…": "see section 3" }
}
```

`422` means the alert body had no usable content — show a form error, not a
crash screen.

---

## 2. Input the backend accepts

Any of these shapes works; unknown fields are ignored.

```json
{
  "identifier": "urn:oid:… (the NWS alert id)",
  "senderName": "…",
  "sent": "…",
  "event": "…",
  "severity": "Unknown | Minor | Moderate | Severe | Extreme",
  "urgency": "…",
  "certainty": "…",
  "onset": "…", "expires": "…",
  "areaDesc": "…",
  "headline": "…",
  "description": "…",
  "instruction": "…",
  "language": "en"
}
```

CAP feeds work too: put the same fields inside `info: [{…}]`. `areaDesc` may be
a string, a list of strings, or `[{ "areaDesc": "…" }]`. `severity` accepts
`Unknown | Minor | Moderate | Severe | Extreme`, any casing.

---

## 3. Response shape

```jsonc
{
  "alert": { "id", "title", "headline", "description", "instruction",
             "severity", "urgency", "certainty", "areas", "area_text",
             "onset", "expires", "sent", "source", "language", "categories" },

  "classification": {
    "hazard": "tornado",                 // key for icons/CSS
    "hazard_label": {"en": "Tornado", "es": "Tornado"},
    "hazard_icon": "🌪",
    "severity": "extreme",               // unknown|minor|moderate|severe|extreme
    "severity_rank": 4,                  // 0–4, use for sorting/colour steps
    "severity_assumed": false,           // true = the feed never said
    "escalated": false,                  // true = we raised the feed's value
    "product": "warning",                // emergency|warning|watch|advisory|statement|alert|null
    "urgency": "immediate",
    "certainty": "observed",
    "confidence": 0.85,                  // 0–1, show as a bar
    "signals": ["event is a warning", "…"],   // why — great for a details panel
    "conflicts": ["feed declared 'minor' but the body indicates 'extreme'…"],
    "rationale": "Classified as tornado (Tornado). severity extreme…",
    "scores": {"tornado": 12.0}
  },

  "safety": {
    "tier": "extreme",
    "tier_label": {"en": "Extreme", "es": "Extrema"},
    "advice": {"en": "Act immediately; this is a threat to life.",
               "es": "Actúe de inmediato; esto es una amenaza para la vida."},
    "color": "#6b0f0f",
    "immediate": [{"en": "Move to a basement…", "es": "Vaya al sótano…",
                   "phase": "immediate", "min_severity": "minor"}],
    "avoid":   [{"en": "…", "es": "…", "phase": "avoid", "min_severity": "minor"}],
    "prepare": [{"en": "…", "es": "…", "phase": "prepare", "min_severity": "minor"}],
    "evacuation_recommended": false,     // show a banner only when true
    "shelter_recommended": true,
    "first_action": {"en": "Move to a basement…", "es": "Vaya al sótano…"},
    "first_action_source": "playbook",   // always curated, never machine output
    "official_instruction": {
      "text": {"en": "Take shelter now…", "es": "Busque refugio ahora…"},
      "method": {"en": "none", "es": "phrasebook"},
      "coverage": {"en": 1.0, "es": 1.0},
      "unknown_terms": {"en": [], "es": []}
    },
    "official_reliable": {"en": true, "es": true},
    "counts": {"immediate": 3, "avoid": 2, "prepare": 1}
  },

  "communication": {
    "title": {"en": "Tornado Warning", "es": "Aviso de tornado"},
    "severity": {"level": "extreme", "color": "#6b0f0f",
                 "label": {"en": "Extreme", "es": "Extrema"}},
    "sms":   {"en": {"text": "…", "chars": 145, "encoding": "gsm7", "segments": 1},
              "es": {"text": "…", "chars": 146, "encoding": "ucs2", "segments": 3}},
    "push":  {"en": {"title": "…", "body": "…"}, "es": {"…": "…"}},
    "bulletin": {"en": "…", "es": "…"},
    "voice": {"en": "…", "es": "…"},
    "translation": {"es": {"method": "untranslated", "coverage": 0.36,
                        "unknown_terms": ["cedar", "rapids"]}}
  },

  "trace": [
    {"agent": "weather", "stage": "classify", "engine": "local", "ms": 0.4,
     "handoff": "hazard=tornado severity=extreme product=warning", "detail": {}},
    {"agent": "safety", "stage": "plan", "engine": "local", "handoff": "…"},
    {"agent": "communication", "stage": "compose", "engine": "local", "handoff": "…"}
  ],

  "warnings": ["no area given; using 'the affected area'"],
  "languages": ["en", "es"],
  "engine": "local",
  "offline": true,
  "pipeline_engine": "local"
}
```

### Rendering rules that matter

1. **Severity colour** — take `safety.color` or `severity_levels[].color` from
   `/v1/hazards`. Never hand-pick colours in the frontend; the backend already
   encodes the ramp (`#4b5563 → #ca8a04 → #d97706 → #b91c1c → #6b0f0f`).
2. **Actionable text** — render `safety.first_action[lng]` and the
   `immediate/avoid/prepare` lists. They are hand-written in both languages and
   are safe to show verbatim.
3. **Official wording** — if you show `official_instruction.text[lng]`, show a
   badge when `official_reliable[lng]` is false ("automatic translation, 82%
   coverage"). That honesty is a judging point, not a defect to hide.
4. **Escalation** — when `classification.escalated` or `conflicts` is non-empty,
   show the conflict line. It is the clearest proof of "we refuse to downgrade".
5. **Mode banner** — `GET /v1/health` → `mode` is `"online"` or `"offline"`.
   Also show `engine` from the job (`local` vs `swarm`) next to each alert; the
   two can differ per alert when the link drops mid-run.
6. **Unit** — `sms.*.segments` is the carrier cost. Spanish needs 3 segments
   where English needs 1 (UCS-2 vs GSM-7), which is a strong story to tell.
7. **Quoted source text** — `communication.translation[lng]` is empty when no
   free text needed translating. When it is present, `method: "untranslated"`
   (or a `coverage` below 0.85) means the agency description is quoted in its
   original language and the bulletin says so. Render it as a quote, do not
   present it as a translation, and do not “fix” it in the frontend: the
   curated titles, SMS and safety actions are always fully bilingual.
8. **Engine labels** — `job.engine` is `local` or `swarm` (which route served
   the job). `result.pipeline_engine` is `local`, `swarm` or `mixed` (which
   stages answered) and `result.engine` names the local processor
   (`local:builtin`). Show `pipeline_engine` for “was this produced offline”,
   and the job-level `engine` for routing.

---

## 4. Fetch example

```js
async function runAlert(rawAlert) {
  const res = await fetch("http://localhost:8000/v1/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ alert: rawAlert, languages: ["en", "es"],
                           wait_seconds: 5 }),
  });
  if (res.status === 422) throw new Error("Alert has no usable content");
  const job = await res.json();
  return job.result;             // classification / safety / communication
}

async function health() {
  return (await fetch("http://localhost:8000/v1/health")).json();
}

async function legend() {
  return (await fetch("http://localhost:8000/v1/hazards")).json();
}
```

`/v1/hazards` returns
`{ "severity_levels": [{key,en,es,color,rank,advice:{en,es}}], "hazards": [{key,en,es,icon}], "products": [{key,en,es}], "languages": [{code,label}], "channels": ["sms",…] }`
— enough to build the whole legend and language switch without hardcoding.

---

## 5. Live loop for the dashboard

1. Statuses, each from its own field: **Internet** = the
   `connectivity.probes` entry for api.weather.gov in `GET /v1/health`;
   **Swarms** = `swarm.last_call` (`ok`, `ms`, `at`, `error`); **NOAA** =
   `GET /v1/noaa` `last_poll_at` / `last_error`.
2. If `GET /v1/noaa` → `point` is `null`, show "Set location" and no weather.
   Location comes from browser GPS, "Find live alert", or `NOAA_LAT`/`NOAA_LON`.
3. "Find live alert": read `https://api.weather.gov/alerts/active?status=actual`,
   pick one active alert, `POST /v1/noaa/location` with a point inside its area,
   then `POST /v1/noaa/poll`. Show the NWS alert id and link to
   `https://api.weather.gov/alerts/{id}`.
4. Render `GET /v1/alerts/active`: severity pill, the three action groups,
   `first_action`, and who wrote it (`result.pipeline_engine` plus each
   `trace[].engine`).
5. Any value missing from a response is shown as "—", never a default.
