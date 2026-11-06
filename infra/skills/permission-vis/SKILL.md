---
name: permission-vis
description: Visualise how principals (service accounts, users, groups) get permissions on cloud resources through nested Google groups, using Pulumi state (e.g. prod-stack.json). Use when asked "how does X get write/read access to Y", "which buckets can these service accounts write to", "who can access this secret/bucket/project", to trace group hierarchies or IAM inheritance in a Pulumi stack, or to produce a permission-flow diagram/graphic like notebook_sa_write_flow.svg.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py *)
disable-model-invocation: true
---

# Permission flow visualisation from Pulumi state

Everything is one stdlib-only script bundled with this skill,
`${CLAUDE_SKILL_DIR}/scripts/permvis.py`, with four subcommands:

| Step | Command | Output |
|---|---|---|
| 1 | `extract STATE` | `graph.json`: groups, service accounts, memberships, IAM grants |
| 2 | `list GRAPH kinds\|roles\|resources\|principals` | text, for choosing filters |
| 3 | `analyze GRAPH --principal ... [--kind --role --resource --key ...]` | `pattern.json` (+ `--report` text with per-principal chains) |
| 4 | `render PATTERN` | SVG diagram |

Run `python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py <subcommand> -h` for every
flag. Shell variables don't persist between Bash calls, so write the full
script path in each command.

## Workflow

1. **Find the state file.** Usually `prod-stack.json` in the project root (a
   backend checkpoint, ~160 MB). `pulumi stack export` output also works. If
   you can't find one, ask the user; don't run `pulumi` commands yourself.
   Never print raw state: it contains secrets and infrastructure detail.

2. **Extract once, reuse.** Write outputs under `./permvis-out/` in the
   current project (never inside the skill directory), one sub-folder per
   question. Re-use an existing `./permvis-out/graph.json` if it is newer than
   the state file:
   ```sh
   mkdir -p permvis-out
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py extract prod-stack.json -o permvis-out/graph.json
   ```
   If the project is a git repo and `permvis-out/` isn't ignored, tell the
   user and suggest adding it to `.gitignore`.

3. **Translate the question into filters.** Explore before guessing:
   ```sh
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py list permvis-out/graph.json kinds
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py list permvis-out/graph.json roles --kind '^storage/bucket$'
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py list permvis-out/graph.json principals --match notebook
   ```
   - `--principal`: regex for the accounts being explained (matched on email).
   - `--kind`: regex on resource kind (`storage/bucket`, `secretmanager/secret`,
     `projects`, `serviceaccount`, `artifactregistry/repository`, `cloudrun`, ...).
   - `--role`: regex on the role. CPG buckets use custom org roles:
     write = `StorageObjectAndBucketMutator` (MUTATE) and
     `StorageViewerAndCreator` (APPEND, create-only); read =
     `StorageObjectAndBucketViewer`, `StorageLister`. Other kinds use
     predefined roles (e.g. `roles/secretmanager.secretAccessor`).
     Confirm with `list roles`; say which roles you counted as "write"/"read".
   - `--resource`: optional regex on resource name (e.g. `analysis`).
   - `--key`: regex with a `(?P<key>...)` group pulling a shared token (usually
     the dataset) out of each principal, e.g. `'^notebook-(?P<key>.+?)@'`. This
     turns many principals into one pattern: the own key becomes
     `--self-label` (e.g. `<ds>`), other keys become `--other-label` (e.g.
     `<dep>`), and keys reached by most principals stay literal (e.g. `common`;
     force with `--literal common`). Digit runs right after a placeholder are
     masked to `N` (`<ds>-full-945` and `projects/<ds>-670875` become
     `<ds>-full-N`, `<ds>-N`) so per-instance numbers don't split the pattern.
     Omit `--key` for one principal or a concrete (non-abstracted) diagram.
   - If dataset-like names stay concrete in the output (e.g. `g-pangenome-full`
     because the SA is called `google-pangenome-full-...`), add them with
     `--extra-keys g-pangenome ...` and re-run.

