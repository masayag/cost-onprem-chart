"""
Shared E2E test helpers and utilities.

This module centralizes common E2E test functionality to avoid duplication across:
- tests/suites/e2e/test_complete_flow.py
- tests/suites/cost_management/conftest.py
- Any other test modules that need E2E setup

Key components:
- NISE data generation
- Source registration in Sources API
- Data upload to ingress
- Processing wait utilities
- Cleanup utilities
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import requests

from utils import (
    create_upload_package_from_files,
    execute_db_query,
    exec_in_pod,
    get_pod_by_label,
    wait_for_condition,
)


# =============================================================================
# Constants and Configuration
# =============================================================================

# Cluster ID prefix for E2E tests (used for cleanup and identification)
E2E_CLUSTER_PREFIX = "e2e-pytest-"

# Default expected values for NISE-generated test data
DEFAULT_NISE_CONFIG = {
    "node_name": "test-node-1",
    "namespace": "test-namespace",
    "pod_name": "test-pod-1",
    "resource_id": "test-resource-1",
    "cpu_cores": 2,
    "memory_gig": 8,
    "cpu_request": 0.5,
    "mem_request_gig": 1,
    "cpu_limit": 1,
    "mem_limit_gig": 2,
    "pod_seconds": 3600,
    "cpu_usage": 0.25,
    "mem_usage_gig": 0.5,
    "labels": "environment:test|app:e2e-test",
}

# S3 bucket name
DEFAULT_S3_BUCKET = "koku-bucket"

# Upload content type
UPLOAD_CONTENT_TYPE = "application/vnd.redhat.hccm.filename+tgz"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class NISEConfig:
    """Configuration for NISE data generation."""
    node_name: str = DEFAULT_NISE_CONFIG["node_name"]
    namespace: str = DEFAULT_NISE_CONFIG["namespace"]
    pod_name: str = DEFAULT_NISE_CONFIG["pod_name"]
    resource_id: str = DEFAULT_NISE_CONFIG["resource_id"]
    cpu_cores: int = DEFAULT_NISE_CONFIG["cpu_cores"]
    memory_gig: int = DEFAULT_NISE_CONFIG["memory_gig"]
    cpu_request: float = DEFAULT_NISE_CONFIG["cpu_request"]
    mem_request_gig: float = DEFAULT_NISE_CONFIG["mem_request_gig"]
    cpu_limit: float = DEFAULT_NISE_CONFIG["cpu_limit"]
    mem_limit_gig: float = DEFAULT_NISE_CONFIG["mem_limit_gig"]
    pod_seconds: int = DEFAULT_NISE_CONFIG["pod_seconds"]
    cpu_usage: float = DEFAULT_NISE_CONFIG["cpu_usage"]
    mem_usage_gig: float = DEFAULT_NISE_CONFIG["mem_usage_gig"]
    labels: str = DEFAULT_NISE_CONFIG["labels"]

    @classmethod
    def for_cluster(cls, index: int) -> "NISEConfig":
        """Generate a unique NISEConfig for a specific cluster index.

        Each cluster gets unique node names, namespaces, pod names, and resource IDs
        to ensure data is distinguishable across clusters.

        Args:
            index: Zero-based cluster index (0, 1, 2, ...)

        Returns:
            NISEConfig with unique values for this cluster
        """
        # Vary resource values slightly per cluster for realistic differentiation
        cpu_request_base = 0.5
        mem_request_base = 1.0

        return cls(
            node_name=f"cluster-{index}-node-1",
            namespace=f"cluster-{index}-namespace",
            pod_name=f"cluster-{index}-pod-1",
            resource_id=f"cluster-{index}-resource-1",
            cpu_cores=2 + (index % 3),  # 2, 3, 4, 2, 3, 4...
            memory_gig=8 + (index * 2),  # 8, 10, 12, 14...
            cpu_request=cpu_request_base + (index * 0.1),  # 0.5, 0.6, 0.7...
            mem_request_gig=mem_request_base + (index * 0.25),  # 1.0, 1.25, 1.5...
            cpu_limit=1.0 + (index * 0.2),  # 1.0, 1.2, 1.4...
            mem_limit_gig=2.0 + (index * 0.5),  # 2.0, 2.5, 3.0...
            pod_seconds=3600,
            cpu_usage=0.25 + (index * 0.05),  # 0.25, 0.30, 0.35...
            mem_usage_gig=0.5 + (index * 0.1),  # 0.5, 0.6, 0.7...
            labels=f"environment:test|app:e2e-cluster-{index}|cluster-index:{index}",
        )
    
    def get_expected_values(self, hours: int = 24) -> Dict:
        """Calculate expected values for validation tests.

        Includes both cost management and ROS expected values.
        """
        return {
            # Resource identifiers
            "node_name": self.node_name,
            "namespace": self.namespace,
            "pod_name": self.pod_name,
            "resource_id": self.resource_id,
            # Cost management expected values
            "cpu_request": self.cpu_request,
            "mem_request_gig": self.mem_request_gig,
            "hours": hours,
            "expected_cpu_hours": self.cpu_request * hours,
            "expected_memory_gb_hours": self.mem_request_gig * hours,
            "expected_node_count": 1,
            "expected_namespace_count": 1,
            "expected_pod_count": 1,
            # ROS expected values
            "cpu_cores": self.cpu_cores,
            "memory_gig": self.memory_gig,
            "cpu_limit": self.cpu_limit,
            "mem_limit_gig": self.mem_limit_gig,
            "cpu_usage": self.cpu_usage,
            "mem_usage_gig": self.mem_usage_gig,
            "labels": self.labels,
            # ROS expects at least 1 experiment per cluster
            "expected_experiment_count": 1,
        }
    
    def to_yaml(self, cluster_id: str, start_date: datetime, end_date: datetime) -> str:
        """Generate NISE static report YAML."""
        return f"""---
