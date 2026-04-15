# Performance Tuning Epic: Cost Management On-Premise

## Context

Cost Management On-Premise processes OpenShift cluster cost and resource optimization data through a multi-stage pipeline: ingestion (insights-ingress-go), cost processing (koku/MASU + Celery workers), resource optimization (ros-ocp-backend + Kruize), all backed by PostgreSQL and Valkey. The current Helm chart deploys all components with conservative defaults (single replicas, low resource limits) without validated sizing guidance. Customers deploying to clusters of varying sizes have no way to predict resource requirements, identify bottlenecks, or understand system limits. This epic establishes empirical performance baselines, identifies scaling boundaries, and produces actionable hardware sizing guidelines.

---

## 1. System Understanding

### Architecture (Performance-Critical View)

```
Upload (tar.gz)
    |
    v
Ingress (Go) ──store──> S3 (ODF/NooBaa)
    |
    v announce
Kafka ─────────────────────────────────────────────────
    |                              |
    v                              v
Koku Listener (Python)       ROS Processor (Go)
    |                              |
    v                              v
S3 (daily CSVs)             Kruize (Java) ──> PostgreSQL (ros)
    |
    v  celery tasks
Workers ────────────────────────────────> PostgreSQL (koku)
  download -> process (CSV->Parquet->DB) -> summarize (9 tables) -> cost model
```

### Natural Bottlenecks Identified from Code

| Component | Bottleneck | Evidence |
|-----------|-----------|----------|
| **Koku Listener** | Single Kafka consumer, synchronous processing | `listener.py` - sequential message handling |
| **Celery Workers** | Concurrency 5 per queue, 1 replica each | `values.yaml:300` - 25 total task slots |
| **PostgreSQL** | Single instance, CPU 500m limit, 512Mi memory limit | `values.yaml:895-901` - severely undersized for analytical workloads |
| **ROS Processor** | Single-threaded, full CSV in-memory, synchronous Kruize calls | `report_processor.go` - no parallelism |
| **Summary SQL** | DELETE/INSERT pattern on 9 tables per provider per update cycle | `ocp_report_db_accessor.py` - 2100+ lines of SQL |
| **Valkey** | 512MB cap, Celery broker + cache dual role | `values.yaml:678` - LRU eviction under load |
| **Parquet conversion** | 200K row batch, full pandas DataFrame in memory | `parquet_report_processor.py` |

---

## 2. Performance Testing Strategy

### Goals

1. Establish **baseline throughput**: end-to-end processing time for a single cluster payload at each scale
2. Identify **saturation points**: which component fails first and at what load
3. Measure **scaling characteristics**: how does doubling cluster count affect processing time?
4. Validate **data retention impact**: how does 3 months of accumulated data affect query/summary performance?
5. Produce **hardware sizing guidelines**: CPU, memory, storage recommendations per cluster profile
6. Identify **configuration tuning opportunities**: which knobs yield the most improvement?

### Success Criteria

- SC-1: Published sizing table mapping cluster profiles to resource requirements
- SC-2: Documented maximum supported cluster count per deployment size (S/M/L/XL)
- SC-3: Identified top-3 bottlenecks with measured impact and mitigation options
- SC-4: Validated that recommended configurations sustain daily processing within a 6-hour window
- SC-5: Soak test demonstrates 7-day stability without OOM, disk exhaustion, or queue starvation

### KPIs

| KPI | Target (Baseline) | Measurement |
|-----|-------------------|-------------|
| End-to-end ingestion latency (upload to summary tables populated) | < 30 min for small cluster | Timer from upload HTTP 202 to last summary table UPDATE timestamp |
| Kafka consumer lag (listener) | < 10 messages | Kafka consumer group lag metric |
| Celery queue depth | < 50 pending tasks | Valkey queue length |
| PostgreSQL query p95 latency (UI summary) | < 5s | pg_stat_statements |
| Memory utilization (all pods) | < 80% of limit | container_memory_working_set_bytes / limit |
| CPU utilization (all pods) | < 70% of limit sustained | container_cpu_usage_seconds_total / limit |
| S3 storage growth rate | Predictable linear | Bucket size over time |
| Error rate (HTTP 5xx, task failures) | < 0.1% | Prometheus counters |

---

## 3. Scenario Definitions

### Cluster Profiles

The data volume produced by a cluster depends on: number of nodes, pods per node, namespaces, and upload frequency. OCP uploads cost data daily (24h reporting window, typically 288 intervals at 5-min granularity per pod).

