#!/bin/bash
set -euo pipefail

HUB_API_URL="${HUB_API_URL:-https://api.cluster-p67mq.dynamic.redhatworkshops.io:6443}"
HUB_ADMIN_USER="${HUB_ADMIN_USER:-admin}"
HUB_ADMIN_PASS="${HUB_ADMIN_PASS:-MjU3MTY5}"

SPOKE_API_URL="${SPOKE_API_URL:-https://api.cluster-fhckt.dynamic.redhatworkshops.io:6443}"
SPOKE_ADMIN_USER="${SPOKE_ADMIN_USER:-admin}"
SPOKE_ADMIN_PASS="${SPOKE_ADMIN_PASS:-MTExNjUx}"
SPOKE_CLUSTER_NAME="${SPOKE_CLUSTER_NAME:-spoke-1}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== Phase 2: Spoke Cluster Import ==="

log "Step 1: Logging into hub cluster..."
oc login "${HUB_API_URL}" -u "${HUB_ADMIN_USER}" -p "${HUB_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Step 2: Creating ManagedCluster CR on hub..."
oc apply -f - <<EOF
apiVersion: cluster.open-cluster-management.io/v1
kind: ManagedCluster
metadata:
  name: ${SPOKE_CLUSTER_NAME}
  labels:
    cloud: auto-detect
    vendor: auto-detect
spec:
  hubAcceptsClient: true
EOF

log "Step 3: Waiting for import secret to be generated..."
until oc get secret "${SPOKE_CLUSTER_NAME}-import" -n "${SPOKE_CLUSTER_NAME}" 2>/dev/null; do
  sleep 10
  echo -n "."
done
echo
log "Import secret available."

log "Step 4: Extracting import manifests..."
IMPORT_YAML=$(oc get secret "${SPOKE_CLUSTER_NAME}-import" -n "${SPOKE_CLUSTER_NAME}" -o jsonpath='{.data.import\.yaml}' | base64 -d)
CRDS_YAML=$(oc get secret "${SPOKE_CLUSTER_NAME}-import" -n "${SPOKE_CLUSTER_NAME}" -o jsonpath='{.data.crds\.yaml}' | base64 -d)

log "Step 5: Logging into spoke cluster..."
oc login "${SPOKE_API_URL}" -u "${SPOKE_ADMIN_USER}" -p "${SPOKE_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Verifying spoke cluster identity..."
oc get clusterversion -o jsonpath='{.items[0].spec.clusterID}'
echo

log "Step 6: Applying import CRDs on spoke..."
echo "${CRDS_YAML}" | oc apply -f -

log "Step 7: Applying import manifests on spoke..."
echo "${IMPORT_YAML}" | oc apply -f -

log "Step 8: Switching back to hub cluster..."
oc login "${HUB_API_URL}" -u "${HUB_ADMIN_USER}" -p "${HUB_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Step 9: Waiting for spoke to join (timeout 300s)..."
oc wait --for=condition=ManagedClusterConditionAvailable managedcluster/"${SPOKE_CLUSTER_NAME}" \
  --timeout=300s || {
    log "WARNING: Spoke not available within timeout. Check status:"
    oc get managedcluster "${SPOKE_CLUSTER_NAME}" -o yaml
    exit 1
  }

log "=== Phase 2 Verification ==="
log "Managed clusters:"
oc get managedclusters
log "Spoke cluster status:"
oc get managedcluster "${SPOKE_CLUSTER_NAME}" -o jsonpath='{.status.conditions[*].type}{"\n"}'

log "=== Phase 2 Complete ==="
