---
name: merge-upstream
description: Weekly merge of upstream hail-is/hail into the CPG fork's current draft branch. Digests incoming commits, explains every conflict from both sides' history, asks before each resolution, commits in team style, pushes only on request.
disable-model-invocation: true
argument-hint: "[upstream ref — default upstream/main]"
---

You are doing the CPG hail fork's weekly upstream merge (team-docs: hail.md, "Merging upstream
changes"). The user is on a roster and may not know the incoming upstream changes well. Do the
mechanics, explain what is coming in, and make every conflict decision an informed one — but the
**user** makes it. Never resolve a hunk without asking. Never `reset`, never force-push, never
touch `main`. Push only after an explicit yes.

Scripts live in `${CLAUDE_SKILL_DIR}/scripts/` (the directory of this SKILL.md; Claude Code substitutes
it). They are read-only.

Target ref: `$ARGUMENTS` if non-empty, otherwise `upstream/main`. Call it `<target>` below.

## 1. Preflight — stop at the first failure and say why
- `git remote get-url origin` must contain `populationgenomics/hail`.
- `git status --porcelain --untracked-files=no` must print nothing. Untracked files are fine.
- `git remote get-url upstream` — if missing, ask, then
  `git remote add upstream https://github.com/hail-is/hail.git`.
- `git fetch origin && git fetch upstream`.
- `git rev-parse --verify <target>^{commit}` must succeed.

## 2. Pick the draft branch
One long-lived `draft-*` branch per release cycle; weekly merges land on it until it is PR'd to
`main`. List the open ones:
```
for b in $(git for-each-ref --sort=-committerdate --format='%(refname:short)' 'refs/remotes/origin/draft-*'); do
  git merge-base --is-ancestor "$b" origin/main || echo "$b  $(git log -1 --format='%ad %an' --date=short "$b")"
done
```
AskUserQuestion: **use the newest listed (recommended)** / another listed one / **new branch**.
If the list is empty, skip straight to asking for a new name. Suggest `draft-<mon><yy>`
(e.g. `draft-oct26`).
- Existing: `git switch <name>` (without `origin/`), then `git pull --ff-only origin <name>`.
  If that fails, stop and show `git status -sb`.
- New: `git switch -c <name> origin/main`.

## 3. Digest — before anything changes
Run `${CLAUDE_SKILL_DIR}/scripts/incoming.sh <target>` and present:
- One line from LAST_SYNC `introduced_by`: when and by whom the branch was last synced, and the
  INCOMING `count`.
- If `count: 0`: say the branch is already up to date with `<target>` and stop.
- Incoming commits grouped by area tag (`[batch]`, `[query]`, `[ci]`, `[services]`, other), one
  plain-language line each: what it does for a Hail user or operator, not the subject verbatim.
  Run `git show --stat <sha>` or read the commit body when the subject is not self-explanatory.
- FLAGGED: commits that touch files where our fork differs from upstream. For each, name the file
  and what CPG keeps there (`git log --format='%h %s' <target>..HEAD -- <file> | head -3` tells
  you). These merge cleanly but can silently drop CPG behaviour — the user should eyeball them
  after the merge.
- DRYRUN_CONFLICTS: files that will conflict.
- CHORES: upstream bumped a base artifact (container base image in `docker/third-party/images.txt`,
  or the `batch-worker-NN` VM boot image) that CPG must re-materialise in hail-295901. For each,
  run the printed `check` command (read-only gcloud) and say whether the artifact already exists;
  if not, give the printed `fix` command. Chores do not block the merge, they block deploy.
AskUserQuestion: proceed with the merge / stop.

## 4. Merge
```
git -c merge.conflictstyle=zdiff3 merge --no-commit <target>
```
(`-c` guarantees a base section in every conflict marker regardless of the user's gitconfig.)
If the merge exits non-zero and `git diff --name-only --diff-filter=U` prints nothing, the merge
failed for a reason other than conflicts: stop, show the error, do not continue to commit.
If the merge exits zero and `--diff-filter=U` prints nothing, go to step 6.

## 5. Each conflicted file
For each path from `git diff --name-only --diff-filter=U`, run
`${CLAUDE_SKILL_DIR}/scripts/conflict_context.sh <path> <target>` and read every section. Then for each
HUNK write, in this order, two to four sentences each:
1. **Why ours differs** — the CPG commit/PR in OURS_HISTORY that introduced our lines and the
   reason its message gives. If OURS_HISTORY is empty, say so: our side is probably an older
   upstream state we never updated, not a deliberate CPG change.
2. **What upstream changed and why** — the upstream PR in THEIRS_HISTORY, in plain language.
3. **If we keep ours** — what upstream behaviour or fix we would miss, and any later upstream code
   that may now assume the new version.
4. **If we take upstream** — which CPG behaviour we would lose, and whether CPG still needs it
   (CPG-only deploy config, pool labels, billing, auth domains almost always still matter).
5. **Recommendation** — one line.
Then AskUserQuestion, one question per hunk (group hunks in one file only when the decision is
obviously shared), options **Keep ours** / **Take upstream** / **Combine**, recommended option
first, each with a `preview` of the exact lines that will end up in the file. Apply the chosen
text by editing the file so that hunk's markers are gone. When every hunk in the file is done,
`git add <path>`.
Edge cases: STAGES missing stage 2 or 3 means modify/delete — ask keep-modified / delete using
the histories, then `git add` or `git rm`. No HUNKS and the file exists (binary): ask ours /
theirs, apply with `git checkout --ours -- <path>` or `--theirs`, then `git add`.

## 6. Verify
- `git diff --cached --name-only --diff-filter=d | xargs grep -nE '^(<{7} |\|{7} |={7}$|>{7} )'` must print nothing.
- For each resolved file, `git diff -w <target> -- <path>` is "what still differs from upstream".
  Confirm it is exactly the CPG divergence the user chose to keep and call out anything else.

## 7. Commit
Propose `Merge upstream as of <D Month YYYY>` using today's date, no ordinal suffix
(e.g. `Merge upstream as of 5 October 2026`). AskUserQuestion: use it / edit it. Then
`git commit -m "<message>"` — exactly that message and nothing else. Do **not** add
`Co-Authored-By`, "Generated with", or any attribution line.

## 8. Push
Show `git log --oneline -1` and `git status -sb`. AskUserQuestion: push / not now.
On yes: `git push origin <branch>`. If the push is rejected (a teammate pushed to the branch
meanwhile), stop and show the error. Never `--force`. Tell the user the fix is
`git pull --no-rebase origin <branch>` (a rebase would rewrite the merge commit), then step 8 again.

## 9. Report
Final message: branch; range merged (count, first and last upstream dates); each conflict with
the decision and a one-line reason; FLAGGED clean merges the user should eyeball; outstanding CHORES with their fix commands; next step
(weekly: nothing more; at release time: open the draft→main PR, and run terraform plan/apply on
the hail-setup VM first if `infra/` changed — see team-docs hail.md).

## If a script fails or the user stops mid-merge
If a script exits non-zero, show its stderr and ask: abort the merge / continue by hand.
AskUserQuestion: `git merge --abort` / leave the merge in progress. If left, print the commands to
finish by hand: `git add <paths>`, `git commit -m "Merge upstream as of <date>"`,
`git push origin <branch>`.
