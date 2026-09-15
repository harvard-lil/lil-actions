# lil-actions

Shared GitHub Actions composite actions and reusable workflows for harvard-lil projects.

## Reusable workflows

Reusable workflows are full deployment pipelines callable with a single `uses:` line. They're the right choice when the entire deployment pattern is identical across apps — inputs cover the per-app variation and there's nothing left to customize.

Reference them with `uses: harvard-lil/lil-actions/.github/workflows/<workflow-name>.yml@main`.

### `ecs-simple-deploy`

Build, push to ECR, force a new ECS deployment, and wait for the service to
become stable. Covers stateless single-container apps whose task definition is
owned by Terraform — CI only does `--force-new-deployment` against the
`:latest` tag. Uses OIDC for AWS authentication.

`app-name` must match the Terraform `app_name` variable; the workflow derives the ECR repository, ECS cluster, and ECS service names from `app-name` (and `environment` if set), matching the [`simple-web-app`](https://github.com/harvard-lil/lil-terraform/tree/main/modules/simple-web-app) module's naming.

Single-environment app:

```yaml
jobs:
  deploy:
    uses: harvard-lil/lil-actions/.github/workflows/ecs-simple-deploy.yml@main
    with:
      app-name: my-app
    secrets:
      aws-role-arn: ${{ secrets.AWS_ROLE_ARN }}
```

Multi-environment app — set `environment` to activate the matching GitHub Environment and target `{environment}-{app-name}` resources:

```yaml
jobs:
  deploy:
    uses: harvard-lil/lil-actions/.github/workflows/ecs-simple-deploy.yml@main
    with:
      app-name: my-app
      environment: staging
    secrets:
      aws-role-arn: ${{ secrets.AWS_ROLE_ARN_STAGING }}
```

### `ecs-deploy-image`

Deploys an image already in ECR and identified by digest. It can move an
explicitly mutable Terraform placeholder tag, add an immutable retention tag,
register a task definition revision pinned to the digest, update the ECS
service, and wait for a healthy rollout. Build and test remain outside this
workflow, so deployments cannot rebuild the artifact they were asked to ship.

The caller's role needs permission to read and tag the ECR repository,
describe and register the task definition, pass its execution and task roles,
and update and describe the service.

### `secret-scan`

Runs [TruffleHog](https://github.com/trufflesecurity/trufflehog) over the change that triggered the calling workflow — the PR's `base..head` on `pull_request`, or the pushed `before..after` range on `push`. It catches secrets *introduced* by a change (even if a later commit in the same range removes them) and does **not** retroactively scan existing history, so it is safe to adopt on a repo that already has content. The job fails if any secret is found, blocking the merge or flagging the push.

```yaml
name: Secret scan

on:
  pull_request:
  push:
    branches: ['**']   # all branches, so a pushed-but-never-PR'd branch is still scanned

jobs:
  secret-scan:
    uses: harvard-lil/lil-actions/.github/workflows/secret-scan.yml@main
```

Scan **every branch push**, not just the default branch: a secret pushed to a feature branch that is never opened as a PR is still in the repo and otherwise goes unscanned. `pull_request` is kept mainly for fork PRs on public repos (the contributor's push lands on their fork, so only the PR event sees it).

Defaults to `--results=verified,unknown` (flags confirmed-live *and* unverifiable matches — the safer choice for repos that hold pasted artifacts). Pass `with: { extra_args: '--results=verified' }` to reduce noise, or add `--exclude-paths` for known false positives. This guards against secrets *landing* in a repo; pair it with a local pre-commit hook for pre-push feedback, since CI can only flag a push after it happens.

**When to use a reusable workflow vs. composite actions:** A reusable workflow is worth adding when a complete deployment pipeline — trigger to finish — is identical across multiple apps with only names changing. Avoid too much if-then, and instead compose complex workflows from building-block actions to make the sequence clear.

## Actions

Actions are building blocks for more complex deployments. Reference them with `uses: harvard-lil/lil-actions/<action-name>@main`.

### `cloudflare-pages-deploy`

Deploys an already-built static directory to a direct-upload Cloudflare Pages
project. It centralizes the pinned Wrangler version and commit metadata while
leaving each repository free to use its own build system.

Set the account ID through the `CLOUDFLARE_ACCOUNT_ID` organization variable.
Store the Pages-only API token in the `CLOUDFLARE_PAGES_TOKEN` organization
secret, restricted to approved deploy repositories, and map it to Wrangler's
expected `CLOUDFLARE_API_TOKEN` environment variable:

```yaml
- name: Deploy to Cloudflare Pages
  uses: harvard-lil/lil-actions/cloudflare-pages-deploy@main
  with:
    project-name: example-site
    directory: dist
  env:
    CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}
    CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_PAGES_TOKEN }}
```

### `docker-compose-update`

Updates image tags in `docker-compose.yml` / `docker-compose.override.yml` based on content hashes, then builds and optionally pushes via `docker buildx bake`. Used by projects that track images in `registry.lil.tools`.

### `ecs-build`

Logs in to AWS ECR, builds a Docker image, and pushes it tagged with both the
commit SHA and a named tag (default: `latest`). It outputs both `image-uri`
(the named tag) and `sha-image-uri` (the immutable commit tag). AWS credentials
must be configured in the calling job before this action runs.

### `cloudflare-maintenance`

Holds traffic at Cloudflare's edge by pointing a route at a maintenance Worker,
or releases it by deleting that route — then confirms by requesting the site,
and fails if it never observes the change. A window that cannot be confirmed to
have started is worse than none, since the work it covers proceeds anyway.

Held at the edge rather than the origin, so it works when the origin is gone,
which is the situation a maintenance page is most wanted in. Unlike
`ecs-maintenance` it also leaves load-balancer routing alone.

The Worker and page live in lil-terraform's `lil-cloudflare-maintenance` module;
this only attaches and detaches. Pass the script name and hostnames as inputs
rather than reading Terraform state, so CI needs no access to the state bucket.

### `cloudflare-purge`

Purges a Cloudflare zone's cache after a deploy, with a direct API call. Pass
`files` to purge specific URLs instead of the whole zone. Replaces
`jakejarvis/cloudflare-purge-action`, which was archived, last committed in 2019,
referenced by branch, and rebuilt from a floating base image on every run while
holding a Cloudflare API token.

### `ecr-tag-image`

Adds an extra tag to an image already in ECR, by re-registering its manifest
rather than pulling and re-pushing. Layers do not move and the digest is
unchanged. Use it at deploy time to mark which build candidates were actually
promoted, so a lifecycle policy can keep deployed images longer than the
candidate churn around them. Safe on repositories with immutable tags — adding a
new tag is permitted, only repointing an existing one is not — and idempotent
when a deploy is re-run. `replace-existing: true` permits a named mutable tag
such as `latest` to move; immutable commit and retention tags should retain the
default.

### `ecr-publication-state`

Classifies a commit-tagged ECR image as `absent`, `partial`, or `complete`.
Callers may supply a JSON array of required OCI artifact types. A publication
is complete only when the image exists and every required type has a readable
referrer whose subject is that image digest. This lets a retry reuse a completed
publication while distinguishing it from a run that pushed the image and then
failed while attaching metadata.

### `ecr-publish-image`

Publishes an already-built local Docker image under one immutable ECR tag. It
does not build, test, create a moving tag, or silently reuse an existing tag.
Repository-owned CI can therefore hand it the exact local image that the same
job tested.

### `ecr-resolve-git-image`

Resolves a tested image while promoting a branch: first the promoted merge
commit's second parent, then the commit itself. This covers merge commits,
squashes, rebases, and fast-forwards while refusing to rebuild a missing
artifact. By default a candidate must have the same Git tree as the promoted
revision, preventing an unbuilt merge resolution from selecting a parent image.
`require-matching-tree: false` explicitly retains the older selection behavior.
The checkout must include the revision and its parents.

### `ecs-resolve-service-image`

Requires one completed deployment with positive desired count, all desired tasks
running and no pending tasks. Reads its selected task definition, checks the
container image against the exact ECR repository URI, and requires exactly one
commit-SHA tag on that digest. It rechecks source stability and revision before
returning. Missing, ambiguous or changing provenance fails promotion.

The role needs `ecs:DescribeServices`, `ecs:DescribeTaskDefinition`,
`ecr:DescribeRepositories`, and `ecr:DescribeImages`. This action does not wait
for an in-progress source rollout; retry after staging is stable.

### `ecs-register-task-def`

Fetches the current revision of an ECS task definition, strips read-only fields,
optionally updates a container image URI and selected container environment
variables, and registers a new revision. Outputs the new task definition ARN —
useful when you need a pinned ARN for EventBridge rules or explicit service
updates.

### `ecs-update-eventbridge`

Updates `EcsParameters.TaskDefinitionArn` on one or more EventBridge rules.
Without `target-id`, each rule must have exactly one ECS target. With it, only
that matching target is updated, leaving other targets alone. All writable target
fields are preserved, including retry/dead-letter settings. Every rule is checked
before writes begin; API partial failures and mismatched readback fail the action.
Rule enabled/disabled state is unchanged. Multiple updates are not atomic: if a
later write fails, earlier rules may already have changed. Retry or recover while
keeping the application's maintenance/schedule policy in effect.

### `ecs-wait-for-deployment`

Pass `task-definition` with the full ARN returned by `ecs-register-task-def` or
`ecs-deploy`. Success requires exactly that revision, one completed deployment,
a positive desired count, all desired tasks running and no pending tasks. A
completed rollback is a failure. If omitted, the expected revision is captured
when waiting begins; a rollback completed before that capture cannot be detected.
`timeout-seconds: '0'` performs one immediate check. Existing scaled-to-zero
services no longer count as successful deployments.

`ecs-deploy-image` supplies the expected ARN and moves the Terraform placeholder
tag only after the requested deployment succeeds.

### `ecs-maintenance`

Toggles an Application Load Balancer HTTPS listener between a live target group and a maintenance target group using weighted routing. Use `mode: on` before a deployment that needs a maintenance window, and `mode: off` after.

### `ecs-deploy`

Forces a new deployment of an existing ECS service. Use this when the task definition is already configured to pull a mutable image tag such as `latest`, or when another step has already updated the task definition. AWS credentials must be configured in the calling job before this action runs.

Example:

```yaml
- name: Deploy to ECS
  uses: harvard-lil/lil-actions/ecs-deploy@main
  with:
    cluster: my-ecs-cluster
    service: my-ecs-service
```

### `ecs-exec-command`

The action keeps Session Manager's input open in CI and requires a remote exit-status
marker before reporting success. A lost session or failed command stops the action;
commands are not retried automatically because they may have changed data.


Runs a command inside a running ECS service task using ECS Exec. Use this for deployment-time commands such as Django migrations, index refreshes, or other one-off application commands that need to run inside the deployed container. AWS credentials must be configured before this action runs, and ECS Exec must be enabled for the service/task.

Example:

```yaml
- name: Run Django migrations
  uses: harvard-lil/lil-actions/ecs-exec-command@main
  with:
    cluster: my-ecs-cluster
    service: my-ecs-service
    container: my-container
    command: python manage.py migrate
```

The same action can be reused for other commands:

```yaml
- name: Create search index
  uses: harvard-lil/lil-actions/ecs-exec-command@main
  with:
    cluster: my-ecs-cluster
    service: my-ecs-service
    container: my-container
    command: invoke create-search-index
```

## Deployment action tests

`uv run --with pyyaml python -m unittest discover -s tests -v` runs the actual
composite shell scripts against isolated simulated CLI responses. CI runs this
suite alongside the existing compose-update tests, actionlint and shellcheck.
Cases cover first publication/retries, partial referrers, promotion provenance,
source stability, rollback, and EventBridge update failures. These are contract
tests, not evidence of a live AWS deployment.

H2O keeps its migration/static/Lambda sequence and composes these helpers.
The prepared Payments adoption uses the same rollout/promotion/schedule checks and ECS Exec migration
model, with a daily reconciliation task whose schedule stays enabled. Filecheck's
simpler image workflow uses the shared exact-revision wait. LIL-owned actions use
`@main`; third-party actions remain pinned to reviewed full commit SHAs. Merge
shared helper changes before consumer workflows that depend on their new inputs
or behavior.

### Shared Django maintenance policy

`ecs-django-maintenance` implements H2O's existing migration-manifest decision:
inspect the incoming image (or read an existing format-1 manifest) and compare it
with the running application's migrations,
then check its database for pending migrations. It returns `needed=true` for
changed or unavailable manifests and failed checks. `force` wins over `skip`;
skip suppresses the window, never migration execution. The caller owns
Cloudflare maintenance and failure recovery.

`ecs-django-migrations` uses ECS Exec in a running web task. Pass `task-definition`
after verifying rollout to reject selection of a stale task. Commands report a
framed remote exit status, and migration completion is followed by a fresh plan
check. A successful SSM session alone is not accepted as Django success.
Both actions require Python 3, AWS CLI, the Session Manager plugin, and ECS Exec
permissions; task inspection also requires `ecs:DescribeTasks`.

The prepared Payments adoption uses these helpers with an application database owner and a web task
role limited to ECS Exec channels. The prepared H2O adoption uses `ecs-django-maintenance` with its
published manifest and existing force/skip label outputs. The incoming web revision must boot against the old schema. These
helpers do not classify migrations as safe for concurrent traffic or coordinate
scheduled jobs. Merge the shared changes before consumers use the new action.

`django-migration-manifest` runs the shared inspector in a built image and writes
its JSON result on the runner. Inputs are `image`, optional `working-directory`
and `settings-module`, and a JSON `environment` of placeholder settings values.
The container has no network access and a read-only root. Django is initialized,
but its migration loader uses no database connection. Application startup hooks
must also work without database access for this inspection path.

`ecs-django-maintenance` accepts either `image` with those same inspection inputs,
or `manifest` for an existing artifact such as H2O's. Image-inspection errors
stop the workflow before maintenance or rollout. An unavailable running-task
check requires maintenance. Both image and running-task inspection execute the
same `scripts/django_manifest.py`; applications need no management command or
baked manifest. `manifest-command` is an optional compatibility override.
The prepared H2O and Payments workflows inspect during CI and publish the result as a required OCI
referrer. Deployment reads that artifact and never re-inspects the incoming image.

### Container smoke tests

`container-smoke-test` starts a built image with its default command, placeholder
`environment` (JSON), no external network, a read-only root, dropped Linux
capabilities, and writable `/tmp`. The caller supplies `probe-command` as JSON
argv and optionally `timeout-seconds`. The probe executes inside the container;
no host port is published. Container exit, probe failure until the deadline, or a
hung probe fails the check. Logs are collected and the container is removed on
both success and failure. This tests startup/health, not database or external API
integration, image contents, or application-specific deployment identities.

### ECR referrer artifacts

`ecr-artifacts` publishes or fetches an array of single-file artifacts described
by `type` and `path` (`media-type` is additionally required for publication).
Configure AWS credentials before fetching, and log Docker in to ECR before
publishing. Publication uses ORAS with a fixed timestamp and stable basename, keeping retries
deterministic. Consumers use `ecr-publication-state` to require those artifact
types before considering the image complete. Missing attachments are repaired
from the existing immutable image, never by rebuilding its commit tag.

Fetching uses the OCI registry API directly: one index, one manifest, and one
blob request per artifact in the normal case, with one ECR login token for the
batch. It verifies the subject, artifact type, SHA-256 digests, and payload size.
Duplicate referrers pointing to identical payloads are accepted; conflicting
payloads, missing artifacts, and authorization errors stop deployment. No Docker
pull, container startup, or ORAS installation occurs on the fetch path. Tar
creation/extraction stays in the caller that understands its static-file layout.

The prepared H2O workflow uses these actions for both static assets and migrations, and delegates its
maintenance decision to `ecs-django-maintenance` with the existing label outputs.
The prepared Payments workflow uses the same flow for migrations; WhiteNoise assets stay in its image.
Merge these shared changes before either consumer's workflow changes.
