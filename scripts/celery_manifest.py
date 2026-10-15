"""Celery task manifests: validation, image inspection and comparison.

A format-1 manifest lists the Celery tasks an image registers and the beat
schedule it would run. Deployments compare the incoming image's manifest with
the running one to decide whether workers can be replaced without pausing
intake: an unchanged manifest means the outgoing code's queued messages are
ones the incoming code registers, with the same arguments, on the same queues.

The document is produced inside the image by celery_task_inspect.py, which
this module runs there with `python -c`, so applications carry no manifest
code of their own (the same arrangement as django_manifest.py). These helpers
also validate, carry and compare it. Format 1, in outline:

    {"format": 1,
     "tasks": {"<task name>": {"argspec": "<hash>", "queue": "<queue>"}},
     "beat":  {"<entry name>": {"task": "<task name>", "schedule": "<text>"}}}

`tasks` and `beat` may also be arrays of objects carrying a `name`; they are
normalised to the keyed form above. Every field of an entry is compared.
"""
import json
import os
import subprocess
from pathlib import Path


def normalise(document):
    if not isinstance(document, dict) or document.get('format') != 1:
        raise ValueError('Unsupported Celery task manifest: expected format 1')
    if 'tasks' not in document:
        raise ValueError('A Celery task manifest must list its tasks')
    normalised = {'format': 1}
    for section in ('tasks', 'beat'):
        entries = document.get(section, {})
        if isinstance(entries, list):
            keyed = {}
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get('name'), str):
                    raise ValueError(f'Every {section} entry must be an object with a name')
                entry = dict(entry)
                keyed[entry.pop('name')] = entry
            entries = keyed
        if not isinstance(entries, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict) for key, value in entries.items()
        ):
            raise ValueError(f'{section} must map entry names to objects')
        normalised[section] = {key: entries[key] for key in sorted(entries)}
    return normalised


def load(path):
    return normalise(json.loads(Path(path).read_text()))


def source():
    return Path(__file__).with_name('celery_task_inspect.py').read_text()


def parse(output):
    documents = [json.loads(line.removeprefix('CELERY_MANIFEST='))
                 for line in output.replace('\r', '').splitlines()
                 if line.startswith('CELERY_MANIFEST=')]
    if len(documents) != 1:
        raise ValueError('Expected one Celery task manifest')
    return normalise(documents[0])


def inspect_image():
    """Run the shared inspector in the image, offline, and return the document."""
    environment = json.loads(os.environ.get('IMAGE_ENVIRONMENT', '{}'))
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not key or '=' in key or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValueError('Image environment must be a JSON object of string values')
    if not os.environ.get('CELERY_APP'):
        raise ValueError('Name the Celery app as `celery -A` takes it')
    args = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp',
            '--entrypoint', 'python']
    for key, value in environment.items():
        args += ['--env', f'{key}={value}']
    args += ['--env', 'CELERY_APP=' + os.environ['CELERY_APP']]
    if os.environ.get('SETTINGS_MODULE'):
        args += ['--env', 'DJANGO_SETTINGS_MODULE=' + os.environ['SETTINGS_MODULE']]
    if os.environ.get('WORKING_DIRECTORY'):
        args += ['--workdir', os.environ['WORKING_DIRECTORY']]
    args += [os.environ['IMAGE'], '-c', source()]
    return parse(subprocess.check_output(args, text=True))


def compare(base, incoming):
    """Return (changed, lines) describing what differs between two normalised manifests."""
    lines = []
    for section, singular in (('tasks', 'task'), ('beat', 'beat entry')):
        before, after = base[section], incoming[section]
        for name in sorted(set(after) - set(before)):
            lines.append(f'added {singular} {name}')
        for name in sorted(set(before) - set(after)):
            lines.append(f'removed {singular} {name}')
        for name in sorted(set(before) & set(after)):
            if before[name] != after[name]:
                fields = sorted(key for key in set(before[name]) | set(after[name])
                                if before[name].get(key) != after[name].get(key))
                lines.append(f'changed {singular} {name} ({", ".join(fields)})')
    return bool(lines), lines


def write_outputs(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')


def main(mode):
    if mode == 'inspect':
        document = inspect_image()
        Path(os.environ['MANIFEST']).write_text(json.dumps(document, indent=2) + '\n')
        print(f'{len(document["tasks"])} tasks and {len(document["beat"])} beat entries '
              f'written to {os.environ["MANIFEST"]}')
    elif mode == 'compare':
        changed, lines = compare(load(os.environ['BASE']), load(os.environ['INCOMING']))
        summary = '; '.join(lines) or 'no changes'
        print('\n'.join(lines) or summary)
        write_outputs(changed=str(changed).lower(), summary=summary)
    else:
        raise ValueError('Expected inspect or compare')


if __name__ == '__main__':
    import sys
    main(sys.argv[1])
