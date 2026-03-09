#!/bin/bash
set -euo pipefail

HUB_API_URL="${HUB_API_URL:-https://api.cluster-p67mq.dynamic.redhatworkshops.io:6443}"
HUB_ADMIN_USER="${HUB_ADMIN_USER:-admin}"
HUB_ADMIN_PASS="${HUB_ADMIN_PASS:-MjU3MTY5}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== Phase 4: Cost Metrics Allowlist Extension ==="

log "Logging into hub cluster..."
oc login "${HUB_API_URL}" -u "${HUB_ADMIN_USER}" -p "${HUB_ADMIN_PASS}" --insecure-skip-tls-verify=true

log "Step 1: Creating custom metrics allowlist ConfigMap..."
oc apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: observability-metrics-custom-allowlist
  namespace: open-cluster-management-observability
data:
  metrics_list.yaml: |
    names:
      - container_cpu_usage_seconds_total
      - container_memory_usage_bytes
      - kube_node_info
      - kube_node_labels
      - kube_node_role
      - kube_pod_status_phase
      - kube_pod_labels
      - kube_pod_container_info
      - kube_pod_container_resource_limits
      - kube_pod_container_resource_requests
      - kube_persistentvolume_capacity_bytes
      - kube_persistentvolume_info
      - kube_persistentvolume_labels
      - kube_persistentvolumeclaim_info
      - kube_persistentvolumeclaim_labels
      - kube_persistentvolumeclaim_resource_requests_storage_bytes
      - kube_pod_spec_volumes_persistentvolumeclaims_info
      - kubelet_volume_stats_used_bytes
      - kube_namespace_labels
      - kubevirt_vm_resource_limits
      - kubevirt_vm_labels
      - kubevirt_vm_disk_allocated_size_bytes
      - DCGM_FI_PROF_GR_ENGINE_ACTIVE
      - DCGM_FI_DEV_GPU_UTIL
      - DCGM_FI_DEV_MEM_COPY_UTIL
      - DCGM_FI_DEV_FB_USED
    matches:
      - __name__="container_cpu_cfs_throttled_seconds_total",container!=""
      - __name__="container_cpu_usage_seconds_total",container!=""
      - __name__="container_memory_usage_bytes",container!=""
      - __name__="container_memory_rss",container=~".+"
      - __name__="container_memory_working_set_bytes",container=~".+"
      - __name__="kube_pod_container_resource_limits",namespace!=""
      - __name__="kube_pod_container_resource_requests",namespace!=""
EOF

log "Step 2: Patching default allowlist to move cost metrics from SNO collect_rules to static collection..."
oc get configmap observability-metrics-allowlist \
  -n open-cluster-management-observability \
  -o jsonpath='{.data.metrics_list\.yaml}' > /tmp/mco-allowlist.yaml

python3 -c "
import re
with open('/tmp/mco-allowlist.yaml') as f:
    content = f.read()

content = content.replace(
    '          - kube_pod_container_resource_limits \n'
    '          - kube_pod_container_resource_requests   \n',
    '')
content = content.replace(
    '          - kube_pod_container_resource_limits \n'
    '          - kube_pod_container_resource_requests \n',
    '')
content = content.replace(
    '          - __name__=\"container_memory_rss\",container!=\"\"\n',
    '')
content = content.replace(
    '          - __name__=\"container_memory_working_set_bytes\",container!=\"\"\n',
    '')

with open('/tmp/mco-allowlist-patched.yaml', 'w') as f:
    f.write(content)
print('Patched')
"

CURRENT_JSON=$(oc get configmap observability-metrics-allowlist -n open-cluster-management-observability -o json)
python3 -c "
import json, sys
cm = json.loads('''${CURRENT_JSON}''')
with open('/tmp/mco-allowlist-patched.yaml') as f:
    cm['data']['metrics_list.yaml'] = f.read()
for key in ['resourceVersion', 'uid', 'creationTimestamp', 'managedFields']:
    cm.get('metadata', {}).pop(key, None)
json.dump(cm, sys.stdout)
" | oc apply -f -

log "Step 3: Waiting for MCO to propagate changes (~2-5 minutes)..."
sleep 180

log "Step 4: Verifying metrics are flowing..."
oc port-forward svc/observability-thanos-query -n open-cluster-management-observability 19092:9090 &
PF_PID=$!
sleep 3

VERIFY_METRICS=(
  "kube_node_info"
  "kube_node_labels"
  "kube_pod_status_phase"
  "container_cpu_usage_seconds_total"
  "container_memory_rss"
  "container_memory_working_set_bytes"
  "kube_pod_container_resource_limits"
  "kube_pod_container_resource_requests"
  "kube_namespace_labels"
)

for metric in "${VERIFY_METRICS[@]}"; do
  count=$(curl -s "http://localhost:19092/api/v1/query?query=count(${metric})" 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    results = data.get('data', {}).get('result', [])
    print(results[0]['value'][1] if results else '0')
except:
    print('error')
" 2>/dev/null)
  if [ "$count" = "0" ] || [ "$count" = "error" ]; then
    log "  ${metric}: NOT YET AVAILABLE (may need more time)"
  else
    log "  ${metric}: ${count} series"
  fi
done

kill $PF_PID 2>/dev/null

log "=== Phase 4 Complete ==="