#### Workload Density Definitions

| Density | Pods/Node | Namespaces | Containers/Pod (avg) | Rationale |
|---------|-----------|------------|---------------------|-----------|
| Low | 10-20 | 5-10 | 1-2 | Bare infrastructure, few workloads |
| Medium | 30-50 | 15-30 | 2-3 | Typical enterprise deployment |
| High | 80-120 | 40-80 | 3-5 | Dense microservices, CI/CD clusters |

#### Scenario Matrix

| Profile | Nodes | Density | Total Pods | Namespaces | CSV Rows/Day | Upload Size (gzip) | DB Rows/Month (koku) | ROS Workloads |
|---------|-------|---------|-----------|------------|-------------|--------------------|--------------------|---------------|
| **S1: SNO Low** | 1 | Low | 15 | 5 | ~4,300 | ~200 KB | ~130K | 15 |
| **S2: SNO Medium** | 1 | Medium | 40 | 10 | ~11,500 | ~500 KB | ~345K | 40 |
| **S3: Small Low** | 3 | Low | 45 | 10 | ~13,000 | ~550 KB | ~390K | 45 |
| **S4: Small Medium** | 5 | Medium | 200 | 25 | ~57,600 | ~2.5 MB | ~1.7M | 200 |
| **S5: Small High** | 5 | High | 500 | 50 | ~144,000 | ~6 MB | ~4.3M | 500 |
| **S6: Medium Low** | 10 | Low | 150 | 20 | ~43,200 | ~1.8 MB | ~1.3M | 150 |
| **S7: Medium Medium** | 25 | Medium | 1,000 | 60 | ~288,000 | ~12 MB | ~8.6M | 1,000 |
| **S8: Medium High** | 50 | High | 5,000 | 200 | ~1,440,000 | ~60 MB | ~43M | 5,000 |
| **S9: Large Low** | 100 | Low | 1,500 | 50 | ~432,000 | ~18 MB | ~13M | 1,500 |
| **S10: Large Medium** | 200 | Medium | 8,000 | 150 | ~2,304,000 | ~95 MB | ~69M | 8,000 |
| **S11: Large High** | 500 | High | 50,000 | 500 | ~14,400,000 | ~600 MB | ~432M | 50,000 |

**Row count formula**: `pods x 288 intervals/day x (pod_usage + storage_usage factor ~1.0)`
**Monthly**: `daily_rows x 30`
**Upload size**: ~43 bytes/CSV row compressed at ~10:1 ratio

#### Multi-Cluster Scenarios

| Scenario | Clusters | Profile Each | Total Daily Rows | Total Monthly DB Rows |
|----------|----------|-------------|-----------------|---------------------|
| **M1: Edge** | 5 | S1 (SNO Low) | ~21,500 | ~650K |
| **M2: Small Enterprise** | 3 | S4 (Small Medium) | ~172,800 | ~5.1M |
| **M3: Medium Enterprise** | 10 | S4 (Small Medium) | ~576,000 | ~17M |
| **M4: Large Enterprise** | 5 | S7 (Medium Medium) | ~1,440,000 | ~43M |
| **M5: Hyperscale** | 20 | S7 (Medium Medium) | ~5,760,000 | ~172M |
| **M6: Extreme** | 50 | S6 (Medium Low) | ~2,160,000 | ~65M |

---

## 4. Data Generation Design

### Tooling: NISE (koku-nise)

NISE is the existing data generation tool used by E2E tests. It generates proper OCP cost CSVs with manifest.json.

```bash
nise report ocp \
  --ros-ocp-info \
  --static-report-file <profile>.yml \
  --ocp-cluster-id <cluster-uuid> \
  --write-monthly \
  --start-date <YYYY-MM-DD> \
  --end-date <YYYY-MM-DD>
```

### Static Report Configuration per Scenario

A NISE static report YAML defines workloads. Example for S4 (Small Medium, 200 pods):

```yaml
generators:
  - OCPGenerator:
      start_date: 2026-04-01
      end_date: 2026-04-30
      nodes:
        - node_name: node-{n}  # 5 nodes
          cpu_cores: 16
          memory_gig: 64
          namespaces:
            - namespace: ns-{m}  # 25 namespaces
              pods:
                - pod_name: pod-{p}  # 8 pods/namespace
                  cpu_request: 100m-2000m
                  cpu_limit: 200m-4000m
                  mem_request_gig: 0.1-4
                  mem_limit_gig: 0.5-8
```

