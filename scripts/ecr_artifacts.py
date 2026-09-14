"""Publish and fetch single-file OCI referrers without pulling application images."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


def digest(value):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('Expected a SHA-256 OCI digest')
    return value


def fetch(image, artifacts):
    registry, repository = image.split('/', 1)
    repository, subject = repository.rsplit('@', 1)
    digest(subject)
    password = subprocess.check_output(['aws', 'ecr', 'get-login-password'], text=True).strip()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'response'

        def get(path, accept, expected=None):
            try:
                status = subprocess.check_output([
                    'curl', '--silent', '--show-error', '--location', '--retry', '3',
                    '--user', 'AWS:' + password, '--header', 'Accept: ' + accept,
                    '--output', str(target), '--write-out', '%{http_code}',
                    f'https://{registry}/v2/{repository}/{path}',
                ], text=True)
            except subprocess.CalledProcessError:
                # curl's argv contains the registry token; do not expose it in
                # an exception traceback when a transport request fails.
                raise RuntimeError('Registry transport request failed') from None
            if status != '200':
                raise RuntimeError(f'Registry returned HTTP {status} for {path}')
            content = target.read_bytes()
            if expected and 'sha256:' + hashlib.sha256(content).hexdigest() != digest(expected):
                raise RuntimeError('Registry artifact digest mismatch')
            return content

        for artifact in artifacts:
            kind = artifact['type']
            index = json.loads(get(f'referrers/{subject}?artifactType={quote(kind, safe="")}',
                                   'application/vnd.oci.image.index.v1+json'))
            layers = {}
            for candidate in index['manifests']:
                if candidate.get('artifactType') != kind:
                    continue
                referrer = digest(candidate['digest'])
                manifest = json.loads(get('manifests/' + referrer,
                                          'application/vnd.oci.image.manifest.v1+json', referrer))
                if manifest.get('subject', {}).get('digest') != subject or manifest.get('artifactType') != kind:
                    raise RuntimeError('Referrer does not match its requested subject and type')
                if len(manifest.get('layers', [])) != 1:
                    raise RuntimeError('Expected a single-file referrer')
                layer = manifest['layers'][0]
                layers[digest(layer['digest'])] = layer
            if len(layers) != 1:
                raise RuntimeError(f'Missing or conflicting referrers for {kind}; repair publication before deploying')
            layer = next(iter(layers.values()))
            content = get('blobs/' + layer['digest'], '*/*', layer['digest'])
            if len(content) != layer['size']:
                raise RuntimeError('Artifact size mismatch')
            destination = Path(artifact['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            print(f'Fetched {kind}: {len(content)} bytes')


def publish(image, artifacts):
    digest(image.rsplit('@', 1)[1])
    for artifact in artifacts:
        path = Path(artifact['path']).resolve(strict=True)
        subprocess.run([
            'oras', 'attach', '--artifact-type', artifact['type'],
            '--annotation', 'org.opencontainers.image.created=1970-01-01T00:00:00Z',
            image, path.name + ':' + artifact['media-type'],
        ], cwd=path.parent, check=True)


if __name__ == '__main__':
    operation = {'publish': publish, 'fetch': fetch}[os.environ['MODE']]
    operation(os.environ['IMAGE'], json.loads(os.environ['ARTIFACTS']))
