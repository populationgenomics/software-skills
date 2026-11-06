#!/usr/bin/env python3
"""
permvis: visualise how principals get permissions on resources, from Pulumi state.

Pipeline (stdlib only):
    extract  Pulumi state JSON -> graph.json (group memberships + IAM grants)
    list     explore graph.json (resource kinds, roles, principals)
    analyze  graph.json -> pattern.json (+ optional per-principal text report)
    render   pattern.json -> SVG

Example (notebook service accounts writing to buckets):
    python permvis.py extract prod-stack.json -o graph.json
    python permvis.py list graph.json roles --kind storage/bucket
    python permvis.py analyze graph.json \
        --principal '^notebook-.+@notebooks-' --key '^notebook-(?P<key>.+?)@' \
        --kind '^storage/bucket$' --role 'Mutator|Creator' \
        --self-label '<ds>' --other-label '<dep>' \
        -o pattern.json --report access.txt
    python permvis.py render pattern.json -o flow.svg
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

GROUP_TYPE = 'gcp:cloudidentity/group:Group'
NATIVE_MEMBERSHIP_TYPE = 'gcp:cloudidentity/groupMembership:GroupMembership'
SERVICE_ACCOUNT_TYPE = 'gcp:serviceaccount/account:Account'
IAM_TYPE = re.compile(r'iam(member|binding|policy)$', re.IGNORECASE)
# Output fields that never name the resource an IAM grant is attached to.
NON_RESOURCE_FIELDS = {
    'condition', 'etag', 'id', 'member', 'members', 'role', 'policyData',
    'location', 'project', 'urn',
}
RESOURCE_PREFIXES = ('b/', 'v1/')


def load_state_resources(path: Path) -> list[dict]:
    """Read resources from a backend checkpoint or `pulumi stack export` file."""
    with path.open() as f:
        data = json.load(f)
    if 'checkpoint' in data:
        return data['checkpoint'].get('latest', {}).get('resources') or []
    if 'deployment' in data:
        return data['deployment'].get('resources') or []
    raise SystemExit(f'{path}: not a Pulumi state file (no checkpoint/deployment key)')


def split_member(member: str) -> tuple[str, str]:
    """'group:X@y' -> ('group', 'x@y'). Unprefixed values are kept verbatim."""
    if ':' in member and not member.startswith('principal'):
        kind, value = member.split(':', 1)
        return kind, value.lower()
    return 'other', member


def iam_kind(rtype: str) -> str:
    """'gcp:storage/bucketIAMMember:BucketIAMMember' -> 'storage/bucket'."""
    parts = rtype.split(':')
    module, _, res = (parts[1] if len(parts) > 1 else rtype).partition('/')
    res = IAM_TYPE.sub('', res)
    return f'{module}/{res}' if res else module


def iam_resource_name(outputs: dict) -> str:
    """Name of the resource an IAM grant targets (bucket, secret, project, ...)."""
    for key in sorted(outputs):
        value = outputs[key]
        if key in NON_RESOURCE_FIELDS or key.startswith('__'):
            continue
        if isinstance(value, str) and value:
            for prefix in RESOURCE_PREFIXES:
                value = value.removeprefix(prefix)
            return value
    project = outputs.get('project')
    return project if isinstance(project, str) else '?'


def iam_bindings(outputs: dict) -> list[tuple[str, list[str]]]:
    """(role, members) pairs for IAMMember, IAMBinding and IAMPolicy outputs."""
    if isinstance(outputs.get('role'), str):
        if isinstance(outputs.get('member'), str):
            return [(outputs['role'], [outputs['member']])]
        if isinstance(outputs.get('members'), list):
            return [(outputs['role'], [m for m in outputs['members'] if isinstance(m, str)])]
    policy = outputs.get('policyData')
    if isinstance(policy, str):
        try:
            bindings = json.loads(policy).get('bindings', [])
        except json.JSONDecodeError:
            return []
        return [(b['role'], b.get('members', [])) for b in bindings if 'role' in b]
    return []


def extract(state: Path) -> dict:
    resources = load_state_resources(state)
    group_emails: dict[str, str] = {}
    raw_memberships: list[tuple[str, str]] = []
    service_accounts: set[str] = set()
    grants: set[tuple[str, str, str, str, str]] = set()

    for r in resources:
        rtype = r.get('type', '')
        out = r.get('outputs') or {}
        if rtype == GROUP_TYPE:
            email = (out.get('groupKey') or {}).get('id')
            if email:
                for gid in (out.get('id'), out.get('name')):
                    if gid:
                        group_emails[gid] = email.lower()
        elif rtype == SERVICE_ACCOUNT_TYPE:
            if isinstance(out.get('email'), str):
                service_accounts.add(out['email'].lower())
        elif rtype == NATIVE_MEMBERSHIP_TYPE:
            keys = out.get('preferredMemberKey') or out.get('preferredMemberKeys') or []
            if isinstance(keys, dict):
                keys = [keys]
            if keys and out.get('group'):
                raw_memberships.append((out['group'], keys[0]['id']))
        elif 'group_key' in out and 'member_key' in out:
            # CPG's dynamic GoogleGroupMembership resource.
            raw_memberships.append((out['group_key'], out['member_key']))
        elif IAM_TYPE.search(rtype.split(':')[1] if ':' in rtype else rtype):
            kind = iam_kind(rtype)
            name = iam_resource_name(out)
            for role, members in iam_bindings(out):
                for m in members:
                    ptype, principal = split_member(m)
                    grants.add((ptype, principal, kind, name, role))

    unresolved = set()
    memberships = set()
    for group, member in raw_memberships:
        if group not in group_emails:
            unresolved.add(group)
        memberships.add((member.lower(), group_emails.get(group, group)))
    if unresolved:
        print(
            f'warning: {len(unresolved)} group(s) referenced by memberships are not '
            'defined in this state; they are kept under their group ID',
            file=sys.stderr,
        )

    return {
        'source': str(state),
        'groups': sorted(set(group_emails.values())),
        'service_accounts': sorted(service_accounts),
        'memberships': sorted([list(m) for m in memberships]),
        'grants': sorted([list(g) for g in grants]),
    }


def cmd_extract(args) -> None:
    graph = extract(args.state)
    args.output.write_text(json.dumps(graph, indent=1))
    print(
        f'wrote {args.output}: {len(graph["groups"])} groups, '
        f'{len(graph["service_accounts"])} service accounts, '
        f'{len(graph["memberships"])} memberships, {len(graph["grants"])} grants',
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def load_graph(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def all_principals(graph: dict) -> dict[str, str]:
    """principal -> type ('group', 'serviceAccount', 'user', ...)."""
    types: dict[str, str] = {}
    for member, group in graph['memberships']:
        types.setdefault(member, 'member')
        types[group] = 'group'
    for ptype, principal, *_ in graph['grants']:
        types.setdefault(principal, ptype)
        if types[principal] == 'member':
            types[principal] = ptype
    for g in graph['groups']:
        types[g] = 'group'
    for sa in graph['service_accounts']:
        types[sa] = 'serviceAccount'
    for p, t in types.items():
        if t == 'member':
            types[p] = 'serviceAccount' if p.endswith('gserviceaccount.com') else 'user'
    return types


def cmd_list(args) -> None:
    graph = load_graph(args.graph)
    kind_re = re.compile(args.kind) if args.kind else None
    match_re = re.compile(args.match) if args.match else None
    grants = [g for g in graph['grants'] if not kind_re or kind_re.search(g[2])]

    if args.what == 'kinds':
        rows = Counter(g[2] for g in grants).most_common()
        print('\n'.join(f'{n:7d}  {k}' for k, n in rows))
    elif args.what == 'roles':
        rows = Counter((g[2], g[4]) for g in grants).most_common()
        print('\n'.join(f'{n:7d}  {k:30s} {r}' for (k, r), n in rows))
    elif args.what == 'resources':
        names = sorted({(g[2], g[3]) for g in grants if not match_re or match_re.search(g[3])})
        print('\n'.join(f'{k:30s} {n}' for k, n in names))
    else:
        types = all_principals(graph)
        for p in sorted(types):
            if not match_re or match_re.search(p):
                print(f'{types[p]:16s} {p}')


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


class Templater:
    """Replaces dataset-like keys in names with placeholders.

    The principal's own key becomes `self_label`; keys in `literal` stay as is;
    any other known key becomes `other_label`. Longest key wins, and keys only
    match on whole tokens (bounded by non-alphanumerics).
    """

    def __init__(self, keys, literal, self_label, other_label):
        self.literal = set(literal)
        self.self_label = self_label
        self.other_label = other_label
        alternation = '|'.join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        self.pattern = (
            re.compile(rf'(?<![a-z0-9])({alternation})(?![a-z0-9])') if keys else None
        )
        labels = '|'.join(re.escape(x) for x in {self_label, other_label} if x)
        self.mask = (
            re.compile(rf'((?:{labels})[a-z0-9_-]*?-)\d{{3,}}(?![a-z0-9])') if labels else None
        )

    def keys_in(self, name: str) -> list[str]:
        return self.pattern.findall(name) if self.pattern else []

    def __call__(self, name: str, own: str | None) -> str:
        if not self.pattern or own is None:
            return name

        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key == own:
                return self.self_label
            if key in self.literal:
                return key
            return self.other_label

        out = self.pattern.sub(repl, name)
        # Per-instance numbers next to a placeholder (e.g. '<ds>-full-945',
        # 'projects/<ds>-670875') would stop principals collapsing into one
        # pattern, so mask them.
        return self.mask.sub(r'\1N', out) if self.mask else out


def short_role(role: str) -> str:
    return role.rsplit('/', 1)[-1]


def analyze(graph: dict, args) -> tuple[dict, str]:
    principal_re = re.compile(args.principal)
    kind_re = re.compile(args.kind) if args.kind else None
    role_re = re.compile(args.role) if args.role else None
    resource_re = re.compile(args.resource) if args.resource else None
    key_re = re.compile(args.key) if args.key else None

    types = all_principals(graph)
    groups = set(graph['groups'])

    parents: dict[str, set[str]] = defaultdict(set)
    children: dict[str, set[str]] = defaultdict(set)
    for member, group in graph['memberships']:
        parents[member].add(group)
        children[group].add(member)

    # Grants on in-scope resources: role-matching ones give access; the rest
    # are only used to say who holds the role on resources that aren't reached.
    in_scope = [
        g for g in graph['grants']
        if (not kind_re or kind_re.search(g[2]))
        and (not resource_re or resource_re.search(g[3]))
    ]
    access_grants: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    holders: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    scope_resources: set[tuple[str, str]] = set()
    for _, principal, kind, resource, role in in_scope:
        scope_resources.add((kind, resource))
        if not role_re or role_re.search(role):
            access_grants[principal].append((kind, resource, role))
            holders[(kind, resource)].add((principal, role))

    principals = sorted(p for p in types if principal_re.search(p))
    if not principals:
        raise SystemExit(f'no principals match {args.principal!r}')

    keys: dict[str, str | None] = {}
    for p in principals:
        m = key_re.search(p) if key_re else None
        if key_re and not (m and 'key' in m.groupdict()):
            print(f'warning: --key did not match {p}; it is not abstracted', file=sys.stderr)
        keys[p] = m.group('key') if m and 'key' in m.groupdict() else None

    per_principal = []
    for p in principals:
        # BFS up the group hierarchy, keeping the shortest chain to each group.
        chains = {p: [p]}
        queue = deque([p])
        while queue:
            node = queue.popleft()
            for parent in sorted(parents.get(node, ())):
                if parent not in chains:
                    chains[parent] = [*chains[node], parent]
                    queue.append(parent)

        grantees = {n for n in chains if access_grants.get(n)}
        # Keep only groups on some route to a grant: walk down from grantees.
        relevant = set(grantees)
        stack = list(grantees)
        while stack:
            node = stack.pop()
            for child in children.get(node, ()):
                if child in chains and child not in relevant:
                    relevant.add(child)
                    stack.append(child)

        edges = {
            (c, g) for c in relevant for g in parents.get(c, ()) if g in relevant
        }
        grants = [(n, *g) for n in sorted(grantees) for g in access_grants[n]]
        reached = {(k, r) for _, k, r, _ in grants}
        per_principal.append({
            'principal': p,
            'key': keys[p],
            'chains': chains,
            'nodes': relevant | {p},
            'edges': edges,
            'grants': grants,
            'reached': reached,
        })

    # --- abstraction -------------------------------------------------------
    known_keys = {k for k in keys.values() if k} | set(args.extra_keys or [])
    pre = Templater(known_keys, [], '', '')
    mentions: Counter = Counter()
    for info in per_principal:
        seen = set()
        for name in [*info['nodes'], *(r for _, _, r, _ in info['grants'])]:
            seen.update(k for k in pre.keys_in(name) if k != info['key'])
        mentions.update(seen)
    literal = set(args.literal or [])
    if key_re:
        literal |= {
            k for k, n in mentions.items()
            if n > args.literal_threshold * len(per_principal)
        }
    templ = Templater(known_keys, literal, args.self_label, args.other_label)

    def display(name: str) -> str:
        if name in groups or types.get(name) == 'group':
            return name.split('@', 1)[0]
        return name

    node_counts: dict[str, set[str]] = defaultdict(set)
    node_types: dict[str, str] = {}
    edge_counts: dict[tuple[str, str], set[str]] = defaultdict(set)
    grant_boxes: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    unreached: dict[str, dict] = defaultdict(lambda: {'principals': set(), 'holders': set()})
    roots: set[str] = set()

    for info in per_principal:
        p, own = info['principal'], info['key']

        def ab(name: str, own=own) -> str:
            return templ(display(name), own)

        roots.add(ab(p))
        for n in info['nodes']:
            node_counts[ab(n)].add(p)
            node_types[ab(n)] = 'principal' if n == p else types.get(n, 'group')
        for c, g in info['edges']:
            edge_counts[(ab(c), ab(g))].add(p)
        for grantee, _, resource, role in info['grants']:
            grant_boxes[(ab(grantee), short_role(role))][ab(resource)].add(p)

        if own:
            for kind, resource in scope_resources - info['reached']:
                if own in pre.keys_in(resource):
                    entry = unreached[ab(resource)]
                    entry['principals'].add(p)
                    for holder, role in holders.get((kind, resource), ()):
                        entry['holders'].add(f'{ab(holder)} ({short_role(role)})')

    total = len(per_principal)
    group_labels = {n for n, t in node_types.items() if t == 'group'} | {
        templ(display(g), k) for g in groups for k in {i['key'] for i in per_principal}
    }
    pattern = {
        'title': args.title or 'How principals reach permissions on resources',
        'subtitle': ' | '.join(filter(None, [
            f'Source: {Path(graph["source"]).name}',
            f'{total} principal(s) matching {args.principal}',
            f'kind ~ {args.kind}' if args.kind else '',
            f'role ~ {args.role}' if args.role else '',
            f'resource ~ {args.resource}' if args.resource else '',
        ])),
        'total_principals': total,
        'labels': {'self': args.self_label, 'other': args.other_label},
        'literal_keys': sorted(literal),
        'roots': sorted(roots),
        'nodes': [
            {'id': n, 'type': node_types[n], 'count': len(ps)}
            for n, ps in sorted(node_counts.items())
        ],
        'edges': [
            {'source': s, 'target': t, 'count': len(ps)}
            for (s, t), ps in sorted(edge_counts.items())
        ],
        'grants': [
            {
                'grantee': grantee,
                'role': role,
                'count': len(set().union(*res.values())),
                'resources': [
                    {'name': r, 'count': len(ps)} for r, ps in sorted(res.items())
                ],
            }
            for (grantee, role), res in sorted(grant_boxes.items())
        ],
        'unreached': [
            {
                'resource': r,
                'count': len(e['principals']),
                # Group holders explain *why* access isn't reached; individual
                # accounts are listed only when no group holds the role.
                'holders': sorted(
                    [h for h in e['holders'] if h.split(' (')[0] in group_labels]
                    or e['holders'],
                ),
            }
            for r, e in sorted(unreached.items())
        ],
        'stats': {
            'grants_per_principal': dict(
                sorted(Counter(len(i['reached']) for i in per_principal).items()),
            ),
            'max': sorted(
                ((len(i['reached']), i['principal']) for i in per_principal),
                reverse=True,
            )[:5],
        },
    }

    report_lines = [
        pattern['title'],
        pattern['subtitle'],
        'Each entry shows the shortest membership chain giving the access.',
        '',
    ]
    for info in per_principal:
        best: dict[tuple[str, str, str], list[str]] = {}
        for grantee, kind, resource, role in info['grants']:
            chain = info['chains'][grantee]
            key = (resource, kind, short_role(role))
            if key not in best or len(chain) < len(best[key]):
                best[key] = chain
        report_lines.append(f'{info["principal"]} ({len(best)} grants)')
        if not best:
            report_lines.append('    (none)')
        for (resource, kind, role), chain in sorted(best.items()):
            via = 'direct grant' if len(chain) == 1 else ' -> '.join(chain[1:])
            report_lines.append(f'    - {resource}  [{kind}] ({role})')
            report_lines.append(f'        via: {via}')
        report_lines.append('')

    return pattern, '\n'.join(report_lines)


def cmd_analyze(args) -> None:
    pattern, report = analyze(load_graph(args.graph), args)
    args.output.write_text(json.dumps(pattern, indent=1))
    print(
        f'wrote {args.output}: {pattern["total_principals"]} principals, '
        f'{len(pattern["nodes"])} abstract nodes, {len(pattern["grants"])} grant boxes, '
        f'{len(pattern["unreached"])} unreached resources',
    )
    if args.report:
        args.report.write_text(report)
        print(f'wrote {args.report}')


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

CHAR_W = 7.3  # approx width of a 12-13px monospace glyph
NODE_H = 46
COL_GAP = 90
ROW_GAP = 34
MARGIN = 30
TOP = 110
STYLE = """
  .mono { font-family: Menlo, Consolas, monospace; }
  .principal { fill: #ede9fe; stroke: #6d28d9; stroke-width: 1.5; }
  .group { fill: #e0f2fe; stroke: #0369a1; stroke-width: 1.5; }
  .grantee { fill: #e0f2fe; stroke: #c2410c; stroke-width: 2.5; }
  .resource { fill: #dcfce7; stroke: #15803d; stroke-width: 1.5; }
  .off { fill: #f3f4f6; stroke: #9ca3af; stroke-width: 1.5; stroke-dasharray: 5 4; }
  .member { stroke: #4b5563; stroke-width: 1.7; fill: none; marker-end: url(#a-member); }
  .back { stroke: #0f766e; stroke-width: 1.5; fill: none; stroke-dasharray: 6 4; marker-end: url(#a-back); }
  .grant { stroke: #c2410c; stroke-width: 2.3; fill: none; marker-end: url(#a-grant); }
  .note { fill: #4b5563; font-size: 11px; }
  .h { font-weight: bold; }
"""


def marker(mid: str, colour: str) -> str:
    return (
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        f'fill="{colour}"/></marker>'
    )


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(n - 1, 1)] + '…'


def text_w(s: str, char_w: float = CHAR_W) -> float:
    return len(s) * char_w


def place_column(items: list[dict], top: float) -> None:
    """Greedy vertical placement around each item's desired centre, then shift
    the column so its block is centred on the mean desired centre."""
    if not items:
        return
    items.sort(key=lambda i: (round(i['want']), -i.get('count', 0), i['id']))
    bottom = top - ROW_GAP
    for it in items:
        it['y'] = max(it['want'] - it['h'] / 2, bottom + ROW_GAP)
        bottom = it['y'] + it['h'] + it.get('pad', 0)
    mean_want = sum(i['want'] for i in items) / len(items)
    block_centre = (items[0]['y'] + bottom) / 2
    shift = max(mean_want - block_centre, top - items[0]['y'])
    for it in items:
        it['y'] += shift


def render(pattern: dict) -> str:
    total = pattern['total_principals']
    nodes = {n['id']: dict(n) for n in pattern['nodes']}
    edges = [e for e in pattern['edges'] if e['source'] in nodes and e['target'] in nodes]
    grantees = {g['grantee'] for g in pattern['grants']}

    # Layer by shortest distance from the principal node(s).
    out_edges: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e['source'] != e['target']:
            out_edges[e['source']].append(e['target'])
    depth = {r: 0 for r in pattern['roots'] if r in nodes}
    queue = deque(depth)
    while queue:
        n = queue.popleft()
        for t in out_edges[n]:
            if t not in depth:
                depth[t] = depth[n] + 1
                queue.append(t)
    for n in nodes:
        depth.setdefault(n, 1)
    # Put every role-holding group in the last group column so grant arrows
    # never pass behind other nodes.
    if grantees & set(nodes):
        last = max(depth.values())
        for g in grantees & set(nodes):
            if g not in pattern['roots']:
                depth[g] = last

    self_loops = {e['source'] for e in edges if e['source'] == e['target']}
    for n in nodes.values():
        local, at, domain = n['id'].partition('@')
        n['label'] = local
        n['note'] = '  '.join(filter(None, [
            f'@{domain}' if at else '',
            f'{n["count"]} of {total}' if n['count'] < total else '',
        ]))
        n['pad'] = 46 if n['id'] in self_loops else 0
        n['w'] = max(150, text_w(n['label']) + 34, text_w(n['note'], 6.2) + 24)
        n['h'] = NODE_H + (16 if n['note'] else 0)
        n['cls'] = (
            'principal' if n['id'] in pattern['roots']
            else 'grantee' if n['id'] in grantees else 'group'
        )

    ncols = max(depth.values()) + 1
    col_w = [0.0] * ncols
    for n in nodes.values():
        col_w[depth[n['id']]] = max(col_w[depth[n['id']]], n['w'])
    col_x = []
    x = MARGIN
    for w in col_w:
        col_x.append(x)
        x += w + COL_GAP
    res_x = x

    # Vertical placement: column by column, aiming at parents' centres.
    preds: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e['source'] != e['target']:
            preds[e['target']].append(e['source'])
    for c in range(ncols):
        col = [n for n in nodes.values() if depth[n['id']] == c]
        for n in col:
            ys = [
                nodes[p]['y'] + nodes[p]['h'] / 2
                for p in preds[n['id']] if depth[p] < c and 'y' in nodes[p]
            ]
            n['want'] = sum(ys) / len(ys) if ys else TOP
        place_column(col, TOP)
        for n in col:
            n['x'] = col_x[c] + (col_w[c] - n['w']) / 2

    # Resource boxes: one per (grantee, role), next to the grantee.
    boxes = []
    for g in pattern['grants']:
        lines = [f'{r["name"]}' + (f'  ({r["count"]}/{total})' if r['count'] < total else '')
                 for r in g['resources']]
        head = f'{g["role"]}' + (f'  - {g["count"]} of {total}' if g['count'] < total else '')
        w = max(text_w(head, 7.0), *(text_w(s, 7.0) for s in lines)) + 30
        gn = nodes.get(g['grantee'])
        boxes.append({
            'id': g['grantee'] + g['role'], 'grantee': g['grantee'], 'head': head,
            'lines': lines, 'w': w, 'h': 30 + 16 * len(lines),
            'want': gn['y'] + gn['h'] / 2 if gn else TOP,
        })
    place_column(boxes, TOP)
    res_w = max([b['w'] for b in boxes] or [0])
    for b in boxes:
        b['x'] = res_x

    width = max(res_x + res_w + MARGIN, 900)
    body_bottom = max(
        [n['y'] + n['h'] + n['pad'] for n in nodes.values()]
        + [b['y'] + b['h'] for b in boxes] + [TOP],
    )

    svg: list[str] = []
    add = svg.append

    def skip_lane(s, end_col, sy, ty):
        """For an edge skipping columns, a free y (and x span) avoiding boxes
        in the skipped columns, or None if the direct curve is clear."""
        cols = range(depth[s['id']] + 1, end_col)
        blockers = [n for n in nodes.values() if depth[n['id']] in cols]
        if not blockers:
            return None
        spans = [(n['y'] - 8, n['y'] + n['h'] + n['pad'] + 8) for n in blockers]
        lo, hi = min(sy, ty), max(sy, ty)
        if not any(a < hi and b > lo for a, b in spans):
            return None
        candidates = [min(a for a, _ in spans) - 10] + [b + 10 for _, b in spans]
        free = [c for c in candidates if not any(a < c < b for a, b in spans)]
        ly = min(free, key=lambda c: abs(c - (sy + ty) / 2))
        xa = col_x[cols[0]] - 24
        xb = col_x[cols[-1]] + col_w[cols[-1]] + 24
        return ly, xa, xb

    # Membership edges, with ports spread so parallel edges don't overlap.
    out_count = Counter(e['source'] for e in edges if e['source'] != e['target'])
    in_count = Counter(e['target'] for e in edges if e['source'] != e['target'])
    out_i: Counter = Counter()
    in_i: Counter = Counter()
    for e in sorted(edges, key=lambda e: (nodes[e['source']]['y'], nodes[e['target']]['y'])):
        s, t = nodes[e['source']], nodes[e['target']]
        if s is t:
            cx, by = s['x'] + s['w'] / 2, s['y'] + s['h']
            add(f'<path class="member" d="M{cx - 30},{by} C{cx - 30},{by + 42} '
                f'{cx + 30},{by + 42} {cx + 30},{by + 2}"/>')
            continue
        so = (out_i[e['source']] - (out_count[e['source']] - 1) / 2) * 8
        ti = (in_i[e['target']] - (in_count[e['target']] - 1) / 2) * 8
        out_i[e['source']] += 1
        in_i[e['target']] += 1
        sy, ty = s['y'] + s['h'] / 2 + so, t['y'] + t['h'] / 2 + ti
        if depth[t['id']] > depth[s['id']]:
            sx, tx = s['x'] + s['w'], t['x'] - 2
            lane = skip_lane(s, depth[t['id']], sy, ty)
            if lane is None:
                mx = (sx + tx) / 2
                add(f'<path class="member" d="M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}"/>')
            else:
                # Skips columns with boxes in the way: run along a free lane.
                ly, xa, xb = lane
                ma, mb = (sx + xa) / 2, (xb + tx) / 2
                add(f'<path class="member" d="M{sx},{sy} C{ma},{sy} {ma},{ly} {xa},{ly} '
                    f'L{xb},{ly} C{mb},{ly} {mb},{ty} {tx},{ty}"/>')
        else:
            # Same or earlier column: route around via the right-hand gap.
            sx = s['x'] + s['w']
            gx = col_x[depth[s['id']]] + col_w[depth[s['id']]] + COL_GAP / 2
            tx = t['x'] + t['w'] + 2
            add(f'<path class="back" d="M{sx},{sy} C{gx},{sy} {gx},{ty} {tx},{ty}"/>')

    for b in boxes:
        gn = nodes.get(b['grantee'])
        if gn:
            sy = gn['y'] + gn['h'] / 2
            ty = b['y'] + b['h'] / 2
            sx, tx = gn['x'] + gn['w'], b['x'] - 2
            lane = skip_lane(gn, ncols, sy, ty)
            if lane is None:
                mx = (sx + tx) / 2
                add(f'<path class="grant" d="M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}"/>')
            else:
                ly, xa, xb = lane
                ma, mb = (sx + xa) / 2, (xb + tx) / 2
                add(f'<path class="grant" d="M{sx},{sy} C{ma},{sy} {ma},{ly} {xa},{ly} '
                    f'L{xb},{ly} C{mb},{ly} {mb},{ty} {tx},{ty}"/>')

    for n in nodes.values():
        add(f'<rect class="{n["cls"]}" x="{n["x"]:.1f}" y="{n["y"]:.1f}" '
            f'width="{n["w"]:.1f}" height="{n["h"]}" rx="8"/>')
        cx = n['x'] + n['w'] / 2
        add(f'<text x="{cx:.1f}" y="{n["y"] + 28:.1f}" text-anchor="middle" class="mono">'
            f'{escape(n["label"])}</text>')
        if n['note']:
            add(f'<text x="{cx:.1f}" y="{n["y"] + 46:.1f}" text-anchor="middle" '
                f'class="note">{escape(n["note"])}</text>')

    for b in boxes:
        add(f'<rect class="resource" x="{b["x"]:.1f}" y="{b["y"]:.1f}" '
            f'width="{b["w"]:.1f}" height="{b["h"]}" rx="8"/>')
        add(f'<text x="{b["x"] + 14:.1f}" y="{b["y"] + 19:.1f}" class="h" '
            f'font-size="12">{escape(b["head"])}</text>')
        for i, line in enumerate(b['lines']):
            add(f'<text x="{b["x"] + 14:.1f}" y="{b["y"] + 38 + 16 * i:.1f}" class="mono" '
                f'font-size="11.5">{escape(line)}</text>')

    y = body_bottom + 70
    labels = pattern['labels']
    notes = []
    if any(labels['self'] in n for n in nodes):
        notes.append(
            f'{labels["self"]} = the principal\'s own key; {labels["other"]} = any other '
            f'key reached through nesting'
            + (f'; kept literal: {", ".join(pattern["literal_keys"])}' if pattern['literal_keys'] else ''),
        )
    gpp = pattern['stats']['grants_per_principal']
    if gpp:
        notes.append('Resources reached per principal: ' + ', '.join(
            f'{k} ({v} principal{"s" if v != 1 else ""})' for k, v in gpp.items()))
    if pattern['stats']['max'] and len(gpp) > 1:
        notes.append('Most reached: ' + ', '.join(
            f'{p} ({n})' for n, p in pattern['stats']['max'][:3]))
    max_chars = int((width - 2 * MARGIN - 40) / 7.0)
    for line in notes:
        add(f'<text x="{MARGIN}" y="{y}" class="note" font-size="12">'
            f'{escape(clip(line, max_chars))}</text>')
        y += 18

    if pattern['unreached']:
        y += 14
        rows = sorted(pattern['unreached'], key=lambda r: (-r['count'], r['resource']))[:15]
        h = 34 + 20 * len(rows) + (20 if len(pattern['unreached']) > 15 else 0)
        add(f'<rect class="off" x="{MARGIN}" y="{y}" width="{width - 2 * MARGIN}" '
            f'height="{h}" rx="8"/>')
        add(f'<text x="{MARGIN + 16}" y="{y + 22}" class="h" fill="#374151">'
            'In scope but NOT reached (resources carrying the principal\'s own key)</text>')
        for i, r in enumerate(rows):
            held = ', '.join(r['holders'][:2]) or 'no matching role holder'
            more = f' +{len(r["holders"]) - 2} more' if len(r['holders']) > 2 else ''
            share = f'({r["count"]} of {total}) ' if r['count'] < total else ''
            budget = max_chars - len(r['resource']) - len(share) - 20
            add(f'<text x="{MARGIN + 16}" y="{y + 44 + 20 * i}" fill="#374151" font-size="12">'
                f'<tspan class="mono">{escape(r["resource"])}</tspan> {escape(share)}'
                f'- held by {escape(clip(held + more, budget))}</text>')
        if len(pattern['unreached']) > 15:
            add(f'<text x="{MARGIN + 16}" y="{y + 44 + 20 * 15}" class="note">'
                f'... {len(pattern["unreached"]) - 15} more in pattern.json</text>')
        y += h

    y += 34
    legend = [
        ('member', 'is a member of'),
        ('back', 'member of a group in the same/earlier column'),
        ('grant', 'grant matching the role filter'),
    ]
    if self_loops:
        legend.insert(1, ('member', 'loop: nested in another group of the same pattern'))
    lx = MARGIN
    for cls, label in legend:
        add(f'<path class="{cls}" d="M{lx},{y} L{lx + 40},{y}"/>')
        add(f'<text x="{lx + 50}" y="{y + 4}">{escape(label)}</text>')
        lx += 70 + text_w(label, 7.2)
    add(f'<rect class="grantee" x="{lx}" y="{y - 9}" width="24" height="18" rx="3"/>')
    add(f'<text x="{lx + 32}" y="{y + 4}">group holding the role</text>')
    width = max(width, lx + 32 + text_w('group holding the role', 7.2) + MARGIN)
    y += 30
    add(f'<text x="{MARGIN}" y="{y}" class="note">Scope: access recorded in this Pulumi '
        'state only. Manual grants, other stacks and inherited org/folder IAM are not shown.'
        '</text>')
    height = y + 24

    title_w = text_w(pattern['title'], 11) + 2 * MARGIN
    width = max(width, title_w, text_w(pattern['subtitle'], 6.4) + 2 * MARGIN)
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="Helvetica, Arial, sans-serif" '
        'font-size="13">',
        f'<title>{escape(pattern["title"])}</title>',
        '<defs>' + marker('a-member', '#4b5563') + marker('a-back', '#0f766e')
        + marker('a-grant', '#c2410c') + f'<style>{STYLE}</style></defs>',
        f'<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{MARGIN}" y="38" font-size="20" class="h">{escape(pattern["title"])}</text>',
        f'<text x="{MARGIN}" y="60" class="note" font-size="12">'
        f'{escape(pattern["subtitle"])}</text>',
        f'<text x="{res_x + res_w / 2:.1f}" y="{TOP - 16}" text-anchor="middle" class="h">'
        'Resources</text>' if boxes else '',
    ]
    return '\n'.join([*head, *svg, '</svg>\n'])


def cmd_render(args) -> None:
    pattern = load_graph(args.pattern)
    if args.title:
        pattern['title'] = args.title
    args.output.write_text(render(pattern))
    print(f'wrote {args.output}')


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n\n', 2)[2],
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('extract', help='Pulumi state -> graph.json')
    p.add_argument('state', type=Path)
    p.add_argument('-o', '--output', type=Path, default=Path('graph.json'))
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser('list', help='explore graph.json')
    p.add_argument('graph', type=Path)
    p.add_argument('what', choices=['kinds', 'roles', 'resources', 'principals'])
    p.add_argument('--kind', help='regex on resource kind, e.g. ^storage/bucket$')
    p.add_argument('--match', help='regex on principal / resource name')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('analyze', help='graph.json -> pattern.json')
    p.add_argument('graph', type=Path)
    p.add_argument('--principal', required=True, help='regex selecting principals')
    p.add_argument('--kind', help='regex on resource kind (see `list kinds`)')
    p.add_argument('--role', help='regex on role (see `list roles`)')
    p.add_argument('--resource', help='regex on resource name')
    p.add_argument('--key', help='regex with (?P<key>...) extracting each principal\'s '
                   'key, e.g. dataset name; enables abstraction across principals')
    p.add_argument('--self-label', default='<key>')
    p.add_argument('--other-label', default='<other>')
    p.add_argument('--literal', nargs='*', help='keys never abstracted (e.g. common)')
    p.add_argument('--literal-threshold', type=float, default=0.5,
                   help='other keys reached by more than this fraction of principals stay '
                   'literal (default: %(default)s)')
    p.add_argument('--extra-keys', nargs='*', help='more keys to recognise in names')
    p.add_argument('--title')
    p.add_argument('-o', '--output', type=Path, default=Path('pattern.json'))
    p.add_argument('--report', type=Path, help='also write a per-principal text report')
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser('render', help='pattern.json -> SVG')
    p.add_argument('pattern', type=Path)
    p.add_argument('-o', '--output', type=Path, default=Path('permission-flow.svg'))
    p.add_argument('--title')
    p.set_defaults(func=cmd_render)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
