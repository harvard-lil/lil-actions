"""Celery manifest comparison and idle waiting without contacting AWS."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / f'scripts/{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load('celery_manifest')
wait_idle = load('celery_wait_idle')


class ManifestTests(unittest.TestCase):
    def test_array_and_object_forms_normalise_alike(self):
        keyed = manifest.normalise({'format': 1, 'tasks': {'b': {'queue': 'q'}, 'a': {'queue': 'q'}}})
        listed = manifest.normalise({'format': 1, 'tasks': [{'name': 'b', 'queue': 'q'}, {'name': 'a', 'queue': 'q'}]})
        self.assertEqual(keyed, listed)
        self.assertEqual(list(keyed['tasks']), ['a', 'b'])
        self.assertEqual(keyed['beat'], {})

    def test_rejects_other_formats_and_shapes(self):
        for document in [{'format': 2, 'tasks': {}}, {'format': 1}, {'format': 1, 'tasks': [{'queue': 'q'}]},
                         {'format': 1, 'tasks': {'a': 'x'}}, []]:
            with self.subTest(document=document), self.assertRaises(ValueError):
                manifest.normalise(document)

    def test_compare_names_every_difference(self):
        base = manifest.normalise({'format': 1, 'tasks': {'a': {'argspec': '1', 'queue': 'q'}, 'b': {'argspec': '2', 'queue': 'q'}},
                                   'beat': {'nightly': {'task': 'a', 'schedule': '0 0'}}})
        incoming = manifest.normalise({'format': 1, 'tasks': {'a': {'argspec': '1', 'queue': 'other'}, 'c': {'argspec': '3', 'queue': 'q'}},
                                       'beat': {'nightly': {'task': 'a', 'schedule': '0 1'}}})
        changed, lines = manifest.compare(base, incoming)
        self.assertTrue(changed)
        self.assertEqual(lines, ['added task c', 'removed task b', 'changed task a (queue)',
                                 'changed beat entry nightly (schedule)'])
        self.assertEqual(manifest.compare(base, base), (False, []))


class InspectImageTests(unittest.TestCase):
    def test_runs_the_shared_inspector_offline(self):
        document = {'format': 1, 'hash': 'abcdef123456', 'default_queue': 'celery',
                    'tasks': {'a': {'argspec': '1', 'queue': 'q'}}, 'beat': {}}
        environment = {'IMAGE': 'image@sha256:digest', 'CELERY_APP': 'perma',
                       'IMAGE_ENVIRONMENT': '{"PERMA_SETTINGS_MODULE":"settings_build"}',
                       'SETTINGS_MODULE': 'perma.settings', 'WORKING_DIRECTORY': '/app'}
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(manifest.subprocess, 'check_output',
                          return_value='import noise\nCELERY_MANIFEST=' + json.dumps(document)) as run:
            self.assertEqual(manifest.inspect_image(), manifest.normalise(document))
            args = run.call_args.args[0]
            self.assertEqual(args[-1], manifest.source())
            self.assertIn('--read-only', args)
            self.assertEqual(args[args.index('--network') + 1], 'none')
            for value in ['CELERY_APP=perma', 'PERMA_SETTINGS_MODULE=settings_build',
                          'DJANGO_SETTINGS_MODULE=perma.settings']:
                self.assertIn(value, args)

    def test_missing_or_multiple_documents_fail(self):
        for output in ['no manifest', 'CELERY_MANIFEST={"format":1,"tasks":{}}\nCELERY_MANIFEST={"format":1,"tasks":{}}']:
            with self.subTest(output=output), self.assertRaises(ValueError):
                manifest.parse(output)

    def test_bad_input_does_not_launch_container(self):
        for environment in [{'IMAGE_ENVIRONMENT': '[]', 'CELERY_APP': 'perma'},
                            {'IMAGE_ENVIRONMENT': '{"KEY":1}', 'CELERY_APP': 'perma'},
                            {'IMAGE_ENVIRONMENT': '{}'}]:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                 patch.object(manifest.subprocess, 'check_output') as run:
                with self.assertRaises(ValueError):
                    manifest.inspect_image()
                run.assert_not_called()


APP_MODULE = """
from celery import Celery
from celery.schedules import crontab

app = Celery('demo')
app.conf.task_routes = ROUTES
app.conf.beat_schedule = {
    'nightly': {'task': 'demo.routed', 'schedule': crontab(minute=0, hour=3)},
    'pinned': {'task': 'demo.plain', 'schedule': 60.0, 'options': {'queue': 'beat-only'}},
}

