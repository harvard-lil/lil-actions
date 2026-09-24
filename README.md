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

## What the catalog records

The actions below are the deployment practice LIL has settled on, written down
once so each application composes it rather than re-deriving it. The practice
errs toward efficiency: migrations run by ECS Exec in a web task that is
already running, not in a dedicated task; a daily reconciliation job is not
paused for a deploy; nothing is purged or rebuilt to make a check pass. The
shape follows from that: small composites with explicit inputs, so a sequence
reads as a list of what happens, with no if-then tangles and no reusable
workflow spine that a consumer has to fit its own steps around. An
application's sequence is these shared steps plus the steps only it needs,
and those are the only ones it writes by hand.

## Promotion invariant

Build, test, and publish on main. Promotion checks compose
`ecr-resolve-git-image` (staging), `ecs-resolve-service-image` plus a source-tree
comparison (production), and `ecr-publication-state` with required artifact types.
Require a complete publication and matching digest. These checks read metadata
and referrers; do not rebuild, pull or launch the application image, or rerun its
test suite. Missing artifacts and unbuilt merge changes fail promotion and must
be resolved on main.

## Deployment credentials

Reusable workflows require a secret contract between caller and callee.
Repository-secret aliases and environment-secret lookup are different cases;
selecting an environment in another job does not make its secrets available to
the caller. Composite actions receive credentials through their documented
inputs or step environment within the calling job.