### Data Generation Automation

Create a script `scripts/perf/generate-test-data.sh` that:

1. Takes scenario ID (S1-S11, M1-M6) as input
2. Generates appropriate NISE static report YAML
3. Runs NISE to produce CSV data
4. Packages into tar.gz with proper manifest.json
5. Optionally uploads directly to the ingress endpoint

### Upload Frequency Simulation

- **Real-world pattern**: Cost Management Metrics Operator uploads every 6 hours (4x/day)
- **Test acceleration**: Upload at 1-minute intervals to simulate backlog processing
- **Spike test**: Burst 24 uploads simultaneously (simulating 6 days of backlog)

### Multi-Cluster Simulation

Each cluster gets a unique `cluster_id` and `source_id`. Data generation produces separate payloads per cluster. Upload script cycles through clusters with configurable delay.

---

## 5. Metrics & Benchmarking Plan

### Metrics Collection by Layer

| Layer | Metric | Source | Collection Method |
|-------|--------|--------|------------------|
| **Ingress** | Upload latency (p50/p95/p99) | `ingress_stage_seconds` | Prometheus |
| **Ingress** | Payload sizes | `ingress_payload_sizes` | Prometheus |
| **Ingress** | Kafka produce failures | `ingress_kafka_produce_failures` | Prometheus |
| **Kafka** | Consumer group lag | kafka consumer lag | Kafka metrics / `kafka-consumer-groups.sh` |
| **Kafka** | Topic throughput | messages/sec per topic | Kafka JMX or Strimzi metrics |
| **Listener** | Message processing time | Custom timer (to add) | Prometheus |
| **Listener** | Kafka connection errors | `KAFKA_CONNECTION_ERRORS_COUNTER` | Prometheus |
| **Celery** | Queue depth per queue | Valkey LLEN | Prometheus exporter or custom |
| **Celery** | Task duration per type | Task timing (to instrument) | Prometheus |
| **Celery** | Task failure rate | Celery events | Prometheus / Flower |
| **Workers** | CPU/memory per pod | cAdvisor | Prometheus + kube-state-metrics |
| **PostgreSQL** | Query latency (p95) | pg_stat_statements | postgres_exporter |
| **PostgreSQL** | Active connections | pg_stat_activity | postgres_exporter |
| **PostgreSQL** | Table sizes | pg_total_relation_size | postgres_exporter |
| **PostgreSQL** | WAL write rate | pg_stat_wal | postgres_exporter |
| **PostgreSQL** | Lock contention | pg_stat_activity (waiting) | postgres_exporter |
| **PostgreSQL** | I/O (reads/writes) | pg_stat_bgwriter, pg_statio | postgres_exporter |
| **Valkey** | Memory usage | INFO memory | Valkey exporter |
| **Valkey** | Evictions | evicted_keys | Valkey exporter |
| **Valkey** | Connected clients | connected_clients | Valkey exporter |
| **ROS** | CSV download time | Custom metric (to add) | Prometheus |
| **ROS** | Kruize API latency | Custom metric (to add) | Prometheus |
| **ROS** | Aggregation time | Custom metric (to add) | Prometheus |
| **Kruize** | Experiment creation time | Kruize metrics | Prometheus |
| **S3** | Upload/download latency | Custom metric | Prometheus |
| **S3** | Bucket size | S3 ListObjects | Script / Prometheus |
| **Node** | CPU, memory, disk I/O, network | node_exporter | Prometheus |

### Testing Phases

| Phase | Purpose | Duration | Load Pattern |
|-------|---------|----------|-------------|
| **Baseline** | Measure single-cluster processing at each scale (S1-S11) | 1 day per scenario | Single upload, wait for completion |
| **Sustained Load** | Measure multi-cluster steady state (M1-M6) | 3 days per scenario | Uploads every 6 hours per cluster |
| **Stress** | Find breaking point | 1 day | Linearly increasing cluster count until failure |
| **Soak** | Validate stability over time with data accumulation | 7 days | M3 scenario at sustained rate |
| **Spike** | Test backlog recovery | 1 day | Burst 24h backlog, then resume normal |
| **Configuration Tuning** | Test tuning knobs | 2 days per knob | Re-run baseline with tuned parameters |

### Observability Stack Requirements

- **Prometheus** with 15-second scrape interval, 30-day retention
- **Grafana** with pre-built dashboards per component
- **postgres_exporter** for PostgreSQL metrics
- **Valkey exporter** for cache metrics
- **kube-state-metrics** for pod/container resource metrics
- **node_exporter** on all cluster nodes

