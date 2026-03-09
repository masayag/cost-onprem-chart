#!/bin/bash
set -euo pipefail

HUB_API_URL="${HUB_API_URL:-https://api.cluster-p67mq.dynamic.redhatworkshops.io:6443}"
HUB_ADMIN_USER="${HUB_ADMIN_USER:-admin}"
HUB_ADMIN_PASS="${HUB_ADMIN_PASS:-MjU3MTY5}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== Phase 3: MCO / Observability Stack ==="

log "Logging into hub cluster..."
oc login "${HUB_API_URL}" -u "${HUB_ADMIN_USER}" -p "${HUB_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Step 1: Creating observability namespace..."
oc apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: open-cluster-management-observability
EOF

log "Step 2: Creating pull-secret for observability namespace..."
DOCKER_CONFIG=$(oc extract secret/pull-secret -n openshift-config --to=- 2>/dev/null)
oc create secret generic multiclusterhub-operator-pull-secret \
  -n open-cluster-management-observability \
  --from-literal=.dockerconfigjson="${DOCKER_CONFIG}" \
  --type=kubernetes.io/dockerconfigjson \
  --dry-run=client -o yaml | oc apply -f -

log "Step 3: Creating OBC for Thanos metrics bucket (uses ODF/NooBaa S3)..."
oc apply -f - <<'EOF'
apiVersion: objectbucket.io/v1alpha1
kind: ObjectBucketClaim
metadata:
  name: thanos-metrics-bucket
  namespace: openshift-storage
spec:
  generateBucketName: thanos-metrics
  storageClassName: openshift-storage.noobaa.io
EOF

log "Waiting for OBC to be bound..."
until [ "$(oc get obc thanos-metrics-bucket -n openshift-storage -o jsonpath='{.status.phase}' 2>/dev/null)" = "Bound" ]; do
  sleep 5
  echo -n "."
done
echo

THANOS_BUCKET=$(oc get obc thanos-metrics-bucket -n openshift-storage -o jsonpath='{.spec.bucketName}')
S3_ACCESS_KEY=$(oc get secret thanos-metrics-bucket -n openshift-storage -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d)
S3_SECRET_KEY=$(oc get secret thanos-metrics-bucket -n openshift-storage -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d)
S3_ENDPOINT="s3.openshift-storage.svc"

log "Bucket: ${THANOS_BUCKET}"
log "Endpoint: ${S3_ENDPOINT}"

log "Step 4: Creating Thanos object storage secret..."
oc apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: thanos-object-storage
  namespace: open-cluster-management-observability
type: Opaque
stringData:
  thanos.yaml: |
    type: s3
    config:
      bucket: ${THANOS_BUCKET}
      endpoint: ${S3_ENDPOINT}
      access_key: ${S3_ACCESS_KEY}
      secret_key: ${S3_SECRET_KEY}
      insecure: false
      http_config:
        tls_config:
          insecure_skip_verify: true
EOF

log "Step 5: Creating MultiClusterObservability CR..."
oc apply -f - <<'EOF'
apiVersion: observability.open-cluster-management.io/v1beta2
kind: MultiClusterObservability
metadata:
  name: observability
spec:
  observabilityAddonSpec:
    enableMetrics: true
    interval: 300
  storageConfig:
    metricObjectStorage:
      name: thanos-object-storage
      key: thanos.yaml
    alertmanagerStorageSize: 1Gi
    compactStorageSize: 10Gi
    receiveStorageSize: 10Gi
    ruleStorageSize: 1Gi
    storeStorageSize: 10Gi
EOF

log "Step 6: Waiting for Thanos components to be ready..."
THANOS_COMPONENTS=(
  "observability-thanos-query"
  "observability-thanos-receive-default"
  "observability-thanos-store-shard-0"
  "observability-thanos-compact"
  "observability-thanos-rule"
)
for component in "${THANOS_COMPONENTS[@]}"; do
  log "  Waiting for ${component}..."
  oc rollout status statefulset/"${component}" -n open-cluster-management-observability --timeout=300s 2>/dev/null || \
  oc rollout status deployment/"${component}" -n open-cluster-management-observability --timeout=300s 2>/dev/null || \
  log "  WARNING: ${component} not ready within timeout"
done

log "=== Phase 3 Verification ==="
log "Pods in observability namespace:"
oc get pods -n open-cluster-management-observability --no-headers
log "Thanos Query service:"
oc get svc observability-thanos-query -n open-cluster-management-observability 2>/dev/null || log "Thanos Query service not found"

log "=== Phase 3 Complete ==="
log ""
log "To verify Thanos is collecting metrics from spoke clusters:"
log "  oc port-forward svc/observability-thanos-query 9090:9090 -n open-cluster-management-observability"
log "  curl 'http://localhost:9090/api/v1/query?query=up' | jq '.data.result | length'"