generators:
  - OCPGenerator:
      start_date: {start_date.strftime('%Y-%m-%d')}
      end_date: {end_date.strftime('%Y-%m-%d')}
      nodes:
        - node:
          node_name: {self.node_name}
          cpu_cores: {self.cpu_cores}
          memory_gig: {self.memory_gig}
          resource_id: {self.resource_id}
          labels: node-role.kubernetes.io/worker:true|kubernetes.io/os:linux
          namespaces:
            {self.namespace}:
              labels: openshift.io/cluster-monitoring:true
              pods:
                - pod:
                  pod_name: {self.pod_name}
                  cpu_request: {self.cpu_request}
                  mem_request_gig: {self.mem_request_gig}
                  cpu_limit: {self.cpu_limit}
                  mem_limit_gig: {self.mem_limit_gig}
                  pod_seconds: {self.pod_seconds}
                  cpu_usage:
                    full_period: {self.cpu_usage}
                  mem_usage_gig:
                    full_period: {self.mem_usage_gig}
                  labels: {self.labels}
"""


@dataclass
class SourceRegistration:
    """Result of source registration."""
    source_id: str
    source_name: str
    cluster_id: str
    org_id: str


@dataclass
class ClusterTestContext:
    """Context for a single cluster in multi-cluster tests.

    Contains all information needed to validate data for one cluster.
    """
    cluster_id: str
    cluster_index: int
    source_id: str
    source_name: str
    schema_name: Optional[str]
    nise_config: "NISEConfig"
    expected: Dict
    start_date: datetime
    end_date: datetime

    @property
    def is_ready(self) -> bool:
        """Check if cluster data is fully processed."""
        return self.schema_name is not None


@dataclass
class MultiClusterResult:
    """Result of multi-cluster data generation.

    Contains contexts for all clusters and shared metadata.
    """
    clusters: List[ClusterTestContext]
    namespace: str
    db_pod: str
    org_id: str
    total_clusters: int
    successful_clusters: int
    failed_clusters: List[str]

    @property
    def all_successful(self) -> bool:
        """Check if all clusters were successfully processed."""
        return self.successful_clusters == self.total_clusters

    def get_cluster(self, index: int) -> Optional[ClusterTestContext]:
        """Get cluster context by index."""
        for cluster in self.clusters:
            if cluster.cluster_index == index:
                return cluster
        return None


# =============================================================================
# NISE Utilities
# =============================================================================

def is_nise_available() -> bool:
    """Check if NISE is available for data generation."""
    try:
        result = subprocess.run(
            ["nise", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def install_nise() -> bool:
    """Attempt to install NISE via pip."""
    try:
        print("  Installing koku-nise...")
        result = subprocess.run(
            ["pip", "install", "koku-nise"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_nise_available() -> bool:
    """Ensure NISE is available, installing if necessary."""
    if is_nise_available():
        return True
    return install_nise()


def generate_nise_data(
    cluster_id: str,
    start_date: datetime,
    end_date: datetime,
    output_dir: str,
    config: Optional[NISEConfig] = None,
    include_ros: bool = True,
) -> Dict[str, List[str]]:
    """Generate NISE OCP data and return categorized file paths.
    
    Args:
        cluster_id: Cluster ID for the generated data
        start_date: Start date for the report period
        end_date: End date for the report period
        output_dir: Directory to write output files
        config: NISE configuration (uses defaults if not provided)
        include_ros: Whether to include ROS data (--ros-ocp-info flag)
    
    Returns:
        Dict with keys: pod_usage_files, ros_usage_files, node_label_files, namespace_label_files
    """
    if config is None:
        config = NISEConfig()
    
    yaml_content = config.to_yaml(cluster_id, start_date, end_date)
    yaml_path = os.path.join(output_dir, "static_report.yml")
    with open(yaml_path, "w") as f:
        f.write(yaml_content)
    
    nise_output = os.path.join(output_dir, "nise_output")
    os.makedirs(nise_output, exist_ok=True)
    
    # Build command
    cmd = [
        "nise", "report", "ocp",
        "--static-report-file", yaml_path,
        "--ocp-cluster-id", cluster_id,
        "-w",  # Write monthly files
    ]
    if include_ros:
        cmd.append("--ros-ocp-info")
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=nise_output,
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"NISE failed: {result.stderr}")
    
    # Categorize generated files
    files = {
        "pod_usage_files": [],
        "ros_usage_files": [],
        "node_label_files": [],
        "namespace_label_files": [],
        "all_files": [],
    }
    
    for root, _, filenames in os.walk(nise_output):
        for f in filenames:
            if f.endswith(".csv"):
                full_path = os.path.join(root, f)
                files["all_files"].append(full_path)
                
                if "pod_usage" in f:
                    files["pod_usage_files"].append(full_path)
                elif "ros_usage" in f:
                    files["ros_usage_files"].append(full_path)
                elif "node_label" in f:
                    files["node_label_files"].append(full_path)
                elif "namespace_label" in f:
                    files["namespace_label_files"].append(full_path)
    
    # Fall back: if no ros_usage files, use pod_usage
    if not files["ros_usage_files"]:
        files["ros_usage_files"] = files["pod_usage_files"]
    
    return files


# =============================================================================
# Cluster ID Generation
# =============================================================================

def generate_cluster_id(prefix: str = "") -> str:
    """Generate a unique cluster ID for E2E tests.
    
    Args:
        prefix: Optional prefix to add after the standard e2e-pytest- prefix
    
    Returns:
        Unique cluster ID like "e2e-pytest-cost-val-abc12345"
    """
    timestamp = int(time.time())
    unique = uuid.uuid4().hex[:8]
    
    if prefix:
        return f"{E2E_CLUSTER_PREFIX}{prefix}-{unique}"
    return f"{E2E_CLUSTER_PREFIX}{timestamp}-{unique}"


# =============================================================================
# Koku API Utilities
# =============================================================================

def get_koku_api_reads_url(helm_release_name: str, namespace: str) -> str:
    """Get the internal Koku API reads URL for GET operations."""
    return (
        f"http://{helm_release_name}-koku-api-reads."
        f"{namespace}.svc.cluster.local:8000/api/cost-management/v1"
    )


def get_koku_api_writes_url(helm_release_name: str, namespace: str) -> str:
    """Get the internal Koku API writes URL for POST/PUT/DELETE operations."""
    return (
        f"http://{helm_release_name}-koku-api-writes."
        f"{namespace}.svc.cluster.local:8000/api/cost-management/v1"
    )


def get_source_type_id(
    namespace: str,
    pod: str,
    api_url: str,
    rh_identity_header: str,
    source_type_name: str = "openshift",
    container: str = "ingress",
) -> Optional[str]:
    """Get the source type ID for a given source type name.
    
    Args:
        namespace: Kubernetes namespace
        pod: Pod name for executing curl commands (typically ingress pod)
        api_url: Koku API URL (reads or writes)
        rh_identity_header: Base64-encoded X-Rh-Identity header value
        source_type_name: Name of the source type (default: "openshift")
        container: Container name in the pod (default: "ingress")
    
    Returns:
        Source type ID as string, or None if not found
    """
    result = exec_in_pod(
        namespace,
        pod,
        [
            "curl", "-s",
            f"{api_url}/source_types",
            "-H", "Content-Type: application/json",
            "-H", f"X-Rh-Identity: {rh_identity_header}",
        ],
        container=container,
    )
    
    if not result:
        return None
    
    try:
        data = json.loads(result)
        for st in data.get("data", []):
            if st.get("name") == source_type_name:
                return st.get("id")
    except json.JSONDecodeError:
        pass
    
    return None


def get_application_type_id(
    namespace: str,
    pod: str,
    api_url: str,
    rh_identity_header: str,
    app_type_name: str = "/insights/platform/cost-management",
    container: str = "ingress",
) -> Optional[str]:
    """Get the application type ID for cost management.
    
    Args:
        namespace: Kubernetes namespace
        pod: Pod name for executing curl commands (typically ingress pod)
        api_url: Koku API URL (reads or writes)
        rh_identity_header: Base64-encoded X-Rh-Identity header value
        app_type_name: Name of the application type
        container: Container name in the pod (default: "ingress")
    
    Returns:
        Application type ID as string, or None if not found
    """
    result = exec_in_pod(
        namespace,
        pod,
        [
            "curl", "-s",
            f"{api_url}/application_types",
            "-H", "Content-Type: application/json",
            "-H", f"X-Rh-Identity: {rh_identity_header}",
        ],
        container=container,
    )
    
    if not result:
        return None
    
    try:
        data = json.loads(result)
        for at in data.get("data", []):
            if at.get("name") == app_type_name:
                return at.get("id")
    except json.JSONDecodeError:
        pass
    
    return None


def register_source(
    namespace: str,
    pod: str,
    api_reads_url: str,
    api_writes_url: str,
    rh_identity_header: str,
    cluster_id: str,
    org_id: str,
    source_name: Optional[str] = None,
    bucket: str = DEFAULT_S3_BUCKET,
    container: str = "ingress",
    max_retries: int = 5,
    initial_retry_delay: int = 5,
) -> SourceRegistration:
    """Register a source in Koku Sources API.
    
    This creates:
    1. A source with source_ref set to cluster_id (critical for matching incoming data)
    2. An application linked to cost-management with cluster_id in extra
    
    Note: On first run for a new org, tenant schema creation can be slow,
    so this function uses retry logic with exponential backoff.
    
    Args:
        namespace: Kubernetes namespace
        pod: Pod name for executing curl commands (typically ingress pod)
        api_reads_url: Koku API reads URL
        api_writes_url: Koku API writes URL
        rh_identity_header: Base64-encoded X-Rh-Identity header value
        cluster_id: Cluster ID for the source
        org_id: Organization ID
        source_name: Optional custom source name (defaults to e2e-source-{cluster_id[:8]})
        bucket: S3 bucket name
        container: Container name in the pod (default: "ingress")
        max_retries: Maximum number of retry attempts (default: 5)
        initial_retry_delay: Initial delay between retries in seconds (default: 5)
    
    Returns:
        SourceRegistration with source details
    """
    # Get type IDs using reads endpoint
    source_type_id = get_source_type_id(
        namespace, pod, api_reads_url, rh_identity_header, container=container
    )
    if not source_type_id:
        raise RuntimeError("Could not get OpenShift source type ID")
    
    app_type_id = get_application_type_id(
        namespace, pod, api_reads_url, rh_identity_header, container=container
    )
    
    # Generate source name
    if not source_name:
        source_name = f"e2e-source-{cluster_id[:8]}"
    
    # Create source with source_ref (critical for matching incoming data)
    source_payload = json.dumps({
        "name": source_name,
        "source_type_id": source_type_id,
        "source_ref": cluster_id,
    })
    
    # Retry logic for source creation
    # First request may fail due to tenant schema creation (slow operation)
    retry_delay = initial_retry_delay
    source_id = None
    last_error = None
    
    for attempt in range(max_retries):
        if attempt > 0:
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)  # Exponential backoff, max 30s
        
        result = exec_in_pod(
            namespace,
            pod,
            [
                "curl", "-s", "-w", "\n__HTTP_CODE__:%{http_code}", "-X", "POST",
                f"{api_writes_url}/sources",
                "-H", "Content-Type: application/json",
                "-H", f"X-Rh-Identity: {rh_identity_header}",
                "-d", source_payload,
            ],
            container=container,
            timeout=120,  # Longer timeout for first request (schema creation)
        )
        
        if not result:
            last_error = "exec_in_pod returned None (curl failed or timed out)"
            continue
        
        # Parse response and status code
        http_code = None
        if "__HTTP_CODE__:" in result:
            body, http_code = result.rsplit("__HTTP_CODE__:", 1)
            result = body.strip()
            http_code = http_code.strip()
        
        if http_code and http_code not in ("200", "201"):
            last_error = f"HTTP {http_code}: {result[:200]}"
            # 5xx errors might be transient, retry
            if http_code.startswith("5"):
                continue
            # 4xx errors are not retryable - break and fail
            break
        
        try:
            source_data = json.loads(result)
            source_id = source_data.get("id")
            if source_id:
                break
            else:
                last_error = f"No 'id' in response: {result[:200]}"
        except json.JSONDecodeError as e:
            last_error = f"Invalid JSON: {result[:200]} - {e}"
    
    if not source_id:
        raise RuntimeError(
            f"Source creation failed after {max_retries} attempts. "
            f"Last error: {last_error}. "
            f"pod={pod}, url={api_writes_url}/sources"
        )
    
    # Create application with cluster_id in extra
    if app_type_id:
        app_payload = json.dumps({
            "source_id": source_id,
            "application_type_id": app_type_id,
            "extra": {"bucket": bucket, "cluster_id": cluster_id},
        })
        
        exec_in_pod(
            namespace,
            pod,
            [
                "curl", "-s", "-X", "POST",
                f"{api_writes_url}/applications",
                "-H", "Content-Type: application/json",
                "-H", f"X-Rh-Identity: {rh_identity_header}",
                "-d", app_payload,
            ],
            container=container,
        )
    
    return SourceRegistration(
        source_id=source_id,
        source_name=source_name,
        cluster_id=cluster_id,
        org_id=org_id,
    )


def delete_source(
    namespace: str,
    pod: str,
    api_writes_url: str,
    rh_identity_header: str,
    source_id: str,
    container: str = "ingress",
) -> bool:
    """Delete a source from Koku Sources API.
    
    Args:
        namespace: Kubernetes namespace
        pod: Pod name for executing curl commands (typically ingress pod)
        api_writes_url: Koku API writes URL
        rh_identity_header: Base64-encoded X-Rh-Identity header value
        source_id: ID of the source to delete
        container: Container name in the pod (default: "ingress")
    
    Returns:
        True if successful, False otherwise
    """
    try:
        exec_in_pod(
            namespace,
            pod,
            [
                "curl", "-s", "-X", "DELETE",
                f"{api_writes_url}/sources/{source_id}",
                "-H", f"X-Rh-Identity: {rh_identity_header}",
            ],
            container=container,
        )
        return True
    except Exception:
        return False


# =============================================================================
# Upload Utilities
# =============================================================================

def upload_with_retry(
    session: requests.Session,
    url: str,
    package_path: str,
    auth_header: Dict[str, str],
    max_retries: int = 3,
    retry_delay: int = 5,
) -> requests.Response:
    """Upload file with retry logic for transient errors.
    
    Args:
        session: Requests session (should have verify=False for self-signed certs)
        url: Upload URL
        package_path: Path to the tar.gz package
        auth_header: Authorization header dict
        max_retries: Maximum number of retry attempts
        retry_delay: Base delay between retries (exponential backoff)
    
    Returns:
        Response object
    
    Raises:
        RuntimeError: If all retries fail
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            with open(package_path, "rb") as f:
                response = session.post(
                    url,
                    files={"file": ("cost-mgmt.tar.gz", f, UPLOAD_CONTENT_TYPE)},
                    headers=auth_header,
                    timeout=60,
                )
            
            if response.status_code in [200, 201, 202]:
                return response
            
            # Retry on 5xx errors
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                print(f"       Attempt {attempt + 1}/{max_retries} failed: {last_error}, retrying...")
                time.sleep(retry_delay * (attempt + 1))
                continue
            
            # Don't retry on 4xx errors
            return response
            
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            print(f"       Attempt {attempt + 1}/{max_retries} failed: {last_error}, retrying...")
            time.sleep(retry_delay * (attempt + 1))
    
    raise RuntimeError(f"Upload failed after {max_retries} attempts: {last_error}")


