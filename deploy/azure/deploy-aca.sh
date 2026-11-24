#!/usr/bin/env bash
# Azure Container Apps deployment. Run from the repo root.
set -euo pipefail
RG=${RG:-rg-web-agent}
LOCATION=${LOCATION:-centralindia}
ACR=${ACR:-webagentacr$RANDOM}
ENV_NAME=${ENV_NAME:-web-agent-env}
APP=${APP:-web-agent}
TAG=${TAG:-0.2.0}

az group create -n "$RG" -l "$LOCATION"
az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true
az acr build -r "$ACR" -t "web-agent:$TAG" .
az containerapp env create -g "$RG" -n "$ENV_NAME" -l "$LOCATION"

az containerapp create -g "$RG" -n "$APP" --environment "$ENV_NAME" \
  --image "$ACR.azurecr.io/web-agent:$TAG" \
  --registry-server "$ACR.azurecr.io" \
  --ingress external --target-port 8000 \
  --cpu 2 --memory 4Gi \
  --min-replicas 1 --max-replicas 1 \
  --secrets aicore-client-secret="$AICORE_CLIENT_SECRET" \
  --env-vars \
    AICORE_AUTH_URL="$AICORE_AUTH_URL" \
    AICORE_CLIENT_ID="$AICORE_CLIENT_ID" \
    AICORE_CLIENT_SECRET=secretref:aicore-client-secret \
    AICORE_BASE_URL="$AICORE_BASE_URL" \
    AICORE_RESOURCE_GROUP="${AICORE_RESOURCE_GROUP:-default}" \
    LLM_MODEL=gpt-4.1 BROWSER_ISOLATION=shared

az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv
