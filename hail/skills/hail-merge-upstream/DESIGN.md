# hail-merge-upstream — design

Date: 2026-10-05. Owner: Yash. Location: `hail/skills/hail-merge-upstream/` in populationgenomics/software-skills (plugin `hail`); originally built as a personal skill in `~/.claude/skills/`.

## Intent

Weekly task on a team roster: pull everything upstream `hail-is/hail` has merged since the fork's
draft branch was last synced, resolve conflicts with an understanding of *why* each side differs,
commit in the team's style, push. Pain points today: repetitive mechanics, and not knowing enough
about upstream's change or our own historical divergence to choose ours/theirs confidently.

What Yash said:
- Follow team-docs "Merging upstream changes" (hail.md), draft-branch convention, commit style on
  `draft-sept26`.
- Prompt for a branch name when needed; work interactively.
- Explain each conflict: trace why our lines diverged, what upstream changed, implications of each
  choice. Always ask before resolving.
- Merge "whatever hasn't been merged since last time".
- No local checks; CI handles it. Ask before pushing. Weekly merge only; personal install.

Assumptions (confirmed in chat):
- "Since last time" == `upstream/main` tip. An optional ref argument overrides the target.
- One long-lived `draft-*` branch per release cycle; reuse the open one.
- Commit message: `Merge upstream as of <D Month YYYY>`.

## Scope

In: preflight, branch selection, incoming digest, merge, per-conflict analysis + resolution,
verification, commit, push, report.

Out (future skills): release-tag merge, version bump (`/release-changelog` exists), PR to main,
cpg tag, terraform, smoke test, dependent-repo bumps, comms text.

## Files

```
~/.claude/skills/hail-merge-upstream/
  SKILL.md                    # the procedure; disable-model-invocation: true
  DESIGN.md                   # this file
  scripts/incoming.sh         # digest data: last sync, incoming commits, flagged files, dry-run conflicts
  scripts/conflict_context.sh # per-file conflict history (ours/theirs/base + blame + upstream log)
  scripts/test.sh             # self-check against a real historical conflict (throwaway worktree)
```

Scripts are bash + git only. They print plain text sections for the model to interpret; they
never mutate the repo.

## Flow

1. **Preflight**
   - Must be inside the hail checkout (`git rev-parse --show-toplevel`; `origin` url contains
     `populationgenomics/hail`). Stop otherwise.
   - `git status --porcelain --untracked-files=no` must be empty. Stop otherwise (untracked files OK).
   - `git remote get-url upstream` else offer `git remote add upstream https://github.com/hail-is/hail.git`.
   - No gitconfig changes. The merge itself runs with `git -c merge.conflictstyle=zdiff3` so base
     sections are always present in conflict markers.
   - `git fetch origin && git fetch upstream`.
   - Target ref: `$ARGUMENTS` if given, else `upstream/main`.

2. **Branch**
   - Candidates: `git for-each-ref --sort=-committerdate --format='%(refname:short) %(committerdate:short)' refs/remotes/origin/draft-*`
     filtered with `! git merge-base --is-ancestor <ref> origin/main`.
   - AskUserQuestion: confirm newest candidate / pick another / new branch.
   - New branch: suggest `draft-<mon><yy>` (e.g. `draft-oct26`), base on `origin/main`.
   - `git switch <branch>` then `git pull --ff-only origin <branch>` (skip pull for new branch).

3. **Digest** — run `scripts/incoming.sh <target>`; output sections:
   - `LAST_SYNC`: merge-base(HEAD, target) sha/date/subject, and the first-parent commit on HEAD
     that introduced it (`git log --first-parent --ancestry-path`), with author/date.
   - `INCOMING`: `git log --format='%h|%ad|%an|%s' --date=short HEAD..<target>`, plus for each
     commit the touched files (`--name-only`).
   - `CPG_DIVERGENT`: `git diff --name-only $(git merge-base HEAD <target>) HEAD`.
   - `FLAGGED`: incoming commits whose files intersect CPG_DIVERGENT (commit -> files).
   - `DRYRUN_CONFLICTS`: `git merge-tree --write-tree --name-only HEAD <target>`; files listed
     after the tree oid; exit status 1 => conflicts.
   - `CHORES`: images added to `docker/third-party/images.txt` (mirror via skopeo into the CPG
     registry) and `WORKER_IMAGE_VERSION` bumps in `batch/gcp-create-worker-image.sh` (build
     batch-worker-NN on hail-dev); each with a read-only `check` and a `fix` command. Added 2026-10-05
     after two clean merges silently created out-of-repo deploy blockers.
   The skill presents: last sync line; N incoming commits summarised in plain language grouped by
   area tag (`[batch]`, `[query]`, `[ci]`...); flagged commits with the CPG files they touch and
   why that matters; conflicted files. Then AskUserQuestion: proceed / stop.

