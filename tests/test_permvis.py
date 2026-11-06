"""
End-to-end test of permvis on a small synthetic Pulumi checkpoint.

Run from the repository root:
    python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent
PERMVIS = PIPELINE / 'skills' / 'permission-vis' / 'scripts' / 'permvis.py'
DOMAIN = '@example.org'
MUTATE = 'organizations/1/roles/StorageObjectAndBucketMutator'
VIEW = 'organizations/1/roles/StorageObjectAndBucketViewer'


def group(name: str) -> dict:
    return {
        'type': 'gcp:cloudidentity/group:Group',
        'outputs': {'id': f'groups/{name}', 'name': f'groups/{name}',
                    'groupKey': {'id': f'{name}{DOMAIN}'}},
    }


def dynamic_membership(group_name: str, member: str) -> dict:
    return {
        'type': 'pulumi-python:dynamic:Resource',
        'outputs': {'group_key': f'groups/{group_name}', 'member_key': member},
    }


def native_membership(group_name: str, member: str) -> dict:
    return {
        'type': 'gcp:cloudidentity/groupMembership:GroupMembership',
        'outputs': {'group': f'groups/{group_name}',
                    'preferredMemberKey': [{'id': member, 'namespace': ''}]},
    }


def bucket_grant(bucket: str, member: str, role: str) -> dict:
    return {
        'type': 'gcp:storage/bucketIAMMember:BucketIAMMember',
        'outputs': {'bucket': f'b/{bucket}', 'member': member, 'role': role,
                    'etag': 'x', 'condition': None, 'id': 'x'},
    }


def sa(name: str) -> str:
    return f'notebook-{name}@nb.iam.gserviceaccount.com'


def fixture_state() -> dict:
    groups = ['ds1-analysis', 'ds1-test-full', 'ds1-full', 'ds2-analysis',
              'ds2-test-full', 'ds2-full', 'common-analysis', 'common-test-full']
    resources = [group(g) for g in groups]
    resources += [
        {'type': 'gcp:serviceaccount/account:Account', 'outputs': {'email': sa('ds1')}},
        {'type': 'gcp:serviceaccount/account:Account', 'outputs': {'email': sa('ds2')}},
        dynamic_membership('ds1-analysis', sa('ds1')),
        native_membership('ds2-analysis', sa('ds2')),
        dynamic_membership('ds1-test-full', f'ds1-analysis{DOMAIN}'),
        dynamic_membership('ds2-test-full', f'ds2-analysis{DOMAIN}'),
        dynamic_membership('common-analysis', f'ds1-analysis{DOMAIN}'),
        dynamic_membership('common-analysis', f'ds2-analysis{DOMAIN}'),
        dynamic_membership('common-test-full', f'common-analysis{DOMAIN}'),
        # ds1 depends on ds2: ds1-analysis is nested in ds2-analysis.
        dynamic_membership('ds2-analysis', f'ds1-analysis{DOMAIN}'),
        bucket_grant('cpg-ds1-test', f'group:ds1-test-full{DOMAIN}', MUTATE),
        bucket_grant('cpg-ds2-test', f'group:ds2-test-full{DOMAIN}', MUTATE),
        bucket_grant('cpg-common-test', f'group:common-test-full{DOMAIN}', MUTATE),
        bucket_grant('cpg-ds1-main', f'group:ds1-full{DOMAIN}', MUTATE),
        bucket_grant('cpg-ds2-main', f'group:ds2-full{DOMAIN}', MUTATE),
        bucket_grant('cpg-ds1-main', f'group:ds1-analysis{DOMAIN}', VIEW),
        {
            'type': 'gcp:secretmanager/secretIamMember:SecretIamMember',
            'outputs': {'secretId': 'projects/p/secrets/s', 'project': 'p',
                        'member': f'group:ds1-analysis{DOMAIN}',
                        'role': 'roles/secretmanager.secretAccessor'},
        },
    ]
    return {'version': 3, 'checkpoint': {'latest': {'resources': resources}}}


def run(*args: str) -> str:
    return subprocess.run(
        [sys.executable, str(PERMVIS), *args],
        check=True, capture_output=True, text=True,
    ).stdout


class PermvisPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'state.json').write_text(json.dumps(fixture_state()))
        run('extract', str(self.tmp / 'state.json'), '-o', str(self.tmp / 'graph.json'))
        self.graph = json.loads((self.tmp / 'graph.json').read_text())

    def analyze(self, *extra: str) -> dict:
        run(
            'analyze', str(self.tmp / 'graph.json'),
            '--principal', '^notebook-', '--kind', '^storage/bucket$',
            '--role', 'Mutator', *extra,
            '-o', str(self.tmp / 'pattern.json'), '--report', str(self.tmp / 'report.txt'),
        )
        return json.loads((self.tmp / 'pattern.json').read_text())

    def test_extract_reads_both_membership_types_and_iam_kinds(self):
        memberships = {tuple(m) for m in self.graph['memberships']}
        self.assertIn((sa('ds1'), f'ds1-analysis{DOMAIN}'), memberships)
        self.assertIn((sa('ds2'), f'ds2-analysis{DOMAIN}'), memberships)
        kinds = {g[2] for g in self.graph['grants']}
        self.assertEqual(kinds, {'storage/bucket', 'secretmanager/secret'})
        self.assertIn(
            ['group', f'ds1-test-full{DOMAIN}', 'storage/bucket', 'cpg-ds1-test', MUTATE],
            self.graph['grants'],
        )

    def test_nested_access_and_abstraction(self):
        pattern = self.analyze(
            '--key', '^notebook-(?P<key>[^@]+)@', '--self-label', '<ds>',
            '--other-label', '<dep>',
        )
        self.assertEqual(pattern['total_principals'], 2)
        self.assertEqual(pattern['literal_keys'], [])  # common is not a key
        edges = {(e['source'], e['target']): e['count'] for e in pattern['edges']}
        self.assertEqual(edges[('notebook-<ds>@nb.iam.gserviceaccount.com', '<ds>-analysis')], 2)
        self.assertEqual(edges[('<ds>-analysis', '<ds>-test-full')], 2)
        self.assertEqual(edges[('<ds>-analysis', 'common-analysis')], 2)
        self.assertEqual(edges[('<ds>-analysis', '<dep>-analysis')], 1)
        self.assertEqual(edges[('<dep>-analysis', '<dep>-test-full')], 1)
        # Groups that lead to no matching grant (e.g. <ds>-full) are pruned.
        self.assertNotIn('<ds>-full', {n['id'] for n in pattern['nodes']})

        boxes = {(g['grantee'], g['role']): g for g in pattern['grants']}
        self.assertEqual(
            [r['name'] for r in boxes[('<dep>-test-full', 'StorageObjectAndBucketMutator')]['resources']],
            ['cpg-<dep>-test'],
        )
        unreached = {u['resource']: u for u in pattern['unreached']}
        self.assertEqual(unreached['cpg-<ds>-main']['count'], 2)
        self.assertEqual(
            unreached['cpg-<ds>-main']['holders'], ['<ds>-full (StorageObjectAndBucketMutator)'],
        )

        report = (self.tmp / 'report.txt').read_text()
        self.assertIn('cpg-ds2-test  [storage/bucket] (StorageObjectAndBucketMutator)', report)
        self.assertIn('via: ds1-analysis@example.org -> ds2-analysis@example.org '
                      '-> ds2-test-full@example.org', report)

    def test_without_key_shows_concrete_names(self):
        pattern = self.analyze()
        ids = {n['id'] for n in pattern['nodes']}
        self.assertIn('ds1-analysis', ids)
        self.assertIn(sa('ds2'), ids)

    def test_render_produces_svg(self):
        self.analyze('--key', '^notebook-(?P<key>[^@]+)@')
        run('render', str(self.tmp / 'pattern.json'), '-o', str(self.tmp / 'out.svg'))
        svg = (self.tmp / 'out.svg').read_text()
        self.assertTrue(svg.startswith('<svg'))
        self.assertIn('&lt;key&gt;-test-full', svg)
        self.assertIn('NOT reached', svg)


if __name__ == '__main__':
    unittest.main()