@app.task(name='demo.routed')
def routed(link_guid, attempts=3):
    pass

@app.task(name='demo.plain')
def plain():
    pass

@app.task(name='demo.own_queue', queue='own')
def own_queue(*args, **kwargs):
    pass
"""


@unittest.skipUnless(importlib.util.find_spec('celery'), 'celery is not installed')
class InspectorTests(unittest.TestCase):
    """Run celery_task_inspect.py against small real apps, as the image would."""

    def inspect(self, routes, module='demo_app', app_source=APP_MODULE):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, f'{module}.py').write_text(app_source.replace('ROUTES', routes))
            output = subprocess.check_output(
                [sys.executable, '-c', manifest.source()], text=True, cwd=directory,
                env={'PATH': os.environ['PATH'], 'PYTHONPATH': directory, 'CELERY_APP': f'{module}:app'})
        [line] = [line for line in output.splitlines() if line.startswith('CELERY_MANIFEST=')]
        return json.loads(line.removeprefix('CELERY_MANIFEST='))

    def test_every_routing_form_gives_the_same_queues(self):
        forms = ["{'demo.routed': {'queue': 'capture'}}",
                 "[{'demo.routed': {'queue': 'capture'}}]",
                 "{'demo.rout*': {'queue': 'capture'}}"]
        documents = [self.inspect(form, module=f'demo_app_{index}') for index, form in enumerate(forms)]
        for document in documents:
            self.assertEqual(document, documents[0])
        document = documents[0]
        self.assertEqual(sorted(document['tasks']), ['demo.own_queue', 'demo.plain', 'demo.routed'])
        self.assertEqual({name: task['queue'] for name, task in document['tasks'].items()},
                         {'demo.routed': 'capture', 'demo.plain': 'celery', 'demo.own_queue': 'own'})
        self.assertEqual(document['beat']['nightly']['queue'], 'capture')
        self.assertEqual(document['beat']['pinned']['queue'], 'beat-only')
        self.assertEqual(document['default_queue'], 'celery')

    def test_argspec_tracks_the_signature_not_the_body(self):
        base = self.inspect('{}', module='sig_a')
        body = APP_MODULE.replace("def routed(link_guid, attempts=3):\n    pass",
                                  "def routed(link_guid, attempts=3):\n    return link_guid")
        signature = APP_MODULE.replace('attempts=3', 'attempts=4')
        results = {name: self.inspect('{}', module=f'sig_{name}', app_source=source)
                   for name, source in [('body', body), ('signature', signature)]}
        argspec = lambda document: document['tasks']['demo.routed']['argspec']
        self.assertEqual(argspec(results['body']), argspec(base))
        self.assertNotEqual(argspec(results['signature']), argspec(base))
        self.assertNotEqual(results['signature']['hash'], base['hash'])


class WaitIdleTests(unittest.TestCase):
    def test_parse_handles_replies_failures_and_noise(self):
        self.assertEqual(wait_idle.parse('-> w1@a: OK\n{"w1@a": []}\n'), {'w1@a': []})
        self.assertIsNone(wait_idle.parse('Error: No nodes replied within time constraint\nCELERY_INSPECT_FAILED=69\n'))
        with self.assertRaises(RuntimeError):
            wait_idle.parse('nothing useful')

    def test_polls_until_idle_or_deadline(self):
        environment = {'APP': 'perma', 'TIMEOUT_SECONDS': '30', 'POLL_INTERVAL_SECONDS': '1', 'GITHUB_OUTPUT': os.devnull}
        with patch.dict(os.environ, environment), patch.object(wait_idle, 'task', return_value='task'), \
             patch.object(wait_idle.time, 'sleep'), \
             patch.object(wait_idle, 'execute', side_effect=['{"w": [{"id": 1}]}', 'CELERY_INSPECT_FAILED=69', '{"w": []}']) as execute:
            wait_idle.main()
            self.assertEqual(execute.call_count, 3)
            self.assertIn('inspect active', execute.call_args.args[1])
            self.assertNotIn('purge', execute.call_args.args[1])
        with patch.dict(os.environ, environment | {'TIMEOUT_SECONDS': '0', 'MIN_WORKERS': '2'}), \
             patch.object(wait_idle, 'task', return_value='task'), \
             patch.object(wait_idle, 'execute', return_value='{"w": []}'):
            wait_idle.main()  # one worker replied, two required: reported, not raised


if __name__ == '__main__':
    unittest.main()