4. **Analyze** and sanity-check before drawing:
   ```sh
   mkdir -p permvis-out/nb-write
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py analyze permvis-out/graph.json \
     --principal '^notebook-.+@notebooks-' --key '^notebook-(?P<key>.+?)@' \
     --kind '^storage/bucket$' --role 'Mutator|Creator' \
     --self-label '<ds>' --other-label '<dep>' \
     --title 'How a notebook service account gets write access to buckets' \
     -o permvis-out/nb-write/pattern.json --report permvis-out/nb-write/access.txt
   ```
   Worked second example (hail full SAs reading secrets):
   `--principal '^[a-z0-9-]+-full-[0-9]+@hail-' --key '^(?P<key>[a-z0-9-]+?)-full-[0-9]+@'
   --kind secret --role secretAccessor`.

   Read `pattern.json` (`edges`, `grants`, `unreached`, `stats`) and spot-check
   a few principals in `access.txt` (it is large; grep it). If abstraction
   looks wrong (e.g. a key matched inside an unrelated name), tighten `--key`
   or pass `--literal` / `--extra-keys`.

5. **Render and look at it:**
   ```sh
   python3 ${CLAUDE_SKILL_DIR}/scripts/permvis.py render permvis-out/nb-write/pattern.json -o permvis-out/nb-write/flow.svg
   ```
   On macOS, preview the whole image (qlmanage crops to a square, so pad it
   first), then Read the PNG:
   ```sh
   cd permvis-out/nb-write && sed -E '1s/width="([0-9]+)" height="([0-9]+)"/width="\1" height="\1" preserveAspectRatio="xMinYMin meet"/' flow.svg > preview.svg \
     && qlmanage -t -s 2000 -o . preview.svg >/dev/null 2>&1 && sips -Z 1600 preview.svg.png >/dev/null
   ```
   Check for overlapping boxes, unreadable labels and clipped text.

6. **Polish only if asked or if the auto layout is confusing.** The rendered
   SVG is plain, hand-editable SVG.
   `${CLAUDE_SKILL_DIR}/examples/notebook_sa_write_flow.svg` is a
   hand-polished reference: three rows (own / common / dependencies), short
   explanatory notes under nodes, a grey "not writable" box and a legend.
   `notebook_sa_write_flow.generated.svg` beside it is the unpolished render of
   the same question. Copy the reference's styles and layout ideas, but every
   number or claim you add must come from `pattern.json` or `access.txt`. Save
   polished versions under a new name; never overwrite the generated file.

7. **Report back:** output paths, the main routes (which groups grant the
   access), outliers from `stats` (principals reaching far more resources),
   what is *not* reached and who holds it, plus the limitations below.

## Reading the diagram

- Purple: the principal(s). Blue: groups. Orange border: group holding a
  matching role. Green: resources, grouped by (holding group, role).
- Grey arrow: "is a member of". Loop under a node: nested inside another group
  of the same abstract shape (transitive dependencies). Dashed teal: membership
  of a group in the same or an earlier column (a parallel route).
- `n of N` on a node or box: only n of the N principals have that route.
- Grey box: in-scope resources whose name carries the principal's own key but
  are not reached, and which groups do hold the role. This answers "why can't
  it write to main?".

## Limitations (always mention the relevant ones)

- Only what this Pulumi state manages: no manual grants, other stacks, or
  org/folder-level IAM. Groups created by other stacks show as `groups/<id>`.
- Group memberships are followed transitively. Service-account impersonation
  (`roles/iam.serviceAccountUser` / `serviceAccountTokenCreator` on a
  `serviceaccount` resource) is **not** followed; check it separately with
  `--kind '^serviceaccount$'` if it matters.
- IAM conditions are ignored (grants are treated as unconditional).
- Memberships come from `gcp:cloudidentity/groupMembership` and CPG's dynamic
  `GoogleGroupMembership` resource (`group_key`/`member_key` outputs).

## Safety

Outputs list real emails, project IDs and access paths. Keep them in
`./permvis-out/` and out of git; don't paste them into tickets or chat unless
the user asks.

## Tests

This skill has an end-to-end test on a synthetic fixture (~1 s). Run it after
changing `permvis.py`:
`cd ${CLAUDE_SKILL_DIR} && python3 -m unittest discover tests`.
