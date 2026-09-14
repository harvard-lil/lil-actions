"""Runner-side invocation of the shared Django manifest inspector."""
import json
import os
import shlex
import subprocess
from pathlib import Path


def source():
    return Path(__file__).with_name('django_manifest.py').read_text()


def configure(command):
    # ECS Exec uses the container's normal settings and working directory unless
    # the workflow specifies them. No helper file is copied into the container.
    prefix = ''
    if os.environ.get('WORKING_DIRECTORY'):
        prefix += 'cd ' + shlex.quote(os.environ['WORKING_DIRECTORY']) + ' && '
    if os.environ.get('SETTINGS_MODULE'):
        prefix += 'DJANGO_SETTINGS_MODULE=' + shlex.quote(os.environ['SETTINGS_MODULE']) + ' '
    return prefix + command


def command():
    return configure('python -c ' + shlex.quote(source()))


def parse(output):
    documents = [json.loads(line.removeprefix('DJANGO_MANIFEST='))
                 for line in output.replace('\r', '').splitlines()
                 if line.startswith('DJANGO_MANIFEST=')]
    if len(documents) != 1:
        raise ValueError('Expected one Django migration manifest')
    return documents[0]


def inspect_image():
    environment = json.loads(os.environ.get('IMAGE_ENVIRONMENT', '{}'))
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not key or '=' in key or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError('Image environment must be a JSON object of string values')
    args = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
            '--entrypoint', 'python']
    for key, value in environment.items():
        args += ['--env', f'{key}={value}']
    if os.environ.get('SETTINGS_MODULE'):
        args += ['--env', 'DJANGO_SETTINGS_MODULE=' + os.environ['SETTINGS_MODULE']]
    if os.environ.get('WORKING_DIRECTORY'):
        args += ['--workdir', os.environ['WORKING_DIRECTORY']]
    args += [os.environ['IMAGE'], '-c', source()]
    return parse(subprocess.check_output(args, text=True))


if __name__ == '__main__':
    Path(os.environ['MANIFEST']).write_text(json.dumps(inspect_image(), indent=2) + '\n')