# =============================================================================
# Processing Wait Utilities
# =============================================================================

def wait_for_provider(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    timeout: int = 300,
    interval: int = 10,
) -> bool:
    """Wait for provider to be created in Koku database.
    
    Note: Timeout increased to 300s for CI environments where Kafka → Koku
    provider creation can be slower due to resource constraints.
    
    Returns True if provider was created, False on timeout.
    """
    def check_provider():
        result = execute_db_query(
            namespace, db_pod, "koku", "koku",
            f"""
            SELECT p.uuid FROM api_provider p
            JOIN api_providerauthentication pa ON p.authentication_id = pa.id
            WHERE pa.credentials->>'cluster_id' = '{cluster_id}'
               OR p.additional_context->>'cluster_id' = '{cluster_id}'
            """
        )
        return result and result[0][0]
    
    return wait_for_condition(check_provider, timeout=timeout, interval=interval)


def wait_for_summary_tables(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    timeout: int = 600,
    interval: int = 30,
) -> Optional[str]:
    """Wait for summary tables to be populated and return schema name.
    
    Returns schema name if successful, None on timeout.
    """
    found_schema = {"name": None}
    
    def check_summary():
        result = execute_db_query(
            namespace, db_pod, "koku", "koku",
            f"""
            SELECT c.schema_name FROM reporting_common_costusagereportmanifest m
            JOIN api_provider p ON m.provider_id = p.uuid
            JOIN api_customer c ON p.customer_id = c.id
            WHERE m.cluster_id = '{cluster_id}' LIMIT 1
            """
        )
        if not result or not result[0][0]:
            return False
        
        schema = result[0][0].strip()
        result = execute_db_query(
            namespace, db_pod, "koku", "koku",
            f"SELECT COUNT(*) FROM {schema}.reporting_ocpusagelineitem_daily_summary WHERE cluster_id = '{cluster_id}'"
        )
        
        if result and int(result[0][0]) > 0:
            found_schema["name"] = schema
            return True
        return False
    
    if wait_for_condition(check_summary, timeout=timeout, interval=interval):
        return found_schema["name"]
    return None


