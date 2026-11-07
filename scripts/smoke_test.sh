#!/usr/bin/env bash
# Smoke test against an ALREADY RUNNING backend. It never starts or stops a server.
# Usage: scripts/smoke_test.sh            (backend on http://127.0.0.1:8000)
#        SMOKE_BASE=http://host:port scripts/smoke_test.sh
set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAILS=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1 -- $2"; FAILS=$((FAILS+1)); }

# .env supplies the Swarms key for the direct Swarms check.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
BASE=${SMOKE_BASE:-http://127.0.0.1:8000}

# 1. Backend is up
code=$(curl -sS -m 5 -o "$TMP/health.json" -w '%{http_code}' "$BASE/v1/health" 2>/dev/null)
if [ "$code" = 200 ]; then
  pass "backend up at $BASE ($($PY -c "import json,sys; d=json.load(open(sys.argv[1])); print('mode='+str(d.get('mode'))+' forced='+str(d['connectivity'].get('forced')))" "$TMP/health.json"))"
else
  fail "backend up at $BASE" "GET /v1/health -> HTTP ${code:-none}. Start the backend first."
  echo "RESULT: $FAILS failed"; exit 1
fi

# 2. GET /v1/noaa returns JSON
code=$(curl -sS -m 10 -o "$TMP/noaa.json" -w '%{http_code}' "$BASE/v1/noaa")
if [ "$code" = 200 ] && $PY -c "import json,sys; json.load(open(sys.argv[1]))" "$TMP/noaa.json" 2>/dev/null
then pass "GET /v1/noaa returns JSON ($($PY -c "import json,sys; d=json.load(open(sys.argv[1])); print('point='+str(d.get('point'))+' last_poll_at='+str(d.get('last_poll_at'))+' last_error='+str(d.get('last_error')))" "$TMP/noaa.json"))"
else fail "GET /v1/noaa returns JSON" "HTTP $code: $(head -c 200 "$TMP/noaa.json")"
fi

# 3. One Swarms call returns text (direct, not through the backend)
if [ -z "${SWARMS_API_KEY:-}" ]; then
  fail "Swarms call returns text" "SWARMS_API_KEY not set"
else
  code=$(curl -sS -m 90 -o "$TMP/swarm.json" -w '%{http_code}' \
    -H "x-api-key: $SWARMS_API_KEY" -H 'Content-Type: application/json' \
    -X POST "${SWARM_BASE_URL:-https://api.swarms.world}${SWARM_ENDPOINT:-/v1/agent/completions}" \
    -d '{"agent_config":{"agent_name":"smoke","model_name":"'"${SWARM_MODEL:-gpt-4o-mini}"'","max_loops":1,"max_tokens":20},"task":"Reply with the single word OK."}')
  text=$($PY - "$TMP/swarm.json" <<'EOF' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
out = d.get("outputs")
if isinstance(out, list):
    out = " ".join(str(o.get("content", "")) if isinstance(o, dict) else str(o) for o in out)
print(str(out or "").strip()[:80])
EOF
)
  if [ "$code" = 200 ] && [ -n "$text" ]; then pass "Swarms call returns text (\"$text\")"
  else fail "Swarms call returns text" "HTTP $code: $(head -c 200 "$TMP/swarm.json")"
  fi
fi

# 4. Dashboard returns HTTP 200
code=$(curl -sS -m 10 -o "$TMP/index.html" -w '%{http_code}' "$BASE/")
if [ "$code" = 200 ] && grep -q '<html' "$TMP/index.html"; then pass "dashboard GET / returns 200 HTML"
else fail "dashboard GET / returns 200" "HTTP $code"
fi

# 5. A real, currently active NWS alert through the full pipeline
code=$(curl -sS -m 20 -o "$TMP/nws.json" -w '%{http_code}' \
  -H "User-Agent: ${NOAA_USER_AGENT:-ResQ-weather-safety/0.1}" -H 'Accept: application/geo+json' \
  'https://api.weather.gov/alerts/active?status=actual&message_type=alert')
if [ "$code" != 200 ]; then
  fail "real NWS alert through full pipeline" "api.weather.gov -> HTTP $code"
else
  $PY - "$TMP/nws.json" > "$TMP/alert_req.json" <<'EOF'
import json, sys
sys.path.insert(0, ".")
from app.core.noaa import to_pipeline_alert
feats = [f["properties"] for f in json.load(open(sys.argv[1]))["features"] if f.get("properties", {}).get("id")]
print(json.dumps({"alert": to_pipeline_alert(feats[0]), "languages": ["en", "es"], "wait_seconds": 45}))
EOF
  code=$(curl -sS -m 60 -o "$TMP/alert.json" -w '%{http_code}' -X POST "$BASE/v1/alerts" \
    -H 'Content-Type: application/json' --data @"$TMP/alert_req.json")
  check=$($PY - "$TMP/alert.json" <<'EOF' 2>&1
import json, sys
j = json.load(open(sys.argv[1]))
r = j.get("result") or {}
agents = [t.get("agent") for t in r.get("trace", [])]
missing = [k for k in ("classification", "safety", "communication") if k not in r]
ok = j.get("status") == "done" and not missing and agents[:3] == ["weather", "safety", "communication"]
print(("OK " if ok else "BAD ") + f"nws_id={(r.get('alert') or {}).get('id')} status={j.get('status')} "
      f"job_engine={j.get('engine')} pipeline_engine={r.get('pipeline_engine')} "
      f"hazard={r.get('classification',{}).get('hazard')} severity={r.get('classification',{}).get('severity')} "
      f"trace={agents} missing={missing} error={j.get('error')}")
EOF
)
  if [ "$code" = 201 ] && [ "${check#OK }" != "$check" ]; then pass "real NWS alert through full pipeline (${check#OK })"
  else fail "real NWS alert through full pipeline" "HTTP $code: $check"
  fi
fi

# 6. Watch list: add a Florida ZIP, see it in GET /v1/watchlist, remove it.
#    Uses a ZIP not already on the list so it never removes one of yours.
curl -sS -m 10 -o "$TMP/wl0.json" "$BASE/v1/watchlist"
ZIP=$($PY -c "import json,sys; have={c.get('zip') for c in json.load(open(sys.argv[1]))['cities']}; print(next(z for z in ('32801','33602','32301','33901','32202','33130') if z not in have))" "$TMP/wl0.json" 2>/dev/null)
add=$(curl -sS -m 20 -o "$TMP/wl1.json" -w '%{http_code}' -X POST "$BASE/v1/watchlist" -H 'Content-Type: application/json' -d "{\"zip\":\"$ZIP\"}")
if [ "$add" != 201 ]; then
  fail "watch list add/list/remove" "POST /v1/watchlist {zip:$ZIP} -> HTTP $add: $(head -c 200 "$TMP/wl1.json")"
else
  curl -sS -m 10 -o "$TMP/wl2.json" "$BASE/v1/watchlist"
  listed=$($PY -c "import json,sys; print(any(c.get('zip')==sys.argv[2] for c in json.load(open(sys.argv[1]))['cities']))" "$TMP/wl2.json" "$ZIP")
  name=$($PY -c "import json,sys; print(json.load(open(sys.argv[1]))['city'].get('name'))" "$TMP/wl1.json")
  del=$(curl -sS -m 10 -o "$TMP/wl3.json" -w '%{http_code}' -X DELETE "$BASE/v1/watchlist/$ZIP")
  gone=$($PY -c "import json,sys; print(not any(c.get('zip')==sys.argv[2] for c in json.load(open(sys.argv[1]))['cities']))" "$TMP/wl3.json" "$ZIP")
  if [ "$listed" = True ] && [ "$del" = 200 ] && [ "$gone" = True ]; then
    pass "watch list add/list/remove (zip=$ZIP name=$name)"
  else
    fail "watch list add/list/remove" "zip=$ZIP listed=$listed delete=HTTP $del gone=$gone"
  fi
fi

echo "RESULT: $FAILS failed"
exit $FAILS
