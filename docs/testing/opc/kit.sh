#!/usr/bin/env bash
# OPC live-test helper. The bot token is read from ~/.dif/secrets/telegram and never printed.
#
#   ./kit.sh chat-id            the founder's Telegram chat id (write any message to the bot first)
#   ./kit.sh set-chat ID        put that chat id in opc-test.json
#   ./kit.sh caddy-route        add https://<host>/opc/ -> 127.0.0.1:8083 to Caddy (keeps a backup)
#   ./kit.sh caddy-restore      put the Caddyfile back as it was
#   ./kit.sh alert [SEVERITY]   raise "p95 latency high" on the monitoring stand-in
#   ./kit.sh clear-alert        clear the alerts
#   ./kit.sh outbox             what the team sent out: posts, emails, CRM changes
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="${DIF_SECRETS_DIR:-$HOME/.dif/secrets}"
STAND="http://127.0.0.1:${STAND_PORT:-8091}"
CADDY=/etc/caddy/Caddyfile
BACKUP=/etc/caddy/Caddyfile.before-opc

token() {
  [ -s "$SECRETS/telegram" ] || { echo "no bot token: uv run dif-general-harness secrets set telegram" >&2; exit 2; }
  tr -d '\r\n' < "$SECRETS/telegram"
}

tg() {  # METHOD: call the Bot API without putting the token on a command line
  local url; url="https://api.telegram.org/bot$(token)/$1"
  curl -sS --config <(printf 'url = "%s"\n' "$url")
}

cmd="${1:-}"; shift || true
case "$cmd" in
  chat-id)
    tg deleteWebhook >/dev/null  # updates are only readable while no webhook is set
    tg getUpdates | python3 -c 'import json,sys
seen = {}
for u in json.load(sys.stdin).get("result", []):
    m = u.get("message") or {}
    c = m.get("chat") or {}
    if c.get("id"): seen[c["id"]] = " ".join(x for x in (c.get("first_name"), c.get("last_name"), c.get("username")) if x)
if not seen: print("no messages yet: write anything to your bot in Telegram, then run this again")
for i, who in seen.items(): print(i, " ", who)'
    ;;
  set-chat)
    id="$1"
    [[ "$id" =~ ^-?[0-9]+$ ]] || { echo "a chat id is a number (from ./kit.sh chat-id)" >&2; exit 2; }
    python3 - "$HERE/opc-test.json" "$id" <<'EOF'
import json, sys
path, chat = sys.argv[1], sys.argv[2]
spec = json.load(open(path))
spec["values"]["founder_telegram"] = chat
open(path, "w").write(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
print("founder_telegram =", chat)
EOF
    ;;
  caddy-route)
    host=$(sudo awk 'NF && $1 !~ /^#/ {print $1; exit}' "$CADDY")
    [ -n "$host" ] || { echo "cannot read the site name from $CADDY" >&2; exit 1; }
    [ -f "$BACKUP" ] || sudo cp "$CADDY" "$BACKUP"
    printf '%s {\n    handle_path /opc/* {\n        reverse_proxy 127.0.0.1:8083\n    }\n    handle {\n        reverse_proxy 127.0.0.1:8080\n    }\n}\n' "$host" | sudo tee "$CADDY" >/dev/null
    sudo systemctl reload caddy
    echo "https://$host/opc/ -> 127.0.0.1:8083 (the client on 8080 is unchanged)"
    echo "public URL for serve: https://$host/opc"
    ;;
  caddy-restore)
    [ -f "$BACKUP" ] || { echo "no backup at $BACKUP: nothing to restore" >&2; exit 1; }
    sudo cp "$BACKUP" "$CADDY" && sudo systemctl reload caddy && echo "Caddyfile restored"
    ;;
  alert)
    curl -sS -X POST -H 'Authorization: Bearer kit' -H 'Content-Type: application/json' \
      -d "{\"name\": \"p95 latency high\", \"severity\": \"${1:-warning}\", \"service\": \"web\"}" \
      "$STAND/control/alert"; echo
    ;;
  clear-alert)
    curl -sS -X DELETE -H 'Authorization: Bearer kit' "$STAND/control/alert"; echo
    ;;
  outbox)
    if [ -s "$HERE/outbox.json" ]; then python3 -m json.tool "$HERE/outbox.json"; else echo "nothing sent out yet"; fi
    ;;
  *)
    sed -n '2,11p' "$0"; exit 2 ;;
esac
