#!/usr/bin/env bash
# Wait for one ECS service to be running exactly one task definition revision.
#
# Shared by ecs-wait-for-deployment and ecs-scale-service (mode: start), so
# both use one definition of a finished rollout: the service selects the
# expected revision, one deployment exists and it is COMPLETED, the desired
# count is positive, every desired task is running and none is pending. A
# completed rollback to another revision is a failure, not a slow success.
#
# Reads: EXPECTED_TASK_DEFINITION (empty captures the service's current
# selection on the first poll), CLUSTER, SERVICE, TIMEOUT_SECONDS,
# POLL_INTERVAL_SECONDS.
set -euo pipefail

if [[ ! "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || [[ ! "$POLL_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "::error::Timeout must be nonnegative and poll interval must be positive."
  exit 1
fi
start_time="$(date +%s)"
while true; do
  response="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --output json)"
  if ! jq -e '(.failures | length) == 0 and (.services | length) == 1' <<< "$response" >/dev/null; then
    echo "::error::Could not read exactly one ECS service."
    exit 1
  fi
  current="$(jq -c '.services[0]' <<< "$response")"
  if [ -z "$EXPECTED_TASK_DEFINITION" ]; then
    EXPECTED_TASK_DEFINITION="$(jq -er '.taskDefinition' <<< "$current")"
  fi
  if jq -e --arg expected "$EXPECTED_TASK_DEFINITION" '
    .taskDefinition == $expected and .desiredCount > 0 and
    .runningCount == .desiredCount and .pendingCount == 0 and
    (.deployments | length) == 1 and
    .deployments[0].taskDefinition == $expected and
    .deployments[0].rolloutState == "COMPLETED"
  ' <<< "$current" >/dev/null; then
    echo "ECS deployment completed: $EXPECTED_TASK_DEFINITION"
    exit 0
  fi
  if jq -e --arg expected "$EXPECTED_TASK_DEFINITION" '
    .desiredCount == 0 or
    any(.deployments[]; .taskDefinition == $expected and .rolloutState == "FAILED") or
    (.taskDefinition != $expected and any(.deployments[];
      .status == "PRIMARY" and .rolloutState == "COMPLETED"))
  ' <<< "$current" >/dev/null; then
    echo "::error::Requested ECS revision failed, rolled back, or has no desired tasks."
    exit 1
  fi
  if [ "$(( $(date +%s) - start_time ))" -ge "$TIMEOUT_SECONDS" ]; then
    echo "::error::Timed out waiting for $EXPECTED_TASK_DEFINITION."
    exit 1
  fi
  sleep "$POLL_INTERVAL_SECONDS"
done
