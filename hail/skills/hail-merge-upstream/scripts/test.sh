#!/usr/bin/env bash
# Self-check for hail-merge-upstream scripts against a real historical conflict in the CPG hail fork.
#   ours   = a3e2b6490  draft-sept26 as of 14 Sep 2026
#   theirs = 9746749c6  upstream/main as of 18 Sep 2026
# Merging theirs into ours conflicts only in infra/gcp/main.tf (resolved by hand in 2ebd3a71d)
# and auto-merges build.yaml, which is CPG-divergent (so it must be FLAGGED).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=${HAIL_REPO:-$HOME/Development/hail}
ours=a3e2b6490
theirs=9746749c6

fail() { echo "FAIL: $*" >&2; exit 1; }

wt=$(mktemp -d /tmp/hmu-test.XXXXXX); rmdir "$wt"
cleanup() {
  git -C "$wt" merge --abort >/dev/null 2>&1 || true
  git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || true
}
trap cleanup EXIT
git -C "$repo" worktree add -q --detach "$wt" "$ours"
cd "$wt"

# --- incoming.sh ---
set +e; "$here/incoming.sh" >/dev/null 2>&1; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "incoming.sh with no args should exit 2, got $rc"

out=$("$here/incoming.sh" "$theirs")
for s in LAST_SYNC INCOMING CPG_DIVERGENT FLAGGED DRYRUN_CONFLICTS CHORES; do
  grep -q "^## $s\$" <<<"$out" || fail "incoming.sh missing section $s"
done
grep -q '^introduced_by: .*Upstream development en route to 0.2.140' <<<"$out" || fail "introduced_by should name the 14 Sep merge commit"
grep -qE '^count: [1-9][0-9]*$' <<<"$out" || fail "expected a positive incoming count"
grep -q -- '-> build.yaml$' <<<"$out" || fail "build.yaml should be FLAGGED"
# DRYRUN_CONFLICTS must list exactly infra/gcp/main.tf
conf=$(sed -n '/^## DRYRUN_CONFLICTS$/,/^## CHORES$/p' <<<"$out" | sed '/^## /d; /^$/d')
[[ "$conf" == "infra/gcp/main.tf" ]] || fail "DRYRUN_CONFLICTS expected infra/gcp/main.tf, got: $conf"

# up-to-date case: target == HEAD
out0=$("$here/incoming.sh" HEAD)
grep -q '^count: 0$' <<<"$out0" || fail "incoming.sh HEAD should report count: 0"
grep -qx 'none' <<<"$out0" || fail "incoming.sh HEAD should report no dry-run conflicts"

# CHORES: fixture range adds ubuntu:noble-20260911 to docker/third-party/images.txt -> mirror chore with the skopeo command
chores=$(sed -n '/^## CHORES$/,$p' <<<"$out")
grep -q '^mirror: ubuntu:noble-20260911$' <<<"$chores" || fail "CHORES should list the mirror chore for ubuntu:noble-20260911"
grep -qF 'skopeo copy --override-os linux --override-arch amd64 docker://docker.io/ubuntu:noble-20260911 docker://australia-southeast1-docker.pkg.dev/hail-295901/hail/ubuntu:noble-20260911' <<<"$chores" || fail "CHORES should give the exact skopeo copy command"
grep -q 'batch-worker-' <<<"$chores" && fail "fixture range has no worker image bump; CHORES must not invent one"
grep -q '^none$' <<<"$(sed -n '/^## CHORES$/,$p' <<<"$out0")" || fail "incoming.sh HEAD should report no chores"
# worker image bump chore (synthetic: bump WORKER_IMAGE_VERSION on a detached commit from ours)
perl -pi -e 's/^WORKER_IMAGE_VERSION=\d+/WORKER_IMAGE_VERSION=99/' batch/gcp-create-worker-image.sh
git commit -qam "bump worker image to 99"; W=$(git rev-parse HEAD)
git checkout -q --detach "$ours"
grep -q 'batch-worker-99' <<<"$("$here/incoming.sh" "$W" | sed -n '/^## CHORES$/,$p')" || fail "CHORES should mention batch-worker-99 after a WORKER_IMAGE_VERSION bump"
echo "ok incoming.sh"

# --- conflict_context.sh ---
set +e; "$here/conflict_context.sh" >/dev/null 2>&1; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "conflict_context.sh with no args should exit 2, got $rc"

set +e; "$here/conflict_context.sh" build.yaml "$theirs" >/dev/null 2>&1; rc=$?; set -e
[[ $rc -eq 2 ]] || fail "conflict_context.sh on a non-conflicted file should exit 2, got $rc"

