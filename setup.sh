#!/usr/bin/env bash
# One command from a fresh clone to a working client solution (Ubuntu, e.g. an AWS EC2 box).
#
#   git clone https://github.com/di-factory/claude_harness.git && cd claude_harness
#   ./setup.sh
#
# 1. installs what is missing (Docker, Caddy, uv) and the Python dependencies;
# 2. runs the guided setup: API key, what the client needs, the questionnaire, build, a test;
# 3. if you say so, starts it online over HTTPS at <public-ip>.sslip.io (Docker + Caddy).
# Safe to run again: it reuses what is already there.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

say "Installing what is missing"
if have apt-get; then
  missing=()
  have docker || missing+=(docker.io)
  docker compose version >/dev/null 2>&1 || missing+=(docker-compose-v2)
  have caddy || missing+=(caddy)
  have git || missing+=(git)
  have curl || missing+=(curl)
  if ((${#missing[@]})); then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
  id -nG "$USER" | grep -qw docker || sudo usermod -aG docker "$USER"
else
  echo "Not an apt system: install Docker, the compose plugin and Caddy yourself; continuing."
fi
if ! have uv; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv sync -q
echo "ok"

# The public address: the EC2 metadata service, else an echo service.
ip=""
token=$(curl -s -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
if [[ -n "$token" ]]; then
  ip=$(curl -s -m 2 -H "X-aws-ec2-metadata-token: $token" \
    http://169.254.169.254/latest/meta-data/public-ipv4 || true)
fi
[[ "$ip" =~ ^[0-9.]+$ ]] || ip=$(curl -s -m 5 https://checkip.amazonaws.com | tr -d '[:space:]' || true)
public_url=""
[[ "$ip" =~ ^[0-9.]+$ ]] && public_url="https://${ip//./-}.sslip.io"

rm -f .dif/online
for d in deploy/build/*/secrets; do  # owned by the container's user once online
  [[ -d "$d" && ! -w "$d" ]] && sudo chown -R "$USER" "$d"
done
uv run dif-general-harness setup ${public_url:+--public-url "$public_url"} "$@"

[[ -f .dif/online ]] || exit 0
folder=$(sed -n 1p .dif/online)
host=$(sed -n 2p .dif/online)

say "Starting it online"
docker_cmd=(docker)
docker info >/dev/null 2>&1 || docker_cmd=(sudo docker)  # the docker group applies from next login
sudo chown -R 10001 "$folder/secrets" && sudo chmod 600 "$folder"/secrets/* 2>/dev/null || true
# One client is served per server (Caddy -> 127.0.0.1:8080): stop any other one first.
# Its data volume stays; ./setup.sh with that client brings it back.
for other in deploy/build/*/docker-compose.yml; do
  [[ -f "$other" && "$(cd "$(dirname "$other")" && pwd)" != "$(cd "$folder" && pwd)" ]] || continue
  if [[ -n "$("${docker_cmd[@]}" compose -f "$other" ps -q 2>/dev/null)" ]]; then
    echo "Stopping the previous client $(basename "$(dirname "$other")") (its data is kept)"
    "${docker_cmd[@]}" compose -f "$other" down
  fi
done
sudo cp "$folder/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl reload caddy || sudo systemctl restart caddy
"${docker_cmd[@]}" compose -f "$folder/docker-compose.yml" up -d --build

printf 'Waiting for https://%s/healthz ' "$host"
for _ in $(seq 1 60); do
  if curl -fs -m 3 "https://$host/healthz" >/dev/null 2>&1; then
    say "Online: https://$host"
    cat <<EOF
  Health:      https://$host/healthz
  Landing:     https://$host/        (the business's page, built from its answers, with the chat)
  Web chat:    https://$host/chat   (or link it from the client's own website)
  WhatsApp:    in Twilio, set "When a message comes in" to https://$host/channels/whatsapp (POST)
               then: uv run dif-general-harness secrets set twilio   (ACxxxx:auth_token)
               and run ./setup.sh again to copy it in
  Admin API:   curl -H "Authorization: Bearer \$(cat ~/.dif/secrets/admin_token)" https://$host/admin/inbox
  Logs:        ${docker_cmd[*]} compose -f $folder/docker-compose.yml logs -f
EOF
    exit 0
  fi
  printf '.'
  sleep 5
done
say "Not answering yet at https://$host"
echo "Check: the security group allows ports 80 and 443 from anywhere;"
echo "       ${docker_cmd[*]} compose -f $folder/docker-compose.yml logs --tail 50"
exit 1
