import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import image_migration_manifest as inspector


class ImageManifestTests(unittest.TestCase):
    def test_same_source_is_used_for_image_and_exec(self):
        document = {'format': 1, 'hash': 'abcdef123456', 'count': 1, 'migrations': ['app.0001']}
        with patch.dict(os.environ, {'IMAGE': 'image@sha256:digest', 'IMAGE_ENVIRONMENT': '{"SECRET_KEY":"placeholder"}', 'WORKING_DIRECTORY': '/app', 'SETTINGS_MODULE': 'config.settings.prod'}, clear=True), patch.object(inspector.subprocess, 'check_output', return_value='startup message\nDJANGO_MANIFEST=' + json.dumps(document)) as run:
            self.assertEqual(inspector.inspect_image(), document)
            args = run.call_args.args[0]
            self.assertEqual(args[-1], inspector.source())
            self.assertIn('--read-only', args)
            self.assertEqual(args[args.index('--network') + 1], 'none')
            self.assertIn('SECRET_KEY=placeholder', args)
            import shlex
            self.assertEqual(shlex.split(inspector.command())[-1], inspector.source())

    def test_missing_or_multiple_documents_fail(self):
        for output in ['no manifest', 'DJANGO_MANIFEST={}\nDJANGO_MANIFEST={}']:
            with self.subTest(output=output), self.assertRaises(ValueError):
                inspector.parse(output)

    def test_invalid_environment_does_not_launch_container(self):
        for environment in ['[]', '{"KEY":123}', '{"BAD=KEY":"value"}']:
            with patch.dict(os.environ, {'IMAGE_ENVIRONMENT': environment}), patch.object(inspector.subprocess, 'check_output') as run:
                with self.assertRaises(ValueError):
                    inspector.inspect_image()
                run.assert_not_called()
