"""Describe a Celery app's registered tasks and beat schedule, format 1.

Executed inside the application image by the `celery-task-manifest` action
(`python -c`, no network, read-only root); never installed in the application.
CELERY_APP names the app the way `celery -A` does (`perma`, `proj.celery:app`),
and is resolved by Celery's own `find_app`, so the manifest describes the app
the workers actually run. When the app configures itself from Django settings,
Django is set up first, as `celery worker` does, so autodiscovered task
modules import.

    {"format": 1,
     "hash": "<12 hex>",                 over {"beat": ..., "tasks": ...}
     "default_queue": "<queue>",
     "tasks": {"<name>": {"argspec": "<12 hex>", "queue": "<queue>"}},
     "beat":  {"<entry>": {"task": "<name>", "schedule": "<repr>", "queue": "<queue>"}}}

Celery's own tasks (`celery.*`) are left out. `argspec` hashes the task's run
function's parameters (name, kind, repr of default) in order, so it changes
when the call signature does and not when the body does. `queue` is the task's
own `queue` option, else whatever the app's router (task_routes in any of its
forms) sends the task to, else the default queue.
"""
import hashlib
import inspect
import json
import os

from celery.app.utils import find_app

HASH_LENGTH = 12


def digest(document):
    payload = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()[:HASH_LENGTH]


def argspec(function):
    return digest([
        [parameter.name, parameter.kind.name,
         None if parameter.default is inspect.Parameter.empty else repr(parameter.default)]
        for parameter in inspect.signature(function).parameters.values()
    ])


def queue_name(queue):
    return getattr(queue, 'name', queue)


app = find_app(os.environ['CELERY_APP'])
if os.environ.get('DJANGO_SETTINGS_MODULE'):
    import django
    django.setup()
app.loader.import_default_modules()
default_queue = app.conf.task_default_queue


def task_queue(name, task=None):
    if task is not None and getattr(task, 'queue', None):
        return queue_name(task.queue)
    routed = app.amqp.router.route({}, name, (), {}).get('queue')
    return queue_name(routed) if routed else default_queue


tasks = {
    name: {'argspec': argspec(task.run), 'queue': task_queue(name, task)}
    for name, task in app.tasks.items()
    if not name.startswith('celery.')
}
beat = {
    name: {
        'task': entry['task'],
        'schedule': repr(entry['schedule']),
        'queue': (entry.get('options') or {}).get('queue') or task_queue(entry['task'], app.tasks.get(entry['task'])),
    }
    for name, entry in (app.conf.beat_schedule or {}).items()
}
print('CELERY_MANIFEST=' + json.dumps({
    'format': 1,
    'hash': digest({'tasks': tasks, 'beat': beat}),
    'default_queue': default_queue,
    'tasks': tasks,
    'beat': beat,
}, sort_keys=True))