# =============================================================================
# Cleanup Utilities
# =============================================================================

def cleanup_database_records(
    namespace: str,
    db_pod: str,
    cluster_id: str,
) -> bool:
    """Clean up database records for a cluster."""
    try:
        # Delete file statuses first (foreign key constraint)
        execute_db_query(
            namespace, db_pod, "koku", "koku",
            f"""
            DELETE FROM reporting_common_costusagereportstatus
            WHERE manifest_id IN (
                SELECT id FROM reporting_common_costusagereportmanifest
                WHERE cluster_id = '{cluster_id}'
            )
            """
        )
        
        # Delete manifests
        execute_db_query(
            namespace, db_pod, "koku", "koku",
            f"DELETE FROM reporting_common_costusagereportmanifest WHERE cluster_id = '{cluster_id}'"
        )
        
        return True
    except Exception:
        return False


def cleanup_e2e_sources(
    namespace: str,
    listener_pod: str,
    sources_api_url: str,
    org_id: str,
    prefix: str = "e2e-source-",
) -> int:
    """Clean up E2E test sources matching a prefix.

    Returns number of sources deleted.
    """
    deleted = 0

    try:
        result = exec_in_pod(
            namespace,
            listener_pod,
            [
                "curl", "-s", f"{sources_api_url}/sources",
                "-H", "Content-Type: application/json",
                "-H", f"x-rh-sources-org-id: {org_id}",
            ],
            container="sources-listener",
        )

        if not result:
            return 0

        sources = json.loads(result)
        for source in sources.get("data", []):
            source_name = source.get("name", "")
            source_id = source.get("id")

            if source_id and source_name.startswith(prefix):
                if delete_source(namespace, listener_pod, sources_api_url, source_id, org_id):
                    deleted += 1
                    time.sleep(1)  # Brief pause between deletions
    except Exception:
        pass

    return deleted


