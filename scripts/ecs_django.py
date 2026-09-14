"""Django maintenance decisions and migrations through ECS Exec.

The remote command's result is framed explicitly: an SSM session exit status
alone does not establish that Django succeeded. No database credentials leave ECS.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from image_migration_manifest import command, configure, inspect_image, parse


def aws(*args):
    return subprocess.check_output(
        ['aws', *args], text=True, env={**os.environ, 'AWS_PAGER': ''},
    )


def task():
    arns = json.loads(aws(
        'ecs', 'list-tasks', '--cluster', os.environ['CLUSTER'],
        '--service-name', os.environ['SERVICE'], '--desired-status', 'RUNNING', '--output', 'json',
    ))['taskArns']
    if not arns:
        raise RuntimeError('No running service task')
    expected = os.environ.get('TASK_DEFINITION', '')
    # The caller waits for a stable service before migrating. Still reject a
    # stale task if an operator changed the service between workflow steps.
    details = json.loads(aws('ecs', 'describe-tasks', '--cluster', os.environ['CLUSTER'],
                             '--tasks', arns[0], '--output', 'json'))
    if details.get('failures') or len(details['tasks']) != 1:
        raise RuntimeError('Could not inspect the selected task')
    selected = details['tasks'][0]
    if selected['lastStatus'] != 'RUNNING' or (expected and selected['taskDefinitionArn'] != expected):
        raise RuntimeError('Selected task does not match the requested running revision')
    return arns[0]


def execute(arn, command):
    marker = 'DJANGO_RESULT_' + uuid.uuid4().hex
    wrapped = f"{command}\nstatus=$?\nprintf '\\n{marker}=%s\\n' \"$status\"\nexit \"$status\""
    output = aws('ecs', 'execute-command', '--cluster', os.environ['CLUSTER'],
                 '--task', arn, '--container', os.environ['CONTAINER'],
                 '--interactive', '--command', '/bin/sh -c ' + shlex.quote(wrapped))
    output = output.replace('\r', '')
    statuses = re.findall(rf'^{marker}=(\d+)$', output, re.MULTILINE)
    if statuses != ['0']:
        print(output)
        raise RuntimeError(f'Remote Django command failed or returned no completion marker: {command}')
    return output[:output.index(marker + '=')]


def pending(arn):
    output = execute(arn, configure(os.environ.get('CHECK_COMMAND', 'python manage.py migrate --plan')))
    lines = {line.strip() for line in output.splitlines()}
    if 'No planned migration operations.' in lines:
        return False
    if 'Planned operations:' in lines:
        return True
    raise RuntimeError('Unrecognized Django migration plan')


def maintenance():
    if os.environ.get('FORCE') == 'true':
        return True
    if os.environ.get('SKIP') == 'true':
        print('::warning::Maintenance explicitly skipped; migrations will still run.')
        return False
    # Missing manifests or unavailable checks require a maintenance window.
    # The later migration action must succeed before traffic is released.
    if os.environ.get('IMAGE') and os.environ.get('MANIFEST'):
        raise ValueError('Supply either image or manifest, not both')
    # Image inspection failure stops deployment before changing traffic or ECS.
    document = inspect_image() if os.environ.get('IMAGE') else None
    try:
        if document is None:
            document = json.loads(Path(os.environ['MANIFEST']).read_text())
        if document.get('format') != 1 or not re.fullmatch('[0-9a-f]{12}', document.get('hash', '')):
            raise ValueError('Unsupported migration manifest')
        arn = task()
        legacy = os.environ.get('MANIFEST_COMMAND')
        running = execute(arn, legacy or command())
        if legacy:
            hashes = re.findall(r'"hash"\s*:\s*"([0-9a-f]{12})"', running)
            matches = hashes == [document['hash']]
        else:
            matches = parse(running) == document
        return not matches or pending(arn)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'::warning::Migration check inconclusive; taking maintenance ({type(error).__name__}).')
        return True


def migrate():
    arn = task()
    if not pending(arn):
        print('No pending migrations.')
        return
    print(execute(arn, os.environ.get('MIGRATE_COMMAND', 'python manage.py migrate --noinput')))
    if pending(arn):
        raise RuntimeError('Migrations remain pending after migrate completed')


if __name__ == '__main__':
    if sys.argv[1] == 'maintenance':
        needed = str(maintenance()).lower()
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'needed={needed}\n')
        print(f'Maintenance needed: {needed}')
    elif sys.argv[1] == 'migrate':
        migrate()
    else:
        raise ValueError('Expected maintenance or migrate')
