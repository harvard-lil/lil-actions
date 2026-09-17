"""Run the deploy helper composite steps against scripted aws and curl, without network access."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
ARN = 'arn:aws:ecs:us-east-1:123456789012:task-definition/beat:42'
OLD_ARN = ARN.replace(':42', ':41')

# Stands in for aws and curl: records each call, answers from a scripted
# queue, and refuses a call the test did not expect.
FAKE = '''import json, os, pathlib, sys
path = pathlib.Path(os.environ['CLI_RESPONSES'])
state = json.loads(path.read_text())
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
call = {'tool': tool, 'args': args}
if '--data' in args:
    value = args[args.index('--data') + 1]
    call['data'] = json.loads(pathlib.Path(value[1:]).read_text())
state['calls'].append(call)
if not state['responses']:
    state['error'] = 'Unexpected extra CLI call: %s %s' % (tool, args)
    path.write_text(json.dumps(state)); sys.exit(79)
response = state['responses'].pop(0)
if response['tool'] != tool or any(arg not in args for arg in response.get('contains', [])):
    state['error'] = 'Unexpected CLI arguments: %s %s' % (tool, args)
    path.write_text(json.dumps(state)); sys.exit(79)
path.write_text(json.dumps(state))
if '--output' in args:
    pathlib.Path(args[args.index('--output') + 1]).write_text(response.get('body', ''))
print(response.get('stdout', ''))
sys.exit(response.get('code', 0))
'''


def reply(tool='aws', output='', contains=(), code=0, **kwargs):
    return dict(tool=tool, stdout=json.dumps(output) if not isinstance(output, str) else output,
                contains=list(contains), code=code, **kwargs)


def described(desired=1, running=1, task_definition=ARN, rollout='COMPLETED', pending=0):
    return reply(output={'failures': [], 'services': [dict(
        taskDefinition=task_definition, desiredCount=desired, runningCount=running, pendingCount=pending,
        deployments=[dict(taskDefinition=task_definition, rolloutState=rollout, status='PRIMARY')])]},
        contains=['describe-services', '--output', 'json'])


def running_count(count):
    return reply(output=str(count), contains=['describe-services', 'services[0].runningCount'])


def parse_outputs(text):
    """GITHUB_OUTPUT with both key=value lines and key<<delimiter heredocs."""
    outputs = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        key, _, value = lines[index].partition('=')
        if '<<' in key:
            key, delimiter = key.split('<<', 1)
            end = lines.index(delimiter, index + 1)
            outputs[key] = '\n'.join(lines[index + 1:end])
            index = end + 1
            continue
        outputs[key] = value
        index += 1
    return outputs


class Helpers(unittest.TestCase):
    def run_action(self, action, inputs=None, responses=(), prepare=None, env=None, ok=True):
        doc = yaml.safe_load((ROOT / action / 'action.yml').read_text())
        values = {key: str(value.get('default', '')) for key, value in doc['inputs'].items()}
        values.update(inputs or {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            queue = path / 'responses.json'
            queue.write_text(json.dumps(dict(responses=list(responses), calls=[])))
            for tool in ['aws', 'curl']:
                executable = path / tool
                executable.write_text(f'#!{sys.executable}\n' + FAKE)
                executable.chmod(0o755)
            outputs = path / 'outputs'
            outputs.touch()
            if prepare:
                prepare(path)
            run_env = dict(os.environ, PATH=f'{path}:{os.environ["PATH"]}', CLI_RESPONSES=str(queue),
                           GITHUB_OUTPUT=str(outputs), GITHUB_REPOSITORY='harvard-lil/h2o',
                           GITHUB_REF_NAME='staging', GITHUB_SERVER_URL='https://github.com', AWS_PAGER='')
            run_env.update(env or {})
            step = doc['runs']['steps'][0]
            for key, expression in step['env'].items():
                if expression == '${{ github.action_path }}':
                    run_env[key] = str(ROOT / action)
                    continue
                match = re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', expression)
                self.assertIsNotNone(match, expression)
                run_env[key] = values[match[1]]
            result = subprocess.run(['bash', '-e', '-c', step['run']], env=run_env, cwd=path,
                                    text=True, capture_output=True, timeout=10)
            state = json.loads(queue.read_text())
            self.assertNotIn('error', state, state)
            self.assertEqual(state['responses'], [], state)
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
            return parse_outputs(outputs.read_text()), result.stdout + result.stderr, state['calls']


class StaticAssetsPublish(Helpers):
    def archive(self, path, files, name='static-assets.tar.gz'):
        with tarfile.open(path / name, 'w:gz') as tar:
            for member in files:
                data = member.encode()
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    def test_two_passes_with_hashed_subdir(self):
        _, log, calls = self.run_action('static-assets-publish', {
            'bucket': 'lil-h2o-static', 'archive': 'static-assets.tar.gz', 'hashed-subdir': 'dist'},
            [reply(contains=['s3', 'sync', 'image-artifacts/static/dist', 's3://lil-h2o-static/static/dist',
                             '--cache-control', 'public, max-age=31536000, immutable', '--no-progress']),
             reply(contains=['s3', 'sync', 'image-artifacts/static', 's3://lil-h2o-static/static',
                             '--exclude', 'dist/*', '--cache-control', 'public, max-age=3600'])],
            prepare=lambda path: self.archive(path, ['static/dist/app.abc123.js', 'static/admin/base.css']))
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertNotIn('--delete', call['args'])
        self.assertIn('Published image-artifacts/static to s3://lil-h2o-static/static', log)

    def test_one_pass_without_hashed_subdir(self):
        _, _, calls = self.run_action('static-assets-publish', {
            'bucket': 'perma-static', 'archive': 'assets.tgz', 'extract-to': 'unpacked'},
            [reply(contains=['s3', 'sync', 'unpacked/static', 's3://perma-static/static',
                             '--cache-control', 'public, max-age=300', '--no-progress'])],
            prepare=lambda path: self.archive(path, ['static/css/site.css'], 'assets.tgz'))
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--delete', calls[0]['args'])
        self.assertNotIn('--exclude', calls[0]['args'])

    def test_prefix_names_both_the_directory_and_the_keys(self):
        _, _, calls = self.run_action('static-assets-publish', {
            'bucket': 'b', 'archive': 'static-assets.tar.gz', 'prefix': 'assets'},
            [reply(contains=['image-artifacts/assets', 's3://b/assets'])],
            prepare=lambda path: self.archive(path, ['assets/x.css']))
        self.assertEqual(len(calls), 1)

    def test_missing_archive_or_directories_fail_before_any_sync(self):
        _, log, calls = self.run_action('static-assets-publish', {'bucket': 'b', 'archive': 'missing.tar.gz'}, ok=False)
        self.assertIn("No archive at 'missing.tar.gz'", log)
        self.assertEqual(calls, [])
        _, log, calls = self.run_action('static-assets-publish', {'bucket': 'b', 'archive': 'static-assets.tar.gz'},
                                        prepare=lambda path: self.archive(path, ['other/x.css']), ok=False)
        self.assertIn("did not contain a 'static' directory", log)
        self.assertEqual(calls, [])
        _, log, calls = self.run_action('static-assets-publish', {'bucket': 'b', 'archive': 'static-assets.tar.gz',
                                        'hashed-subdir': 'dist'}, prepare=lambda path: self.archive(path, ['static/x.css']), ok=False)
        self.assertIn("No 'dist' directory", log)
        self.assertEqual(calls, [])

    def test_failed_sync_fails(self):
        self.run_action('static-assets-publish', {'bucket': 'b', 'archive': 'static-assets.tar.gz'},
                        [reply(output='AccessDenied', code=1)],
                        prepare=lambda path: self.archive(path, ['static/x.css']), ok=False)


class EcsScaleService(Helpers):
    def scale(self, mode, responses, ok=True, **extra):
        inputs = {'mode': mode, 'cluster': 'perma', 'service': 'beat', 'timeout-seconds': '0'} | extra
        return self.run_action('ecs-scale-service', inputs, responses, ok=ok)

    def test_stop_records_and_scales_to_zero(self):
        outputs, _, calls = self.scale('stop', [described(desired=1, running=1),
                                                reply(contains=['update-service', '--desired-count', '0']),
                                                running_count(0)])
        self.assertEqual(outputs, {'desired-count': '1', 'task-definition': ARN})
        update = calls[1]['args']
        self.assertEqual(update[update.index('--desired-count') + 1], '0')
        self.assertNotIn('--task-definition', update)

    def test_stop_waits_and_times_out(self):
        _, log, calls = self.scale('stop', [described(), reply(contains=['update-service']), running_count(1)], ok=False)
        self.assertIn('still has 1 task(s) running after 0s', log)
        self.assertEqual(len(calls), 3)

    def test_stop_missing_service_changes_nothing(self):
        _, log, calls = self.scale('stop', [reply(output={'failures': [], 'services': []}, contains=['describe-services'])], ok=False)
        self.assertIn("Could not read exactly one ECS service 'beat'", log)
        self.assertEqual(len(calls), 1)

    def test_start_restores_and_waits_for_the_exact_revision(self):
        _, log, calls = self.scale('start', [reply(contains=['update-service', '--force-new-deployment']), described()],
                                   **{'desired-count': '1', 'task-definition': ARN})
        update = calls[0]['args']
        self.assertEqual(update[update.index('--task-definition') + 1], ARN)
        self.assertEqual(update[update.index('--desired-count') + 1], '1')
        self.assertIn(f'ECS deployment completed: {ARN}', log)

    def test_start_rejects_rollback_and_timeout(self):
        self.scale('start', [reply(contains=['update-service']), described(task_definition=OLD_ARN)],
                   ok=False, **{'desired-count': '1', 'task-definition': ARN})
        self.scale('start', [reply(contains=['update-service']), described(running=0, pending=1, rollout='IN_PROGRESS')],
                   ok=False, **{'desired-count': '1', 'task-definition': ARN})

    def test_start_at_zero_does_not_wait(self):
        _, log, calls = self.scale('start', [reply(contains=['update-service', '--desired-count', '0'])],
                                   **{'desired-count': '0', 'task-definition': ARN})
        self.assertEqual(len(calls), 1)
        self.assertIn('no rollout to wait for', log)

    def test_start_needs_count_and_revision(self):
        for extra in [{'desired-count': '1'}, {'task-definition': ARN}, {'desired-count': 'one', 'task-definition': ARN}]:
            with self.subTest(extra=extra):
                _, _, calls = self.scale('start', [], ok=False, **extra)
                self.assertEqual(calls, [])

    def test_bad_mode_and_timeout_call_nothing(self):
        _, _, calls = self.scale('restart', [], ok=False)
        self.assertEqual(calls, [])
        _, _, calls = self.scale('stop', [], ok=False, **{'timeout-seconds': 'soon'})
        self.assertEqual(calls, [])


class DeployPreflight(Helpers):
    def preflight(self, ok=True, **extra):
        inputs = {'image-digest': DIGEST, 'source-sha': SHA, 'tier': 'staging',
                  'required-secrets': json.dumps({'CLOUDFLARE_API_TOKEN': 'cf-secret-value', 'SLACK_WEBHOOK_URL': 'https://hooks/x'})} | extra
        return self.run_action('deploy-preflight', inputs, ok=ok)

    def test_well_formed_inputs_pass_without_printing_values(self):
        _, log, _ = self.preflight()
        self.assertIn('secret CLOUDFLARE_API_TOKEN: set', log)
        self.assertIn('secret SLACK_WEBHOOK_URL: set', log)
        self.assertNotIn('cf-secret-value', log)
        self.assertNotIn('hooks/x', log)

    def test_empty_secret_is_named_without_its_neighbour(self):
        _, log, _ = self.preflight(ok=False, **{'required-secrets': json.dumps({'CLOUDFLARE_API_TOKEN': '', 'SLACK_WEBHOOK_URL': 'https://hooks/x'})})
        self.assertIn('::error::required secret CLOUDFLARE_API_TOKEN is empty in the staging environment', log)
        self.assertNotIn('SLACK_WEBHOOK_URL is empty', log)
        self.assertNotIn('hooks/x', log)
        _, log, _ = self.preflight(ok=False, **{'required-secrets': json.dumps({'TOKEN': None})})
        self.assertIn('required secret TOKEN is not a string', log)

    def test_shapes_and_tier(self):
        for extra, message in [({'image-digest': 'sha256:short'}, 'image-digest is not a sha256 digest'),
                               ({'image-digest': '123456789012.dkr.ecr.us-east-1.amazonaws.com/h2o@' + DIGEST}, 'image-digest'),
                               ({'source-sha': SHA[:7]}, 'source-sha is not a 40-character commit SHA'),
                               ({'tier': 'production'}, "tier 'production' is not one of staging, prod")]:
            with self.subTest(extra=extra):
                _, log, _ = self.preflight(ok=False, **extra)
                self.assertIn(f'::error::{message}', log)
        self.preflight(**{'tier': 'production', 'allowed-tiers': 'staging, production'})

    def test_every_problem_is_reported_at_once(self):
        _, log, _ = self.preflight(ok=False, **{'image-digest': '', 'source-sha': '', 'tier': '',
                                                'required-secrets': json.dumps({'A': '', 'B': ' '})})
        self.assertEqual(log.count('::error::'), 5)

    def test_malformed_secrets_json_does_not_echo_it(self):
        _, log, _ = self.preflight(ok=False, **{'required-secrets': '{"TOKEN":"cf-secret-value}'})
        self.assertIn('required-secrets is not valid JSON', log)
        self.assertNotIn('cf-secret-value', log)
        _, log, _ = self.preflight(ok=False, **{'required-secrets': '["cf-secret-value"]'})
        self.assertIn('must be a JSON object', log)
        self.assertNotIn('cf-secret-value', log)


if __name__ == '__main__':
    unittest.main()
