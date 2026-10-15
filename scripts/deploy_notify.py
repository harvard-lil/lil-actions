"""Post a deploy's result to a Slack incoming webhook.

One step for both outcomes, chosen by STATUS, so a consumer has one Slack
step under `if: always()` rather than a success step and a failure step whose
payloads drift apart. The blocks are the ones H2O's two steps built: a
headline, the repository, branch and commit, the state summary deploy-outcome
produced, and links to the run and the site. Sent with curl so no third-party
action holds the webhook and nothing secret passes through a job output.
"""
import json
import os
import subprocess
import tempfile

HEADLINES = {
    'success': '{tier} {product} deployed successfully!',
    'failure': '{tier} {product} deploy FAILED',
    'cancelled': '{tier} {product} deploy CANCELLED',
}


def section(text):
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}


def build_payload(status, product, tier_label, site_url, summary, run_url, commit, repository, ref_name, server_url):
    headline = HEADLINES[status].format(tier=tier_label, product=product)
    repo_url = f'{server_url}/{repository}'
    blocks = [
        section(f'*{headline}*'),
        section(f'*Repo:*\n<{repo_url}|`{repository}`>\n'
                f'*Branch:*\n<{repo_url}/tree/{ref_name}|`{ref_name}`>\n'
                f'*Commit:*\n<{repo_url}/commit/{commit}|`{commit}`>'),
    ]
    if summary:
        blocks.append(section(summary))
    blocks.append(section(f'*Run:* <{run_url}|{run_url.rsplit("/", 1)[-1]}>\n*View the site:* <{site_url}|{site_url}>'))
    return {'text': headline, 'blocks': blocks}


def post(webhook, payload):
    with tempfile.TemporaryDirectory() as directory:
        body = os.path.join(directory, 'payload.json')
        response = os.path.join(directory, 'response')
        with open(body, 'w') as handle:
            json.dump(payload, handle)
        status = subprocess.run(
            ['curl', '--silent', '--show-error', '--request', 'POST',
             '--header', 'Content-type: application/json', '--data', f'@{body}',
             '--output', response, '--write-out', '%{http_code}', webhook],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if status != '200':
            with open(response) as handle:
                print(f'::error::Slack answered HTTP {status}: {handle.read().strip()}')
            raise SystemExit(1)
    print(f'Posted: {payload["text"]}')


def main():
    status = os.environ['STATUS'].strip()
    if status not in HEADLINES:
        raise ValueError(f'status must be one of {", ".join(HEADLINES)}, got {status!r}')
    webhook = os.environ['WEBHOOK']
    if not webhook.strip():
        raise ValueError('webhook is empty; pass the incoming-webhook URL secret')
    payload = build_payload(
        status, os.environ['PRODUCT'], os.environ['TIER_LABEL'], os.environ['SITE_URL'],
        os.environ.get('SUMMARY', '').strip(), os.environ['RUN_URL'], os.environ['COMMIT'],
        os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_REF_NAME'],
        os.environ.get('GITHUB_SERVER_URL', 'https://github.com'),
    )
    post(webhook, payload)


if __name__ == '__main__':
    main()
