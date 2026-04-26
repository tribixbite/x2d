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
#     by our own block. We deliberately ignore `stop_hook_active` because
#     this loop is supposed to keep firing across many turns until the
#     ledger is empty. The natural termination is "0 unchecked items" —
#     `pending == 0` makes the script exit 0 without emitting JSON, so
#     Claude is free to stop. If you want to bail out manually, edit
#     IMPROVEMENTS.md to flip the open boxes (or Ctrl+C the session).
#   * We count `^- \[ \] \*\*` lines in IMPROVEMENTS.md (top-level only;
#     sub-task checkboxes don't keep the loop alive on their own).
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

# Drain stdin so Claude Code's hook plumbing doesn't see EPIPE, but we
# don't actually need any field from the JSON. We DO NOT honour
# stop_hook_active — see header comment.
cat >/dev/null

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
