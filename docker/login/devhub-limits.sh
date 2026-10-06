#!/bin/bash
# The Dev Hub's scratch-org allocations: log it in (login-devhub.sh), print them, log it out.
# `make devhub-limits` runs this with a throwaway login store, so grading is never affected.
set -euo pipefail
bash "$(dirname "$0")/login-devhub.sh"
trap 'sf org logout --target-org devhub --no-prompt >/dev/null' EXIT
# shellcheck disable=SC2016  # a JavaScript program: its ${...} are JavaScript's, not the shell's
sf org list limits --target-org devhub --json | node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  const wanted = ["ActiveScratchOrgs", "DailyScratchOrgs", "Package2VersionCreates"];
  for (const l of JSON.parse(s).result.filter((l) => wanted.includes(l.name)))
    console.log(`${l.name}: ${l.remaining} left of ${l.max}`);
});'
