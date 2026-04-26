#!/usr/bin/env bash
# loop-improvements.sh — Stop hook that refuses to let Claude exit while
# IMPROVEMENTS.md still has unchecked items.
#
# Mechanism:
#   * Claude Code fires the Stop hook every time the assistant finishes
#     responding. We read the hook's stdin JSON, decide whether work
#     remains, and if so emit a JSON document with `decision: block` plus
#     a continuation prompt. The runtime feeds that prompt back to Claude
#     as if the user had said it, and Claude keeps going.
#   * The Stop hook fires for every session end — including ones triggered
#     by our own block. The runtime sets `stop_hook_active: true` in the
#     stdin JSON for those re-entries; we honour that flag and exit 0
#     immediately so Claude can actually stop when the work is done.
#   * We count `^- \[ \]` lines in IMPROVEMENTS.md. Zero unchecked items
#     means everything is shipped, so we let Claude exit cleanly.
#
# Output JSON shape:
#   { "decision": "block",
#     "reason": "<continuation prompt>" }
#
# Tested with `printf '{}' | loop-improvements.sh` (no jq match → block,
# correct prompt); `printf '{"stop_hook_active":true}' | loop-improvements.sh`
# (active → exit 0 silently). See settings.json `hooks.Stop[]` entry.

set -uo pipefail

ROOT="/data/data/com.termux/files/home/git/x2d"
LEDGER="${ROOT}/IMPROVEMENTS.md"

input="$(cat)"
active="$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null || printf 'false')"
if [ "$active" = "true" ]; then
    exit 0
fi

if [ ! -f "$LEDGER" ]; then
    exit 0
fi

# Count unchecked items (top-level checkboxes only — sub-task checkboxes
# under a top-level item shouldn't keep the loop alive on their own).
# `grep -c` prints "0" on no-match and exits 1, so do NOT chain `|| printf 0`
# — that would emit "0\n0" and the `-eq` test below would die with an
# "integer expression expected" error, falling through to emit a useless
# continuation prompt with an empty title.
pending="$(grep -cE '^- \[ \] \*\*' "$LEDGER" 2>/dev/null)"
pending=${pending:-0}
if ! [ "$pending" -gt 0 ] 2>/dev/null; then
    exit 0
fi

next_line="$(grep -m1 -E '^- \[ \] \*\*' "$LEDGER" || printf '')"
# Strip the markdown bullet + checkbox + bold markers.
next_title="$(printf '%s' "$next_line" | sed -E 's/^- \[ \] \*\*([^*]*)\*\*.*/\1/')"

reason=$(cat <<EOF
${pending} item(s) still unchecked in ${LEDGER}. Resume work on the next
incomplete item — currently: "${next_title}".

Rules: no stubs, no shortcuts, no skipped sub-tasks. Implement fully,
test end-to-end against the real X2D where applicable, commit + push to
github.com/tribixbite/x2d, and refresh the dist tarball + GitHub release
asset whenever a user-facing artefact changes. Update ${LEDGER} —
flip the top-level checkbox to [x] and any completed sub-task lines to
[x] — only after the item is fully done by those criteria.

When every top-level item is checked, summarise the whole pass and exit.
EOF
)

# JSON-escape the reason via jq.
printf '%s' "$reason" | jq -Rs '{decision:"block", reason:.}'
exit 0
