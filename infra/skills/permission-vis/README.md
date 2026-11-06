# permission-vis

Skill in the `infra` plugin. It turns Pulumi state into diagrams showing how
principals (service accounts, users, groups) get permissions on resources
through nested Google groups.

```
infra/skills/permission-vis/
├── SKILL.md             # how Claude drives the pipeline
├── scripts/permvis.py   # the pipeline: extract | list | analyze | render (stdlib only)
├── examples/            # hand-polished and auto-rendered reference diagrams
└── tests/test_permvis.py  # end-to-end test on a synthetic state file
```

## Install

```sh
claude plugin marketplace add populationgenomics/software-skills
claude plugin install infra@populationgenomics --scope user
```

In a session: `/plugin marketplace add populationgenomics/software-skills`
then `/plugin install infra@populationgenomics`. The skill is invoked as
`/infra:permission-vis`, or loads automatically when you ask e.g. "which
buckets can the notebook service accounts write to, and through which
groups?".

Update after a new version is pushed:
`claude plugin update infra@populationgenomics`.

## Develop

From the repository root:

```sh
claude plugin validate ./infra
claude --plugin-dir ./infra    # load for one session; /reload-plugins after edits
```

From this skill directory:

```sh
python3 -m unittest discover tests
```

## Use without Claude

From this skill directory:

```sh
S=scripts/permvis.py
python3 $S extract prod-stack.json -o permvis-out/graph.json
python3 $S list permvis-out/graph.json roles --kind '^storage/bucket$'
python3 $S analyze permvis-out/graph.json \
  --principal '^notebook-.+@notebooks-' --key '^notebook-(?P<key>.+?)@' \
  --kind '^storage/bucket$' --role 'Mutator|Creator' \
  --self-label '<ds>' --other-label '<dep>' \
  -o permvis-out/pattern.json --report permvis-out/access.txt
python3 $S render permvis-out/pattern.json -o permvis-out/flow.svg
```

Outputs contain real emails, project IDs and access paths: keep
`permvis-out/` out of git (the repository `.gitignore` covers it).
