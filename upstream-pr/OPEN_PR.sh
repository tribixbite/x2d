#!/usr/bin/env bash
# OPEN_PR.sh — runs the gh commands to fork bambulab/BambuStudio,
# create a branch with the touch-drift fix, and open a PR. Documented
# step-by-step so the user knows exactly what gets posted on their
# behalf before they run it.
#
# Per CLAUDE.md global rule, the assistant won't run this on its own —
# it requires explicit per-instance approval each time. Re-read it before
# invoking.
set -euo pipefail

REPO_OWNER=tribixbite
UPSTREAM=bambulab/BambuStudio
BRANCH=termux/touch-drift-fix
PATCH=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/touchscreen-button-fix.patch
PR_BODY=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/PR_BODY.md

cd "$(mktemp -d)"

echo "→ forking $UPSTREAM as $REPO_OWNER/BambuStudio (skipped if exists)"
gh repo fork "$UPSTREAM" --clone --remote --default-branch-only \
    || git clone "https://github.com/$REPO_OWNER/BambuStudio.git" BambuStudio

cd BambuStudio
git fetch upstream master --depth 1
git checkout -B "$BRANCH" upstream/master

echo "→ applying $PATCH"
git apply --check "$PATCH"
git apply         "$PATCH"

git -c user.name="x2d-bot" -c user.email="willstone@gmail.com" \
    add src/slic3r/GUI/Widgets/Button.cpp \
        src/slic3r/GUI/Widgets/AxisCtrlButton.cpp \
        src/slic3r/GUI/Widgets/SideButton.cpp \
        src/slic3r/GUI/TabButton.cpp
git -c user.name="x2d-bot" -c user.email="willstone@gmail.com" \
    commit -m "Custom Button widgets: fire click on any release while pressedDown

Touchscreen / convertible / kiosk users see button-up coords drift a
few pixels from button-down (finger roll), and the strict
wxRect.Contains check in mouseReleased silently swallows the click.

Standard wxButton + wxNotebook tabs aren't affected because they
hit-test on down. Behaviour change for desktop mouse users: dragging
off a custom button no longer cancels the click.

Affects Button.cpp, AxisCtrlButton.cpp, SideButton.cpp, TabButton.cpp.
"

git push origin "$BRANCH"

echo "→ opening PR with body from $PR_BODY"
gh pr create \
    --repo "$UPSTREAM" \
    --base master \
    --head "$REPO_OWNER:$BRANCH" \
    --title "Custom Button widgets: fire click on any release while pressedDown" \
    --body-file "$PR_BODY"

echo "Done."
