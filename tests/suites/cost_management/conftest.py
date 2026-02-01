"""
Cost Management (Koku) suite fixtures.

Note: Sources API has been merged into Koku. All sources endpoints are now
available via the Koku API at /api/cost-management/v1/ using X-Rh-Identity
header for authentication instead of x-rh-sources-org-id.

Includes the cost_validation_data fixture that runs the full E2E flow for cost validation tests.
This makes cost_validation tests SELF-CONTAINED - they don't depend on other test modules.
"""

import os
import shutil
import tempfile
from datetime import datetime, timedelta

import pytest
import requests

from e2e_helpers import (
    DEFAULT_S3_BUCKET,
    ClusterTestContext,
    MultiClusterResult,
    NISEConfig,
    cleanup_database_records,
    cleanup_multi_cluster_data,
    delete_source,
    ensure_nise_available,
    generate_cluster_id,
    generate_multi_cluster_data,
    generate_nise_data,
    get_koku_api_reads_url,
    get_koku_api_writes_url,
    register_source,
    upload_with_retry,
    wait_for_provider,
    wait_for_summary_tables,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    execute_db_query,
    exec_in_pod,
    get_pod_by_label,
)


def cleanup_old_cost_val_clusters(
    namespace: str,
    db_pod: str,
    ingress_pod: str,
    api_reads_url: str,
    api_writes_url: str,
    rh_identity_header: str,
):
    """Clean up any leftover cost-val clusters from previous test runs.
    
    This ensures cost_validation tests start with a clean slate and don't
    pick up data from previous runs.
    """
    import json
    
    # Find and delete old cost-val sources
    try:
        result = exec_in_pod(
            namespace,
            ingress_pod,
            [
                "curl", "-s", f"{api_reads_url}/sources",
                "-H", "Content-Type: application/json",
                "-H", f"X-Rh-Identity: {rh_identity_header}",
            ],
            container="ingress",
        )
        
        if result:
            sources = json.loads(result)
            for source in sources.get("data", []):
                source_ref = source.get("source_ref", "")
                if source_ref and source_ref.startswith("cost-val-"):
                    source_id = source.get("id")
                    print(f"       Deleting old source: {source.get('name')} (ref: {source_ref})")
                    exec_in_pod(
                        namespace,
                        ingress_pod,
                        [
                            "curl", "-s", "-X", "DELETE",
                            f"{api_writes_url}/sources/{source_id}",
                            "-H", f"X-Rh-Identity: {rh_identity_header}",
                        ],
                        container="ingress",
                    )
    except Exception as e:
        print(f"       Warning: Could not clean old sources: {e}")
    
    # Clean up database records for cost-val clusters
    try:
        # Delete manifest statuses
        execute_db_query(
            namespace, db_pod, "koku", "koku",
            """
            DELETE FROM reporting_common_costusagereportstatus 
            WHERE manifest_id IN (
                SELECT id FROM reporting_common_costusagereportmanifest 
                WHERE cluster_id LIKE 'cost-val-%'
            )
            """
        )
        
        # Delete manifests
        execute_db_query(
            namespace, db_pod, "koku", "koku",
            "DELETE FROM reporting_common_costusagereportmanifest WHERE cluster_id LIKE 'cost-val-%'"
        )
        
        # Get all schemas and clean summary tables + tenant provider mappings
        schemas = execute_db_query(
            namespace, db_pod, "koku", "koku",
            "SELECT DISTINCT schema_name FROM api_customer WHERE schema_name IS NOT NULL"
        )
        
        if schemas:
            for row in schemas:
                schema = row[0].strip() if row[0] else None
                if schema:
                    try:
                        execute_db_query(
                            namespace, db_pod, "koku", "koku",
                            f"DELETE FROM {schema}.reporting_ocpusagelineitem_daily_summary WHERE cluster_id LIKE 'cost-val-%'"
                        )
                    except Exception:
                        pass  # Table might not exist in all schemas
                    
                    # Delete tenant-provider mappings (FK constraint on api_provider)
                    try:
                        execute_db_query(
                            namespace, db_pod, "koku", "koku",
                            f"""
                            DELETE FROM {schema}.reporting_tenant_api_provider 
                            WHERE provider_id IN (
                                SELECT uuid FROM public.api_provider 
                                WHERE name LIKE 'cost-validation%'
                            )
                            """
                        )
                    except Exception:
                        pass  # Table might not exist in all schemas
        
        # Delete providers (after FK references are removed)
        try:
            execute_db_query(
                namespace, db_pod, "koku", "koku",
                "DELETE FROM public.api_provider WHERE name LIKE 'cost-validation%'"
            )
        except Exception:
            pass  # May fail if no matching providers
                        
    except Exception as e:
        print(f"       Warning: Could not clean database records: {e}")


