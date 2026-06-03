#!/bin/sh
set -eu

WORKFLOWS_DIR="${N8N_WORKFLOWS_DIR:-/opt/legalfam/workflows}"
PUBLISH_IDS="${N8N_PUBLISH_WORKFLOW_IDS:-}"

if [ ! -d "$WORKFLOWS_DIR" ]; then
  echo "Workflow directory not found: $WORKFLOWS_DIR" >&2
  exit 1
fi

echo "Importing workflows from $WORKFLOWS_DIR"
n8n import:workflow --input="$WORKFLOWS_DIR" --separate

if [ -z "$PUBLISH_IDS" ]; then
  echo "N8N_PUBLISH_WORKFLOW_IDS is empty; skipping publish"
  exit 0
fi

for workflow_id in $(printf '%s' "$PUBLISH_IDS" | tr ',' ' '); do
  echo "Publishing workflow $workflow_id"
  n8n publish:workflow --id="$workflow_id"
done
