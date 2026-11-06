# software-skills

Skills for coding agents, written and maintained by the [Centre for Population Genomics](https://populationgenomics.org.au).

A skill is a folder holding a `SKILL.md` with instructions, plus any scripts and reference files the agent needs. Agents load a skill on demand, so the instructions cost nothing until they are used. The format follows the [Agent Skills standard](https://agentskills.io), so these skills work in Claude Code and in other agents that read `SKILL.md`.

Skills are grouped by domain into [Claude Code plugins](https://docs.claude.com/en/docs/claude-code/plugins). This repository is also a plugin marketplace, so a plugin and every skill in it can be installed with two commands.

## Skills

| Plugin | Skill | What it does |
| --- | --- | --- |
| `hail` | [`merge-upstream`](hail/skills/merge-upstream/SKILL.md) | The weekly merge of upstream `hail-is/hail` into our fork's draft branch. Digests incoming commits, flags ones that touch files where our fork differs, explains every conflict from both sides' history, lets you decide each hunk, and lists the out-of-repo chores a merge creates (image mirrors, worker VM images). |

## Installing

### Claude Code

Register this repository as a marketplace once, then install the plugins you want:

```
/plugin marketplace add populationgenomics/software-skills
/plugin install hail@populationgenomics
```

Plugin skills are namespaced, so the skill above is invoked as `/hail:merge-upstream`. Run `/plugin` to see installed plugins, check for updates, or uninstall.

### Other agents, or without plugins

Clone the repository and point your agent at a skill folder. For Claude Code without plugins, symlink a skill into your personal skills directory:

```bash
git clone git@github.com:populationgenomics/software-skills.git
ln -s "$PWD/software-skills/hail/skills/merge-upstream" ~/.claude/skills/merge-upstream
```

Skills reference their own scripts through `${CLAUDE_SKILL_DIR}`, so they work the same whether installed as a plugin, a personal skill, or a project skill.

## Repository layout

```
software-skills/
├── .claude-plugin/
│   └── marketplace.json        # lists the plugins in this repository
├── hail/                       # one plugin per domain
│   ├── .claude-plugin/
│   │   └── plugin.json
│   └── skills/
│       └── merge-upstream/
│           ├── SKILL.md        # the instructions the agent follows
│           ├── DESIGN.md       # why it works the way it does
│           └── scripts/        # helpers the skill runs, plus a self-test
├── LICENSE
└── README.md
```

## Adding a skill

1. Pick the plugin that matches the domain, or create a new one: a folder at the repository root with `.claude-plugin/plugin.json` and a `skills/` directory, plus an entry in `.claude-plugin/marketplace.json`.
2. Create `<plugin>/skills/<skill-name>/SKILL.md`. Start with frontmatter:

   ```yaml
   ---
   name: skill-name
   description: One sentence saying what the skill does and when to use it. The agent reads this to decide when the skill applies.
   disable-model-invocation: true   # only for skills a person should start deliberately
   ---
   ```

   Then write the procedure. Be concrete about commands, what to show the user, and where to stop and ask. Say what the skill must never do.
3. Put helper scripts in `scripts/` and reference them as `${CLAUDE_SKILL_DIR}/scripts/<name>`. Keep scripts read-only where you can, and leave a runnable self-test behind (`scripts/test.sh` in `merge-upstream` is an example).
4. Add the skill to the table in this README.
5. Run `claude plugin validate .` from the repository root, open a pull request, and ask someone who will use the skill to try it.

A good skill is written for a capable engineer who does not know our systems: it names the files, the commands, and the people to ask, and it explains the why when the what is surprising. The [skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) are worth a read before your first one.

## License

[MIT](LICENSE)
