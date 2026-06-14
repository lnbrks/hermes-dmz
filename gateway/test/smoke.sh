#!/usr/bin/env bash
# Standalone gateway smoke test — no Hermes, no real Google credentials needed.
#
# Builds the gateway image, runs it, and checks the three policy properties
# that DON'T need a real token:
#   1. A non-allowlisted host is denied (403).
#   2. The OAuth /token endpoint is stubbed (200 + dummy access_token).
#   3. A mutating Gmail call with no token configured fails closed (503),
#      not open.
#
# For the full path (real reads/labels) you need a real token file — see
# host/refresh-token and the README.
#
# Usage:  gateway/test/smoke.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
IMG="inbox-gateway:smoke"
NAME="inbox-gateway-smoke"
PORT="${PORT:-18080}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> building $IMG"
docker build -t "$IMG" "$HERE"

echo "==> starting gateway on :$PORT (no token file → mutations must fail closed)"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" "$IMG" >/dev/null

# Wait for the proxy to accept connections.
for _ in $(seq 1 30); do
  if curl -s -x "http://127.0.0.1:$PORT" -o /dev/null http://example.com 2>/dev/null; then break; fi
  sleep 0.5
done

# Extract the CA the proxy generated so curl can trust the MITM'd TLS.
CA="$(mktemp)"
for _ in $(seq 1 20); do
  if docker exec "$NAME" cat /home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem > "$CA" 2>/dev/null \
     && [ -s "$CA" ]; then break; fi
  sleep 0.5
done

px=(-x "http://127.0.0.1:$PORT" --cacert "$CA" -s -o /dev/null -w "%{http_code}")
fail=0

echo -n "1. deny non-allowlisted host (example.com) → expect 403 ... "
code="$(curl "${px[@]}" https://example.com/ || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "2. stub OAuth token endpoint → expect 200 + dummy token ... "
body="$(curl -x "http://127.0.0.1:$PORT" --cacert "$CA" -s \
        -X POST https://oauth2.googleapis.com/token || true)"
echo "$body" | grep -q "dummy-sandbox-token" && echo "OK" || { echo "FAIL ($body)"; fail=1; }

# Shared-secret dummy credentials the sandbox is configured to present.
GTOK="dummy-sandbox-token"
ORK="dummy-openrouter-key"
TGTOK="111111:DUMMYtelegramTOKEN0000000000000000"

echo -n "3. Gmail read (valid dummy), no upstream token → expect 503 (fail closed) ... "
code="$(curl "${px[@]}" -H "Authorization: Bearer $GTOK" \
        https://gmail.googleapis.com/gmail/v1/users/me/labels || true)"
[ "$code" = "503" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "4. Gmail batchModify of a system label → expect 403 (policy) ... "
code="$(curl -x "http://127.0.0.1:$PORT" --cacert "$CA" -s -o /dev/null -w "%{http_code}" \
        -X POST -H "Authorization: Bearer $GTOK" -H "Content-Type: application/json" \
        -d '{"ids":["x"],"addLabelIds":["TRASH"]}' \
        https://gmail.googleapis.com/gmail/v1/users/me/messages/batchModify || true)"
# With no token the label cache is empty, so TRASH is "not in ai-cleanup/*" → 403.
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "5. OpenRouter disallowed endpoint (GET completions) → expect 403 ... "
code="$(curl "${px[@]}" -H "Authorization: Bearer $ORK" \
        https://openrouter.ai/api/v1/chat/completions || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "6. Telegram send to non-allowlisted chat → expect 403 ... "
code="$(curl -x "http://127.0.0.1:$PORT" --cacert "$CA" -s -o /dev/null -w "%{http_code}" \
        -X POST -H "Content-Type: application/json" -d '{"chat_id":999,"text":"hi"}' \
        "https://api.telegram.org/bot$TGTOK/sendMessage" || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "7. Telegram unknown method (setWebhook) → expect 403 ... "
code="$(curl -x "http://127.0.0.1:$PORT" --cacert "$CA" -s -o /dev/null -w "%{http_code}" \
        -X POST -H "Content-Type: application/json" -d '{"url":"https://evil.example"}' \
        "https://api.telegram.org/bot$TGTOK/setWebhook" || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "8. Telegram read (getMe, valid dummy), no token configured → expect 503 ... "
code="$(curl "${px[@]}" "https://api.telegram.org/bot$TGTOK/getMe" || true)"
[ "$code" = "503" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

# ── shared-secret gate: wrong dummy credential must be denied ──────────────
echo -n "9. Gmail with WRONG dummy bearer → expect 403 (bad-sandbox-credential) ... "
code="$(curl "${px[@]}" -H "Authorization: Bearer not-the-dummy" \
        https://gmail.googleapis.com/gmail/v1/users/me/labels || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "10. OpenRouter with WRONG dummy key → expect 403 (bad-sandbox-credential) ... "
code="$(curl "${px[@]}" -H "Authorization: Bearer not-the-key" \
        -X POST https://openrouter.ai/api/v1/chat/completions || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo -n "11. Telegram with WRONG path token → expect 403 (bad-sandbox-credential) ... "
code="$(curl "${px[@]}" "https://api.telegram.org/bot999999:WRONGTOKEN/getMe" || true)"
[ "$code" = "403" ] && echo "OK" || { echo "FAIL ($code)"; fail=1; }

echo "==> audit log:"
docker exec "$NAME" cat /home/mitmproxy/.mitmproxy/audit.jsonl 2>/dev/null || true

rm -f "$CA"
[ "$fail" = "0" ] && echo "ALL PASS" || { echo "SOME FAILED"; exit 1; }
