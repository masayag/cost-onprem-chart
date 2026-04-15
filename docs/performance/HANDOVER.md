# Performance Tuning Epic - Handover Document

## Epic

- **Jira Epic**: [FLPATH-4036](https://redhat.atlassian.net/browse/FLPATH-4036) - CoP - Performance Tuning & Hardware Sizing Guidelines
- **Parent Feature**: [FLPATH-2601](https://redhat.atlassian.net/browse/FLPATH-2601) - Cost Management On-Premise (CoP) Support
- **Component**: `insights-on-prem`

## Current State

The epic FLPATH-4036 has been created in Jira with the full performance tuning plan as its description. The plan body covers architecture analysis, scenario definitions, metrics strategy, bottleneck hypotheses, and hardware sizing recommendations.

### What Has Been Done

1. Epic created and linked to parent FLPATH-2601
2. NISE scenario profile generator created at `scripts/perf/scenarios/generate_scenarios.py`
3. Scenario profiles S1-S11 generated as YAML files in `scripts/perf/scenarios/`

### What Remains

1. Create the Jira stories listed below under the epic
2. Implement each story per the acceptance criteria
3. Run performance tests and produce the sizing guide

---

## Stories to Create in Jira

All stories should be created with:
- **Project**: FLPATH
- **Type**: Story
- **Parent**: FLPATH-4036
- **Component**: insights-on-prem

### Phase 1: Foundation (Infrastructure & Tooling)

#### Story 1.1: Build Performance Test Data Generator

**Summary**: `CoP Perf: Build performance test data generator script`

**Description**:
Create automated tooling around NISE to generate test datasets for all scenario profiles (S1-S11). The generator should accept a scenario ID and produce ready-to-upload tar.gz payloads with correct manifest.json structure.

A scenario profile generator script already exists at `scripts/perf/scenarios/generate_scenarios.py` and NISE YAML profiles are generated under `scripts/perf/scenarios/`. The remaining work is to create `scripts/perf/generate-test-data.sh` that:

1. Takes scenario ID (S1-S11, M1-M6) as input
2. Uses the pre-generated NISE static report YAML for the scenario
3. Runs NISE to produce CSV data
4. Packages into tar.gz with proper manifest.json (including required `start`, `end` fields)
5. Optionally uploads directly to the ingress endpoint
6. Supports multi-cluster generation with unique cluster_id per cluster

Acceptance Criteria:
- Script accepts scenario ID and produces correctly structured upload payload
- Generated data matches expected row counts (within 5%) for each scenario
- Supports multi-cluster generation with unique cluster_id per cluster
- Generates both cost data (pod_usage.csv) and ROS data (ros_usage.csv)
- manifest.json includes start, end, files, resource_optimization_files fields

Reference files:
- `tests/e2e_helpers.py` - existing NISE integration (NISEConfig, generate_nise_data)
- `tests/utils.py` - create_upload_package_from_files function
- `tests/data/nise_templates/` - existing NISE template examples

---

#### Story 1.2: Deploy Observability Stack

**Summary**: `CoP Perf: Deploy observability stack for metrics collection`

**Description**:
Deploy Prometheus, Grafana, postgres_exporter, Valkey exporter, and node_exporter on the test cluster. Create dashboards for each component layer.

Acceptance Criteria:
- All exporters deployed and scraping successfully
- 6 Grafana dashboards created (Overview, Ingress, Processing, Database, ROS, Infrastructure)
- pg_stat_statements enabled on PostgreSQL
- Metrics retention set to 30 days
- Dashboard JSON exported for reproducibility

Metrics to capture per layer (see full plan in epic description, Section 5):
- Ingress: upload latency, payload sizes, Kafka produce failures
- Kafka: consumer group lag, topic throughput
- Celery: queue depth per queue, task duration, failure rate
- PostgreSQL: query latency p95, active connections, table sizes, WAL write rate, lock contention
- Valkey: memory usage, evictions, connected clients
- ROS: CSV download time, Kruize API latency, aggregation time
- Node: CPU, memory, disk I/O, network

---

#### Story 1.3: Build Upload Automation Framework

**Summary**: `CoP Perf: Build upload automation framework`

**Description**:
Create a test harness (`scripts/perf/upload-test-data.sh`) that can submit uploads at configurable rates, simulate multiple clusters, and record timing data. Should support baseline (sequential), sustained (periodic), stress (increasing), and spike (burst) patterns.

Acceptance Criteria:
- Supports all 4 load patterns (baseline, sustained, stress, spike)
- Configurable: upload rate, cluster count, scenario profile
- Records upload HTTP response times and status codes
- Produces CSV output of all timing data
- Handles JWT authentication via Keycloak
- Supports multi-cluster simulation with unique cluster_ids

Reference files:
- `tests/e2e_helpers.py` - upload_with_retry, register_source functions
- `tests/conftest.py` - JWT token acquisition from Keycloak

---

#### Story 1.4: Establish PostgreSQL Performance Baseline

**Summary**: `CoP Perf: Establish PostgreSQL performance baseline`

**Description**:
Before load testing, characterize PostgreSQL performance with pg_stat_statements. Run summary SQL queries manually at different data volumes to establish query-level baselines.

Acceptance Criteria:
- pg_stat_statements capturing all queries
- Baseline query times for all 9 UI summary tables documented
- Index usage validated with EXPLAIN ANALYZE
- Vacuum and bloat baseline documented

Key tables to profile:
- reporting_ocpusagelineitem_daily_summary
- reporting_ocpusagepodlabel_summary
- reporting_ocpcostentrylineitem_daily_summary
- reporting_ocp_compute_summary_p
- reporting_ocp_cost_summary_p
- reporting_ocp_cost_summary_by_project_p
- reporting_ocp_cost_summary_by_node_p
- reporting_ocp_pod_summary_p
- reporting_ocp_volume_summary_p

---

### Phase 2: Baseline Testing

#### Story 2.1: Single-Cluster Baseline (S1-S8)

**Summary**: `CoP Perf: Run single-cluster baseline tests (S1-S8)`

**Description**:
Run baseline tests for scenarios S1 through S8. For each scenario: upload a single day's data, measure end-to-end processing time, record per-component resource usage.

Acceptance Criteria:
- End-to-end latency measured for each scenario (upload to summary table population)
- Per-component CPU/memory peak and average recorded
- PostgreSQL query times for summary updates recorded
- ROS processing time (CSV download, aggregation, Kruize calls) recorded
- Results documented in a comparison table

Dependencies: Stories 1.1, 1.2, 1.3

---

#### Story 2.2: Multi-Cluster Baseline (M1-M4)

**Summary**: `CoP Perf: Run multi-cluster baseline tests (M1-M4)`

**Description**:
Run baseline tests for multi-cluster scenarios M1 through M4. Upload data for all clusters in the scenario at realistic intervals (every 6 hours simulated).

Multi-cluster scenario definitions:
- M1: Edge - 5 clusters x S1 (SNO Low)
- M2: Small Enterprise - 3 clusters x S4 (Small Medium)
- M3: Medium Enterprise - 10 clusters x S4 (Small Medium)
- M4: Large Enterprise - 5 clusters x S7 (Medium Medium)

Acceptance Criteria:
- Steady-state processing verified (queue depth returns to 0 between upload cycles)
- Per-cluster processing time measured
- Kafka consumer lag stays below threshold
- Resource utilization captured per component
- Identified scenario where steady-state breaks (queue growth > drain)

Dependencies: Story 2.1

---

### Phase 3: Stress & Soak Testing

#### Story 3.1: Stress Test - Find Breaking Point

**Summary**: `CoP Perf: Stress test to find system breaking point`

**Description**:
Starting from M3 configuration, linearly increase cluster count (adding 2 clusters every 30 minutes) until the system can no longer process within a 24-hour window.

Acceptance Criteria:
- Maximum cluster count identified before processing falls behind
- First-to-saturate component identified with evidence
- Resource utilization at saturation point documented
- Kafka consumer lag, Celery queue depth, and PostgreSQL lock contention at breaking point

Dependencies: Story 2.2

---

#### Story 3.2: Soak Test - 7-Day Stability

**Summary**: `CoP Perf: 7-day soak test for stability validation`

**Description**:
Run M3 scenario at sustained rate for 7 days. Monitor for memory leaks, disk growth, queue buildup, and performance degradation as data accumulates.

Acceptance Criteria:
- No OOM kills over 7 days
- PostgreSQL disk usage growth rate is linear and predictable
- Valkey eviction rate stays below 100 evictions/hour
- No Celery chord failures
- End-to-end latency does not degrade >20% from day 1 to day 7
- Autovacuum keeps up with dead tuple generation

Dependencies: Story 2.2

---

#### Story 3.3: Spike Test - Backlog Recovery

**Summary**: `CoP Perf: Spike test for backlog recovery validation`

**Description**:
Simulate 24-hour outage recovery: accumulate 24 hours of uploads (4 per cluster x N clusters), then submit all at once. Measure recovery time.

Acceptance Criteria:
- Recovery time measured (time to drain all queues to 0)
- No data loss during spike processing
- System returns to normal operation after spike
- Peak resource utilization during spike documented

Dependencies: Story 2.2

---

### Phase 4: Bottleneck Investigation

#### Story 4.1: PostgreSQL Bottleneck Analysis

**Summary**: `CoP Perf: PostgreSQL bottleneck analysis and tuning`

**Description**:
Investigate PostgreSQL as bottleneck. Test with increased CPU/memory (2/4/8 cores, 4/8/16Gi). Measure impact on summary update duration, API query latency, and overall throughput.

Hypothesis H1: PostgreSQL is the primary bottleneck at S7+ due to 500m CPU / 512Mi memory limits and DELETE/INSERT summary update pattern.

Acceptance Criteria:
- Summary update duration measured at 3+ PostgreSQL sizing levels
- API query p95 latency measured at each sizing level
- Diminishing returns threshold identified
- PostgreSQL tuning parameters (shared_buffers, work_mem) impact quantified
- Lock contention analysis completed

Dependencies: Story 3.1

---

#### Story 4.2: Celery Worker Scaling Analysis

**Summary**: `CoP Perf: Celery worker horizontal scaling analysis`

**Description**:
Test horizontal scaling of Celery workers. For the top-2 busiest queues (priority, summary), add replicas (1, 2, 4) and measure throughput improvement.

Hypothesis H2: At M3+ (10 clusters), Celery queue depth grows faster than drain rate with 25 total task slots.

Acceptance Criteria:
- Task throughput measured at each replica count for priority and summary queues
- Database connection impact of additional workers quantified
- Valkey memory impact of additional workers measured
- Optimal worker count per queue identified for each tier

Dependencies: Story 3.1

---

#### Story 4.3: ROS Processing Pipeline Analysis

**Summary**: `CoP Perf: ROS processor bottleneck analysis`

**Description**:
Investigate ROS processor bottleneck. Measure time spent in CSV download, aggregation, and Kruize API calls separately. Test with increased memory limits.

Hypotheses:
- H3: ROS processor will OOM at S8+ due to csv.ReadAll() loading full CSV into memory
- H4: Kruize sequential API calls dominate processing time at S5+ (500 workloads)

Acceptance Criteria:
- Time breakdown: CSV download vs aggregation vs Kruize API calls documented
- Memory profile during S5-S8 processing captured (peak RSS)
- OOM threshold identified (max CSV size before crash)
- Kruize API latency characterized (p50/p95/p99)
- Recommendations for ROS processor improvements documented

Dependencies: Story 2.1

---

#### Story 4.4: Valkey Capacity Analysis

**Summary**: `CoP Perf: Valkey memory capacity and eviction analysis`

**Description**:
Test Valkey under load to determine memory requirements. Monitor eviction behavior, Celery chord stability, and task result availability at different maxmemory settings.

Hypothesis H6: At M4+ load, Celery task results + queue metadata + application cache exceeds 512MB, causing LRU eviction of active task results and chord callback failures.

Acceptance Criteria:
- Eviction rate measured at 512MB, 1GB, 2GB maxmemory
- Celery chord failure correlation with eviction rate established
- Minimum safe maxmemory for each tier identified
- Task result loss rate measured at each setting

Dependencies: Story 3.1

---

### Phase 5: Tuning & Validation

#### Story 5.1: Configuration Tuning Validation

**Summary**: `CoP Perf: Validate tuned configuration recommendations`

**Description**:
Apply recommended tuning parameters from Phase 4 findings. Re-run stress test to validate improvement.

Acceptance Criteria:
- Tuned configuration supports 2x cluster count vs default configuration
- End-to-end latency improved by >30% at same load
- No new failure modes introduced
- Tuning parameters documented with rationale

Dependencies: Stories 4.1, 4.2, 4.3, 4.4

---

#### Story 5.2: Produce Hardware Sizing Guide

**Summary**: `CoP Perf: Produce hardware sizing guide document`

**Description**:
Based on all test results, produce a hardware sizing guide document with specific recommendations per tier. Include PostgreSQL tuning parameters.

Acceptance Criteria:
- Sizing table for 4 tiers (Small/Medium/Large/XL) with specific resource values
- PostgreSQL tuning parameters per tier
- Valkey sizing per tier
- Storage projections (3-month and 12-month)
- Maximum cluster count per tier documented
- Known limitations and caveats listed

Dependencies: Story 5.1

---

#### Story 5.3: Create Values Override Files

**Summary**: `CoP Perf: Create Helm values override files per sizing tier`

**Description**:
Create Helm values override files for each tier (values-small.yaml, values-medium.yaml, values-large.yaml, values-xl.yaml) with validated resource allocations and PostgreSQL tuning.

Acceptance Criteria:
- 4 values override files created and validated
- Each file deploys successfully on target hardware
- Baseline performance test passes with each override file
- Files documented in chart README

Dependencies: Story 5.2

---

### Phase 6: Regression & Automation

#### Story 6.1: Automated Performance Regression Suite

**Summary**: `CoP Perf: Create automated performance regression test suite`

**Description**:
Create a CI-runnable performance test that validates baseline scenarios (S4, M2) complete within expected time bounds. Integrate with existing CI framework.

Acceptance Criteria:
- Automated test runs S4 scenario and validates end-to-end latency < threshold
- Test produces JUnit XML output compatible with existing CI
- Performance metrics exported for trend analysis
- Can be triggered manually or on schedule

Dependencies: Story 5.1

---

## Implementation Notes

### Codebase Context

- **Helm chart**: `cost-onprem/` directory, `values.yaml` at root
- **Tests**: `tests/` directory with pytest framework
- **Scripts**: `scripts/` directory
- **NISE data generation**: `tests/e2e_helpers.py` has NISEConfig and generate_nise_data()
- **Upload mechanism**: `tests/e2e_helpers.py` has upload_with_retry()
- **Existing NISE templates**: `tests/data/nise_templates/`
- **Performance scenarios**: `scripts/perf/scenarios/` (already generated)

### Key Files to Understand

| File | Purpose |
|------|---------|
| `cost-onprem/values.yaml` | All component resource limits, replicas, configuration |
| `tests/e2e_helpers.py` | NISE integration, source registration, upload logic |
| `tests/utils.py` | Kubernetes utilities, upload package creation |
| `tests/conftest.py` | JWT token fixtures, cluster config |
| `scripts/perf/scenarios/generate_scenarios.py` | Generates NISE profiles for all scenarios |
| `scripts/perf/scenarios/s*.yml` | Pre-generated NISE scenario profiles |

### Current Default Resource Limits (from values.yaml)

| Component | CPU Limit | Memory Limit | Replicas |
|-----------|----------|-------------|----------|
| Koku API | 1 | 2Gi | 1 |
| MASU | 500m | 2Gi | 1 |
| Listener | 300m | 600Mi | 1 |
| Celery Beat | 100m | 400Mi | 1 |
| Worker Default | 200m | 400Mi | 1 |
| Worker Priority | 500m | 2Gi | 1 |
| Worker Summary | 500m | 2Gi | 1 |
| Worker OCP | 500m | 1Gi | 1 |
| Worker Cost Model | 200m | 512Mi | 1 |
| PostgreSQL | 500m | 512Mi | 1 (30Gi PVC) |
| Valkey | 500m | 512Mi | 1 (5Gi PVC, 512MB maxmemory) |
| ROS Processor | 1 | 1Gi | 1 |
| Kruize | 1000m | 2Gi | 1 |

### Creating the Jira Stories

Use the following jira CLI command pattern for each story:

```bash
jira issue create \
  -p FLPATH \
  -t Story \
  -P FLPATH-4036 \
  -C "insights-on-prem" \
  -s "<summary from above>" \
  -b "<description from above>" \
  --no-input
```

Or use the `--template` flag to load long descriptions from files:

```bash
jira issue create \
  -p FLPATH \
  -t Story \
  -P FLPATH-4036 \
  -C "insights-on-prem" \
  -s "<summary>" \
  -T /path/to/description.md \
  --no-input
```

### Naming Convention

Existing epics under FLPATH-2601 use the prefix `CoP - `. Stories under performance tuning should use `CoP Perf: ` for easy filtering.

### Story Count Summary

| Phase | Stories | Description |
|-------|---------|-------------|
| Phase 1: Foundation | 4 | Tooling, observability, upload framework, DB baseline |
| Phase 2: Baseline | 2 | Single-cluster and multi-cluster baselines |
| Phase 3: Stress & Soak | 3 | Stress, soak (7-day), and spike tests |
| Phase 4: Bottleneck Investigation | 4 | PostgreSQL, Celery, ROS, Valkey deep-dives |
| Phase 5: Tuning & Validation | 3 | Tuning validation, sizing guide, values files |
| Phase 6: Regression | 1 | Automated CI regression suite |
| **Total** | **17** | |
