import copy
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from tools import validate_posix_snapshot as snapshot


REPO = 'owner/Chromix'
SHA = 'a' * 40


class Client:
    def __init__(self, platform='macos', arch='arm64'):
        self.platform = platform
        self.run = {'id': 123, 'name': f'build-{platform}-{arch}',
                    'path': f'.github/workflows/build-{platform}-{arch}.yml',
                    'head_branch': 'main', 'event': 'workflow_dispatch',
                    'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO},
                    'status': 'completed', 'head_sha': SHA, 'run_attempt': 2}
        self.jobs = [{'id': 456, 'name': f'build / {platform}-{arch} stage 7 (resume compile)',
                      'status': 'completed', 'conclusion': 'success', 'steps': [
                          {'name': 'Verify handoff snapshot', 'conclusion': 'success'},
                          *[{'name': f'Upload tree part {n}', 'conclusion': 'success'} for n in range(1, 5)]]}]
        artifact_platform = 'mac' if platform == 'macos' else platform
        self.artifacts = [{'id': 100 + n, 'name': f'chromix-{artifact_platform}-{arch}-tree-s7-attempt-1-part{n}',
                           'size_in_bytes': 1024, 'expired': False, 'digest': 'sha256:' + 'b' * 64,
                           'workflow_run': {'id': 123, 'head_sha': SHA}} for n in (1, 2)]
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        return self.run

    def items(self, path, key):
        self.calls.append(path)
        return self.jobs if key == 'jobs' else self.artifacts


