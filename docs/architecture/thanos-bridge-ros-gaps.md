# Thanos Bridge — ROS Data Gaps and Implementation Plan

**Status:** Planned (not started)
**Date:** 2026-03-08
**Depends on:** Thanos Bridge cost data path (Phase 5–7, complete)

---

## Context

The Thanos Bridge currently handles **cost data only** (pod_usage, storage_usage, node_labels, namespace_labels, vm_usage, gpu_usage). ROS (Resource Optimization Service) data is not produced by the bridge. This document identifies the gaps and outlines the implementation plan to close them.

ROS data flows through a separate downstream path: the `kafka_msg_handler` extracts files listed in `resource_optimization_files` from the manifest, hands them to `ROSReportShipper`, which uploads them to a ROS-specific S3 bucket and publishes to `hccm.ros.events` Kafka topic for the ROS backend (`ros-ocp-backend`) to consume.

---

## Current State

### What CMMO does for ROS

1. **Namespace opt-in filtering**: Queries `kube_namespace_labels` for namespaces labeled with `cost_management_optimizations=true` (or the `insights_` prefixed variant). ROS data is only collected for opted-in namespaces. System namespaces (`kube-*`, `openshift*`) are always excluded.

2. **15-minute granularity**: ROS metrics are collected in 4 × 15-minute windows per hour (vs hourly for cost data), enabling more granular resource optimization analysis.

3. **Container-level queries**: CMMO defines 63 ROS-specific queries (`ros:` prefix):
   - 43 container-level queries: CPU/memory request, limit, usage, throttling (avg/min/max/sum), RSS memory, GPU accelerator metrics, image ownership, workload mapping
   - 20 namespace-level queries: CPU/memory aggregates, running/total pod counts

4. **Separate CSV schemas**:
   - ROS Container CSV: 49 columns (`ros-openshift-container-YYYYMM.csv`)
   - ROS Namespace CSV: 25 columns (`ros-openshift-namespace-YYYYMM.csv`)

5. **Separate manifest field**: ROS files are listed under `resource_optimization_files` (not `files`) in manifest.json.

### What the bridge produces today

- 6 cost report types: pod_usage, storage_usage, node_labels, namespace_labels, vm_usage, gpu_usage
- Listed under `files` in manifest.json
- No `resource_optimization_files` field
- No namespace opt-in filtering
- Hourly step only

---

## Identified Gaps

### Gap 1: No ROS queries in queries.py

The bridge's `queries.py` contains 33 cost queries in 7 groups. None of the 63 CMMO ROS queries are ported.

**Source:** `koku-metrics-operator/internal/collector/queries.go` lines 50–109, 507–1058

**Required queries** (by category):

| Category | Count | Examples |
|----------|-------|---------|
| Container CPU request | 2 | `ros:cpu_request_container_avg`, `ros:cpu_request_container_sum` |
| Container CPU limit | 2 | `ros:cpu_limit_container_avg`, `ros:cpu_limit_container_sum` |
| Container CPU usage | 4 | `ros:cpu_usage_container_{avg,min,max,sum}` |
| Container CPU throttle | 4 | `ros:cpu_throttle_container_{avg,min,max,sum}` |
| Container memory request | 2 | `ros:memory_request_container_avg`, `ros:memory_request_container_sum` |
| Container memory limit | 2 | `ros:memory_limit_container_avg`, `ros:memory_limit_container_sum` |
| Container memory usage | 4 | `ros:memory_usage_container_{avg,min,max,sum}` |
| Container memory RSS | 4 | `ros:memory_rss_usage_container_{avg,min,max,sum}` |
| Container image/workload | 2 | `ros:image_owners`, `ros:image_workloads` |
| GPU accelerator | 9 | core usage, memory copy, frame buffer (min/max/avg) |
| Namespace CPU | 6 | request sum, limit sum, usage avg/max/min, throttle avg/max/min |
| Namespace memory | 6 | same pattern as CPU |
| Namespace RSS | 3 | avg/max/min |
| Namespace pod counts | 4 | running max/avg, total max/avg |

All ROS queries use `[15m]` range windows and aggregate by `container, pod, namespace` (container queries) or `namespace` (namespace queries).

### Gap 2: No namespace opt-in filtering

CMMO gates ROS collection on the namespace label:

