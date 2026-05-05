# lil-actions

Shared GitHub Actions composite actions for harvard-lil projects.

Reference actions from a workflow with `harvard-lil/lil-actions/<action-name>@main`.

## Actions

### `docker-compose-update`

Updates image tags in `docker-compose.yml` / `docker-compose.override.yml` based on content hashes, then builds and optionally pushes via `docker buildx bake`. Used by projects that track images in `registry.lil.tools`.

### `ecs-build`

Logs in to AWS ECR, builds a Docker image, and pushes it tagged with both the commit SHA and a named tag (default: `latest`). AWS credentials must be configured in the calling job before this action runs.

### `ecs-register-task-def`

Fetches the current revision of an ECS task definition, strips read-only fields, optionally updates the container image URI, and registers a new revision. Outputs the new task definition ARN — useful when you need a pinned ARN for EventBridge rules or explicit service updates.

### `ecs-update-eventbridge`

Updates `EcsParameters.TaskDefinitionArn` on one or more EventBridge rules to point at a new task definition revision.

### `ecs-maintenance`

Toggles an Application Load Balancer HTTPS listener between a live target group and a maintenance target group using weighted routing. Use `mode: on` before a deployment that needs a maintenance window, and `mode: off` after.
