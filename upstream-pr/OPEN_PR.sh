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

UPSTREAM=bambulab/BambuStudio
BRANCH=touchscreen-button-release-slop
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PATCH=$HERE/touchscreen-button-fix.patch
PR_BODY=$HERE/PR_BODY.md

# Use the user's git config identity if set, otherwise fall back to a
# noreply address — never inline a real personal email here, since the
# author trailer ends up in a public commit.
GIT_NAME=$(git config --global --get user.name 2>/dev/null || true)
GIT_NAME=${GIT_NAME:-x2d-contributor}
GIT_EMAIL=$(git config --global --get user.email 2>/dev/null || true)
if [ -z "$GIT_EMAIL" ]; then
    GH_USER=$(gh api user --jq .login 2>/dev/null || echo "")
    if [ -n "$GH_USER" ]; then
        GIT_EMAIL="${GH_USER}@users.noreply.github.com"
    else
        GIT_EMAIL="noreply@users.noreply.github.com"
    fi
fi
echo "→ commit identity: $GIT_NAME <$GIT_EMAIL>"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "→ forking $UPSTREAM (skipped if fork already exists)"
gh repo fork "$UPSTREAM" --clone --remote --default-branch-only \
    || git clone "https://github.com/$(gh api user --jq .login)/BambuStudio.git" BambuStudio

cd BambuStudio
# Make sure both remotes exist regardless of which path above we took.
if ! git remote get-url upstream >/dev/null 2>&1; then
    git remote add upstream "https://github.com/$UPSTREAM.git"
fi
git fetch upstream master --depth 50
git checkout -B "$BRANCH" upstream/master

echo "→ applying $PATCH (3-way merge — recoverable from upstream drift)"
git apply --check -3 "$PATCH" 2>/dev/null || true
git apply        -3 "$PATCH"

git -c user.name="$GIT_NAME" -c user.email="$GIT_EMAIL" \
    add src/slic3r/GUI/Widgets/Button.cpp \
        src/slic3r/GUI/Widgets/AxisCtrlButton.cpp \
        src/slic3r/GUI/Widgets/SideButton.cpp \
        src/slic3r/GUI/TabButton.cpp
git -c user.name="$GIT_NAME" -c user.email="$GIT_EMAIL" \
    commit -m "Fix touchscreen taps being dropped on custom Button widgets

Add a 15 px release-slop to the bounds check in mouseReleased on
Button / AxisCtrlButton / SideButton / TabButton. Strict
wxRect.Contains was silently swallowing every touchscreen tap
because finger contact rolls between press and release, taking the
up-coord outside the rect.

The deliberate desktop drag-off-to-cancel gesture is preserved (any
release further than 15 px outside still cancels). For consistency,
the patch also wraps ReleaseMouse() in a HasCapture() guard for the
two widgets (AxisCtrlButton, TabButton) that called it
unconditionally — wxWidgets asserts in debug builds otherwise.
"

git push origin "$BRANCH"

echo "→ opening PR with body from $PR_BODY"
gh pr create \
    --repo "$UPSTREAM" \
    --base master \
    --head "$(gh api user --jq .login):$BRANCH" \
    --title "Fix touchscreen taps being dropped on custom Button widgets" \
    --body-file "$PR_BODY"

echo "Done."
