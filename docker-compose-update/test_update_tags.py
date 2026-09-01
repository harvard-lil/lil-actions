from hashlib import sha256
from pathlib import Path
from random import shuffle
from shutil import copytree
from textwrap import dedent

import pytest
import os

import update_tags


### test helpers ###

def hash_string(s):
    """Hash a string in the same way we hash file contents."""
    return sha256(s.encode('utf8')).hexdigest()[:32]


@pytest.fixture
def test_files(tmp_path):
    """Return path to a temp copy of the test_files directory."""
    test_files_path = tmp_path.joinpath('test_files')
    copytree(Path(__file__).parent / 'test_files', test_files_path)
    return test_files_path


### tests ###

def test_get_hash(tmp_path):
    # set up 10 files
    contents = 'abcdefghij'
    paths = [tmp_path / f'{c}.txt' for c in contents]
    for c, path in zip(contents, paths):
        path.write_text(c)

    # get_hash for list of paths returns hash of their contents concatenated
    shuffle(paths)
    assert update_tags.get_hash(paths, init_string='foo') == hash_string('fooabcdefghij')

    # get_hash for directory includes all files in directory
    assert update_tags.get_hash([tmp_path], init_string='foo') == hash_string('fooabcdefghij')


def test_main(test_files, monkeypatch):
    compose_path = test_files / 'docker-compose.yml'

    # patch remote_tag_exists to return False
    monkeypatch.setattr(update_tags, 'remote_tag_exists', lambda *args: False)

    # first run detects changes to toplevel and subdir
    update_tags.main(compose_path)
    assert github_output() == "services-to-rebuild=toplevel subdir"

    # no update the second time
    update_tags.main(compose_path)
    assert github_output() == "services-to-rebuild="

    # 'push' action checks rebuild for all tags if there are any
    # changes
    update_tags.main(compose_path, action='push')
    assert github_output() == "services-to-rebuild=toplevel subdir"

    # check hash values -- hashes are: build config, Dockerfile contents, x-hash-paths contents
    toplevel_hash = "bd018100e5b1c9159130decc1fa8884c"
    subdir_hash = "e6f9e079c6d2d933a50120f9b20ff869"
    assert toplevel_hash == hash_string(
        "{'context': '.', 'x-bake': {}, 'x-hash-paths': ['a.txt', 'subdir/b.txt']}" +
        '# toplevel\nFROM hello-world:latest' +
        'a' +
        'b'
    )
    assert subdir_hash == hash_string(
        "{'context': 'subdir', 'x-bake': {}, 'x-hash-paths': ['b.txt']}" +
        '# subdir\nFROM hello-world:latest' +
        'b'
    )

    # docker-compose.yml new contents
    assert compose_path.read_text().strip() == dedent(f"""
        services:
          toplevel:
            image: toplevel:0.2-{toplevel_hash}
          subdir:
            image: subdir:2-{subdir_hash}
    """).strip()

    # docker-compose.override.yml new contents
    assert compose_path.with_suffix('.override.yml').read_text().strip() == dedent(f"""
        services:
          toplevel:
            build:
              context: .
              x-bake:
                tags:
                  - toplevel:0.2-{toplevel_hash}
              x-hash-paths:
                - a.txt
                - subdir/b.txt
          subdir:
            build:
              context: subdir
              x-bake:
                tags:
                  - subdir:2-{subdir_hash}
              x-hash-paths:
                - b.txt
    """).strip()


def test_run_from_command_line(monkeypatch):
    def main_patch(docker_compose_path, action):
        assert docker_compose_path == 'foo/docker-compose.yml'
        assert action == 'push'
    monkeypatch.setattr("sys.argv", ["foo", "-a", "push", "-f", "foo/docker-compose.yml"])
    monkeypatch.setattr(update_tags, 'main', main_patch)
    update_tags.run_from_command_line()


def github_output():
    """
    This returns the last (i.e. current) non-blank line of the file
    that GitHub uses to track action output. See
    https://github.blog/changelog/2022-10-11-github-actions-deprecating-save-state-and-set-output-commands/
    which advises appending to this file.
    """
    return Path(
        os.environ['GITHUB_OUTPUT']
    ).read_text().rstrip().split('\n')[-1]


### remote_tag_exists ###

class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self):
        return self._json_body


def fake_get(responses):
    """Return a requests.get replacement that pops from `responses` and records calls."""
    calls = []

    def _get(url, params=None, headers=None):
        calls.append({'url': url, 'params': params, 'headers': headers})
        return responses.pop(0)

    _get.calls = calls
    return _get


def test_remote_tag_exists_when_registry_allows_anonymous_reads(monkeypatch):
    # Harbor answered /tags/list directly, with no token exchange.
    get = fake_get([FakeResponse(200, {'name': 'project/image', 'tags': ['0.01', '0.02']})])
    monkeypatch.setattr(update_tags.requests, 'get', get)

    assert update_tags.remote_tag_exists('example.com/project/image:0.01') is True
    assert get.calls[0]['url'] == 'https://example.com/v2/project/image/tags/list'


def test_remote_tag_exists_false_when_tag_absent(monkeypatch):
    monkeypatch.setattr(update_tags.requests, 'get',
                        fake_get([FakeResponse(200, {'tags': ['0.02']})]))
    assert update_tags.remote_tag_exists('example.com/project/image:0.01') is False


def test_remote_tag_exists_follows_bearer_challenge(monkeypatch):
    # GHCR returns 401 even for public packages, and only answers once we have
    # exchanged the WWW-Authenticate challenge for a token.
    challenge = (
        'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
        'scope="repository:harvard-lil/h2o-python:pull"'
    )
    get = fake_get([
        FakeResponse(401, {'errors': [{'code': 'UNAUTHORIZED'}]},
                     headers={'www-authenticate': challenge}),
        FakeResponse(200, {'token': 'sekrit'}),
        FakeResponse(200, {'tags': ['0.118-abc']}),
    ])
    monkeypatch.setattr(update_tags.requests, 'get', get)

    assert update_tags.remote_tag_exists('ghcr.io/harvard-lil/h2o-python:0.118-abc') is True

    # asked the realm for the service and scope it named, then retried with the token
    assert get.calls[1]['url'] == 'https://ghcr.io/token'
    assert get.calls[1]['params'] == {
        'service': 'ghcr.io',
        'scope': 'repository:harvard-lil/h2o-python:pull',
    }
    assert get.calls[2]['headers'] == {'Authorization': 'Bearer sekrit'}


def test_remote_tag_exists_false_when_challenge_cannot_be_answered(monkeypatch):
    # Previously this raised KeyError('tags') on the 401 body, failing the whole
    # workflow rather than reporting the tag missing.
    monkeypatch.setattr(update_tags.requests, 'get', fake_get([
        FakeResponse(401, {'errors': [{'code': 'UNAUTHORIZED'}]},
                     headers={'www-authenticate': 'Bearer realm="https://ghcr.io/token"'}),
        FakeResponse(403, {}),
    ]))
    assert update_tags.remote_tag_exists('ghcr.io/harvard-lil/private:0.01') is False


def test_remote_tag_exists_false_for_missing_repository(monkeypatch):
    monkeypatch.setattr(update_tags.requests, 'get', fake_get([FakeResponse(404, {})]))
    assert update_tags.remote_tag_exists('example.com/project/nope:0.01') is False


def test_remote_tag_exists_handles_null_tags(monkeypatch):
    # A registry with an empty repository may return {"name": ..., "tags": null}.
    monkeypatch.setattr(update_tags.requests, 'get',
                        fake_get([FakeResponse(200, {'name': 'project/image', 'tags': None})]))
    assert update_tags.remote_tag_exists('example.com/project/image:0.01') is False
