"""Run the shipped composite bash against scripted CLI responses, without AWS access."""
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
DIGEST = 'sha256:' + 'b' * 64
ARN = 'arn:aws:ecs:us-east-1:123456789012:task-definition/web:42'
REPOSITORY = '123456789012.dkr.ecr.us-east-1.amazonaws.com/web'

FAKE = '''import json, os, pathlib, re, sys
path = pathlib.Path(os.environ['CLI_RESPONSES'])
state = json.loads(path.read_text())
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
call = {'tool': tool, 'args': args}
if '--targets' in args:
    value = args[args.index('--targets') + 1]
    call['targets'] = json.loads(pathlib.Path(value[7:]).read_text()) if value.startswith('file://') else json.loads(value)
state['calls'].append(call)
if not state['responses']:
    state['error'] = 'Unexpected extra CLI call'
    path.write_text(json.dumps(state)); sys.exit(79)
response = state['responses'].pop(0)
if response['tool'] != tool or any(arg not in args for arg in response.get('contains', [])):
    state['error'] = 'Unexpected CLI arguments'
    path.write_text(json.dumps(state)); sys.exit(79)
path.write_text(json.dumps(state))
if tool == 'curl':
    pathlib.Path(args[args.index('--output') + 1]).write_text(json.dumps(response['body']))
print(response.get('stdout', ''))
if 'remote_status' in response:
    marker = re.search(r'DJANGO_RESULT_[0-9a-f]+', args[args.index('--command') + 1]).group()
    print(f"{marker}={response['remote_status']}")
if response.get('stderr'):
    print(response['stderr'], file=sys.stderr)
sys.exit(response.get('code', 0))
'''


def reply(tool='aws', output='', contains=(), code=0, **kwargs):
    return dict(tool=tool, stdout=json.dumps(output) if not isinstance(output, str) else output,
                contains=list(contains), code=code, **kwargs)


def service(**changes):
    return dict(taskDefinition=ARN, desiredCount=2, runningCount=2, pendingCount=0,
                deployments=[dict(taskDefinition=ARN, rolloutState='COMPLETED', status='PRIMARY')]) | changes


def described(current=None):
    return reply(output={'failures': [], 'services': [current or service()]}, contains=['describe-services'])


