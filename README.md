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

Set the account ID through the `CLOUDFLARE_ACCOUNT_ID` organization variable and
provide an API token with Pages Write access through `CLOUDFLARE_API_TOKEN`:

```yaml
- name: Deploy to Cloudflare Pages
  uses: harvard-lil/lil-actions/cloudflare-pages-deploy@main
  with:
    project-name: example-site
    directory: dist
  env:
    CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}
    CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
```

### `docker-compose-update`

Updates image tags in `docker-compose.yml` / `docker-compose.override.yml` based on content hashes, then builds and optionally pushes via `docker buildx bake`. Used by projects that track images in `registry.lil.tools`.

### `ecs-build`

Logs in to AWS ECR, builds a Docker image, and pushes it tagged with both the
commit SHA and a named tag (default: `latest`). It outputs both `image-uri`
(the named tag) and `sha-image-uri` (the immutable commit tag). AWS credentials
must be configured in the calling job before this action runs.

### `ecs-register-task-def`

Fetches the current revision of an ECS task definition, strips read-only fields, optionally updates a container image URI, and registers a new revision. Outputs the new task definition ARN — useful when you need a pinned ARN for EventBridge rules or explicit service updates.

### `ecs-update-eventbridge`

Updates `EcsParameters.TaskDefinitionArn` on one or more EventBridge rules to point at a new task definition revision.

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