# =============================================================================
# ROS Validation Utilities
# =============================================================================


def get_kruize_experiments_for_cluster(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    kruize_user: str,
    kruize_password: str,
) -> List[Dict]:
    """Get Kruize experiments for a specific cluster.

    Kruize stores cluster information in the experiment_name field, which
    includes the cluster_id as part of the experiment identifier.

    Args:
        namespace: Kubernetes namespace
        db_pod: Database pod name
        cluster_id: Cluster ID to filter by
        kruize_user: Kruize database user
        kruize_password: Kruize database password

    Returns:
        List of experiment dicts with id, experiment_name, cluster_name
    """
    # Kruize stores cluster_id in experiment_name field, not cluster_name
    # The experiment_name format includes the cluster_id
    query = f"""
        SELECT experiment_id, experiment_name, cluster_name
        FROM kruize_experiments
        WHERE experiment_name LIKE '%{cluster_id}%'
           OR cluster_name LIKE '%{cluster_id}%'
    """
    result = execute_db_query(
        namespace, db_pod, "kruize_db", kruize_user, query, password=kruize_password
    )

    if not result:
        return []

    return [
        {
            "experiment_id": row[0],
            "experiment_name": str(row[1]).strip() if row[1] else None,
            "cluster_name": str(row[2]).strip() if row[2] else None,
        }
        for row in result
    ]


