"""Run the deploy-flags composite step against a scripted gh, without GitHub access."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40

# Stands in for gh: prints the labels the test scripted, or fails the way a
# missing token or a deleted pull request would.
FAKE_GH = '''import os, sys
if os.environ.get('FAKE_GH_EXIT', '0') != '0':
    print('gh: Not Found (HTTP 404)', file=sys.stderr)
    sys.exit(int(os.environ['FAKE_GH_EXIT']))
assert sys.argv[1:3] == ['api', 'repos/harvard-lil/h2o/commits/%s/pulls'] and sys.argv[3:] == ['--jq', '.[].labels[].name']
print(os.environ['FAKE_GH_LABELS'])
''' % SHA


class DeployFlags(unittest.TestCase):
    def run_action(self, event_name='push', labels=(), inputs=None, hold_variable='', gh_exit=0, overrides=None, ok=True):
        doc = yaml.safe_load((ROOT / 'deploy-flags' / 'action.yml').read_text())
        values = {key: str(value.get('default', '')) for key, value in doc['inputs'].items()}
        values.update({'event-name': event_name, 'inputs': json.dumps(inputs or {}), 'hold-variable': hold_variable})
        values.update(overrides or {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            executable = path / 'gh'
            executable.write_text(f'#!{sys.executable}\n' + FAKE_GH)
            executable.chmod(0o755)
            outputs = path / 'outputs'
            outputs.touch()
            summary = path / 'summary'
            summary.touch()
            env = dict(os.environ, PATH=f'{path}:{os.environ["PATH"]}', GITHUB_OUTPUT=str(outputs),
                       GITHUB_STEP_SUMMARY=str(summary), GITHUB_REPOSITORY='harvard-lil/h2o', GITHUB_SHA=SHA,
                       FAKE_GH_LABELS='\n'.join(labels), FAKE_GH_EXIT=str(gh_exit))
            step = doc['runs']['steps'][0]
            for key, expression in step['env'].items():
                if expression == '${{ github.action_path }}':
                    env[key] = str(ROOT / 'deploy-flags')
                    continue
                match = re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', expression)
                self.assertIsNotNone(match, expression)
                env[key] = values[match[1]]
            result = subprocess.run(['bash', '-e', '-c', step['run']], env=env, cwd=path,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
            parsed = dict(line.split('=', 1) for line in outputs.read_text().splitlines())
            return parsed, result.stdout + result.stderr, summary.read_text()

    def assertFlags(self, outputs, force, skip, hold):
        self.assertEqual({key: outputs[key] for key in ('force', 'skip', 'hold')},
                         {'force': force, 'skip': skip, 'hold': hold})

    def test_push_without_pull_request(self):
        outputs, log, summary = self.run_action()
        self.assertFlags(outputs, 'false', 'false', 'false')
        self.assertEqual(outputs['sources'], 'none')
        self.assertIn('::warning::No pull request labels found', log)
        self.assertIn('| force | false |', summary)
        self.assertIn('Sources: none', summary)

    def test_push_with_each_label(self):
        outputs, log, _ = self.run_action(labels=['deploy:force-maintenance-mode', 'unrelated'])
        self.assertFlags(outputs, 'true', 'false', 'false')
        self.assertEqual(outputs['sources'], 'force from label deploy:force-maintenance-mode')
        self.assertNotIn('::warning::', log)

        outputs, _, _ = self.run_action(labels=['deploy:skip-maintenance-mode'])
        self.assertFlags(outputs, 'false', 'true', 'false')
        self.assertEqual(outputs['sources'], 'skip from label deploy:skip-maintenance-mode')

    def test_label_names_are_configurable(self):
        outputs, _, _ = self.run_action(labels=['ops/window'], overrides={'label-prefix': 'ops/', 'force-label': 'window'})
        self.assertFlags(outputs, 'true', 'false', 'false')
        outputs, _, _ = self.run_action(labels=['deploy:force-maintenance-mode'], overrides={'label-prefix': 'ops/'})
        self.assertFlags(outputs, 'false', 'false', 'false')

    def test_dispatch_inputs_are_ignored_on_push(self):
        for event in ['push', 'workflow_call', 'pull_request']:
            with self.subTest(event=event):
                outputs, _, _ = self.run_action(event_name=event, inputs={
                    'force-maintenance': True, 'skip-maintenance': True, 'hold-maintenance': True})
                self.assertFlags(outputs, 'false', 'false', 'false')
                self.assertEqual(outputs['sources'], 'none')

    def test_dispatch_inputs_are_honoured(self):
        outputs, _, _ = self.run_action('workflow_dispatch', inputs={'force-maintenance': True, 'environment': 'staging'})
        self.assertFlags(outputs, 'true', 'false', 'false')
        self.assertEqual(outputs['sources'], 'force from dispatch input force-maintenance')

        outputs, _, _ = self.run_action('workflow_dispatch', inputs={'skip-maintenance': 'true'})
        self.assertFlags(outputs, 'false', 'true', 'false')

        outputs, _, _ = self.run_action('workflow_dispatch', inputs={'hold-maintenance': True, 'force-maintenance': False})
        self.assertFlags(outputs, 'true', 'false', 'true')
        self.assertEqual(outputs['sources'], 'force from hold; hold from dispatch input hold-maintenance')

        outputs, _, _ = self.run_action('workflow_dispatch', inputs={'force-maintenance': 'false', 'hold-maintenance': None})
        self.assertFlags(outputs, 'false', 'false', 'false')

    def test_input_keys_are_configurable(self):
        outputs, _, _ = self.run_action('workflow_dispatch', inputs={'window': True}, overrides={'input-force': 'window'})
        self.assertFlags(outputs, 'true', 'false', 'false')
        self.assertEqual(outputs['sources'], 'force from dispatch input window')

    def test_variable_holds_on_any_event(self):
        for event in ['push', 'workflow_dispatch']:
            with self.subTest(event=event):
                outputs, _, _ = self.run_action(event, hold_variable='true')
                self.assertFlags(outputs, 'true', 'false', 'true')
                self.assertEqual(outputs['sources'], 'force from hold; hold from variable')
        for value in ['', 'false', 'yes', '1']:
            with self.subTest(value=value):
                outputs, _, _ = self.run_action(hold_variable=value)
                self.assertFlags(outputs, 'false', 'false', 'false')

    def test_sources_combine(self):
        outputs, _, _ = self.run_action('workflow_dispatch', labels=['deploy:force-maintenance-mode'],
                                        inputs={'force-maintenance': True}, hold_variable='true')
        self.assertFlags(outputs, 'true', 'false', 'true')
        self.assertEqual(outputs['sources'],
                         'force from label deploy:force-maintenance-mode and dispatch input force-maintenance; hold from variable')

    def test_force_wins_over_skip(self):
        outputs, log, _ = self.run_action(labels=['deploy:force-maintenance-mode', 'deploy:skip-maintenance-mode'])
        self.assertFlags(outputs, 'true', 'false', 'false')
        self.assertEqual(outputs['sources'], 'force from label deploy:force-maintenance-mode; '
                                             'skip from label deploy:skip-maintenance-mode (overridden: force wins)')
        self.assertIn('force wins', log)

        outputs, _, _ = self.run_action('workflow_dispatch', labels=['deploy:skip-maintenance-mode'],
                                        inputs={'hold-maintenance': True})
        self.assertFlags(outputs, 'true', 'false', 'true')
        self.assertEqual(outputs['sources'], 'force from hold; skip from label deploy:skip-maintenance-mode '
                                             '(overridden: force wins); hold from dispatch input hold-maintenance')

    def test_label_lookup_failure_warns_and_continues(self):
        outputs, log, _ = self.run_action('workflow_dispatch', gh_exit=1, inputs={'skip-maintenance': True})
        self.assertFlags(outputs, 'false', 'true', 'false')
        self.assertIn('::warning::Could not read pull request labels', log)
        self.assertIn('HTTP 404', log)

    def test_invalid_inputs_fail(self):
        for inputs, event in [('[]', 'push'), ('not json', 'push'), ('{}', '')]:
            with self.subTest(inputs=inputs, event=event):
                with self.assertRaises(AssertionError):
                    self.run_action(event, overrides={'inputs': inputs})
