"""Wait, for a bounded time, until Celery workers report no active tasks.

Polls `celery inspect active` through ECS Exec in a running service task. The
broker is never purged: a message left in a queue is work that the incoming
code will pick up, and the deployment decides separately whether that is
acceptable. A timeout is reported through the `idle` output rather than as a
failure; the caller decides what a busy fleet means for its rollout.
"""
import json
import os
import re
import shlex
import time

from ecs_django import execute, task

FAILED = 'CELERY_INSPECT_FAILED'


def parse(output):
    """Return {worker: [active tasks]} from the inspect output, or None when no worker replied."""
    if re.search(rf'^{FAILED}=\d+$', output, re.MULTILINE):
        return None
    replies = None
    for line in output.replace('\r', '').splitlines():
        line = line.strip()
        if line.startswith('{'):
            replies = json.loads(line)
    if not isinstance(replies, dict) or not replies:
        raise RuntimeError('Unrecognized celery inspect output')
    return replies


def main():
    app = os.environ['APP']
    timeout = int(os.environ.get('TIMEOUT_SECONDS', '120'))
    interval = int(os.environ.get('POLL_INTERVAL_SECONDS', '10'))
    minimum = int(os.environ.get('MIN_WORKERS', '1'))
    if timeout < 0 or interval < 1 or minimum < 1:
        raise ValueError('timeout must be nonnegative; poll interval and min-workers must be positive')
    # A failed inspect (no node replied in time) ends the remote command with
    # a marker rather than a nonzero status, so the exec framing stays intact
    # and the poll can distinguish "nobody answered" from a broken session.
    command = (f'celery -A {shlex.quote(app)} inspect active --json --timeout 10'
               f' || printf \'\\n{FAILED}=%s\\n\' "$?"')
    arn = task()
    deadline = time.monotonic() + timeout
    replied, active = 0, 0
    while True:
        replies = parse(execute(arn, command))
        if replies is None:
            replied, active = 0, 0
            print('No worker replied to inspect.')
        else:
            replied = len(replies)
            active = sum(len(tasks) for tasks in replies.values())
            print(f'{replied} worker(s) replied, {active} active task(s).')
            if replied >= minimum and active == 0:
                idle = True
                break
        if time.monotonic() >= deadline:
            idle = False
            break
        time.sleep(interval)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'idle={str(idle).lower()}\nworkers-replied={replied}\nactive-tasks={active}\n')
    if idle:
        print(f'Workers idle: {replied} replied with no active tasks.')
    else:
        print(f'::warning::Workers not idle after {timeout}s: {replied} replied, {active} task(s) active.')


if __name__ == '__main__':
    main()
