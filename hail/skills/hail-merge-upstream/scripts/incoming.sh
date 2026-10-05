#!/usr/bin/env bash
# Usage: incoming.sh <target-ref>
# Run inside the hail checkout on the draft branch. Read-only: prints facts about what merging
# <target-ref> into HEAD would bring in. Sections: LAST_SYNC, INCOMING, CPG_DIVERGENT, FLAGGED,
# DRYRUN_CONFLICTS.
set -euo pipefail

target=${1:-}
[[ -n "$target" ]] || { echo "usage: $0 <target-ref>" >&2; exit 2; }
git rev-parse --verify -q "${target}^{commit}" >/dev/null || { echo "unknown ref: $target" >&2; exit 2; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

base=$(git merge-base HEAD "$target")

echo "## LAST_SYNC"
echo "merge_base: $(git log -1 --format='%h %ad %an | %s' --date=short "$base")"
introducer=$(git log --first-parent --ancestry-path --format='%h %ad %an | %s' --date=short "$base..HEAD" | tail -1)
echo "introduced_by: ${introducer:-HEAD is at the merge base (nothing merged since, or branch only ahead)}"
echo

echo "## INCOMING"
echo "count: $(git rev-list --count "HEAD..$target")"
git log --format='%h|%ad|%an|%s' --date=short "HEAD..$target"
echo

echo "## CPG_DIVERGENT"
git diff --name-only "$base" HEAD | sort > "$tmp/divergent"
cat "$tmp/divergent"
echo

echo "## FLAGGED"
# incoming commits that touch a file where our fork differs from upstream
git log --format='%h|%s' --name-only "HEAD..$target" | awk -v div="$tmp/divergent" '
  BEGIN { while ((getline l < div) > 0) d[l] = 1 }
  /^[0-9a-f]+\|/ { c = $0; next }
  /^$/ { next }
  ($0 in d) { print c " -> " $0 }'
echo

echo "## DRYRUN_CONFLICTS"
set +e
mt=$(git merge-tree --write-tree --name-only HEAD "$target" 2>&1)
rc=$?
set -e
case $rc in
  0) echo "none" ;;
  1) # line 1 is the resulting tree oid; conflicted paths follow until the first blank line
     printf '%s\n' "$mt" | sed -n '2,/^$/p' | sed '/^$/d' ;;
  *) echo "merge-tree failed ($rc): $mt" >&2; exit 1 ;;
esac
echo

echo "## CHORES"
# Upstream bumps of base artifacts that CPG must re-materialise in its own GCP project
# (hail-295901) before the merged code can deploy. Read-only here: prints check + fix commands.
registry=australia-southeast1-docker.pkg.dev/hail-295901/hail
chores=0
added=$(git diff "$base" "$target" -- docker/third-party/images.txt | grep '^+[^+]' | sed 's/^+//' || true)
if [[ -n "$added" ]]; then
  chores=1
  echo "# docker/third-party/images.txt gained images; each must be mirrored into the CPG registry before any deploy:"
  while IFS= read -r img; do
    [[ -n "$img" ]] || continue
    echo "mirror: $img"
    echo "  check: gcloud artifacts docker tags list $registry/${img%%:*} --format='value(tag)' | grep -x '${img##*:}'"
    echo "  fix:   skopeo copy --override-os linux --override-arch amd64 docker://docker.io/$img docker://$registry/$img"
  done <<<"$added"
fi
wv=$(git diff "$base" "$target" -- batch/gcp-create-worker-image.sh | sed -n 's/^+WORKER_IMAGE_VERSION=//p')
if [[ -n "$wv" ]]; then
  chores=1
  echo "# worker VM boot image bumped to batch-worker-$wv; it must exist in hail-295901 before this reaches main:"
  echo "  check: gcloud compute images list --project hail-295901 --filter='name=batch-worker-$wv' --format='value(name)'"
  echo "  fix:   on the hail-dev VM, from a checkout containing this merge: NAMESPACE=default \$HAIL/batch/gcp-create-worker-image.sh"
  echo "         (team-docs hail.md -> 'Building a new boot disk image'; background: dev-docs/services/batch/worker-vm-image.md)"
fi
[[ $chores -eq 1 ]] || echo "none"