@pytest.fixture(scope="module")
def koku_api_reads_url_cost_mgmt(cluster_config) -> str:
    """Get Koku API reads URL for cost management tests."""
    return get_koku_api_reads_url(cluster_config.helm_release_name, cluster_config.namespace)


@pytest.fixture(scope="module")
def koku_api_writes_url_cost_mgmt(cluster_config) -> str:
    """Get Koku API writes URL for cost management tests."""
    return get_koku_api_writes_url(cluster_config.helm_release_name, cluster_config.namespace)


@pytest.fixture(scope="module")
def koku_api_writes_url(cluster_config) -> str:
    """Get Koku API writes URL for operations that modify state (POST, PUT, DELETE).
    
    The Koku deployment separates reads/writes for scalability.
    """
    return (
        f"http://{cluster_config.helm_release_name}-koku-api-writes."
        f"{cluster_config.namespace}.svc.cluster.local:8000/api/cost-management/v1"
    )


@pytest.fixture(scope="module")
def koku_api_reads_url(cluster_config) -> str:
    """Get Koku API reads URL for read-only operations (GET).
    
    The Koku deployment separates reads/writes for scalability.
    """
    return (
        f"http://{cluster_config.helm_release_name}-koku-api-reads."
        f"{cluster_config.namespace}.svc.cluster.local:8000/api/cost-management/v1"
    )


@pytest.fixture(scope="module")
def ingress_pod(cluster_config) -> str:
    """Get ingress pod name for executing API calls.
    
    The ingress pod has NetworkPolicy access to koku-api, so we use it
    to make internal API calls.
    """
    pod = get_pod_by_label(
        cluster_config.namespace,
        "app.kubernetes.io/component=ingress"
    )
    if not pod:
        pytest.skip("Ingress pod not found for API calls")
    return pod


@pytest.fixture(scope="module")
def rh_identity_header(org_id) -> str:
    """Get X-Rh-Identity header value for the test org."""
    return create_rh_identity_header(org_id)


# =============================================================================
# E2E Test Data Fixture - Self-Contained Setup for Cost Validation
# =============================================================================