set +e; git -c merge.conflictstyle=zdiff3 merge --no-commit "$theirs" >/dev/null 2>&1; set -e
[[ "$(git diff --name-only --diff-filter=U)" == "infra/gcp/main.tf" ]] || fail "fixture merge should conflict only in infra/gcp/main.tf"

out=$("$here/conflict_context.sh" infra/gcp/main.tf "$theirs")
for s in STAGES HUNKS OURS_HISTORY THEIRS_HISTORY SIDE_DIFFS; do
  grep -q "^## $s\$" <<<"$out" || fail "conflict_context.sh missing section $s"
done
grep -q '^### HUNK 1 (working-tree lines [0-9]*-[0-9]*)$' <<<"$out" || fail "expected HUNK 1 header"
grep -q '^--- ours (HEAD)$' <<<"$out" || fail "expected ours block"
grep -q '^--- base$' <<<"$out" || fail "expected base block"
grep -q "^--- theirs ($theirs)$" <<<"$out" || fail "expected theirs block"
grep -q 'artifact_registry_push_admin' <<<"$out" || fail "ours block should contain the artifact registry resource"
# THEIRS_HISTORY must carry an upstream PR reference from a squash-merged commit body/subject
sed -n '/^## THEIRS_HISTORY$/,/^## SIDE_DIFFS$/p' <<<"$out" | grep -qE '\(#[0-9]+\)' || fail "THEIRS_HISTORY should mention an upstream PR number"
# OURS_HISTORY must list at least one CPG-only commit
sed -n '/^## OURS_HISTORY$/,/^## THEIRS_HISTORY$/p' <<<"$out" | grep -qE '^[0-9a-f]{9,} [0-9]{4}-' || fail "OURS_HISTORY should list a CPG commit"
sed -n '/^# blame of our side/,/^## THEIRS_HISTORY$/p' <<<"$out" | grep -qE '^[0-9a-f]{9,} [0-9]{4}-' || fail "blame block for HUNK 1 should list a CPG commit"
git merge --abort

# markers without a base section (merge done by hand without zdiff3)
set +e; git -c merge.conflictstyle=merge merge --no-commit "$theirs" >/dev/null 2>&1; set -e
out=$("$here/conflict_context.sh" infra/gcp/main.tf "$theirs")
grep -q '(no base section' <<<"$out" || fail "expected a '(no base section' note when markers lack a base"
grep -q "^--- theirs ($theirs)$" <<<"$out" || fail "theirs block must still be present without base section"
git merge --abort
echo "ok conflict_context.sh"

# --- leftover-marker regex used by SKILL.md step 6: real markers match, RST underlines do not ---
marker_re='^(<{7} |\|{7} |={7}$|>{7} )'
grep -qF "$marker_re" "$here/../SKILL.md" || fail "SKILL.md step 6 must use the marker regex $marker_re"
set +e; git -c merge.conflictstyle=zdiff3 merge --no-commit "$theirs" >/dev/null 2>&1; set -e
[[ "$(grep -cE "$marker_re" infra/gcp/main.tf)" -ge 4 ]] || fail "marker regex should match the 4 real markers in infra/gcp/main.tf"
printf 'Title\n==================\n\nmore\n========\n' > "$wt/rst-sample.rst"
grep -qE "$marker_re" "$wt/rst-sample.rst" && fail "marker regex must not match RST underlines"
rm -f "$wt/rst-sample.rst"
git merge --abort
echo "ok marker regex"

# --- tab-indented ours lines must still be attributed (synthetic hail/Makefile conflict) ---
tabline=$(grep -n "$(printf '\t')" hail/Makefile | head -1 | cut -d: -f1)
[[ -n "$tabline" ]] || fail "hail/Makefile should have a tab-indented line"
perl -pi -e "s/\$/ # cpg-ours/ if \$. == $tabline" hail/Makefile
git commit -qam "cpg tab-line change"; A=$(git rev-parse HEAD)
git checkout -q --detach "$ours"
perl -pi -e "s/\$/ # upstream-theirs/ if \$. == $tabline" hail/Makefile
git commit -qam "upstream tab-line change"; B=$(git rev-parse HEAD)
git checkout -q --detach "$A"
set +e; git -c merge.conflictstyle=zdiff3 merge --no-commit "$B" >/dev/null 2>&1; set -e
[[ "$(git diff --name-only --diff-filter=U)" == "hail/Makefile" ]] || fail "synthetic Makefile merge should conflict only in hail/Makefile"
out=$("$here/conflict_context.sh" hail/Makefile "$B")
sed -n '/^# blame of our side/,/^## THEIRS_HISTORY$/p' <<<"$out" | grep -q "cpg tab-line change" || fail "blame block should attribute the tab-indented ours line to the CPG commit"
git merge --abort
echo "ok tab-indented blame"

echo PASS