### Dashboard Design

1. **Overview Dashboard**: End-to-end pipeline latency, error rates, queue depths
2. **Ingress Dashboard**: Upload latency, payload sizes, S3 staging time
3. **Processing Dashboard**: Celery queue depths, task durations, worker utilization
4. **Database Dashboard**: Query latency, connections, table sizes, I/O
5. **ROS Dashboard**: Kruize latency, aggregation time, recommendation throughput
6. **Infrastructure Dashboard**: Node CPU/memory/disk, pod resource utilization

---

## 6. Identified Bottlenecks & Hypotheses

### H1: PostgreSQL is the Primary Bottleneck

**Evidence**: 500m CPU limit and 512Mi memory limit for a database serving analytical queries across 9 summary tables. DELETE/INSERT pattern in summary updates will cause heavy WAL writes and table bloat.

**Prediction**: At S7+ (>288K rows/day), summary table updates will exceed available CPU/memory, causing:
- Query latency degradation > 30s
- Lock contention blocking API reads during summary writes
- Autovacuum falling behind, causing table bloat

**Test**: Compare summary update duration at S4 vs S7 vs S9. Monitor pg_stat_activity for lock waits.

### H2: Celery Queue Saturation at Multi-Cluster Scale

**Evidence**: 25 total task slots (5 workers x 5 concurrency). Each cluster upload generates multiple chained tasks (download -> process -> summarize -> cost model). With PROCESSING_WAIT_TIMER=3 days, tasks may pile up.

**Prediction**: At M3+ (10 clusters), Celery queue depth will grow faster than drain rate, causing:
- Increasing end-to-end latency
- Valkey memory pressure from task metadata
- Task result expiry (28800s) dropping results before consumers read them

**Test**: Monitor Valkey LLEN for each queue during M3 sustained load. Track task completion rate vs submission rate.

### H3: ROS Processor Memory Cliff

**Evidence**: `csv.ReadAll()` loads entire CSV into memory. gota DataFrame duplicates data for aggregation. Single-threaded, no streaming.

**Prediction**: At S8+ (~60MB compressed upload -> ~600MB uncompressed CSV), ROS processor will OOM with 1Gi limit. Even at S7, memory usage will spike during aggregation.

**Test**: Monitor ROS processor memory during S5-S8 uploads. Identify the CSV size threshold for OOM.

### H4: Kruize Becomes I/O Bottleneck for ROS

**Evidence**: Synchronous HTTP calls to Kruize for each workload: createExperiment + updateResults (chunked) + updateRecommendations. No parallelism.

**Prediction**: At S5+ (500 workloads), Kruize round-trips will dominate ROS processing time. With 120s kruizeWaitTime and sequential processing, 500 workloads x ~2 API calls x ~1s/call = ~17 minutes minimum.

**Test**: Measure Kruize API latency distribution. Calculate theoretical minimum processing time vs actual.

### H5: Koku Listener Single-Consumer Throughput Limit

**Evidence**: Single Kafka consumer, sequential message processing (extract tar.gz -> validate -> split -> upload to S3).

**Prediction**: Listener throughput caps at ~1-2 uploads/minute for medium payloads. Multi-cluster scenarios (M3+) with simultaneous uploads will build Kafka consumer lag.

**Test**: Submit 10 uploads simultaneously, measure consumer lag growth and drain rate.

### H6: Valkey Memory Exhaustion Under Load

**Evidence**: 512MB cap with LRU eviction. Dual role: Celery broker (task queue + results) + application cache. No separate instances.

**Prediction**: At M4+ load, Celery task results + queue metadata + application cache will exceed 512MB, causing LRU eviction of active task results, leading to chord callback failures and stuck task chains.

**Test**: Monitor Valkey eviction rate and memory usage during M3-M5 sustained load. Track Celery chord failures.

### H7: S3/ODF Throughput at Large Scale

**Evidence**: All components interact with S3: ingress uploads, listener downloads/re-uploads, workers download for processing. Single ODF endpoint.

**Prediction**: At M5+ load, concurrent S3 operations may saturate ODF/NooBaa throughput, adding latency across all pipeline stages.

**Test**: Measure S3 operation latency under concurrent load. Compare with baseline single-operation latency.

---

## 7. System Limits & Scaling Model

### Theoretical Limits (Default Configuration)