```promql
kube_namespace_labels{label_insights_cost_management_optimizations='true', namespace!~'kube-.*|openshift|openshift-.*'}
or
kube_namespace_labels{label_cost_management_optimizations='true', namespace!~'kube-.*|openshift|openshift-.*'}
```

The bridge must query this to determine which namespaces to include in ROS data. If no namespaces are labeled, ROS data generation should be skipped entirely (matching CMMO behavior).

**Note:** `kube_namespace_labels` is already in the MCO allowlist and available in Thanos.

### Gap 3: No ROS CSV transformer

`csv_transformer.py` has no methods to produce ROS CSVs. Two new transforms are needed:

**ROS Container CSV** (49 columns):
```
report_period_start, report_period_end, interval_start, interval_end,
container_name, pod, owner_name, owner_kind, workload, workload_type,
namespace, image_name, node, resource_id,
cpu_request_container_avg, cpu_request_container_sum,
cpu_limit_container_avg, cpu_limit_container_sum,
cpu_usage_container_avg, cpu_usage_container_min, cpu_usage_container_max, cpu_usage_container_sum,
cpu_throttle_container_avg, cpu_throttle_container_max, cpu_throttle_container_min, cpu_throttle_container_sum,
memory_request_container_avg, memory_request_container_sum,
memory_limit_container_avg, memory_limit_container_sum,
memory_usage_container_avg, memory_usage_container_min, memory_usage_container_max, memory_usage_container_sum,
memory_rss_usage_container_avg, memory_rss_usage_container_min, memory_rss_usage_container_max, memory_rss_usage_container_sum,
accelerator_model_name, accelerator_profile_name,
accelerator_core_usage_percentage_min, accelerator_core_usage_percentage_max, accelerator_core_usage_percentage_avg,
accelerator_memory_copy_percentage_min, accelerator_memory_copy_percentage_max, accelerator_memory_copy_percentage_avg,
accelerator_frame_buffer_usage_min, accelerator_frame_buffer_usage_max, accelerator_frame_buffer_usage_avg
```

**ROS Namespace CSV** (25 columns):
```
report_period_start, report_period_end, interval_start, interval_end,
namespace,
cpu_request_namespace_sum, cpu_limit_namespace_sum,
cpu_usage_namespace_avg, cpu_usage_namespace_max, cpu_usage_namespace_min,
cpu_throttle_namespace_avg, cpu_throttle_namespace_max, cpu_throttle_namespace_min,
memory_request_namespace_sum, memory_limit_namespace_sum,
memory_usage_namespace_avg, memory_usage_namespace_max, memory_usage_namespace_min,
memory_rss_usage_namespace_avg, memory_rss_usage_namespace_max, memory_rss_usage_namespace_min,
namespace_running_pods_max, namespace_running_pods_avg,
namespace_total_pods_max, namespace_total_pods_avg
```

### Gap 4: 15-minute window granularity

The bridge currently queries Thanos with `step=3600` (hourly). ROS requires `step=900` (15 minutes) and produces one row per 15-minute interval. The Thanos `query_range` API supports this natively — the bridge just needs to use a different step for ROS queries.

CMMO generates 4 windows per hour:
- HH:00:01 → HH:14:59
- HH:15:01 → HH:29:59
- HH:30:01 → HH:44:59
- HH:45:01 → HH:59:59

### Gap 5: Manifest missing resource_optimization_files

`manifest_builder.py` does not include `resource_optimization_files`. The downstream `kafka_msg_handler.py` reads this field to route ROS files to `ROSReportShipper`. Without it, ROS files in the payload are silently ignored.

### Gap 6: ROS file naming convention

CMMO uses specific file prefixes that the ROS backend relies on:
- `ros-openshift-container-YYYYMM.N.csv` (container-level)
- `ros-openshift-namespace-YYYYMM.N.csv` (namespace-level)

Where `N` is a sequence number when multiple 15-minute windows produce separate files.

### Gap 7: MCO allowlist for ROS-specific metrics

Some ROS-required metrics may not be in the MCO allowlist:
- `container_cpu_cfs_throttled_seconds_total` (CPU throttling) — needs `matches:` entry with `container!=""` filter
- `kube_pod_container_info` (container image info for ROS)

These need to be verified against the current MCO allowlist and added if missing. Note that MCO reconciles the ConfigMap, so runtime patching is not durable (see Phase 4 findings).

---

## Implementation Plan

### Step 1: Add ROS queries to queries.py

