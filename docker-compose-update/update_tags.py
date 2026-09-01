import argparse
import hashlib
import requests
from pathlib import Path
import os

import yaml


# update hashes in docker-compose.yml and docker-compose.override.yml, and return services affected by the update


def get_hash(input_paths, init_string=""):
    """Get hash of files or directories."""
    hasher = hashlib.sha256()
    hasher.update(init_string.encode('utf-8'))
    for path in sorted(input_paths):
        path = Path(path)
        if path.is_file():
            file_paths = [path]
        elif path.is_dir():
            file_paths = sorted(p for p in path.glob('**/*') if p.is_file())
        else:
            raise ValueError(f"x-hash-paths: {path} is not a file or directory")
        for file_path in file_paths:
            print(f" - Hashing {file_path}")
            hasher.update(file_path.read_bytes())
    return hasher.hexdigest()[:32]


def get_changed_tags(override_path, override_text):
    """
        Given text of docker-compose.override.yml, find all values of service.build.x-bake.tags that no longer match,
        based on hashing service.build itself, plus hashing Dockerfile, plus hashing all paths in service.build.x-hash-paths.
        Return list of (service_name, old_tag, new_tag) tuples,
        e.g. ("web", "harvardlil/web:1-oldhash", "harvardlil/web:2-newhash").
    """
    override_yaml = yaml.safe_load(override_text)
    changed_tags = []
    all_services = []

    for service_name, service in override_yaml['services'].items():
        # only process services with an x-hash-paths setting
        if 'build' in service and 'x-hash-paths' in service['build']:
            print(f"- Processing {service_name}")
            build = service['build']
            tags = build['x-bake'].pop('tags')  # pop tags so they won't be included in hash

            # calculate new hash
            context = override_path.parent / build.get('context', '.')
            docker_file_path = context / build.get('dockerfile', 'Dockerfile')
            hash_paths = sorted(set([docker_file_path] + [context / path for path in build['x-hash-paths']]))
            hash = get_hash(hash_paths, init_string=str(service['build']))

            # if new hash isn't in current tag, calculate new tag
            old_tag = tags[0]
            if hash not in old_tag:
                print(f" - Updating to new hash {hash}")
                image_name, image_tag = old_tag.split(':', 1)
                digits, old_hash = image_tag.split('-', 1)
                digits = digits.split('.')
                digits[-1] = str(int(digits[-1]) + 1)
                new_tag = f"{image_name}:{'.'.join(digits)}-{hash}"
                changed_tags.append((service_name, old_tag, new_tag))
                all_services.append((service_name, new_tag))
            else:
                all_services.append((service_name, old_tag))
                print(" - No change")
        else:
            print(f"- Skipping {service_name}")

    return changed_tags, all_services


def anonymous_bearer_token(www_authenticate):
    """Get an anonymous bearer token from the realm named in a WWW-Authenticate header.

    The header looks like:
        Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:org/img:pull"
    We hand realm the service and scope it asked for and get a token back.
    Returns None if the header isn't a Bearer challenge or the token request fails.
    """
    if not www_authenticate.lower().startswith('bearer '):
        return None

    params = {}
    for part in www_authenticate[len('bearer '):].split(','):
        key, sep, value = part.partition('=')
        if sep:
            params[key.strip()] = value.strip().strip('"')

    realm = params.pop('realm', None)
    if not realm:
        return None

    r = requests.get(realm, params=params)
    if r.status_code != 200:
        return None
    body = r.json()
    # GHCR returns "token"; some registries return "access_token".
    return body.get('token') or body.get('access_token')


def remote_tag_exists(tag):
    """Check if a remote docker tag exists.

    Registries implement the Docker Registry v2 auth flow: an unauthenticated
    request is answered with 401 plus a WWW-Authenticate header naming a token
    endpoint, and the real request is retried with that token. Harbor happened to
    serve anonymous reads directly, so this used to work without the round trip;
    GHCR returns 401 even for public packages, so the round trip is required.
    """
    # the full tag is something like example.com/project/image:0.01
    # and we need https://example.com/v2/project/image/tags/list
    remote, version = tag.split(':')
    host, repository = remote.split('/', 1)

    # can we assume https?
    url = f'https://{host}/v2/{repository}/tags/list'
    r = requests.get(url)

    if r.status_code == 401:
        token = anonymous_bearer_token(r.headers.get('www-authenticate', ''))
        if token:
            r = requests.get(url, headers={'Authorization': f'Bearer {token}'})

    if r.status_code != 200:
        # 404 means the repository doesn't exist yet. A 401 that survived the
        # token exchange means it is private or absent and we can't see it.
        # Either way we can't confirm the tag, so report it missing and let the
        # caller rebuild -- pushing an image that already exists is harmless,
        # whereas skipping a needed build is not.
        return False

    # A registry with no tags may return {"name": ..., "tags": null}.
    return version in (r.json().get('tags') or [])


def main(docker_compose_path='docker-compose.yml', action='load'):
    """Get hash based on paths that go into docker image, update docker image tag, and set current tag."""
    # load docker-compose.yml and docker-compose.override.yml files
    print(f"Processing {docker_compose_path}")
    docker_compose_path = Path(docker_compose_path)
    docker_compose_text = docker_compose_path.read_text()
    override_path = docker_compose_path.with_suffix('.override.yml')
    override_text = override_path.read_text()
    changed_tags, all_services = get_changed_tags(override_path, override_text)

    # write updated tags to docker-compose.yml and docker-compose.override.yml
    if changed_tags:
        for service_name, old_tag, new_tag in changed_tags:
            print(f"- Updating {service_name} from {old_tag} to {new_tag}")
            docker_compose_text = docker_compose_text.replace(old_tag, new_tag)
            override_text = override_text.replace(old_tag, new_tag)
        docker_compose_path.write_text(docker_compose_text)
        override_path.write_text(override_text)

    # check which services need rebuild
    if action == 'push':
        to_rebuild = [s[0] for s in all_services if not remote_tag_exists(s[1])]
    else:
        to_rebuild = [c[0] for c in changed_tags if not remote_tag_exists(c[2])]

    # append services-to-rebuild to GITHUB_OUTPUT
    print(f"Setting services-to-rebuild: {to_rebuild}")
    with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
        print(f"services-to-rebuild={' '.join(to_rebuild)}", file=fh)


def run_from_command_line():
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', dest='action', help='Buildx bake action -- if push, will check all services for need to rebuild', choices=['load', 'push'], default='load')
    parser.add_argument('-f', dest='docker_compose_path', help='path to docker-compose.yml', default='docker-compose.yml')
    args = parser.parse_args()
    out = main(args.docker_compose_path, args.action)
    print(out)


if __name__ == '__main__':
    run_from_command_line()
