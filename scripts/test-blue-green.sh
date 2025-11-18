#!/usr/bin/env bash
set -euo pipefail

# Required environment variables
: "${RESOURCE_GROUP:?Set RESOURCE_GROUP to the Container Apps resource group}"
: "${CONTAINERAPP_NAME:?Set CONTAINERAPP_NAME to the Container Apps name}"
: "${ACR_NAME:?Set ACR_NAME to the Azure Container Registry name}"
: "${IMAGE_TAG:?Set IMAGE_TAG to the image tag, e.g. v1.0.0}"

IMAGE="${ACR_NAME}.azurecr.io/${CONTAINERAPP_NAME}:${IMAGE_TAG}"
REV_SUFFIX="rev$(date +%H%M%S)"

log() {
  echo "[$(date -Iseconds)] $*"
}

log "Logging in to ACR ${ACR_NAME}"
az acr login --name "${ACR_NAME}"

log "Building container image ${IMAGE}"
docker build -t "${IMAGE}" .

log "Pushing container image"
docker push "${IMAGE}"

log "Deploying new revision suffix ${REV_SUFFIX}"
az containerapp update \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --image "${IMAGE}" \
  --revision-suffix "${REV_SUFFIX}" \
  --set-env-vars REVISION_SUFFIX="${REV_SUFFIX}"

log "Fetching revision names"
mapfile -t ACTIVE_REVISIONS < <(az containerapp revision list \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --query "[?properties.active==\`true\`].name" -o tsv)

NEW_REV=$(printf "%s" "${ACTIVE_REVISIONS[@]}" | tr ' ' '\n' | grep "${REV_SUFFIX}" | head -n1)
CURRENT_DEFAULT=$(az containerapp show \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --query "properties.latestRevisionName" -o tsv)

if [[ -z "${NEW_REV}" ]]; then
  echo "New revision not detected" >&2
  exit 1
fi

log "Setting traffic split 50/50 between ${CURRENT_DEFAULT} and ${NEW_REV}"
az containerapp ingress traffic set \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --revision-weight "${CURRENT_DEFAULT}=50" "${NEW_REV}=50"

log "Waiting 120 seconds for steady state"
sleep 120

log "Sending log stream command for observation"
echo "az containerapp logs show --name ${CONTAINERAPP_NAME} --resource-group ${RESOURCE_GROUP} --follow"

log "Shifting 100 percent traffic to ${NEW_REV}"
az containerapp ingress traffic set \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --revision-weight "${NEW_REV}=100"

log "Waiting 60 seconds before disabling old revision"
sleep 60

log "Disabling old revision ${CURRENT_DEFAULT}"
az containerapp revision deactivate \
  --name "${CONTAINERAPP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --revision "${CURRENT_DEFAULT}"

log "Blue-Green workflow complete"
