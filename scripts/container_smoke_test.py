"""Start a container and wait for its caller-supplied health probe."""
import json
import os
import subprocess
import time


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def smoke_test():
    probe = json.loads(os.environ['PROBE_COMMAND'])
    environment = json.loads(os.environ.get('CONTAINER_ENVIRONMENT', '{}'))
    timeout = int(os.environ.get('TIMEOUT_SECONDS', '30'))
    if not isinstance(probe, list) or not probe or not all(isinstance(arg, str) for arg in probe):
        raise ValueError('probe-command must be a nonempty JSON array of strings')
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not key or '=' in key or not isinstance(value, str)
        for key, value in environment.items()
    ) or timeout <= 0:
        raise ValueError('Invalid container environment or timeout')
    args = ['docker', 'run', '-d', '--read-only', '--tmpfs', '/tmp', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--network', 'none']
    for key, value in environment.items():
        args += ['--env', f'{key}={value}']
    container = run(*args, os.environ['IMAGE'])
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = json.loads(run('docker', 'inspect', container))[0]['State']
            if not state['Running']:
                raise RuntimeError('Container exited before becoming healthy')
            result = subprocess.run(['docker', 'exec', container, *probe], capture_output=True,
                                    timeout=max(0.1, deadline - time.monotonic()))
            if result.returncode == 0:
                print('Container smoke test passed.')
                return
            time.sleep(1)
        raise TimeoutError('Container did not become healthy before the deadline')
    finally:
        # Failure to collect logs must not prevent cleanup or mask the result.
        subprocess.run(['docker', 'logs', container], check=False)
        subprocess.run(['docker', 'rm', '-f', '-v', container], check=True)


if __name__ == '__main__':
    smoke_test()
