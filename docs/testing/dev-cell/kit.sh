#!/usr/bin/env bash
# Dev Cell live-test helper. The GitHub token is read from ~/.dif/secrets/github_app and is
# never printed or put on a command line (git gets it through environment config).
#
#   ./kit.sh check   OWNER REPO           token, repo access, docker and the sandbox image
#   ./kit.sh seed    OWNER REPO           push sample-repo to an EMPTY repo, create the label
#                                         and the three test issues (unlabelled)
#   ./kit.sh webhook OWNER REPO URL       create the issues webhook (URL ends in /hooks/github)
#   ./kit.sh clone   OWNER REPO DIR       the local clone the cell works in (its workspace)
#   ./kit.sh label   OWNER REPO NUMBER    add the pickup label to an issue: starts the cell
#   ./kit.sh reset   DIR                  put the workspace back on the default branch, clean
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SECRETS="${DIF_SECRETS_DIR:-$HOME/.dif/secrets}"
API="https://api.github.com"
LABEL="${DEVCELL_LABEL:-agent}"
IMAGE="${DEVCELL_IMAGE:-python:3.12-slim}"

token() {
  [ -s "$SECRETS/github_app" ] || { echo "no GitHub token: uv run dif-general-harness secrets set github_app" >&2; exit 2; }
  tr -d '\r\n' < "$SECRETS/github_app"
}

gh_api() {  # METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  local auth; auth="Authorization: Bearer $(token)"
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H @<(printf '%s\n' "$auth") -H "Accept: application/vnd.github+json" \
      -H "Content-Type: application/json" --data "$body" "$API$path"
  else
    curl -sS -X "$method" -H @<(printf '%s\n' "$auth") -H "Accept: application/vnd.github+json" "$API$path"
  fi
}

json() { python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }

git_auth() {  # run git with the token as an HTTP header, never in argv or a file
  local basic; basic=$(printf 'x-access-token:%s' "$(token)" | base64 -w0)
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.https://github.com/.extraheader \
    GIT_CONFIG_VALUE_0="Authorization: Basic $basic" git "$@"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  check)
    owner="$1"; repo="$2"
    out=$(gh_api GET "/repos/$owner/$repo")
    python3 -c 'import json,sys; d=json.load(sys.stdin); p=d.get("permissions") or {}
print("repo:", d.get("full_name") or d.get("message"), "| default branch:", d.get("default_branch"),
      "| push:", p.get("push"), "| private:", d.get("private"))' <<<"$out"
    echo "token: $(token | wc -c) characters (not shown)"
    docker version --format 'docker: {{.Server.Version}}' || { echo "docker is not usable by $(whoami)" >&2; exit 1; }
    docker run --rm --network none "$IMAGE" python3 -c 'print("sandbox image: ok")'
    ;;
  seed)
    owner="$1"; repo="$2"
    work=$(mktemp -d)  # a throwaway copy in /tmp; the system cleans it up
    cp -r "$HERE/sample-repo/." "$work/"
    (cd "$work" && git init -q -b main && git add -A \
      && git -c user.name="Dev Cell test" -c user.email="devcell-test@di-factory.biz" commit -qm "Sample repository for the Dev Cell test" \
      && git_auth push -q "https://github.com/$owner/$repo.git" main)
    gh_api POST "/repos/$owner/$repo/labels" "$(json name "$LABEL" color 1d76db description "Hands the issue to the Dev Cell")" >/dev/null
    for f in "$HERE"/issues/*.md; do
      title=$(head -n1 "$f"); body=$(tail -n +2 "$f")
      n=$(gh_api POST "/repos/$owner/$repo/issues" "$(json title "$title" body "$body")" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("number"))')
      echo "issue #$n: $title"
    done
    ;;
  webhook)
    owner="$1"; repo="$2"; url="$3"
    [ -s "$SECRETS/github_webhook" ] || { echo "no webhook secret: openssl rand -hex 24 | uv run dif-general-harness secrets set github_webhook" >&2; exit 2; }
    body=$(python3 -c 'import json,sys; print(json.dumps({"name": "web", "active": True, "events": ["issues"],
      "config": {"url": sys.argv[1], "content_type": "json", "secret": open(sys.argv[2]).read().strip()}}))' \
      "$url" "$SECRETS/github_webhook")
    gh_api POST "/repos/$owner/$repo/hooks" "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("webhook:", d.get("id") or d.get("message"))'
    ;;
  clone)
    owner="$1"; repo="$2"; dir="$3"
    git_auth clone -q "https://github.com/$owner/$repo.git" "$dir"
    echo "workspace: $dir"
    ;;
  label)
    owner="$1"; repo="$2"; n="$3"
    gh_api POST "/repos/$owner/$repo/issues/$n/labels" "{\"labels\": [\"$LABEL\"]}" >/dev/null
    echo "issue #$n labelled '$LABEL': the cell should pick it up within seconds"
    ;;
  reset)
    dir="$1"
    [ -d "$dir/.git" ] || { echo "$dir is not a git clone" >&2; exit 2; }
    branch=$(git -C "$dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || echo main)
    git_auth -C "$dir" fetch -q origin
    git -C "$dir" checkout -q "$branch"
    git -C "$dir" reset -q --hard "origin/$branch"
    git -C "$dir" clean -qfd
    echo "workspace reset to origin/$branch"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 2 ;;
esac
