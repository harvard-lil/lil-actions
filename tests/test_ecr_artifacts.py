import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('artifacts', Path(__file__).parents[1] / 'scripts/ecr_artifacts.py')
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def encoded(value):
    return json.dumps(value).encode()


def digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


class ArtifactTests(unittest.TestCase):
    def fixture(self, payload=b'manifest', subject=None):
        self.subject = 'sha256:' + 'a' * 64
        self.image = 'registry.example/app@' + self.subject
        self.payload = payload
        self.manifest = encoded({'subject': {'digest': subject or self.subject}, 'artifactType': 'test/type', 'layers': [{'digest': digest(payload), 'size': len(payload)}]})
        self.index = encoded({'manifests': [{'artifactType': 'test/type', 'digest': digest(self.manifest)}]})
        self.requests = []

    def cli(self, args, **kwargs):
        if args[0] == 'aws':
            return 'password'
        self.assertEqual(args[0], 'curl')
        url = args[-1]
        self.requests.append(url)
        data = self.index if '/referrers/' in url else self.manifest if '/manifests/' in url else self.payload
        Path(args[args.index('--output') + 1]).write_bytes(data)
        return '200'

    def fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/artifact'
            artifacts.fetch(self.image, [{'type': 'test/type', 'path': str(path)}])
            return path.read_bytes()

    def test_fetch_reads_only_index_manifest_and_blob(self):
        self.fixture()
        with patch.object(artifacts.subprocess, 'check_output', side_effect=self.cli):
            self.assertEqual(self.fetch(), b'manifest')
        self.assertEqual(len(self.requests), 3)

    def test_duplicate_identical_referrers_are_accepted(self):
        self.fixture()
        entry = json.loads(self.index)['manifests'][0]
        self.index = encoded({'manifests': [entry, entry]})
        with patch.object(artifacts.subprocess, 'check_output', side_effect=self.cli):
            self.assertEqual(self.fetch(), b'manifest')

    def test_conflicting_payloads_are_rejected(self):
        self.fixture()
        other = encoded({'subject': {'digest': self.subject}, 'artifactType': 'test/type', 'layers': [{'digest': digest(b'other'), 'size': 5}]})
        entries = json.loads(self.index)['manifests']
        entries.append({'artifactType': 'test/type', 'digest': digest(other)})
        self.index = encoded({'manifests': entries})
        def cli(args, **kwargs):
            if args[-1].endswith('/manifests/' + digest(other)):
                Path(args[args.index('--output') + 1]).write_bytes(other)
                return '200'
            return self.cli(args, **kwargs)
        with patch.object(artifacts.subprocess, 'check_output', side_effect=cli), self.assertRaisesRegex(RuntimeError, 'conflicting'):
            self.fetch()

    def test_wrong_subject_is_rejected(self):
        self.fixture(subject='sha256:' + 'b' * 64)
        with patch.object(artifacts.subprocess, 'check_output', side_effect=self.cli), self.assertRaisesRegex(RuntimeError, 'subject'):
            self.fetch()

    def test_corrupt_payload_is_rejected(self):
        self.fixture()
        self.payload = b'corrupt'
        with patch.object(artifacts.subprocess, 'check_output', side_effect=self.cli), self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
            self.fetch()

    def test_missing_artifact_stops(self):
        self.fixture()
        self.index = encoded({'manifests': []})
        with patch.object(artifacts.subprocess, 'check_output', side_effect=self.cli), self.assertRaisesRegex(RuntimeError, 'Missing or conflicting'):
            self.fetch()

    def test_auth_failure_does_not_fall_back_to_an_image(self):
        self.fixture()
        with patch.object(artifacts.subprocess, 'check_output', side_effect=['password', '403']), self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.fetch()

    def test_transport_error_does_not_expose_registry_credentials(self):
        self.fixture()
        with patch.object(artifacts.subprocess, 'check_output', side_effect=['password', subprocess.CalledProcessError(1, ['curl', '--user', 'AWS:password'])]), self.assertRaisesRegex(RuntimeError, '^Registry transport request failed$'):
            self.fetch()

    def test_publication_has_fixed_timestamp_and_stable_filename(self):
        self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text('{}')
            with patch.object(artifacts.subprocess, 'run') as run:
                artifacts.publish(self.image, [{'type': 'test/type', 'path': str(path), 'media-type': 'application/json'}])
                args = run.call_args.args[0]
                self.assertIn('org.opencontainers.image.created=1970-01-01T00:00:00Z', args)
                self.assertEqual(args[-1], 'manifest.json:application/json')
                self.assertEqual(run.call_args.kwargs['cwd'], path.parent.resolve())
