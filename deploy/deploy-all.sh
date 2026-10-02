#!/usr/bin/env bash
# Deploys every microservice's Helm release to the current kubectl context.
# Lives here (not its own repo) because mosaic_common_service is this
# platform's shared/cross-cutting home - see deploy/PLATFORM.md.
#
# Usage:
#   bash deploy/deploy-all.sh              # deploy every service listed below
#   bash deploy/deploy-all.sh worldmonitor  # deploy just one, by name
#
# To add a new microservice to the platform: build its chart under
# <its-repo>/deploy/helm/<name>/ (see this repo or worldmonitor_aidan_mosaic
# for the pattern), then add one line to the SERVICES array below.
set -euo pipefail
cd "$(dirname "$0")/.."

# name|namespace|chart path|values-prod path (paths relative to this repo's root)
# All three share the "mosaic-ai" namespace - per-service isolation was
# tried and reverted before anything was deployed, at the infra team's
# request (they own this cluster and asked for one shared namespace). Safe
# either way: each chart's resources are prefixed by its own release name
# (mosaic-common-service / worldmonitor / mosaic-ai-chat), so nothing
# collides sharing one namespace. Use
# `kubectl -n mosaic-ai get pods -l app.kubernetes.io/instance=<name>` to
# scope kubectl to just one service within it.
SERVICES=(
  "mosaic-common-service|mosaic-ai|deploy/helm/mosaic-common-service|deploy/helm/mosaic-common-service/values-prod.yaml"
  "worldmonitor|mosaic-ai|../worldmonitor_aidan_mosaic/deploy/helm/worldmonitor|../worldmonitor_aidan_mosaic/deploy/helm/worldmonitor/values-prod.yaml"
  "mosaic-ai-chat|mosaic-ai|../mosaic-ai-chat/deploy/helm/mosaic-ai-chat|../mosaic-ai-chat/deploy/helm/mosaic-ai-chat/values-prod.yaml"
)

ONLY="${1:-}"
step() { printf '\n==> %s\n' "$*"; }

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r name namespace chart_path values_path <<< "$entry"

  if [[ -n "$ONLY" && "$ONLY" != "$name" ]]; then
    continue
  fi

  if [[ ! -d "$chart_path" ]]; then
    step "Skipping $name - no chart yet at $chart_path"
    continue
  fi

  if [[ ! -f "$values_path" ]]; then
    step "Skipping $name - $values_path not found (copy values-prod.yaml.example and fill it in)"
    continue
  fi

  step "Deploying $name to namespace $namespace"
  helm upgrade --install "$name" "$chart_path" \
    -n "$namespace" --create-namespace \
    -f "$values_path"
done

step "Done. Check rollout status with: kubectl get pods -A"
