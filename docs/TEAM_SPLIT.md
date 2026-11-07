# Team split and integration contract

Mapped to the four roles in this weekend's WhatsApp thread. The backend half is
built; the other three columns are what the rest of the team builds against it.

## 1. Who owns what

| # | Role | Owns | Done when |
|---|---|---|---|
| 1 | **AI / Swarm engineer** | Swarm project, Weather/Safety/Communication agent prompts, model choice, keys | one real Swarm call returns JSON that passes `docs/SWARM_AGENTS.md` §2 and the backend accepts it |
| 2 | **Backend / offline (this repo)** | `app/`, queue, router, sync, alert pipeline, tests | `pytest` green, `offline_drill.py` all checks pass, queue survives a restart |
| 3 | **Frontend** | dashboard: alert list, severity, safety steps, EN/ES switch, online/offline badge | renders `POST /v1/alerts` output per `docs/FRONTEND_CONTRACT.md`, no dead spinner offline |
| 4 | **Integration / testing** | live-alert scenarios, Wi-Fi-off rehearsal, presentation run-through | drill passes twice in a row on the presentation laptop with Wi-Fi actually off |

Status right now: **#2 done**. #1 and #3 have a written contract and the live
Swarms API to build against; #4 has `scripts/offline_drill.py`, which drives a
live NWS alert through the pipeline.

## 2. Frozen contract

**Ports:** backend `8000`. Swarm is the hosted API at `https://api.swarms.world`.
**Base URL for the frontend:** `http://localhost:8000`.

The frontend consumes only these:

```
POST /v1/alerts         -> { id, status, engine, sync_state, result }
GET  /v1/jobs/{id}      -> same shape
GET  /v1/alerts/active  -> { count, alerts: [job, …] }
GET  /v1/noaa           -> NOAA point, last poll, observation
GET  /v1/hazards        -> legend: severity_levels, hazards, products, languages
GET  /v1/health         -> { mode, connectivity, queue, swarm }
```

`result` is described field by field in `docs/FRONTEND_CONTRACT.md` §3. Change
nothing in those shapes without telling the other side.

**Rules the UI should follow:**

1. Render `result.safety` — it is curated text in both languages, safe to show
   verbatim. Do not translate it in the browser.
2. Show "offline — saved on device" whenever `sync_state == "pending"`.
3. Show `result.pipeline_engine` next to the alert (`local` = nothing left the
   device). Use the job-level `engine` only for routing info.
4. When `classification.escalated` or `conflicts` is non-empty, show it. That is
   the safety-first rule made visible.
5. Poll `/v1/jobs/{id}` no faster than every 500 ms, or use `?wait=5`.

## 3. Boundaries that will bite if crossed

* The backend is assumed to run **on the same device** as the app. On a shared
  server the "offline" path sits on the far side of the network and the offline
  story collapses. Decide this before the UI hardcodes a URL.
* The Safety agent is deliberately **not** delegable to Swarm. If the model owns
  the life-safety text, the offline claim is false and the fallback text is
  different from the online text. Prompt drafts already exist in
  `docs/SWARM_AGENTS.md` §3 for comparison, not for production routing.
* SQLite is the source of truth. Do not keep alert state in browser memory.
* If the real Swarm response differs, change `app/core/processors/swarm.py` and
  the `_valid()` check in `app/agents/cloud.py` only; the rest of the system
  depends on the normalised channel shape.

## 4. Presentation plan (who speaks)

| Minute | On screen | Who |
|---|---|---|
| 0:00–0:30 | problem: warnings die with the network | #4 or #5 |
| 0:30–1:15 | Find live alert (real NWS alert), show trace + EN/ES SMS | #3 drives, #2 narrates |
| 1:15–1:50 | pull Wi-Fi, re-submit, identical classification, `pipeline_engine: local` | #4 pulls the plug |
| 1:50–2:20 | queue + sync catches up when back online | #2 |
| 2:20–2:45 | Swarm agent chain (model drafts copy, safety stays local) | #1 |
| 2:45–3:00 | close: deterministic safety, graceful degradation | #5 |

Rehearse it once with Wi-Fi actually off — not just the `POST /v1/demo/offline`
switch. The switch is for the stage; the real test is the radio.