Follow the [LIL Engineering secret-scope and preflight guide](https://github.com/harvard-lil/lil-engineering/blob/main/docs/services/github-actions.md#secrets-across-workflow-boundaries).
Validate credential availability and API permissions in the actual deployment
job before maintenance or infrastructure changes. PR CI success does not test
those deployment credentials.

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

### `deploy-flags`

Resolves the three deploy intentions -- force a maintenance window, skip one,
hold traffic after a successful deploy -- from every place a deployer can
express them, and reports where each came from. The sources are the labels on
the pull request behind the deployed commit (`deploy:force-maintenance-mode`,
`deploy:skip-maintenance-mode`), the `workflow_dispatch` inputs
(`force-maintenance`, `skip-maintenance`, `hold-maintenance`), and a standing
variable such as `HOLD_MAINTENANCE`. Label and input names are configurable;
the defaults are the ones H2O and Payments already use.

```yaml
- id: flags
  uses: harvard-lil/lil-actions/deploy-flags@main
  with:
    event-name: ${{ github.event_name }}
    inputs: ${{ toJSON(inputs) }}
    hold-variable: ${{ vars.HOLD_MAINTENANCE }}
  env:
    GH_TOKEN: ${{ github.token }}
```

Outputs `force`, `skip` and `hold` are the strings `true` or `false`, and
`sources` is one line such as `force from label deploy:force-maintenance-mode;
hold from variable`, or `none`. Feed `force` and `skip` to
`ecs-django-maintenance`; `hold` is for the caller's release step.

Rules: labels are read off the commit with `gh api`, so a push carries them and
a direct push or a deleted pull request finds none, which warns rather than
fails. Dispatch inputs count only when `event-name` is `workflow_dispatch`; a
reusable workflow sees the caller's event, so a tier workflow passes its inputs
through as `workflow_call` inputs and the sequence hands them over with
`toJSON(inputs)`. `hold` is true when the dispatch input or the variable says
so, and implies `force`: a held site needs a window to be held in. When force
and skip are both requested, force wins, `skip` comes out `false`, and
`sources` says the skip was overridden. The label lookup needs
`pull-requests: read` and a `GH_TOKEN` in the step's `env`.

### `deploy-preflight`

Fails before anything changes when the image digest or source SHA is the
wrong shape, the tier is not in `allowed-tiers` (default `staging,prod`), or a
required secret is empty in the selected environment. Every problem is
reported at once, by name; no value is ever printed. Shape and presence only,
which is what Payments and Perma already checked by hand: PR CI cannot prove a
deployment credential works, and a permission probe here would only pretend to.

```yaml
- name: Check the deploy inputs
  uses: harvard-lil/lil-actions/deploy-preflight@main
  with:
    image-digest: ${{ inputs.image-digest }}
    source-sha: ${{ inputs.source-sha }}
    tier: ${{ inputs.environment }}
    required-secrets: >-
      {"CLOUDFLARE_API_TOKEN":"${{ secrets.CLOUDFLARE_API_TOKEN }}",
       "SLACK_WEBHOOK_URL":"${{ secrets.SLACK_WEBHOOK_URL }}"}
```

`required-secrets` is a JSON object of name to value written by the caller,
since a composite cannot read the caller's `secrets` context. A value holding
a double quote or backslash would break the JSON; tokens and webhook URLs do
not. Why this shape: an empty token is found here, not at the maintenance step
after the rollout has begun.

### `deploy-outcome`

Says what a run left behind, from four facts every sequence has: whether a
maintenance window opened (`window`, from `ecs-django-maintenance`), whether
the deployer asked for a hold (`hold`, from `deploy-flags`), and the outcomes
of the migrate and release steps. When the window opened and either the
migration did not end in `success` or `skipped` or the release did not
succeed, it prints `::error` lines and fails the run: the site is behind the
page and nobody asked for that. A hold with a clean migration is a `::notice`
with the release procedure, and the run stays green. Otherwise it says nothing
special. It does not call Cloudflare; the caller keeps its own release step.

```yaml
- name: Release traffic at the edge
  id: release
  if: always() && steps.window.outputs.needed == 'true' && steps.flags.outputs.hold != 'true' && (steps.migrate.outcome == 'success' || steps.migrate.outcome == 'skipped')
  uses: harvard-lil/lil-actions/cloudflare-maintenance@main
  with:
    mode: 'off'
    # zone-id, hostnames, token as for mode 'on'

- name: Report what this run left behind
  id: outcome
  if: always()
  uses: harvard-lil/lil-actions/deploy-outcome@main
  with:
    window: ${{ steps.window.outputs.needed }}
    hold: ${{ steps.flags.outputs.hold }}
    migrate-outcome: ${{ steps.migrate.outcome }}
    release-outcome: ${{ steps.release.outcome }}
    site-url: ${{ vars.SITE_URL }}
    sources: ${{ steps.flags.outputs.sources }}
```

The release `if:` above is the one to copy: lifted whenever migrations either
succeeded or never ran, since every failure before the migration leaves the
previous tasks serving on an unchanged schema; kept up after a migration that
failed partway, because the schema may suit neither version; kept up for a
hold. `deploy-outcome` is its complement, so the two conditions are not
derived twice.

Outputs are `summary`, multi-line Slack mrkdwn (`*State:*`, one line per
thing, `*Deploy flags:*` when `sources` is passed) without the payload
envelope, and `left-behind`, `true` or `false`. A caller with more state to
report -- Perma's capture intake and scheduler -- passes it as
newline-separated `extra-lines`, and sets `extra-left-behind: 'true'` when one
of those lines is something it left switched off, which fails the run the same
way. Why this shape: the report is one step that always runs, rather than a
hand-written condition on each consumer's release step and its inverse on a
report step.

### `deploy-notify`

Posts one Slack message for a deploy: a headline by `status` (`success`,
`failure` or `cancelled`), the repository, branch and commit, the
`deploy-outcome` summary as its own section, and links to the run and the
site. One step serves both outcomes:

```yaml
- name: Notify Slack
  if: always()
  uses: harvard-lil/lil-actions/deploy-notify@main
  with:
    webhook: ${{ secrets.SLACK_WEBHOOK_URL }}
    status: ${{ job.status }}
    product: H2O
    tier-label: ${{ inputs.environment-label }}
    site-url: ${{ vars.SITE_URL }}
    summary: ${{ steps.outcome.outputs.summary }}
```

`run-url` defaults to this run and `commit` to `github.sha`; Perma passes its
`source-sha` instead. The payload goes with `curl`, so no third-party action
holds the webhook and no secret passes through a job output; a non-200 answer
fails the step with Slack's response. Why this shape: h2o's two Slack steps carried
different blocks and different wording for the same state, because each
assembled its own JSON.

### `static-assets-publish`

Unpacks the static-file archive `ecr-artifacts` fetched and syncs it to the
bucket every deployed version shares. Never `--delete`: a browser holding a
page from the previous deploy can still fetch that page's files. Run it before
the rollout, so no task serves a page whose files are not in the bucket yet.

```yaml
- name: Publish the image's static files
  uses: harvard-lil/lil-actions/static-assets-publish@main
  with:
    bucket: lil-h2o-static
    archive: static-assets.tar.gz
    hashed-subdir: dist
```

`prefix` (default `static`) is both the top-level directory in the archive and
the key prefix; `extract-to` (default `image-artifacts`) is where the archive
is unpacked, the same directory `ecr-artifacts` writes manifests into. With
`hashed-subdir` set, that subtree is synced with `max-age=31536000, immutable`
and the rest with `max-age=3600` (h2o: Vite's `dist/` carries content hashes,
Django admin's files do not); without it, one pass at `max-age=300` (Perma:
nothing is hashed, and the edge cache is purged after the deploy). A missing
archive, prefix directory or hashed subdirectory fails before any sync. Why
this shape: the cache lifetimes are the policy, and they were copied by hand
between sequences.

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

`images` sets several containers' images in the one revision, as a JSON object
of container names to URIs. Use it when a task's containers ship different
images that are only tested together: two calls would register an intermediate
revision pairing one new image with one old one, and that revision becomes the
family's newest.

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

The wait loop is `scripts/ecs_wait_for_deployment.sh`, shared with
`ecs-scale-service`.

### `ecs-scale-service`

Stops a singleton service for the length of a deploy and starts it again on
the revision the deploy chose. `mode: stop` records the service's desired
count and task definition as outputs `desired-count` and `task-definition`,
scales it to zero and waits, bounded by `timeout-seconds` (default 300), until
no task is running. `mode: start` restores `desired-count` on `task-definition`
with `--force-new-deployment` and waits for that exact revision with the same
predicate `ecs-wait-for-deployment` uses, by running the same script; a
`desired-count` of `0` restores the count and does not wait.

```yaml
- name: Stop the scheduler
  id: stop-beat
  if: steps.plan.outputs.pause == 'true'
  uses: harvard-lil/lil-actions/ecs-scale-service@main
  with:
    mode: stop
    cluster: ${{ env.ECS_CLUSTER }}
    service: ${{ env.BEAT_SERVICE }}

- name: Start the scheduler
  if: always() && steps.stop-beat.outcome == 'success' && steps.roll.outcome == 'success'
  uses: harvard-lil/lil-actions/ecs-scale-service@main
  with:
    mode: start
    cluster: ${{ env.ECS_CLUSTER }}
    service: ${{ env.BEAT_SERVICE }}
    desired-count: ${{ steps.stop-beat.outputs.desired-count }}
    task-definition: ${{ steps.beat.outputs.task-definition-arn }}
```

This is for services that must never run twice, such as one Celery beat, where
stopping the old one before the new one starts is the only way to guarantee no
overlap. It is not for cron-style scheduled tasks (EventBridge rules running
an ECS task): those keep running through a deploy, and `ecs-update-eventbridge`
repoints them without touching their schedule. Why this shape: the count and
revision are recorded so the service is put back rather than guessed, and the
start reuses the rollout predicate rather than a second definition of stable.

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

### `ecs-roll-services`

Moves several services in one cluster to task definition revisions the caller
has already registered (one `ecs-register-task-def` per family), then waits for
all of them together and reports how each rollout ended. For a product whose
web and worker roles are separate services running one image: every service
moves in one step, and a service that fails to stabilise is named alongside
the ones that succeeded rather than hidden behind the first failure.

Success per service is `ecs-wait-for-deployment`'s definition: exactly the
requested revision, one completed deployment, all desired tasks running and
none pending. The `jq` predicates are the ones in
`scripts/ecs_wait_for_deployment.sh`, inlined rather than run: that script
waits on one service and exits, while this loop polls every service on each
pass and classifies how each one ended. A service whose desired count is 0 when the roll begins has its
revision recorded by `update-service` but is not waited for, and is reported
as `skipped`, so a scaled-down worker or a scheduler the caller stopped does
not fail the roll and does not count as verified either. Other states are
`rolled-back` (the circuit breaker returned the service to its previous
revision), `failed`, `replaced` (something else moved the service) and
`timed-out`. `results` is JSON keyed by service; `summary` is one line for a
notification; `skipped` lists the services not waited for. The step fails
unless every service is `completed` or `skipped`.

```yaml
- name: Roll the web and worker services
  id: roll
  uses: harvard-lil/lil-actions/ecs-roll-services@main
  with:
    cluster: prod-perma
    services: >-
      {"prod-perma-web": "${{ steps.web.outputs.task-definition-arn }}",
       "prod-perma-capture": "${{ steps.capture.outputs.task-definition-arn }}"}
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
source stability, rollback, EventBridge update failures, deploy-flag
resolution from labels, dispatch inputs and the hold variable, the deploy
helpers (preflight shapes and empty secrets, outcome states and exit codes,
the Slack payload against a scripted `curl`, static publication against a
scripted `aws` with a real archive, and scaling a service to zero and back),
multi-service rolls (skipped, rolled-back and timed-out services), Celery
manifest comparison and the idle wait. These are contract tests, not evidence
of a live AWS deployment.

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
skip suppresses the window, never migration execution. `deploy-flags` resolves
both from labels, dispatch inputs and the hold variable. The caller owns
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

### Celery task manifests and idle workers

`celery-task-manifest` describes a Celery app from a built image with no
network, a read-only root and placeholder `environment`, and writes the
format-1 result on the runner. Like `django-migration-manifest`, the
inspection code lives here (`scripts/celery_task_inspect.py`, run in the image
with `python -c`), so the application adds nothing: `app` is the value the
workers pass to `celery -A`, resolved the same way Celery resolves it, and
Django is set up first when the app configures itself from Django settings.
The document lists registered tasks (Celery's own excluded) with a hash of
each run function's parameters and the queue the app's router sends it to,
and the beat schedule. CI publishes it as an OCI referrer next to the
migration manifest.

`celery-manifest-compare` takes two manifest paths and outputs `changed` and a
one-line `summary` of added, removed and changed tasks and beat entries. A
deployment reads the running image's referrer and the incoming one: unchanged
means messages the outgoing code queued name tasks the incoming code registers
with the same arguments on the same queues, so workers can be replaced with a
warm shutdown and nothing paused. Changed, or a manifest that cannot be read,
means the deployment should pause intake and drain before rolling.

`celery-wait-idle` polls `celery -A <app> inspect active` through ECS Exec in
a running service task (each poll is one session, framed like the Django
helpers) until at least `min-workers` reply with no active tasks or
`timeout-seconds` passes. It never purges a queue: a queued message is work
for the incoming code, and whether that is acceptable is the manifest
comparison's question. A timeout is reported through `idle=false` and a
warning, not a failed step; a session that cannot run the command fails.

```yaml
- name: Wait for the workers to finish what they are doing
  id: idle
  uses: harvard-lil/lil-actions/celery-wait-idle@main
  with:
    cluster: prod-perma
    service: prod-perma-web
    container: prod-perma-web
    app: perma
    timeout-seconds: '120'
```

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