@pytest.fixture(scope="module")
def cost_validation_data(cluster_config, s3_config, jwt_token, ingress_url, org_id):
    """Run full E2E setup for cost validation tests - SELF-CONTAINED.
    
    This fixture:
    1. Generates NISE data with known expected values
    2. Registers a source in Koku Sources API
    3. Uploads data via JWT-authenticated ingress
    4. Waits for Koku to process and populate summary tables
    5. Yields the test context
    6. Cleans up all test data on teardown (if E2E_CLEANUP_AFTER=true)
    
    Environment Variables:
    - E2E_CLEANUP_BEFORE: Run cleanup before tests (default: true)
    - E2E_CLEANUP_AFTER: Run cleanup after tests (default: true)
    """
    # Check cleanup settings
    cleanup_before = os.environ.get("E2E_CLEANUP_BEFORE", "true").lower() == "true"
    cleanup_after = os.environ.get("E2E_CLEANUP_AFTER", "true").lower() == "true"
    
    # Check NISE availability
    if not ensure_nise_available():
        pytest.skip("NISE not available and could not be installed")
    
    # Generate unique cluster ID
    cluster_id = generate_cluster_id(prefix="cost-val")
    
    # Get required pods
    db_pod = get_pod_by_label(cluster_config.namespace, "app.kubernetes.io/component=database")
    if not db_pod:
        pytest.skip("Database pod not found")
    
    ingress_pod = get_pod_by_label(cluster_config.namespace, "app.kubernetes.io/component=ingress")
    if not ingress_pod:
        pytest.skip("Ingress pod not found")
    
    temp_dir = tempfile.mkdtemp(prefix="cost_validation_")
    source_registration = None
    
    # Use Koku API URLs (sources are now part of Koku)
    api_reads_url = get_koku_api_reads_url(cluster_config.helm_release_name, cluster_config.namespace)
    api_writes_url = get_koku_api_writes_url(cluster_config.helm_release_name, cluster_config.namespace)
    rh_identity = create_rh_identity_header(org_id)
    
    # Use centralized NISE config
    nise_config = NISEConfig()
    
    try:
        print(f"\n{'='*60}")
        print("COST VALIDATION TEST SETUP (Self-Contained)")
        print(f"{'='*60}")
        print(f"  Cluster ID: {cluster_id}")
        print(f"  Cleanup before: {cleanup_before}")
        print(f"  Cleanup after: {cleanup_after}")
        
        # Pre-test cleanup: Remove any leftover cost-val clusters from previous runs
        if cleanup_before:
            print("\n  [0/5] Pre-test cleanup...")
            cleanup_old_cost_val_clusters(
            cluster_config.namespace, db_pod, ingress_pod,
            api_reads_url, api_writes_url, rh_identity
        )
            print("       Cleanup complete")
        else:
            print("\n  [0/5] Pre-test cleanup SKIPPED (E2E_CLEANUP_BEFORE=false)")
        
        # Step 1: Generate NISE data
        # Use 2 days ago to yesterday to get exactly 24 hours of data
        # (NISE generates from start_date 00:00 to end_date 23:59, so same day = 0 data)
        print("\n  [1/5] Generating NISE data...")
        now = datetime.utcnow()
        # Use dates 2-3 days ago to ensure we get exactly 24 hours
        start_date = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        files = generate_nise_data(cluster_id, start_date, end_date, temp_dir, config=nise_config)
        print(f"       Generated {len(files['all_files'])} CSV files")
        
        if not files["all_files"]:
            pytest.skip("NISE generated no CSV files")
        
        # Step 2: Register source via Koku API
        print("\n  [2/5] Registering source...")
        source_registration = register_source(
            namespace=cluster_config.namespace,
            pod=ingress_pod,
            api_reads_url=api_reads_url,
            api_writes_url=api_writes_url,
            rh_identity_header=rh_identity,
            cluster_id=cluster_id,
            org_id=org_id,
            source_name=f"cost-validation-{cluster_id[:16]}",
            container="ingress",
        )
        print(f"       Source ID: {source_registration.source_id}")
        
        # Step 3: Wait for provider
        print("\n  [3/5] Waiting for provider in Koku...")
        if not wait_for_provider(cluster_config.namespace, db_pod, cluster_id):
            pytest.fail(f"Provider not created for cluster {cluster_id}")
        print("       Provider created")
        
        # Step 4: Upload data
        print("\n  [4/5] Uploading data via ingress...")
        
        package_path = create_upload_package_from_files(
            pod_usage_files=files["pod_usage_files"],
            ros_usage_files=files["ros_usage_files"],
            cluster_id=cluster_id,
            start_date=start_date,
            end_date=end_date,
            node_label_files=files["node_label_files"] if files["node_label_files"] else None,
            namespace_label_files=files["namespace_label_files"] if files["namespace_label_files"] else None,
        )
        
        upload_url = f"{ingress_url}/v1/upload"
        print(f"       Ingress URL: {upload_url}")
        print(f"       Package size: {os.path.getsize(package_path)} bytes")
        
        # Create a session with SSL verification disabled
        session = requests.Session()
        session.verify = False
        
        response = upload_with_retry(
            session,
            upload_url,
            package_path,
            jwt_token.authorization_header,
        )
        
        if response.status_code not in [200, 201, 202]:
            pytest.fail(f"Upload failed: {response.status_code} - {response.text[:200] if response.text else 'No body'}")
        print(f"       Upload successful: {response.status_code}")
        
        # Step 5: Wait for processing
        print("\n  [5/5] Waiting for Koku processing...")
        schema_name = wait_for_summary_tables(cluster_config.namespace, db_pod, cluster_id)
        
        if not schema_name:
            pytest.fail(f"Timeout waiting for summary tables for cluster {cluster_id}")
        print("       Summary tables populated")
        
        print(f"\n{'='*60}")
        print("SETUP COMPLETE - Running validation tests")
        print(f"{'='*60}\n")
        
        # Query the actual number of days of data in the DB
        # Koku aggregates hourly data into daily summaries
        result = execute_db_query(
            cluster_config.namespace, db_pod, "koku", "koku",
            f"""
            SELECT COUNT(DISTINCT usage_start)
            FROM {schema_name}.reporting_ocpusagelineitem_daily_summary
            WHERE cluster_id = '{cluster_id}'
            """
        )
        actual_days = int(result[0][0]) if result and result[0][0] else 1
        actual_hours = actual_days * 24  # Each day has 24 hours of data
        print(f"  Actual days of data in DB: {actual_days} ({actual_hours} hours)")
        
        yield {
            "namespace": cluster_config.namespace,
            "db_pod": db_pod,
            "cluster_id": cluster_id,
            "schema_name": schema_name,
            "source_id": source_registration.source_id,
            "org_id": org_id,
            "expected": nise_config.get_expected_values(hours=actual_hours),
        }
        
    finally:
        # Cleanup (only if enabled)
        print(f"\n{'='*60}")
        if cleanup_after:
            print("COST VALIDATION TEST CLEANUP")
            print(f"{'='*60}")
            
            if source_registration:
                if delete_source(
                    cluster_config.namespace,
                    ingress_pod,
                    api_writes_url,
                    rh_identity,
                source_registration.source_id,
                    container="ingress",
                ):
                    print(f"  Deleted source {source_registration.source_id}")
                else:
                    print(f"  Warning: Could not delete source {source_registration.source_id}")
            
            if db_pod:
                if cleanup_database_records(cluster_config.namespace, db_pod, cluster_id):
                    print("  Cleaned up database records")
                else:
                    print("  Warning: Could not clean database records")
        else:
            print("COST VALIDATION TEST CLEANUP SKIPPED (E2E_CLEANUP_AFTER=false)")
            print(f"{'='*60}")
            print(f"  Data preserved for cluster: {cluster_id}")
            if source_registration:
                print(f"  Source ID: {source_registration.source_id}")
        
        # Always clean up temp directory (local files only)
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print("  Cleaned up temp directory")

        print(f"{'='*60}\n")