def get_kruize_recommendations_for_cluster(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    kruize_user: str,
    kruize_password: str,
) -> List[Dict]:
    """Get Kruize recommendations for a specific cluster.

    Args:
        namespace: Kubernetes namespace
        db_pod: Database pod name
        cluster_id: Cluster ID to filter by
        kruize_user: Kruize database user
        kruize_password: Kruize database password

    Returns:
        List of recommendation dicts
    """
    # Match cluster_id in experiment_name or cluster_name
    query = f"""
        SELECT r.id, e.experiment_name, e.cluster_name
        FROM kruize_recommendations r
        JOIN kruize_experiments e ON r.experiment_id = e.experiment_id
        WHERE e.experiment_name LIKE '%{cluster_id}%'
           OR e.cluster_name LIKE '%{cluster_id}%'
    """
    result = execute_db_query(
        namespace, db_pod, "kruize_db", kruize_user, query, password=kruize_password
    )

    if not result:
        return []

    return [
        {
            "recommendation_id": row[0],
            "experiment_name": str(row[1]).strip() if row[1] else None,
            "cluster_name": str(row[2]).strip() if row[2] else None,
        }
        for row in result
    ]


def wait_for_kruize_experiments(
    namespace: str,
    db_pod: str,
    cluster_id: str,
    kruize_user: str,
    kruize_password: str,
    timeout: int = 240,
    interval: int = 20,
) -> bool:
    """Wait for Kruize experiments to be created for a cluster.

    Args:
        namespace: Kubernetes namespace
        db_pod: Database pod name
        cluster_id: Cluster ID to filter by
        kruize_user: Kruize database user
        kruize_password: Kruize database password
        timeout: Maximum wait time in seconds
        interval: Check interval in seconds

    Returns:
        True if experiments found, False on timeout
    """
    def check_experiments():
        experiments = get_kruize_experiments_for_cluster(
            namespace, db_pod, cluster_id, kruize_user, kruize_password
        )
        return len(experiments) > 0

    return wait_for_condition(check_experiments, timeout=timeout, interval=interval)


