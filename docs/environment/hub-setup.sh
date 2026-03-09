#!/bin/bash
set -euo pipefail

HUB_API_URL="${HUB_API_URL:-https://api.cluster-p67mq.dynamic.redhatworkshops.io:6443}"
HUB_ADMIN_USER="${HUB_ADMIN_USER:-admin}"
HUB_ADMIN_PASS="${HUB_ADMIN_PASS:-MjU3MTY5}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== Phase 1: ACM Installation on Hub Cluster ==="

log "Logging into hub cluster..."
oc login "${HUB_API_URL}" -u "${HUB_ADMIN_USER}" -p "${HUB_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Verifying cluster identity..."
oc get clusterversion -o jsonpath='{.items[0].spec.clusterID}'
echo

log "Step 1: Creating namespace open-cluster-management..."
oc apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: open-cluster-management
EOF

log "Step 2: Creating OperatorGroup..."
oc apply -f - <<'EOF'
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: acm-operatorgroup
  namespace: open-cluster-management
spec:
  targetNamespaces:
    - open-cluster-management
EOF

log "Step 3: Creating Subscription for advanced-cluster-management..."
oc apply -f - <<'EOF'
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: advanced-cluster-management
  namespace: open-cluster-management
spec:
  channel: release-2.12
  installPlanApproval: Automatic
  name: advanced-cluster-management
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

log "Step 4: Waiting for operator CSV to succeed..."
log "  (This may take several minutes)"
until oc get csv -n open-cluster-management -o jsonpath='{.items[?(@.spec.displayName=="Advanced Cluster Management for Kubernetes")].status.phase}' 2>/dev/null | grep -q "Succeeded"; do
  sleep 15
  echo -n "."
done
echo
log "ACM operator CSV succeeded."

log "Step 5: Creating MultiClusterHub CR..."
oc apply -f - <<'EOF'
apiVersion: operator.open-cluster-management.io/v1
kind: MultiClusterHub
metadata:
  name: multiclusterhub
  namespace: open-cluster-management
spec: {}
EOF

log "Step 6: Waiting for MultiClusterHub to be ready (timeout 900s)..."
oc wait --for=condition=Complete multiclusterhub/multiclusterhub \
  -n open-cluster-management --timeout=900s || {
    log "WARNING: MCH not ready within timeout. Check status:"
    oc get multiclusterhub -n open-cluster-management -o yaml
    exit 1
  }

log "=== Phase 1 Verification ==="
log "MultiClusterHub status:"
oc get multiclusterhub -n open-cluster-management
log "Pods in open-cluster-management:"
oc get pods -n open-cluster-management --no-headers | head -20
log "Local cluster registration:"
oc get managedcluster local-cluster 2>/dev/null || log "local-cluster not yet registered"

log "=== Phase 1 Complete ==="

log "Step 7: Increasing maxPods limit for SNO (required for ACM+MCO)..."
oc apply -f - <<'EOF'
apiVersion: machineconfiguration.openshift.io/v1
kind: KubeletConfig
metadata:
  name: increase-max-pods
spec:
  machineConfigPoolSelector:
    matchLabels:
      pools.operator.machineconfiguration.openshift.io/master: ""
  kubeletConfig:
    maxPods: 500
EOF

log "Waiting for MachineConfigPool master to finish updating (node reboot expected)..."
oc wait --for=condition=Updated mcp/master --timeout=900s || {
  log "WARNING: MCP update not complete within timeout."
  oc get mcp master -o yaml
  exit 1
}
log "maxPods increased to 500. Node rebooted and ready."