Port all 63 `ros:` queries from CMMO `queries.go` into `queries.py` as two new groups:
- `ROS_CONTAINER_QUERIES` (43 queries)
- `ROS_NAMESPACE_QUERIES` (20 queries)

Plus the namespace filter query:
- `ROS_NAMESPACE_FILTER` (1 query)

Each query uses `[15m]` range and aggregates by `container, pod, namespace` or `namespace`.

### Step 2: Add namespace opt-in filter to bridge.py

In `_process_cluster()`, before ROS query execution:
1. Query `kube_namespace_labels` with the opt-in label filter
2. Extract the list of enabled namespaces
3. If empty, skip ROS data generation (log and continue with cost-only)
4. If non-empty, add `namespace=~"ns1|ns2|..."` filter to all ROS queries

### Step 3: Add ROS CSV transforms to csv_transformer.py

Implement two new methods:
- `transform_ros_containers(query_results, enabled_namespaces)` → DataFrame (49 columns)
- `transform_ros_namespaces(query_results, enabled_namespaces)` → DataFrame (25 columns)

Key differences from cost transforms:
- 15-minute interval alignment (not hourly)
- Container-level granularity (not pod-level)
- `owner_name`, `owner_kind`, `workload`, `workload_type` from `ros:image_owners` / `ros:image_workloads`
- No seconds multiplication (ROS uses raw values, not `*_seconds`)
- Metric values are pre-aggregated (avg/min/max/sum come from separate queries)

### Step 4: Update bridge.py _process_cluster()

Add ROS data path after cost data generation:
1. Query namespace filter
2. If namespaces enabled: execute ROS queries, transform, generate ROS CSVs
3. Name files with `ros-openshift-container-` / `ros-openshift-namespace-` prefixes
4. Pass ROS filenames to manifest builder separately

### Step 5: Update manifest_builder.py

Accept `ros_filenames` parameter and populate `resource_optimization_files` in the manifest:

```python
def build_manifest(cluster_id, window_start, window_end, csv_filenames, ros_filenames=None):
    ...
    manifest["resource_optimization_files"] = ros_filenames or []
    ...
```

### Step 6: Verify MCO allowlist for ROS metrics

Confirm these metrics are in the MCO allowlist (or add them):
- `container_cpu_cfs_throttled_seconds_total`
- `kube_pod_container_info`
- `kube_pod_status_phase` (for running pod counts)

This requires an upstream MCO PR — not a runtime ConfigMap patch.

### Step 7: Add tests

- `test_queries.py` — verify all 63 ROS queries are present and valid
- `test_csv_transformer.py` — ROS container/namespace transform correctness (compare against CMMO expected_reports)
- `test_bridge.py` — namespace filter logic, ROS data path skipping when no namespaces labeled
- `test_manifest_builder.py` — verify `resource_optimization_files` field

### Step 8: E2E validation

- Label a namespace on the spoke cluster with `cost_management_optimizations=true`
- Deploy workloads in that namespace
- Run bridge, verify ROS CSVs in payload
- Verify `ROSReportShipper` uploads to ROS S3 bucket
- Verify ROS backend receives and processes the data
- Verify Kruize receives experiment data

---

## Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| Cost data path (Phase 5-7) | Complete | Bridge produces cost CSVs end-to-end |
| MCO allowlist for ROS metrics | Partial | Some metrics may be missing; needs upstream PR |
| Spoke cluster with labeled namespace | Required for E2E | Namespace must have `cost_management_optimizations=true` |
| ROS backend deployed on hub | Required for E2E | Already deployed in cost-onprem |

---

## References

- CMMO ROS queries: `koku-metrics-operator/internal/collector/queries.go` lines 50–109, 507–1058
- CMMO ROS report generation: `koku-metrics-operator/internal/collector/collector.go` lines 235–263, 549–631
- CMMO ROS CSV schemas: `koku-metrics-operator/internal/collector/types.go` lines 475–638
- CMMO expected ROS output: `koku-metrics-operator/internal/collector/test_files/expected_reports/ros-openshift-container-*.csv`
- Koku ROS file handling: `koku/masu/external/kafka_msg_handler.py` lines 432–443
- ROS report shipper: `koku/masu/external/ros_report_shipper.py`
- ROS backend CSV parsing: `ros-ocp-backend/internal/types/csvColumnMapping.go`
