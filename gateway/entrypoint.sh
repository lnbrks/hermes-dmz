#!/bin/sh
# Gateway entrypoint: run the webhook ingress forwarder + the egress proxy.
#
# The gateway is the DMZ — the only container on both the host-facing (egress)
# and the jailed (internal) networks. So it carries ingress too: a small socat
# forwarder accepts the host's webhook POST on :8644 and forwards it to the
# Hermes container on the internal network. Hermes therefore never needs a
# host-published port, and the host never touches the jailed container directly
# — all traffic in BOTH directions crosses this one controlled boundary.
#
# socat resolves the target per-connection (fork), so it tolerates Hermes not
# being up yet at gateway start.
set -e

WEBHOOK_TARGET="${WEBHOOK_TARGET:-inbox-hermes:8644}"
WEBHOOK_LISTEN_PORT="${WEBHOOK_LISTEN_PORT:-8644}"

socat "TCP-LISTEN:${WEBHOOK_LISTEN_PORT},fork,reuseaddr" "TCP:${WEBHOOK_TARGET}" &

exec mitmdump "$@"
