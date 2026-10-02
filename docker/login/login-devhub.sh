#!/bin/bash
# Log the Dev Hub in from inside the sandbox (provisioning mode), as `devhub`.
#
# sf 2.x has no device login any more, so this uses the web flow: it prints the login URL for
# you to open in your browser on this machine. The browser's redirect to localhost:1717 reaches
# the container through the port the Makefile publishes (127.0.0.1 only) and oauth-relay.js,
# because sf listens on the container's own localhost. Any user other than
# FORCEBENCH_DEVHUB_USERNAME is logged straight out again.
set -euo pipefail
want=${FORCEBENCH_DEVHUB_USERNAME:?not in provisioning mode: use make sandbox-provision or make devhub-limits}
dir=$(cd "$(dirname "$0")" && pwd)
url=$(mktemp)
node "$dir/oauth-relay.js" &
relay=$!
trap 'kill $relay 2>/dev/null; rm -f "$url" "$url.log"' EXIT

BROWSER="$dir/print-url.sh" LOGIN_URL_FILE="$url" \
  sf org login web --alias devhub --instance-url https://login.salesforce.com >"$url.log" 2>&1 &
login=$!
until [ -s "$url" ] || ! kill -0 $login 2>/dev/null; do sleep 0.2; done
if [ -s "$url" ]; then
  printf '\nOpen this in your browser and log in as %s:\n\n%s\n\n' "$want" "$(cat "$url")"
fi
if ! wait $login; then
  cat "$url.log" >&2
  exit 1
fi

got=$(sf org display --target-org devhub --json |
  node -e 'let s = ""; process.stdin.on("data", (d) => (s += d)).on("end", () => console.log(JSON.parse(s).result.username))')
if [ "$got" != "$want" ]; then
  sf org logout --target-org devhub --no-prompt >/dev/null
  echo "logged in as $got, not $want: logged out again" >&2
  exit 1
fi
echo "Dev Hub $got is logged in as devhub. Log it out when done: sf org logout --target-org devhub --no-prompt"
