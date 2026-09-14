"""Migration policy and ECS Exec result handling without contacting AWS."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('ecs_django', Path(__file__).parents[1] / 'scripts/ecs_django.py')
django = importlib.util.module_from_spec(spec)
spec.loader.exec_module(django)


class DjangoActionsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            'CLUSTER': 'cluster', 'SERVICE': 'service', 'CONTAINER': 'web',
            'TASK_DEFINITION': '', 'FORCE': 'false', 'SKIP': 'false',
            'MANIFEST_COMMAND': 'python manage.py migration_manifest',
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_remote_failure_is_not_a_successful_session(self):
        with patch.object(django.uuid, 'uuid4') as uuid, patch.object(django, 'aws', return_value='DJANGO_RESULT_token=1\n'):
            uuid.return_value.hex = 'token'
            with self.assertRaisesRegex(RuntimeError, 'Remote Django command failed'):
                django.execute('task', 'false')

    def test_missing_completion_marker_fails(self):
        with patch.object(django, 'aws', return_value='Starting session\n'):
            with self.assertRaises(RuntimeError):
                django.execute('task', 'true')

    def test_command_quoting_and_actual_exit_status(self):
        def aws(*args):
            command = args[args.index('--command') + 1]
            return subprocess.check_output(command, shell=True, text=True)
        with patch.object(django, 'aws', side_effect=aws):
            self.assertEqual(django.execute('task', "printf '%s' \"it's quoted\"").strip(), "it's quoted")

    def test_force_wins_over_skip(self):
        with patch.dict(os.environ, {'FORCE': 'true', 'SKIP': 'true'}), patch.object(django, 'task') as task:
            self.assertTrue(django.maintenance())
            task.assert_not_called()

    def test_skip_does_not_check_database(self):
        with patch.dict(os.environ, {'SKIP': 'true'}), patch.object(django, 'task') as task:
            self.assertFalse(django.maintenance())
            task.assert_not_called()

    def test_unknown_manifest_requires_maintenance(self):
        with patch.dict(os.environ, {'MANIFEST': '/missing/manifest.json'}):
            self.assertTrue(django.maintenance())

    def test_manifest_and_database_state(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text(json.dumps({'format': 1, 'hash': 'abcdef123456'}))
            with patch.dict(os.environ, {'MANIFEST': str(manifest)}), patch.object(django, 'task', return_value='task'):
                for running_hash, pending, expected in [('abcdef123456', False, False), ('abcdef123456', True, True), ('123456abcdef', False, True)]:
                    with self.subTest(running_hash=running_hash, pending=pending), patch.object(django, 'execute', return_value=json.dumps({'hash': running_hash})), patch.object(django, 'pending', return_value=pending):
                        self.assertEqual(django.maintenance(), expected)

    def test_shared_image_inspection_and_remote_inspection_agree(self):
        document = {'format': 1, 'hash': 'abcdef123456', 'count': 1, 'migrations': ['app.0001']}
        with patch.dict(os.environ, {'IMAGE': 'image', 'MANIFEST_COMMAND': ''}), patch.object(django, 'inspect_image', return_value=document), patch.object(django, 'task', return_value='task'), patch.object(django, 'execute', return_value='DJANGO_MANIFEST=' + json.dumps(document)), patch.object(django, 'pending', return_value=False):
            self.assertFalse(django.maintenance())

    def test_image_inspection_failure_stops_before_maintenance(self):
        with patch.dict(os.environ, {'IMAGE': 'image'}), patch.object(django, 'inspect_image', side_effect=subprocess.CalledProcessError(1, 'docker')), patch.object(django, 'task') as task:
            with self.assertRaises(subprocess.CalledProcessError):
                django.maintenance()
            task.assert_not_called()

    def test_failed_check_requires_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            manifest.write_text(json.dumps({'format': 1, 'hash': 'abcdef123456'}))
            with patch.dict(os.environ, {'MANIFEST': str(manifest)}), patch.object(django, 'task', side_effect=RuntimeError('No task')):
                self.assertTrue(django.maintenance())

    def test_unknown_plan_is_an_error(self):
        with patch.object(django, 'execute', return_value='Unexpected output'):
            with self.assertRaises(RuntimeError):
                django.pending('task')

    def test_no_migrations_does_not_run_migrate(self):
        with patch.object(django, 'task', return_value='task'), patch.object(django, 'pending', return_value=False), patch.object(django, 'execute') as execute:
            django.migrate()
            execute.assert_not_called()

    def test_migrations_are_verified_after_execution(self):
        for remaining in [False, True]:
            with self.subTest(remaining=remaining), patch.object(django, 'task', return_value='task'), patch.object(django, 'pending', side_effect=[True, remaining]), patch.object(django, 'execute', return_value='Applied'):
                if remaining:
                    with self.assertRaises(RuntimeError):
                        django.migrate()
                else:
                    django.migrate()

    def test_wrong_task_revision_is_rejected(self):
        with patch.dict(os.environ, {'TASK_DEFINITION': 'expected'}), patch.object(django, 'aws', side_effect=[
            json.dumps({'taskArns': ['task']}), json.dumps({'tasks': [{'lastStatus': 'RUNNING', 'taskDefinitionArn': 'old'}]}),
        ]):
            with self.assertRaisesRegex(RuntimeError, 'requested running revision'):
                django.task()
