"""Resolve force, skip and hold maintenance flags from their three sources.

A merged pull request's labels, a workflow_dispatch's inputs and a standing
variable all express the same three intentions. Each consumer used to read a
different subset in its own way; this reads all of them and applies one set of
rules: hold implies force, and force wins over skip.
"""
import json
import os
import subprocess


def read_labels():
    """Labels on the pull requests the deployed commit belongs to.

    A push carries no pull request context, so the labels are looked up from
    the commit. The lookup failing, or finding nothing, is a warning: a direct
    push, or a merge whose pull request has since been deleted, is a deploy
    that proceeds on its own judgement rather than one that stops.
    """
    repository = os.environ['GITHUB_REPOSITORY']
    sha = os.environ['GITHUB_SHA']
    try:
        output = subprocess.run(
            ['gh', 'api', f'repos/{repository}/commits/{sha}/pulls', '--jq', '.[].labels[].name'],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        print(error.stderr, end='')
        print(f'::warning::Could not read pull request labels for {sha}. Continuing as though none were set.')
        return []
    labels = [line.strip() for line in output.splitlines() if line.strip()]
    if not labels:
        print(f'::warning::No pull request labels found for {sha}: either no pull request '
              'is associated with this commit or it carries none. Continuing as though none were set.')
    return labels


def truthy(value):
    """A boolean dispatch input arrives as JSON true; a string one as 'true'."""
    return value is True or (isinstance(value, str) and value.strip().lower() == 'true')


def resolve(labels, event_name, dispatch_inputs, hold_variable):
    """Return (flags, sources): the three booleans and where each true one came from."""
    prefix = os.environ.get('LABEL_PREFIX', 'deploy:')
    label_names = {
        'force': prefix + os.environ.get('FORCE_LABEL', 'force-maintenance-mode'),
        'skip': prefix + os.environ.get('SKIP_LABEL', 'skip-maintenance-mode'),
    }
    input_keys = {
        'force': os.environ.get('INPUT_FORCE', 'force-maintenance'),
        'skip': os.environ.get('INPUT_SKIP', 'skip-maintenance'),
        'hold': os.environ.get('INPUT_HOLD', 'hold-maintenance'),
    }

    sources = {'force': [], 'skip': [], 'hold': []}
    for flag, label in label_names.items():
        if label in labels:
            sources[flag].append(f'label {label}')
    if event_name == 'workflow_dispatch':
        for flag, key in input_keys.items():
            if truthy(dispatch_inputs.get(key)):
                sources[flag].append(f'dispatch input {key}')
    if truthy(hold_variable):
        sources['hold'].append('variable')

    hold = bool(sources['hold'])
    if hold and not sources['force']:
        sources['force'].append('hold')
    force = bool(sources['force'])
    skip_requested = bool(sources['skip'])
    # Whichever was meant, keeping traffic off a schema in motion is the safe
    # reading of an ambiguous instruction.
    skip = skip_requested and not force

    parts = []
    for flag in ('force', 'skip', 'hold'):
        if sources[flag]:
            part = f'{flag} from {" and ".join(sources[flag])}'
            if flag == 'skip' and force:
                part += ' (overridden: force wins)'
            parts.append(part)
    return {'force': force, 'skip': skip, 'hold': hold}, '; '.join(parts) or 'none'


def main():
    event_name = os.environ['EVENT_NAME']
    if not event_name:
        raise ValueError('event-name must be set, normally to github.event_name')
    dispatch_inputs = json.loads(os.environ.get('DISPATCH_INPUTS') or '{}')
    if not isinstance(dispatch_inputs, dict):
        raise ValueError('inputs must be a JSON object, normally toJSON(inputs)')

    labels = read_labels()
    flags, sources = resolve(labels, event_name, dispatch_inputs, os.environ.get('HOLD_VARIABLE', ''))

    lines = [f'{flag}: {str(value).lower()}' for flag, value in flags.items()]
    print(f'Event: {event_name}')
    print(f'Labels: {", ".join(labels) or "(none)"}')
    print('\n'.join(lines))
    print(f'Sources: {sources}')

    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for flag, value in flags.items():
            output.write(f'{flag}={str(value).lower()}\n')
        output.write(f'sources={sources}\n')
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a') as summary:
            summary.write('### Deploy flags\n\n')
            summary.write('| Flag | Value |\n| --- | --- |\n')
            for flag, value in flags.items():
                summary.write(f'| {flag} | {str(value).lower()} |\n')
            summary.write(f'\nSources: {sources}\n\nLabels: {", ".join(labels) or "(none)"}\n')


if __name__ == '__main__':
    main()