# =============================================================================
# Multi-Cluster Validation Fixture
# =============================================================================


@pytest.fixture(scope="module")
def multi_cluster_validation_data(
    cluster_config, s3_config, jwt_token, ingress_url, org_id, cluster_count, cluster_prefix
):
    """Generate data for multiple clusters - SELF-CONTAINED.

    This fixture generates and uploads data for N clusters, where N is specified
    by the --cluster-count CLI option. Each cluster gets unique NISE configuration
    with different node names, namespaces, pod names, and resource values.

    Usage:
        pytest --cluster-count 3 -m multi_cluster

    The fixture:
    1. Generates unique NISE configs for each cluster (different resources)
    2. Creates and registers sources for each cluster
    3. Uploads data packages sequentially
    4. Waits for Koku to process each cluster
    5. Yields MultiClusterResult with all cluster contexts
    6. Cleans up all test data on teardown (if E2E_CLEANUP_AFTER=true)

    CLI Options:
    - --cluster-count N: Number of clusters (default: 1)
    - --cluster-prefix PREFIX: Prefix for cluster IDs (default: "multi")

    Environment Variables:
    - E2E_CLEANUP_BEFORE: Run cleanup before tests (default: true)
    - E2E_CLEANUP_AFTER: Run cleanup after tests (default: true)
    """
    # Check cleanup settings
    cleanup_before = os.environ.get("E2E_CLEANUP_BEFORE", "true").lower() == "true"
    cleanup_after = os.environ.get("E2E_CLEANUP_AFTER", "true").lower() == "true"

    # Check NISE availability
    if not ensure_nise_available():
        pytest.skip("NISE not available and could not be installed")

    # Get required pods
    db_pod = get_pod_by_label(cluster_config.namespace, "app.kubernetes.io/component=database")
    if not db_pod:
        pytest.skip("Database pod not found")

    ingress_pod = get_pod_by_label(cluster_config.namespace, "app.kubernetes.io/component=ingress")
    if not ingress_pod:
        pytest.skip("Ingress pod not found")

    # Use Koku API URLs
    api_reads_url = get_koku_api_reads_url(cluster_config.helm_release_name, cluster_config.namespace)
    api_writes_url = get_koku_api_writes_url(cluster_config.helm_release_name, cluster_config.namespace)
    rh_identity = create_rh_identity_header(org_id)

    result = None

    try:
        print(f"\n{'='*60}")
        print("MULTI-CLUSTER VALIDATION TEST SETUP")
        print(f"{'='*60}")
        print(f"  Cluster count: {cluster_count}")
        print(f"  Cluster prefix: {cluster_prefix}")
        print(f"  Cleanup before: {cleanup_before}")
        print(f"  Cleanup after: {cleanup_after}")

        # Pre-test cleanup if enabled
        if cleanup_before:
            print("\n  [0] Pre-test cleanup...")
            cleanup_old_cost_val_clusters(
                cluster_config.namespace, db_pod, ingress_pod,
                api_reads_url, api_writes_url, rh_identity
            )
            print("      Cleanup complete")

        # Generate multi-cluster data
        result = generate_multi_cluster_data(
            cluster_count=cluster_count,
            namespace=cluster_config.namespace,
            db_pod=db_pod,
            ingress_pod=ingress_pod,
            api_reads_url=api_reads_url,
            api_writes_url=api_writes_url,
            rh_identity_header=rh_identity,
            org_id=org_id,
            ingress_url=ingress_url,
            jwt_auth_header=jwt_token.authorization_header,
            cluster_prefix=cluster_prefix,
        )

        if not result.clusters:
            pytest.fail("No clusters were successfully created")

        if not result.all_successful:
            print(f"  WARNING: {len(result.failed_clusters)} clusters failed to process")

        print(f"\n{'='*60}")
        print("SETUP COMPLETE - Running multi-cluster validation tests")
        print(f"{'='*60}\n")

        yield result

    finally:
        print(f"\n{'='*60}")
        if cleanup_after and result:
            print("MULTI-CLUSTER VALIDATION TEST CLEANUP")
            print(f"{'='*60}")

            cleanup_multi_cluster_data(
                result=result,
                ingress_pod=ingress_pod,
                api_writes_url=api_writes_url,
                rh_identity_header=rh_identity,
            )
        else:
            print("MULTI-CLUSTER VALIDATION TEST CLEANUP SKIPPED")
            print(f"{'='*60}")
            if result:
                print(f"  Data preserved for {len(result.clusters)} clusters")
                for ctx in result.clusters:
                    print(f"    - {ctx.cluster_id}")

        print(f"{'='*60}\n")
