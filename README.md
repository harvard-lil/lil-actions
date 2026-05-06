# lil-actions

Shared GitHub Actions composite actions and reusable workflows for harvard-lil projects.

## Reusable workflows

Reusable workflows are full deployment pipelines callable with a single `uses:` line. They're the right choice when the entire deployment pattern is identical across apps — inputs cover the per-app variation and there's nothing left to customize.

Reference them with `uses: harvard-lil/lil-actions/.github/workflows/<workflow-name>.yml@main`.

### `ecs-simple-deploy`

Build, push to ECR, and force a new ECS deployment. Covers stateless single-container apps whose task definition is owned by Terraform — CI only does `--force-new-deployment` against the `:latest` tag. Uses OIDC for AWS authentication.

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

**When to use a reusable workflow vs. composite actions:** A reusable workflow is worth adding when a complete deployment pipeline — trigger to finish — is identical across multiple apps with only names changing. Avoid too much if-then, and instead compose complex workflows from building-block actions to make the sequence clear.

## Actions

Actions are building blocks for more complex deployments. Reference them with `uses: harvard-lil/lil-actions/<action-name>@main`.

### `docker-compose-update`

Updates image tags in `docker-compose.yml` / `docker-compose.override.yml` based on content hashes, then builds and optionally pushes via `docker buildx bake`. Used by projects that track images in `registry.lil.tools`.

### `ecs-build`

Logs in to AWS ECR, builds a Docker image, and pushes it tagged with both the commit SHA and a named tag (default: `latest`). AWS credentials must be configured in the calling job before this action runs.

### `ecs-register-task-def`

Fetches the current revision of an ECS task definition, strips read-only fields, optionally updates a container image URI, and registers a new revision. Outputs the new task definition ARN — useful when you need a pinned ARN for EventBridge rules or explicit service updates.

### `ecs-update-eventbridge`

Updates `EcsParameters.TaskDefinitionArn` on one or more EventBridge rules to point at a new task definition revision.

### `ecs-maintenance`

Toggles an Application Load Balancer HTTPS listener between a live target group and a maintenance target group using weighted routing. Use `mode: on` before a deployment that needs a maintenance window, and `mode: off` after.
