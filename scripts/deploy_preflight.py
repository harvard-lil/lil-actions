"""Check a deploy's inputs before anything changes.

An empty secret would otherwise be found at the maintenance step, after the
rollout had begun; a malformed digest at the task-definition step. These are
the shape and presence checks Payments and Perma already run by hand at the
top of their sequences. Values are never printed: a failure names the input,
not what was in it. No API permission is probed here; PR CI cannot prove a
deployment credential works, and this does not pretend to.
"""
import json
import os
import re

DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
SHA = re.compile(r'^[0-9a-f]{40}$')


def check(image_digest, source_sha, tier, allowed_tiers, required_secrets):
    """Return the list of problems, empty when the deploy may proceed."""
    problems = []
    if not DIGEST.match(image_digest):
        problems.append('image-digest is not a sha256 digest')
    if not SHA.match(source_sha):
        problems.append('source-sha is not a 40-character commit SHA')
    if tier not in allowed_tiers:
        problems.append(f'tier {tier!r} is not one of {", ".join(allowed_tiers)}')
    for name, value in required_secrets.items():
        if not isinstance(value, str):
            problems.append(f'required secret {name} is not a string')
        elif not value.strip():
            problems.append(f'required secret {name} is empty in the {tier} environment')
    return problems


def main():
    allowed_tiers = [tier.strip() for tier in os.environ.get('ALLOWED_TIERS', '').split(',') if tier.strip()]
    if not allowed_tiers:
        raise ValueError('allowed-tiers must name at least one tier')
    try:
        required_secrets = json.loads(os.environ.get('REQUIRED_SECRETS') or '{}')
    except json.JSONDecodeError as error:
        # The message names the position only; the document holds secret values.
        raise ValueError(f'required-secrets is not valid JSON (at character {error.pos})') from None
    if not isinstance(required_secrets, dict):
        raise ValueError('required-secrets must be a JSON object of name to value')

    tier = os.environ.get('TIER', '').strip()
    problems = check(os.environ.get('IMAGE_DIGEST', '').strip(), os.environ.get('SOURCE_SHA', '').strip(),
                     tier, allowed_tiers, required_secrets)
    for problem in problems:
        print(f'::error::{problem}')
    if problems:
        raise SystemExit(1)
    print(f'image-digest: {os.environ["IMAGE_DIGEST"].strip()}')
    print(f'source-sha: {os.environ["SOURCE_SHA"].strip()}')
    print(f'tier: {tier}')
    for name in required_secrets:
        print(f'secret {name}: set')


if __name__ == '__main__':
    main()