| Constraint | Limit | Limiting Factor |
|-----------|-------|----------------|
| **Max upload throughput** | ~100 uploads/hour | Listener single-consumer processing rate |
| **Max daily CSV rows processable** | ~500K-1M | Worker task throughput (5 workers x 5 concurrency, ~1h/batch) |
| **Max concurrent API requests** | 8 | Gunicorn 2 workers x 4 threads |
| **Max DB connections** | ~60 | Koku Django (default CONN_MAX_AGE) + ROS (30) + Kruize (5) |
| **Max Celery tasks in flight** | 25 | 5 active queues x 5 concurrency |
| **Valkey effective capacity** | ~400MB usable | 512MB minus system overhead |
| **PostgreSQL effective capacity** | ~50GB | 30Gi PVC, ~60% usable after WAL/indexes/bloat |

### Scaling Characteristics

| Component | Scaling Type | Scaling Method |
|-----------|-------------|---------------|
| Ingress | Linear | Add replicas (stateless) |
| Listener | Step function | Add replicas with Kafka partition scaling |
| Celery Workers | Linear per queue | Add replicas per queue |
| PostgreSQL | Sublinear with cliffs | Vertical scaling (CPU/memory), then read replicas |
| Valkey | Linear up to memory cap | Vertical (memory increase) |
| ROS Processor | Step function | Add replicas with Kafka partition scaling |
| Kruize | Sublinear | Vertical scaling, limited by sequential processing model |

### Estimated Max Cluster Support (Default Config)

| Cluster Profile | Max Clusters | Bottleneck |
|----------------|-------------|-----------|
| S1 (SNO Low) | ~50 | PostgreSQL summary throughput |
| S4 (Small Medium) | ~10-15 | Celery queue saturation + PostgreSQL |
| S7 (Medium Medium) | ~3-5 | PostgreSQL + ROS processor + listener |
| S9 (Large Low) | ~2-3 | PostgreSQL + worker throughput |
| S10 (Large Medium) | 1 | Single cluster may exceed daily processing window |

---

## 8. Hardware Sizing Recommendations

### Tier Definitions

| Tier | Target Scenario | Clusters | Daily Rows |
|------|----------------|----------|-----------|
| **Small** | M1-M2: Edge/Small Enterprise | 1-5 small clusters | < 200K |
| **Medium** | M3: Medium Enterprise | 5-10 medium clusters | 200K-1M |
| **Large** | M4-M5: Large Enterprise | 10-20 medium clusters | 1M-6M |
| **XL** | M6+: Hyperscale | 20-50 clusters | > 6M |

### Resource Recommendations (to be validated by testing)

#### Small Tier

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit | Replicas | Storage |
|-----------|-----------|----------|---------------|-------------|----------|---------|
| Koku API | 250m | 1 | 1Gi | 2Gi | 1 | - |
| MASU | 250m | 500m | 1Gi | 2Gi | 1 | - |
| Listener | 200m | 500m | 512Mi | 1Gi | 1 | - |
| Celery Workers (each) | 250m | 500m | 512Mi | 1Gi | 1 | - |
| PostgreSQL | 500m | 2 | 1Gi | 4Gi | 1 | 50Gi |
| Valkey | 100m | 500m | 512Mi | 1Gi | 1 | 5Gi |
| ROS Processor | 500m | 1 | 1Gi | 2Gi | 1 | - |
| Kruize | 500m | 1 | 1Gi | 2Gi | 1 | - |
| **Total** | ~4 cores | ~10 cores | ~10Gi | ~22Gi | - | 55Gi |

#### Medium Tier

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit | Replicas | Storage |
|-----------|-----------|----------|---------------|-------------|----------|---------|
| Koku API | 500m | 2 | 2Gi | 4Gi | 2 | - |
| MASU | 500m | 1 | 2Gi | 4Gi | 1 | - |
| Listener | 500m | 1 | 1Gi | 2Gi | 1 | - |
| Celery Workers (each) | 500m | 1 | 1Gi | 2Gi | 1 | - |
| PostgreSQL | 2 | 4 | 4Gi | 8Gi | 1 | 100Gi |
| Valkey | 250m | 1 | 1Gi | 2Gi | 1 | 10Gi |
| ROS Processor | 1 | 2 | 2Gi | 4Gi | 1 | - |
| Kruize | 1 | 2 | 2Gi | 4Gi | 1 | - |
| **Total** | ~10 cores | ~22 cores | ~22Gi | ~46Gi | - | 110Gi |

#### Large Tier

