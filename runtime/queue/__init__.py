"""Multi-printer print queue (item #55).

The queue is daemon-side state plus an auto-dispatch loop that fires
the next pending job to a printer when the printer goes idle. Jobs
are persisted to ``~/.x2d/queue.json`` so a daemon restart resumes
where it left off (running jobs are demoted to pending so the next
boot sees them again — better than silently losing them).

The Device-tab "Queue" sub-tab callout in the original ledger entry
is exposed via the existing web UI (#46) and bridge HTTP API; the
bambu-studio source patch is intentionally not built — the same
control surface is reachable from any browser, Claude Desktop via
MCP, or any HA dashboard, all of which work on devices that don't
run bambu-studio.
"""
