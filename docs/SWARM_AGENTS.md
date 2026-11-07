# Swarm integration — what the AI/Swarm engineer needs to build

This document is the contract between the Python backend (this repo) and the
Swarm API side. It exists so the two halves can be built in parallel and joined
on Saturday night without a rewrite.

## 1. Current split (already implemented)

| Stage | Where it runs today | Can be delegated to Swarm? |
| --- | --- | --- |
| `weather` — hazard + severity classification | local, deterministic | not wired; see §4 |
| `safety` — playbook, bilingual instructions | local, curated text | not wired, by design |
| `communication` — SMS / push / bulletin / voice | local, deterministic | **yes**, `ALERT_CLOUD_STAGES` |

Set `SWARM_BASE_URL`, `SWARM_API_KEY`, and `ALERT_CLOUD_STAGES=communication`.
When the link is up and Swarm answers with valid JSON, the communication channel
is composed by the model; if the call fails, times out, or returns anything
malformed, the pipeline silently falls back to the local composer and records
`engine: "local"` in the result. Nothing about the alert is lost.

That fallback is the point of the project: **the model is an enhancement, never
a dependency.**

## 2. What the backend sends you (communication stage)

`POST {SWARM_BASE_URL}/v1/chat/completions` with `model`, `messages`,
`response_format: {"type": "json_object"}`, `temperature: 0.2`.

System prompt already in the backend (keep it, or improve it in
`app/core/processors/swarm.py`):

```
You are the Communication agent of an emergency alerting system. You receive a
classified weather alert and a verified safety plan. Rewrite the delivery
channels so they are clear and calm. Do not invent facts, do not change the
severity, and do not add actions that are not in the safety plan.
Return ONLY a JSON object with this exact shape:
{"title": {"en": str, "es": str},
 "sms": {"en": {"text": str}, "es": {"text": str}},
 "push": {"en": {"title": str, "body": str}, "es": {"title": str, "body": str}},
 "bulletin": {"en": str, "es": str},
 "voice": {"en": str, "es": str}}.
Spanish must be natural Spanish, not word-for-word English.
SMS stays under 160 characters.
```

User message (JSON):

```json
{
  "alert": { "event": "...", "severity": "extreme", "area_text": "...",
             "headline": "...", "description": "...", "instruction": "..." },
  "classification": {"hazard": "tornado", "severity": "extreme", "product": "warning"},
  "safety": {"immediate": ["Move to a basement..."], "avoid": ["Do not wait..."]}
}
```

## 3. Agent prompts for the three Swarm agents

Use these as the system prompts of your Weather, Safety, and Communication
agents. Each is written to match the JSON the backend already produces, so a
model answer can be diffed against the local answer for the same alert.

### Weather agent

```
You classify emergency weather alerts. You never invent hazards and you never
lower a stated severity.

Rules:
1. hazard is one of: tornado, hurricane, flood, thunderstorm, winter, heat,
   wildfire, wind, fog, air_quality, earthquake, tsunami, other.
2. severity is one of: unknown, minor, moderate, severe, extreme.
3. A "Warning" product is at least moderate, an "Emergency" at least severe.
   Text evidence may only raise severity, never lower it.
4. If the feed states no severity, return "unknown" and say so in signals.

Return ONLY JSON:
{"hazard": str, "severity": str, "product": str|null,
 "confidence": 0..1, "signals": [str], "rationale": str}
```

### Safety agent

```
You turn a classified alert into three short action lists. You never give
advice that contradicts official guidance and never tell someone to evacuate
unless the alert says so.

Rules:
1. immediate: what to do in the next 60 seconds.
2. avoid: what not to do.
3. prepare: what to have ready.
4. Each item must be a single imperative sentence, at most 18 words, in both
   English and Spanish, authored separately — not machine-translated.

Return ONLY JSON:
{"immediate": [{"en": str, "es": str}],
 "avoid": [{"en": str, "es": str}],
 "prepare": [{"en": str, "es": str}],
 "evacuation_recommended": bool, "shelter_recommended": bool}
```

### Communication agent

The prompt in §2. Constraint that matters: the Spanish must read like Spanish.
A judge will compare the two SMS messages side by side.

## 4. Wiring the Weather / Safety agents into the pipeline

The pipeline is deliberately synchronous and deterministic for these two stages,
so the offline story stays true. If you want Swarm in the loop for them, the
supported path is:

1. Build the agents in Swarm so they return the JSON in §3.
2. Run them **offline-of-record**: call Swarm for currently active NWS alerts
   (`https://api.weather.gov/alerts/active?status=actual`) and for the test
   fixtures in `tests/fixtures/samples.py`, store the responses, and compare
   them to the local `classification`/`safety` (and each fixture's `expected`).
3. Where they disagree, the local answer wins unless the model found evidence
   the rules missed — that is a rule change in `app/knowledge/hazards.py`, not a
   new code path.

This keeps one source of truth for safety text and still supports the claim
"three agents, chained, with a deterministic fallback".

## 5. Contract test you can run against your agents

Pull real active alerts and run each through the backend:

```bash
curl -s -H 'User-Agent: ResQ-weather-safety/0.1' \
  'https://api.weather.gov/alerts/active?status=actual&message_type=alert' \
  | jq -c '.features[:5][] | .properties | {identifier: .id, event, severity, urgency, certainty, areaDesc, headline, description, instruction}'
```

Then post each of those alerts to `POST /v1/alerts` and to your Swarm agent, and diff:

| Field | Must match exactly | Allowed to differ |
| --- | --- | --- |
| `hazard` | yes | — |
| `severity` | yes | — |
| `product` | yes | — |
| `evacuation_recommended` | yes | — |
| `shelter_recommended` | yes | — |
| wording of instructions | — | yes, as long as no new action appears |
| SMS length | under 160 chars | — |

The test fixtures include two entries intentionally mislabelled by the feed
(`heat_understated`, `thunderstorm_minor` in `tests/fixtures/samples.py`). A
correct agent must raise both and explain why; an agent that agrees with the feed
is wrong.

## 6. Failure modes already handled on our side

* Swarm returns prose instead of JSON → `ProcessorUnavailable`, local fallback.
* Swarm times out (20 s default, 2 retries) → circuit breaker opens after 3
  failures, local engine serves every request until it closes.
* Link drops mid-pipeline → job still completes locally and is marked
  `sync_state: pending`; the offline record is pushed to
  `SWARM_SYNC_ENDPOINT` when the link returns.
* Swarm returns a valid JSON object with missing keys → shape validation
  rejects it and the local composer wins.