| Component | CPU Request | CPU Limit | Memory Request | Memory Limit | Replicas | Storage |
|-----------|-----------|----------|---------------|-------------|----------|---------|
| Koku API | 1 | 2 | 2Gi | 4Gi | 2 | - |
| MASU | 1 | 2 | 2Gi | 4Gi | 1 | - |
| Listener | 1 | 2 | 2Gi | 4Gi | 2 | - |
| Celery Workers (each) | 1 | 2 | 2Gi | 4Gi | 2 | - |
| PostgreSQL | 4 | 8 | 8Gi | 16Gi | 1 | 250Gi |
| Valkey | 500m | 2 | 2Gi | 4Gi | 1 | 20Gi |
| ROS Processor | 2 | 4 | 4Gi | 8Gi | 2 | - |
| Kruize | 2 | 4 | 4Gi | 8Gi | 1 | - |
| **Total** | ~22 cores | ~48 cores | ~46Gi | ~92Gi | - | 270Gi |

#### PostgreSQL Tuning (Across Tiers)

| Parameter | Small | Medium | Large | XL |
|-----------|-------|--------|-------|-----|
| shared_buffers | 1GB | 2GB | 4GB | 8GB |
| work_mem | 64MB | 128MB | 256MB | 512MB |
| maintenance_work_mem | 256MB | 512MB | 1GB | 2GB |
| effective_cache_size | 3GB | 6GB | 12GB | 24GB |
| max_connections | 100 | 200 | 300 | 500 |
| max_parallel_workers | 2 | 4 | 8 | 16 |
| autovacuum_max_workers | 3 | 5 | 8 | 10 |

---

## 9. Risks and Open Questions

### Risks

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|-----------|
| **R1**: PostgreSQL 30Gi PVC shared across 3 databases becomes exhausted at Large tier with 3-month retention | Data loss / processing halt | High at L/XL tiers | Validate storage projections early; consider separate PVCs per database |
| **R2**: NISE cannot generate data at S10+ scale in reasonable time | Blocks large scenario testing | Medium | Profile NISE generation time; may need custom CSV generators |
| **R3**: ODF/NooBaa S3 performance varies significantly between environments | Non-reproducible results | Medium | Document S3 backend and measure S3 baseline independently |
| **R4**: Celery task chaining breaks under load (chord callback failures from Valkey eviction) | Silent data processing failures | High at M3+ | Test Valkey sizing early in Phase 2 |
| **R5**: Single PostgreSQL instance cannot be vertically scaled enough for L/XL tiers | Architecture limit | Medium | May require read replica or connection pooler (pgbouncer) |
| **R6**: Kafka partition count limits Listener horizontal scaling | Cannot add listener replicas effectively | Low | Validate Kafka topic partition count matches desired consumer count |

### Assumptions

- **A1**: Test cluster has sufficient nodes to provide requested resources without contention
- **A2**: Kafka is deployed separately and has adequate capacity (not a bottleneck in itself)
- **A3**: ODF/S3 storage performance is representative of customer deployments
- **A4**: NISE-generated data is representative of real OCP cost data in structure and volume
- **A5**: 6-hour upload frequency is the standard customer pattern
- **A6**: Data retention of 3 months (RETAIN_NUM_MONTHS=3) is the target configuration
- **A7**: Only OCP provider type is relevant (not AWS/Azure/GCP cloud providers)
- **A8**: The system does not need to handle concurrent API reads during heavy processing (no SLA on API latency during ingestion)

### Open Questions

- **Q1**: What is the target processing SLA? Must daily data be processed within 6 hours? 12 hours? 24 hours?
- **Q2**: Is there a maximum acceptable end-to-end latency from upload to data visible in UI?
- **Q3**: Should the sizing guide target a specific OpenShift version or hardware profile?
- **Q4**: Are there existing customer deployments we can profile for realistic workload patterns?
- **Q5**: Should we test with PostgreSQL connection pooling (pgbouncer) as a potential optimization?
- **Q6**: Is Kafka partition scaling within scope, or is the current single-partition setup a given?
- **Q7**: What is the expected ROS adoption rate? Should all scenarios assume 100% of pods have ROS data?
- **Q8**: Should the sizing guide include Kafka and ODF resource requirements, or just the Helm chart components?
- **Q9**: Are there plans to support PostgreSQL read replicas (USE_READREPLICA=True) in on-premise deployments?
- **Q10**: What monitoring stack will customers have? Should the sizing guide include Prometheus/Grafana overhead?
