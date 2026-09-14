import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('smoke', Path(__file__).parents[1] / 'scripts/container_smoke_test.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTest(unittest.TestCase):
    def test_health_and_failure_both_cleanup(self):
        for running in [True, False]:
            with self.subTest(running=running), patch.dict(os.environ, {'IMAGE': 'image', 'PROBE_COMMAND': '["probe"]'}, clear=True), patch.object(smoke, 'run', side_effect=['container', json.dumps([{'State': {'Running': running}}])]), patch.object(smoke.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                if running:
                    smoke.smoke_test()
                else:
                    with self.assertRaises(RuntimeError):
                        smoke.smoke_test()
                self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'rm', '-f', '-v', 'container'])

    def test_hanging_probe_still_cleans_up(self):
        with patch.dict(os.environ, {'IMAGE': 'image', 'PROBE_COMMAND': '["probe"]'}, clear=True), patch.object(smoke, 'run', side_effect=['container', json.dumps([{'State': {'Running': True}}])]), patch.object(smoke.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('probe', 30), subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                smoke.smoke_test()
            self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'rm', '-f', '-v', 'container'])

    def test_invalid_probe_does_not_start_container(self):
        with patch.dict(os.environ, {'PROBE_COMMAND': '"shell string"'}, clear=True), patch.object(smoke, 'run') as run:
            with self.assertRaises(ValueError):
                smoke.smoke_test()
            run.assert_not_called()
