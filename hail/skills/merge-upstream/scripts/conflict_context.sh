#!/usr/bin/env bash
# Usage: conflict_context.sh <path> <target-ref>
# Run while `git merge --no-commit <target-ref>` is in progress. Read-only. For one conflicted
# file prints: STAGES, HUNKS (ours/base/theirs text per marker block), OURS_HISTORY (CPG-only
# commits behind our lines), THEIRS_HISTORY (upstream commits since last sync, with PR bodies),
# SIDE_DIFFS (each side vs the merge base).
set -euo pipefail

file=${1:-}
target=${2:-}
[[ -n "$file" && -n "$target" ]] || { echo "usage: $0 <path> <target-ref>" >&2; exit 2; }
git rev-parse --verify -q "${target}^{commit}" >/dev/null || { echo "unknown ref: $target" >&2; exit 2; }
git diff --name-only --diff-filter=U -- "$file" | grep -q . || { echo "$file is not in conflict" >&2; exit 2; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
base=$(git merge-base HEAD "$target")

echo "## STAGES"
echo "# stage 1 = base, 2 = ours (HEAD), 3 = theirs ($target). A missing stage means that side deleted the file."
git ls-files -u -- "$file"
echo

echo "## HUNKS"
if [[ -f "$file" ]]; then
  awk -v tmp="$tmp" -v target="$target" '
    /^<<<<<<< / { n++; side = "ours"; start = NR; o = b = t = ""; hasbase = 0; next }
    side == "ours" && /^\|\|\|\|\|\|\| / { side = "base"; hasbase = 1; next }
    (side == "ours" || side == "base") && /^=======$/ { side = "theirs"; next }
    side == "theirs" && /^>>>>>>> / {
      printf "### HUNK %d (working-tree lines %d-%d)\n", n, start, NR
      print "--- ours (HEAD)";            printf "%s", o
      print "--- base";                   if (hasbase) printf "%s", b; else print "(no base section: merge was not run with zdiff3)"
      print "--- theirs (" target ")";    printf "%s", t
      print ""
      printf "%s", o > (tmp "/ours." n); close(tmp "/ours." n)
      side = ""; next }
    side == "ours"   { o = o $0 "\n"; next }
    side == "base"   { b = b $0 "\n"; next }
    side == "theirs" { t = t $0 "\n"; next }
  ' "$file"
  ls "$tmp"/ours.* >/dev/null 2>&1 || echo "(no conflict markers found: binary file or whole-file conflict — decide from STAGES and the histories)"
else
  echo "(file absent from working tree: deleted on one side — decide from STAGES and the histories)"
fi
echo

echo "## OURS_HISTORY"
echo "# CPG-only commits (reachable from HEAD, not from $target) that touched $file, newest first:"
git log -15 --format='%h %ad %an|%P|%s' --date=short "$target..HEAD" -- "$file" \
  | awk -F'|' '{ m = (split($2, p, " ") > 1) ? " [merge]" : ""; print $1 m " | " $3 }'
echo
echo "# blame of our side of each hunk, CPG-only commits (matched by exact line text against HEAD):"
# sha<TAB>text for every line of the HEAD version of the file
git blame --porcelain HEAD -- "$file" | awk '
  /^[0-9a-f]{40} [0-9]+ [0-9]+/ { sha = $1; next }
  /^\t/ { print sha "\t" substr($0, 2) }' > "$tmp/blame"
for o in "$tmp"/ours.*; do
  [[ -e "$o" ]] || continue
  n=${o##*.}
  echo "### HUNK $n"
  # ignore blank / punctuation-only lines, which blame to arbitrary commits
  awk 'NR == FNR { if ($0 !~ /^[[:space:]]*[][{}();,]*[[:space:]]*$/) want[$0] = 1; next }
       { i = index($0, "\t"); if (substr($0, i + 1) in want) print substr($0, 1, i - 1) }' "$o" "$tmp/blame" | sort -u | while read -r sha; do
    [[ -n "$sha" ]] || continue
    git merge-base --is-ancestor "$sha" "$target" && continue   # upstream commit, not ours
    git log -1 --format='%h %ad %an | %s' --date=short "$sha"
  done
done
echo

echo "## THEIRS_HISTORY"
echo "# upstream commits since last sync ($(git rev-parse --short "$base")..$target) that touched $file. Squash-merged: body = PR description."
git log --format='%h %ad %an | %s%n%b%n---' --date=short "$base..$target" -- "$file" | awk 'NR <= 200; NR == 201 { print "(truncated at 200 lines)" }'
echo

echo "## SIDE_DIFFS"
echo "### ours: what CPG changed vs the merge base"
git diff -w "$base" HEAD -- "$file" | awk 'NR <= 200; NR == 201 { print "(truncated at 200 lines)" }'
echo
echo "### theirs: what upstream changed vs the merge base"
git diff -w "$base" "$target" -- "$file" | awk 'NR <= 200; NR == 201 { print "(truncated at 200 lines)" }'