class Actions(unittest.TestCase):
    def run_action(self, action, responses, inputs=None, ok=True):
        doc = yaml.safe_load((ROOT / action / 'action.yml').read_text())
        values = {key: str(value.get('default', '')) for key, value in doc['inputs'].items()}
        values.update(inputs or {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            queue = path / 'responses.json'
            queue.write_text(json.dumps(dict(responses=responses, calls=[])))
            for tool in ['aws', 'docker', 'curl', 'git']:
                executable = path / tool
                executable.write_text(f'#!{sys.executable}\n' + FAKE)
                executable.chmod(0o755)
            outputs = path / 'outputs'
            outputs.touch()
            env = dict(os.environ, PATH=f'{path}:{os.environ["PATH"]}', CLI_RESPONSES=str(queue),
                       GITHUB_OUTPUT=str(outputs), AWS_PAGER='')
            step = doc['runs']['steps'][0]
            for key, expression in step.get('env', {}).items():
                if expression == '${{ github.action_path }}':
                    env[key] = str(ROOT / action)
                    continue
                match = re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', expression)
                self.assertIsNotNone(match, expression)
                env[key] = values[match[1]]
            result = subprocess.run(['bash', '-c', step['run']], env=env, cwd=path,
                                    text=True, capture_output=True, timeout=10)
            state = json.loads(queue.read_text())
            self.assertNotIn('error', state, state)
            self.assertEqual(state['responses'], [], state)
            self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
            return dict(line.split('=', 1) for line in outputs.read_text().splitlines()), state['calls']

    def test_exec_requires_remote_success(self):
        for response, ok in [
            (reply(contains=['execute-command'], remote_status=0), True),
            (reply(contains=['execute-command'], remote_status=1), False),
            (reply(contains=['execute-command'], output='Cannot perform start session: EOF'), False),
            (reply(contains=['execute-command'], remote_status=0, code=2), False),
        ]:
            with self.subTest(response=response):
                self.run_action('ecs-exec-command', [
                    reply(output={'taskArns': ['task']}, contains=['list-tasks']),
                    reply(output={'tasks': [{'lastStatus': 'RUNNING', 'taskDefinitionArn': ARN}]}, contains=['describe-tasks']),
                    response,
                ], {'cluster': 'web', 'service': 'web', 'container': 'web', 'command': 'invoke create-search-index'}, ok)

    def wait(self, current, ok):
        return self.run_action('ecs-wait-for-deployment', [described(current)],
                               {'cluster': 'web', 'service': 'web', 'task-definition': ARN,
                                'timeout-seconds': '0'}, ok)

    def test_wait_exact_revision(self):
        self.wait(service(), True)

    def test_wait_rejects_rollback(self):
        old = service(taskDefinition=ARN.replace(':42', ':41'))
        old['deployments'][0]['taskDefinition'] = old['taskDefinition']
        self.wait(old, False)

    def test_wait_rejects_incomplete_services(self):
        for changes in [dict(desiredCount=0, runningCount=0), dict(pendingCount=1), dict(runningCount=1),
                        dict(deployments=[dict(taskDefinition=ARN, rolloutState='FAILED', status='PRIMARY')]),
                        dict(deployments=service()['deployments'] * 2)]:
            with self.subTest(changes=changes):
                self.wait(service(**changes), False)

    def test_wait_missing_service(self):
        self.run_action('ecs-wait-for-deployment', [reply(output={'failures': [], 'services': []})],
                        {'cluster': 'web', 'service': 'web'}, False)

    def test_wait_captures_revision_for_legacy_callers(self):
        self.run_action('ecs-wait-for-deployment', [described()], {'cluster': 'web', 'service': 'web'})

    def resolve_responses(self, image=None, tags=None):
        return [described(), reply(output=REPOSITORY, contains=['describe-repositories']),
                reply(output=image or f'{REPOSITORY}@{DIGEST}', contains=['describe-task-definition']),
                reply(output=tags if tags is not None else [SHA, 'staging-latest'], contains=['describe-images']),
                described()]

    def resolve(self, responses, ok=True):
        return self.run_action('ecs-resolve-service-image', responses, {'cluster': 'web', 'service': 'web',
            'container-name': 'web', 'ecr-repository': 'web'}, ok)

    def test_resolve_stable_service(self):
        outputs, _ = self.resolve(self.resolve_responses())
        self.assertEqual(outputs['source-sha'], SHA)
        self.assertEqual(outputs['task-definition-arn'], ARN)

    def test_resolve_rejects_unstable_service(self):
        self.resolve([described(service(pendingCount=1))], False)

    def test_resolve_rejects_wrong_registry_or_mutable_tag(self):
        for image in [f'other.example/web@{DIGEST}', f'{REPOSITORY}:latest']:
            with self.subTest(image=image):
                self.resolve(self.resolve_responses(image=image)[:3], False)

    def test_resolve_rejects_ambiguous_source(self):
        self.resolve(self.resolve_responses(tags=[SHA, 'c' * 40])[:4], False)

    def test_resolve_rechecks_source_before_returning(self):
        responses = self.resolve_responses()
        responses[-1] = described(service(pendingCount=1))
        self.resolve(responses, False)

    def test_schedule_preserves_target_fields(self):
        target = dict(Id='job', Arn='cluster', RoleArn='role', Input='{}',
                      EcsParameters={'TaskDefinitionArn': 'old'},
                      DeadLetterConfig={'Arn': 'queue'}, RetryPolicy={'MaximumRetryAttempts': 2})
        updated = target | {'EcsParameters': {'TaskDefinitionArn': ARN}}
        _, calls = self.run_action('ecs-update-eventbridge', [reply(output={'Targets': [target]}),
            reply(output={'FailedEntryCount': 0}), reply(output={'Targets': [updated]})],
            {'event-rules': 'rule', 'task-definition-arn': ARN})
        self.assertEqual(calls[1]['targets'], [updated])

    def test_schedule_refuses_ambiguous_targets_before_writing(self):
        self.run_action('ecs-update-eventbridge', [reply(output={'Targets': [{'Id': 'a'}, {'Id': 'b'}]})],
                        {'event-rules': 'rule', 'task-definition-arn': ARN}, False)

    def test_schedule_explicit_target_and_api_failure(self):
        target = {'Id': 'job', 'EcsParameters': {'TaskDefinitionArn': 'old'}}
        self.run_action('ecs-update-eventbridge', [reply(output={'Targets': [target, {'Id': 'other'}]}),
            reply(output={'FailedEntryCount': 1})],
            {'event-rules': 'rule', 'task-definition-arn': ARN, 'target-id': 'job'}, False)

    def test_schedule_verifies_readback(self):
        target = {'Id': 'job', 'EcsParameters': {'TaskDefinitionArn': 'old'}}
        self.run_action('ecs-update-eventbridge', [reply(output={'Targets': [target]}),
            reply(output={'FailedEntryCount': 0}), reply(output={'Targets': [target]})],
            {'event-rules': 'rule', 'task-definition-arn': ARN}, False)

    def test_git_promotion_skips_unbuilt_merge_tree(self):
        parent = 'c' * 40
        responses = [reply('git', SHA), reply('git', 'tree-new'), reply('git', parent),
                     reply('git', 'tree-old'), reply('git', 'tree-new'), reply(output=DIGEST)]
        outputs, calls = self.run_action('ecr-resolve-git-image', responses, {'repository': 'web'})
        self.assertEqual(outputs['source-sha'], SHA)
        self.assertEqual(len([call for call in calls if call['tool'] == 'aws']), 1)

    def test_git_promotion_matching_parent_and_aws_errors(self):
        parent = 'c' * 40
        prefix = [reply('git', SHA), reply('git', 'tree'), reply('git', parent), reply('git', 'tree')]
        outputs, _ = self.run_action('ecr-resolve-git-image', prefix + [reply(output=DIGEST)], {'repository': 'web'})
        self.assertEqual(outputs['source-sha'], parent)
        self.run_action('ecr-resolve-git-image', prefix + [reply(output='AccessDenied', code=1)], {'repository': 'web'}, False)

    def test_publication_absent_complete_and_access_denied(self):
        inputs = {'repository': 'web', 'registry': REPOSITORY.rsplit('/', 1)[0], 'tag': SHA}
        outputs, _ = self.run_action('ecr-publication-state', [reply(output='ImageNotFoundException', code=1)], inputs)
        self.assertEqual(outputs['state'], 'absent')
        outputs, _ = self.run_action('ecr-publication-state', [reply(output=DIGEST)], inputs)
        self.assertEqual(outputs['state'], 'complete')
        self.run_action('ecr-publication-state', [reply(output='AccessDenied', code=1)], inputs, False)

    def test_publication_referrers_partial_complete_and_wrong_subject(self):
        artifact = 'application/example'
        inputs = {'repository': 'web', 'registry': REPOSITORY.rsplit('/', 1)[0], 'tag': SHA,
                  'required-artifact-types': json.dumps([artifact])}
        prefix = [reply(output=DIGEST), reply(output='dummy-test-token')]
        outputs, _ = self.run_action('ecr-publication-state', prefix + [
            reply('curl', '200', body={'manifests': []})], inputs)
        self.assertEqual(outputs['state'], 'partial')
        for subject, expected in [(DIGEST, 'complete'), ('sha256:' + 'd' * 64, 'partial')]:
            with self.subTest(subject=subject):
                outputs, _ = self.run_action('ecr-publication-state', prefix + [
                    reply('curl', '200', body={'manifests': [{'artifactType': artifact, 'digest': 'sha256:' + 'e' * 64}]}),
                    reply('curl', '200', body={'subject': {'digest': subject}, 'artifactType': artifact, 'layers': [{}]})], inputs)
                self.assertEqual(outputs['state'], expected)

    def test_publish_once_and_refuse_overwrite(self):
        inputs = {'repository': 'web', 'registry': REPOSITORY.rsplit('/', 1)[0], 'local-image': 'tested', 'tag': SHA}
        prefix = [reply('docker', contains=['inspect'])]
        outputs, calls = self.run_action('ecr-publish-image', prefix + [
            reply(output='ImageNotFoundException', code=1), reply('docker', contains=['tag']),
            reply('docker', contains=['push']), reply(output=DIGEST)], inputs)
        self.assertEqual(outputs['digest'], DIGEST)
        self.assertEqual([call['args'][0] for call in calls if call['tool'] == 'docker'], ['image', 'tag', 'push'])
        self.run_action('ecr-publish-image', prefix + [reply(output=DIGEST)], inputs, False)
        self.run_action('ecr-publish-image', prefix + [reply(output='AccessDenied', code=1)], inputs, False)

    def roll(self, services, responses, ok=True, **inputs):
        return self.run_action('ecs-roll-services', responses,
                               {'cluster': 'perma', 'services': json.dumps(services),
                                'poll-interval-seconds': '1', 'timeout-seconds': '0'} | inputs, ok)

    def test_roll_moves_every_service_then_waits(self):
        worker = ARN.replace('web', 'worker')
        active = service(status='ACTIVE')
        responses = [described(active), reply(output='web', contains=['update-service', '--task-definition', ARN]),
                     described(active), reply(output='worker', contains=['update-service', worker]),
                     described(active), described(service(status='ACTIVE', taskDefinition=worker,
                         deployments=[dict(taskDefinition=worker, rolloutState='COMPLETED', status='PRIMARY')]))]
        outputs, calls = self.roll({'web': ARN, 'worker': worker}, responses)
        results = json.loads(outputs['results'])
        self.assertEqual({name: result['state'] for name, result in results.items()},
                         {'web': 'completed', 'worker': 'completed'})
        self.assertEqual(outputs['skipped'], '')
        self.assertEqual([call['args'][1] for call in calls],
                         ['describe-services', 'update-service'] * 2 + ['describe-services'] * 2)

    def test_roll_skips_services_without_desired_tasks(self):
        ia = ARN.replace('web', 'ia')
        idle = service(status='ACTIVE', desiredCount=0, runningCount=0)
        responses = [described(service(status='ACTIVE')), reply(contains=['update-service']),
                     described(idle), reply(contains=['update-service', ia]),
                     described(service(status='ACTIVE'))]
        outputs, _ = self.roll({'web': ARN, 'ia': ia}, responses)
        self.assertEqual(outputs['skipped'], 'ia')
        self.assertEqual(json.loads(outputs['results'])['ia']['state'], 'skipped')

    def test_roll_waits_across_polls(self):
        responses = [described(service(status='ACTIVE')), reply(contains=['update-service']),
                     described(service(status='ACTIVE', pendingCount=1)), described(service(status='ACTIVE'))]
        outputs, _ = self.roll({'web': ARN}, responses, **{'timeout-seconds': '10'})
        self.assertEqual(json.loads(outputs['results'])['web']['state'], 'completed')

    def test_roll_reports_rollback_and_timeout(self):
        previous = ARN.replace(':42', ':41')
        rolled_back = service(status='ACTIVE', taskDefinition=previous, deployments=[
            dict(taskDefinition=previous, rolloutState='COMPLETED', status='PRIMARY'),
            dict(taskDefinition=ARN, rolloutState='FAILED', status='ACTIVE')])
        for current, state in [(rolled_back, 'rolled-back'), (service(status='ACTIVE', pendingCount=1), 'timed-out')]:
            with self.subTest(state=state):
                outputs, _ = self.roll({'web': ARN}, [described(service(status='ACTIVE')),
                                                      reply(contains=['update-service']), described(current)], False)
                self.assertEqual(json.loads(outputs['results'])['web']['state'], state)

    def test_roll_rejects_malformed_services(self):
        for services in ['[]', '{}', '{"web": ""}']:
            with self.subTest(services=services):
                self.run_action('ecs-roll-services', [], {'cluster': 'perma', 'services': services}, False)

    def manifest(self, directory, name, **changes):
        document = {'format': 1,
                    'tasks': {'perma.celery_tasks.run_next_capture': {'argspec': 'abc123', 'queue': 'celery'},
                              'perma.celery_tasks.upload_to_ia': {'argspec': 'def456', 'queue': 'ia'}},
                    'beat': {'run-next-capture': {'task': 'perma.celery_tasks.run_next_capture', 'schedule': '*/1'}}}
        for section, entries in changes.items():
            document[section] = entries
        path = Path(directory) / name
        path.write_text(json.dumps(document))
        return str(path)

    def test_celery_manifest_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            base = self.manifest(directory, 'base.json')
            same = self.manifest(directory, 'same.json')
            outputs, _ = self.run_action('celery-manifest-compare', [], {'base': base, 'incoming': same})
            self.assertEqual(outputs['changed'], 'false')
            changed = self.manifest(directory, 'changed.json',
                tasks={'perma.celery_tasks.run_next_capture': {'argspec': 'abc124', 'queue': 'celery'},
                       'perma.celery_tasks.new_task': {'argspec': '0', 'queue': 'background'}},
                beat=[])
            outputs, _ = self.run_action('celery-manifest-compare', [], {'base': base, 'incoming': changed})
            self.assertEqual(outputs['changed'], 'true')
            self.assertEqual(outputs['summary'].split('; '), [
                'added task perma.celery_tasks.new_task', 'removed task perma.celery_tasks.upload_to_ia',
                'changed task perma.celery_tasks.run_next_capture (argspec)', 'removed beat entry run-next-capture'])
            self.run_action('celery-manifest-compare', [], {'base': base, 'incoming': '/missing.json'}, False)

    def test_celery_task_manifest_inspects_offline(self):
        document = {'format': 1, 'tasks': [{'name': 'perma.t', 'argspec': 'a', 'queue': 'celery'}], 'beat': {}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'celery-tasks.json'
            _, calls = self.run_action('celery-task-manifest',
                                       [reply('docker', 'startup noise\nCELERY_MANIFEST=' + json.dumps(document), contains=['run'])],
                                       {'image': 'perma-prod:sha', 'output': str(output), 'app': 'perma',
                                        'environment': '{"PERMA_SETTINGS_MODULE":"settings_build"}'})
            self.assertEqual(json.loads(output.read_text())['tasks'], {'perma.t': {'argspec': 'a', 'queue': 'celery'}})
            args = calls[0]['args']
            for flag in ['--network', 'none', '--read-only', 'PERMA_SETTINGS_MODULE=settings_build', 'CELERY_APP=perma']:
                self.assertIn(flag, args)
            # The inspector is this repository's source, not a command the application ships.
            self.assertIn("find_app(os.environ['CELERY_APP'])", args[-1])
            self.run_action('celery-task-manifest', [reply('docker', 'CELERY_MANIFEST={"format": 2}')],
                            {'image': 'perma-prod:sha', 'output': str(output), 'app': 'perma'}, False)
            self.run_action('celery-task-manifest', [],
                            {'image': 'perma-prod:sha', 'output': str(output)}, False)

    def test_celery_wait_idle(self):
        prefix = [reply(output={'taskArns': ['task']}, contains=['list-tasks']),
                  reply(output={'tasks': [{'lastStatus': 'RUNNING', 'taskDefinitionArn': ARN}]}, contains=['describe-tasks'])]
        inputs = {'cluster': 'perma', 'service': 'web', 'container': 'web', 'app': 'perma', 'timeout-seconds': '0'}
        for stdout, idle in [('{"w1@a": [], "w2@b": []}', 'true'), ('{"w1@a": [{"id": "1"}]}', 'false'),
                             ('Error: No nodes replied\nCELERY_INSPECT_FAILED=69', 'false')]:
            with self.subTest(stdout=stdout):
                outputs, calls = self.run_action('celery-wait-idle', prefix + [
                    reply(output=stdout, contains=['execute-command'], remote_status=0)], inputs)
                self.assertEqual(outputs['idle'], idle)
                self.assertNotIn('purge', calls[-1]['args'][calls[-1]['args'].index('--command') + 1])
        self.run_action('celery-wait-idle', prefix + [reply(output='', contains=['execute-command'], remote_status=1)], inputs, False)


if __name__ == '__main__':
    unittest.main()