4. **Merge** — `git -c merge.conflictstyle=zdiff3 merge --no-commit <target>`. If exit 0 and no `UU` entries: go to 6.

5. **Per conflict** — for each `git diff --name-only --diff-filter=U` file, run
   `scripts/conflict_context.sh <file> <target>`; output sections:
   - `HUNKS`: working-tree file content between markers, numbered per hunk (zdiff3 => base shown).
   - `STAGES`: `git ls-files -u -- <file>` (which of base/ours/theirs exist; detects modify/delete).
   - `OURS_HISTORY`: (a) `git log <target>..HEAD -- <file>` = every CPG-only commit that ever touched
     the file (max 15); (b) `git blame --porcelain HEAD -- <file>` reduced to `sha<TAB>text`, matched
     by exact text against the non-trivial lines of our side of each hunk (blaming the working tree
     is unreliable while markers are present), unique shas filtered by
     `! git merge-base --is-ancestor <sha> <target>`, each printed via `git log -1`.
   - `THEIRS_HISTORY`: `git log --format='%h %ad %an%n%s%n%b%n---' $(merge-base)..<target> -- <file>`
     (squash-merged, so bodies carry the PR description).
   - `SIDE_DIFFS`: `git diff <merge-base> HEAD -- <file>` and `git diff <merge-base> <target> -- <file>`,
     whitespace-insensitive, each capped at 200 lines.
   For each hunk the skill writes, in this order: *Why ours differs* (CPG commit/PR + reason);
   *What upstream changed and why* (upstream PR + reason); *If we keep ours*; *If we take
   upstream*; *Recommendation*. Then AskUserQuestion with options Keep ours / Take upstream /
   Combine (proposed text) — each option carries a `preview` of the resulting lines. Related
   hunks in one file may be grouped into one question when the decision is clearly shared.
   Apply the choice by editing the file (remove markers), then `git add <file>`.
   Binary or add/add/delete conflicts: present `git log` for both sides and ask ours/theirs.

6. **Verify**
   - `git diff --cached --name-only | xargs grep -lE '^(<{7}|={7}|>{7})' ` must be empty.
   - For each resolved file: `git diff -w <target> -- <file>` shown as "what still differs from
     upstream after resolution"; the skill checks it matches the intended CPG divergence and
     flags anything unexpected.

7. **Commit** — propose `Merge upstream as of <D Month YYYY>` (today's date, no ordinal suffix,
   e.g. `Merge upstream as of 5 October 2026`). AskUserQuestion: use / edit. `git commit -m` with exactly that message. No `Co-Authored-By`, no "Generated with" or any other
   Claude attribution line, ever. The merge commit must look like a teammate wrote it.

8. **Push** — show `git log --oneline -1` and `git status -sb`; AskUserQuestion: push / not now.
   `git push origin <branch>`.

9. **Report** — final message: branch, upstream range merged (count, date span), conflicts and
   the decision + one-line rationale each, flagged clean-merged changes to eyeball, next step
   from team-docs (at release time: PR draft->main, terraform if `infra/` changed).

## Error handling

- Any stop/decline after step 4 began: AskUserQuestion `git merge --abort` vs leave in progress;
  if left, print the exact commands to finish by hand (`git add`, `git commit`, `git push`).
- Script failure: show stderr, offer abort. Never `reset --hard`, never force-push, never touch
  `main`, never auto-resolve a hunk.
- Fetch failure (network/auth): stop before any local change.

## Testing

- Happy path: real run now. `draft-sept26` is 12 commits behind `upstream/main` with no dry-run
  conflicts. Push only on Yash's yes.
- Conflict path: `scripts/test.sh` adds a throwaway worktree at a3e2b6490 (draft-sept26, 14 Sep),
  runs `incoming.sh 9746749c6` (upstream as of 18 Sep; asserts sections, `infra/gcp/main.tf` in
  DRYRUN_CONFLICTS, `build.yaml` flagged), merges 9746749c6 with zdiff3 to reproduce the conflict
  piyumi resolved in 2ebd3a71d, runs `conflict_context.sh infra/gcp/main.tf 9746749c6` (asserts
  sections, HUNK 1, base section, an upstream PR number), then aborts and removes the worktree.
  Also checks: 0-incoming case, non-zdiff3 markers, usage errors exit 2.
- Scripts have `set -euo pipefail`; a missing-arg call prints usage and exits 2.