# =============================================================================
# Multi-Cluster Data Generation
# =============================================================================


def generate_multi_cluster_data(
    cluster_count: int,
    namespace: str,
    db_pod: str,
    ingress_pod: str,
    api_reads_url: str,
    api_writes_url: str,
    rh_identity_header: str,
    org_id: str,
    ingress_url: str,
    jwt_auth_header: Dict[str, str],
    cluster_prefix: str = "multi",
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> MultiClusterResult:
    """Generate and upload data for multiple clusters sequentially.

    This function:
    1. Generates unique NISE configs for each cluster
    2. Creates NISE data files for each cluster
    3. Registers sources for each cluster
    4. Uploads data packages for each cluster
    5. Waits for processing completion for each cluster

    Args:
        cluster_count: Number of clusters to generate
        namespace: Kubernetes namespace
        db_pod: Database pod name
        ingress_pod: Ingress pod name for API calls
        api_reads_url: Koku API reads URL
        api_writes_url: Koku API writes URL
        rh_identity_header: X-Rh-Identity header value
        org_id: Organization ID
        ingress_url: Ingress upload URL base
        jwt_auth_header: JWT authorization header dict
        cluster_prefix: Prefix for cluster IDs (default: "multi")
        start_date: Start date for data (default: 2 days ago)
        end_date: End date for data (default: 1 day ago)

    Returns:
        MultiClusterResult with all cluster contexts
    """
    import requests
    import tempfile
    import shutil

    # Default dates: 2 days ago to 1 day ago for 24 hours of data
    now = datetime.utcnow()
    if start_date is None:
        start_date = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end_date is None:
        end_date = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    clusters: List[ClusterTestContext] = []
    failed_clusters: List[str] = []
    successful_count = 0

    print(f"\n{'='*60}")
    print(f"MULTI-CLUSTER DATA GENERATION")
    print(f"{'='*60}")
    print(f"  Cluster count: {cluster_count}")
    print(f"  Cluster prefix: {cluster_prefix}")
    print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"{'='*60}\n")

    # Create session for uploads
    session = requests.Session()
    session.verify = False

    for i in range(cluster_count):
        cluster_id = generate_cluster_id(prefix=f"{cluster_prefix}-{i}")
        nise_config = NISEConfig.for_cluster(i)
        temp_dir = tempfile.mkdtemp(prefix=f"multi_cluster_{i}_")

        print(f"\n  [{i+1}/{cluster_count}] Processing cluster: {cluster_id}")
        print(f"       Node: {nise_config.node_name}")
        print(f"       Namespace: {nise_config.namespace}")
        print(f"       CPU request: {nise_config.cpu_request}")

        try:
            # Step 1: Generate NISE data
            print(f"       [1/4] Generating NISE data...")
            files = generate_nise_data(cluster_id, start_date, end_date, temp_dir, config=nise_config)

            if not files["all_files"]:
                print(f"       ERROR: NISE generated no files")
                failed_clusters.append(cluster_id)
                continue

            print(f"       Generated {len(files['all_files'])} files")

            # Step 2: Register source
            print(f"       [2/4] Registering source...")
            source_reg = register_source(
                namespace=namespace,
                pod=ingress_pod,
                api_reads_url=api_reads_url,
                api_writes_url=api_writes_url,
                rh_identity_header=rh_identity_header,
                cluster_id=cluster_id,
                org_id=org_id,
                source_name=f"multi-cluster-{i}-{cluster_id[:8]}",
                container="ingress",
            )
            print(f"       Source ID: {source_reg.source_id}")

            # Wait for provider
            print(f"       [3/4] Waiting for provider...")
            if not wait_for_provider(namespace, db_pod, cluster_id, timeout=180):
                print(f"       ERROR: Provider not created")
                failed_clusters.append(cluster_id)
                continue
            print(f"       Provider created")

            # Step 3: Upload data
            print(f"       [4/4] Uploading data...")
            package_path = create_upload_package_from_files(
                pod_usage_files=files["pod_usage_files"],
                ros_usage_files=files["ros_usage_files"],
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
                node_label_files=files.get("node_label_files"),
                namespace_label_files=files.get("namespace_label_files"),
            )

            upload_url = f"{ingress_url}/v1/upload"
            response = upload_with_retry(session, upload_url, package_path, jwt_auth_header)

            if response.status_code not in [200, 201, 202]:
                print(f"       ERROR: Upload failed with {response.status_code}")
                failed_clusters.append(cluster_id)
                continue

            print(f"       Upload successful: {response.status_code}")

            # Wait for summary tables
            schema_name = wait_for_summary_tables(namespace, db_pod, cluster_id, timeout=300)

            if not schema_name:
                print(f"       WARNING: Summary tables not populated (may still be processing)")

            # Calculate expected values
            actual_hours = 24  # Default assumption
            expected = nise_config.get_expected_values(hours=actual_hours)

            # Create cluster context
            context = ClusterTestContext(
                cluster_id=cluster_id,
                cluster_index=i,
                source_id=source_reg.source_id,
                source_name=source_reg.source_name,
                schema_name=schema_name,
                nise_config=nise_config,
                expected=expected,
                start_date=start_date,
                end_date=end_date,
            )
            clusters.append(context)
            successful_count += 1
            print(f"       SUCCESS: Cluster {i} ready")

        except Exception as e:
            print(f"       ERROR: {e}")
            failed_clusters.append(cluster_id)

        finally:
            # Clean up temp directory
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"\n{'='*60}")
    print(f"MULTI-CLUSTER GENERATION COMPLETE")
    print(f"{'='*60}")
    print(f"  Successful: {successful_count}/{cluster_count}")
    if failed_clusters:
        print(f"  Failed: {', '.join(failed_clusters)}")
    print(f"{'='*60}\n")

    return MultiClusterResult(
        clusters=clusters,
        namespace=namespace,
        db_pod=db_pod,
        org_id=org_id,
        total_clusters=cluster_count,
        successful_clusters=successful_count,
        failed_clusters=failed_clusters,
    )


def cleanup_multi_cluster_data(
    result: MultiClusterResult,
    ingress_pod: str,
    api_writes_url: str,
    rh_identity_header: str,
) -> None:
    """Clean up all data created by multi-cluster generation.

    Args:
        result: MultiClusterResult from generate_multi_cluster_data
        ingress_pod: Ingress pod name
        api_writes_url: Koku API writes URL
        rh_identity_header: X-Rh-Identity header value
    """
    print(f"\n{'='*60}")
    print(f"MULTI-CLUSTER CLEANUP")
    print(f"{'='*60}")

    for ctx in result.clusters:
        print(f"  Cleaning cluster {ctx.cluster_index}: {ctx.cluster_id}")

        # Delete source
        if delete_source(
            result.namespace,
            ingress_pod,
            api_writes_url,
            rh_identity_header,
            ctx.source_id,
            container="ingress",
        ):
            print(f"    Deleted source {ctx.source_id}")

        # Delete database records
        if cleanup_database_records(result.namespace, result.db_pod, ctx.cluster_id):
            print(f"    Cleaned database records")

    print(f"{'='*60}\n")
