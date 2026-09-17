"""Celery manifest comparison and idle waiting without contacting AWS."""
import importlib.util
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