class SnapshotValidationTest(unittest.TestCase):
    def validate(self, client):
        return snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102])

    def validate_linux(self, client):
        return snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102], platform='linux')

    def test_exact_original_attempt_from_terminal_retry(self):
        client = Client()
        client.artifacts.append(dict(client.artifacts[0], id=900, name='chromix-mac-arm64-tree-s7-attempt-2-part1'))
        report = self.validate(client)
        self.assertEqual(report['head_sha'], SHA)
        self.assertEqual(report['pattern'], 'chromix-mac-arm64-tree-s7-attempt-1-part*')
        self.assertEqual([item['id'] for item in report['artifacts']], [101, 102])
        self.assertIn('/actions/runs/123/attempts/1/jobs', client.calls)

    def test_linux_arm64_donor_uses_linux_identity_and_pattern(self):
        client = Client(platform='linux')
        report = self.validate_linux(client)
        self.assertEqual(report['workflow'], 'build-linux-arm64')
        self.assertEqual(report['pattern'], 'chromix-linux-arm64-tree-s7-attempt-1-part*')
        self.assertEqual(report['job_id'], 456)
        self.assertEqual([item['id'] for item in report['artifacts']], [101, 102])

    def test_linux_x64_snapshot_cannot_be_used_for_arm64(self):
        client = Client(platform='linux', arch='x64')
        report = snapshot.validate(client, REPO, 123, 7, 1, 'x64', [101, 102], platform='linux')
        self.assertEqual(report['workflow'], 'build-linux-x64')
        self.assertEqual(report['arch'], 'x64')
        self.assertEqual(report['pattern'], 'chromix-linux-x64-tree-s7-attempt-1-part*')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.validate_linux(client)

    def test_wrong_origin_platform_or_unfinished_run_rejected(self):
        for key, value in {'id': 456, 'name': 'build-macos-x64', 'head_branch': 'feature',
                           'path': '.github/workflows/other.yml', 'event': 'pull_request',
                           'head_sha': 'bad\nsha', 'run_attempt': 0, 'status': 'in_progress',
                           'repository': {'full_name': 'foreign/Chromix'},
                           'head_repository': {'full_name': 'foreign/Chromix'}}.items():
            with self.subTest(key=key):
                client = Client()
                client.run[key] = value
                with self.assertRaises(ValueError):
                    self.validate(client)

    def test_same_recovery_branch_manual_donor_must_be_explicit(self):
        client = Client()
        client.run['head_branch'] = 'repair/macos-arm64-old-patches-20260913'
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.validate(client)
        report = snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102],
                                   recovery_branch=client.run['head_branch'])
        self.assertEqual(report['head_sha'], SHA)
        with self.assertRaisesRegex(ValueError, 'identity'):
            snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102],
                              recovery_branch='repair/other')
        client.run['event'] = 'push'
        with self.assertRaisesRegex(ValueError, 'identity'):
            snapshot.validate(client, REPO, 123, 7, 1, 'arm64', [101, 102],
                              recovery_branch=client.run['head_branch'])

    def test_missing_expired_duplicate_noncontiguous_parts_rejected(self):
        cases = [[], [Client().artifacts[1]], [Client().artifacts[0]], [Client().artifacts[0]] * 2]
        for field, value in [('expired', True), ('size_in_bytes', 0),
                             ('name', 'chromix-mac-arm64-tree-s7-attempt-1-part5'),
                             ('workflow_run', {'id': 999, 'head_sha': SHA})]:
            artifacts = copy.deepcopy(Client().artifacts)
            artifacts[0][field] = value
            cases.append(artifacts)
        for artifacts in cases:
            with self.subTest(artifacts=artifacts):
                client = Client()
                client.artifacts = artifacts
                with self.assertRaises(ValueError):
                    self.validate(client)

    def test_snapshot_digest_is_required_and_preserved(self):
        for digest in (None, 123, '', 'sha256:bad', 'sha512:' + 'b' * 64, 'sha256:' + 'B' * 64):
            client = Client()
            client.artifacts[0]['digest'] = digest
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'SHA-256'):
                self.validate(client)
        client = Client()
        del client.artifacts[0]['digest']
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.validate(client)
        report = self.validate(Client())
        self.assertEqual(report['repository'], REPO)
        self.assertEqual(report['artifacts'][0]['digest'], 'sha256:' + 'b' * 64)

    def test_unverified_or_failed_upload_rejected(self):
        for index in range(5):
            client = Client()
            client.jobs[0]['steps'][index]['conclusion'] = 'failure'
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.validate(client)

    def test_invalid_numeric_inputs(self):
        for value in ('', '0', '-1', '1\nfoo=bar', '1.0', 'true', '01'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot.positive(value, 'input')
        self.assertEqual(snapshot.positive('123', 'input'), 123)

    def test_metadata_redirects_are_rejected_without_forwarding_token(self):
        redirect = snapshot.NoRedirect()
        request = urllib.request.Request('https://api.github.com/repos/' + REPO,
                                         headers={'Authorization': 'Bearer fixture'})
        self.assertIsNone(redirect.redirect_request(request, None, 302, 'Found', {}, 'https://foreign.invalid/'))
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                getattr(redirect, f'http_error_{code}')(request, None, code, 'Found', {'Location': 'https://['})
        client = snapshot.Client(REPO, 'fixture')
        self.assertTrue(any(isinstance(handler, snapshot.NoRedirect) for handler in client.opener.handlers))
        error = urllib.error.HTTPError(request.full_url, 302, 'Found', {}, None)
        with patch.object(client.opener, 'open', side_effect=error) as opened:
            with self.assertRaises(urllib.error.HTTPError):
                client.get('/actions/runs/123')
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0].get_header('Authorization'), 'Bearer fixture')

    def test_distinct_recorded_ids_cannot_hide_noncontiguous_or_duplicate_parts(self):
        for part, error in ((3, 'not contiguous'), (1, 'duplicate snapshot part')):
            client = Client()
            client.artifacts[1]['name'] = f'chromix-mac-arm64-tree-s7-attempt-1-part{part}'
            with self.subTest(part=part), self.assertRaisesRegex(ValueError, error):
                self.validate(client)

    def test_pagination_is_bounded_and_complete(self):
        client = snapshot.Client(REPO, 'test')
        responses = iter([{'items': list(range(100)), 'total_count': 101},
                          {'items': [100], 'total_count': 101}])
        client.get = lambda path: next(responses)
        self.assertEqual(client.items('/items', 'items'), list(range(101)))
        client.get = lambda path: {'items': [], 'total_count': 1}
        with self.assertRaises(ValueError):
            client.items('/items', 'items')


if __name__ == '__main__':
    unittest.main()
