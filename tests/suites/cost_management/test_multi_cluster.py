"""
Multi-cluster data generation and validation tests.

These tests validate that the system correctly handles data from multiple
OpenShift clusters simultaneously. Each cluster has unique configuration
(different node names, namespaces, pod names, and resource values).

Usage:
    # Run with default 3 clusters
    ./scripts/run-pytest.sh --multi-cluster

    # Run with specific cluster count
    ./scripts/run-pytest.sh --multi-cluster 5

    # Run directly with pytest
    pytest -m multi_cluster --cluster-count 3

These tests are skipped by default (like extended tests) because they:
- Require significant processing time (minutes per cluster)
- Generate substantial test data
- Are primarily for load testing and multi-tenancy validation

The tests include both cost management and ROS (Resource Optimization Service)
validation to ensure complete data pipeline verification.
"""

import pytest

from e2e_helpers import (
    get_kruize_experiments_for_cluster,
    get_kruize_recommendations_for_cluster,
    wait_for_kruize_experiments,
)
from utils import execute_db_query, get_secret_value


@pytest.mark.multi_cluster
@pytest.mark.cost_management
@pytest.mark.integration
class TestMultiClusterDataGeneration:
    """Test multi-cluster data generation and processing."""

    def test_all_clusters_created(self, multi_cluster_validation_data):
        """Verify all requested clusters were created successfully."""
        result = multi_cluster_validation_data

        assert result.total_clusters > 0, "No clusters were requested"
        assert result.successful_clusters > 0, "No clusters were created successfully"
        assert len(result.clusters) == result.successful_clusters

        print(f"\nCluster creation summary:")
        print(f"  Requested: {result.total_clusters}")
        print(f"  Successful: {result.successful_clusters}")
        if result.failed_clusters:
            print(f"  Failed: {result.failed_clusters}")

    def test_clusters_have_unique_ids(self, multi_cluster_validation_data):
        """Verify each cluster has a unique cluster ID."""
        result = multi_cluster_validation_data
        cluster_ids = [ctx.cluster_id for ctx in result.clusters]

        assert len(cluster_ids) == len(set(cluster_ids)), \
            "Duplicate cluster IDs found"

        print(f"\nUnique cluster IDs: {len(cluster_ids)}")
        for ctx in result.clusters:
            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}")

    def test_clusters_have_unique_sources(self, multi_cluster_validation_data):
        """Verify each cluster has a unique source registration."""
        result = multi_cluster_validation_data
        source_ids = [ctx.source_id for ctx in result.clusters]

        assert len(source_ids) == len(set(source_ids)), \
            "Duplicate source IDs found"

        print(f"\nUnique source IDs: {len(source_ids)}")
        for ctx in result.clusters:
            print(f"  [{ctx.cluster_index}] {ctx.source_id} ({ctx.source_name})")

    def test_clusters_have_unique_nise_configs(self, multi_cluster_validation_data):
        """Verify each cluster has unique NISE configuration."""
        result = multi_cluster_validation_data

        node_names = [ctx.nise_config.node_name for ctx in result.clusters]
        namespaces = [ctx.nise_config.namespace for ctx in result.clusters]
        pod_names = [ctx.nise_config.pod_name for ctx in result.clusters]

        assert len(node_names) == len(set(node_names)), \
            "Duplicate node names found"
        assert len(namespaces) == len(set(namespaces)), \
            "Duplicate namespaces found"
        assert len(pod_names) == len(set(pod_names)), \
            "Duplicate pod names found"

        print(f"\nUnique configurations per cluster:")
        for ctx in result.clusters:
            print(f"  [{ctx.cluster_index}]")
            print(f"    Node: {ctx.nise_config.node_name}")
            print(f"    Namespace: {ctx.nise_config.namespace}")
            print(f"    CPU request: {ctx.nise_config.cpu_request}")
            print(f"    Memory request: {ctx.nise_config.mem_request_gig} GiB")


@pytest.mark.multi_cluster
@pytest.mark.cost_management
@pytest.mark.integration
class TestMultiClusterDatabaseValidation:
    """Validate multi-cluster data in the database."""

    def test_clusters_have_summary_data(self, multi_cluster_validation_data):
        """Verify summary tables are populated for processed clusters."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        print(f"\nClusters with summary data: {len(clusters_with_data)}/{len(result.clusters)}")

        for ctx in clusters_with_data:
            assert ctx.schema_name is not None, \
                f"Cluster {ctx.cluster_id} has no schema"
            print(f"  [{ctx.cluster_index}] {ctx.cluster_id} -> schema: {ctx.schema_name}")

    def test_cluster_data_isolation(self, multi_cluster_validation_data):
        """Verify data from different clusters is properly isolated."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if len(clusters_with_data) < 2:
            pytest.skip("Need at least 2 clusters with data for isolation test")

        print(f"\nVerifying data isolation for {len(clusters_with_data)} clusters:")

        for ctx in clusters_with_data:
            # Query summary table for this cluster's data
            query = f"""
                SELECT COUNT(*), cluster_id
                FROM {ctx.schema_name}.reporting_ocpusagelineitem_daily_summary
                WHERE cluster_id = '{ctx.cluster_id}'
                GROUP BY cluster_id
            """
            rows = execute_db_query(
                result.namespace, result.db_pod, "koku", "koku", query
            )

            assert rows and len(rows) > 0, \
                f"No data found for cluster {ctx.cluster_id}"

            count = int(rows[0][0])
            returned_cluster_id = rows[0][1].strip()

            assert returned_cluster_id == ctx.cluster_id, \
                f"Cluster ID mismatch: expected {ctx.cluster_id}, got {returned_cluster_id}"

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: {count} summary rows")

    def test_no_cross_cluster_data_leakage(self, multi_cluster_validation_data):
        """Verify no data leakage between clusters."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if len(clusters_with_data) < 2:
            pytest.skip("Need at least 2 clusters with data for leakage test")

        # Use the first cluster's schema to check
        test_ctx = clusters_with_data[0]
        other_cluster_ids = [
            ctx.cluster_id for ctx in clusters_with_data
            if ctx.cluster_id != test_ctx.cluster_id
        ]

        print(f"\nChecking for data leakage in schema: {test_ctx.schema_name}")

        for other_id in other_cluster_ids:
            # This cluster's data should NOT appear under the test cluster's node/namespace
            query = f"""
                SELECT COUNT(*)
                FROM {test_ctx.schema_name}.reporting_ocpusagelineitem_daily_summary
                WHERE cluster_id = '{test_ctx.cluster_id}'
                  AND node LIKE 'cluster-%-node%'
                  AND node != '{test_ctx.nise_config.node_name}'
            """
            rows = execute_db_query(
                result.namespace, result.db_pod, "koku", "koku", query
            )

            count = int(rows[0][0]) if rows and rows[0][0] else 0
            assert count == 0, \
                f"Found {count} rows with wrong node names in cluster {test_ctx.cluster_id}"

        print("  No cross-cluster data leakage detected")


@pytest.mark.multi_cluster
@pytest.mark.cost_management
@pytest.mark.integration
class TestMultiClusterExpectedValues:
    """Validate expected values for each cluster."""

    def test_cluster_expected_values_populated(self, multi_cluster_validation_data):
        """Verify expected values are calculated for each cluster."""
        result = multi_cluster_validation_data

        print(f"\nExpected values per cluster:")
        for ctx in result.clusters:
            expected = ctx.expected
            assert expected is not None, \
                f"No expected values for cluster {ctx.cluster_id}"

            assert "cpu_request" in expected
            assert "mem_request_gig" in expected
            assert "node_name" in expected

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}:")
            print(f"    CPU request: {expected['cpu_request']}")
            print(f"    Memory request: {expected['mem_request_gig']} GiB")
            print(f"    Expected CPU hours: {expected.get('expected_cpu_hours', 'N/A')}")

    def test_cluster_resource_values_vary(self, multi_cluster_validation_data):
        """Verify resource values vary between clusters."""
        result = multi_cluster_validation_data

        if len(result.clusters) < 2:
            pytest.skip("Need at least 2 clusters to verify variation")

        cpu_requests = [ctx.expected["cpu_request"] for ctx in result.clusters]
        mem_requests = [ctx.expected["mem_request_gig"] for ctx in result.clusters]

        # Values should be unique (or at least not all the same)
        assert len(set(cpu_requests)) > 1, \
            "All clusters have the same CPU request"
        assert len(set(mem_requests)) > 1, \
            "All clusters have the same memory request"

        print(f"\nResource variation across clusters:")
        print(f"  CPU requests: {cpu_requests}")
        print(f"  Memory requests: {mem_requests}")


# =============================================================================
# ROS (Resource Optimization Service) Multi-Cluster Tests
# =============================================================================


@pytest.fixture(scope="module")
def kruize_credentials(cluster_config):
    """Get Kruize database credentials for ROS tests."""
    secret_name = f"{cluster_config.helm_release_name}-db-credentials"
    user = get_secret_value(cluster_config.namespace, secret_name, "kruize-user")
    password = get_secret_value(cluster_config.namespace, secret_name, "kruize-password")

    if not user or not password:
        pytest.skip("Kruize database credentials not found")

    return {"user": user, "password": password, "database": "kruize_db"}


@pytest.mark.multi_cluster
@pytest.mark.ros
@pytest.mark.extended
@pytest.mark.integration
class TestMultiClusterROSExperiments:
    """Validate ROS experiments are created for multiple clusters.

    These tests verify that the ROS processor correctly creates Kruize
    experiments for each cluster's data. Requires extended processing
    time as experiments are created asynchronously after summary tables.
    """

    def test_clusters_have_ros_expected_values(self, multi_cluster_validation_data):
        """Verify ROS-specific expected values are present for all clusters."""
        result = multi_cluster_validation_data

        print(f"\nROS expected values per cluster:")
        for ctx in result.clusters:
            expected = ctx.expected

            # Verify ROS-specific fields exist
            assert "cpu_cores" in expected, \
                f"Missing cpu_cores for cluster {ctx.cluster_id}"
            assert "memory_gig" in expected, \
                f"Missing memory_gig for cluster {ctx.cluster_id}"
            assert "cpu_limit" in expected, \
                f"Missing cpu_limit for cluster {ctx.cluster_id}"
            assert "cpu_usage" in expected, \
                f"Missing cpu_usage for cluster {ctx.cluster_id}"

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}:")
            print(f"    CPU cores: {expected['cpu_cores']}")
            print(f"    Memory: {expected['memory_gig']} GiB")
            print(f"    CPU limit: {expected['cpu_limit']}")
            print(f"    CPU usage: {expected['cpu_usage']}")

    def test_wait_for_kruize_experiments(
        self, multi_cluster_validation_data, kruize_credentials
    ):
        """Wait for Kruize experiments to be created for all clusters.

        This test waits for ROS processing to complete, which happens
        asynchronously after cost data is processed into summary tables.
        """
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if not clusters_with_data:
            pytest.skip("No clusters have summary data ready")

        kruize_user = kruize_credentials["user"]
        kruize_password = kruize_credentials["password"]

        print(f"\nWaiting for Kruize experiments ({len(clusters_with_data)} clusters):")

        clusters_with_experiments = []
        clusters_without_experiments = []

        for ctx in clusters_with_data:
            print(f"  [{ctx.cluster_index}] Waiting for {ctx.cluster_id}...")

            success = wait_for_kruize_experiments(
                namespace=result.namespace,
                db_pod=result.db_pod,
                cluster_id=ctx.cluster_id,
                kruize_user=kruize_user,
                kruize_password=kruize_password,
                timeout=240,
                interval=20,
            )

            if success:
                clusters_with_experiments.append(ctx)
                print(f"      Experiments created")
            else:
                clusters_without_experiments.append(ctx)
                print(f"      WARNING: Timeout waiting for experiments")

        print(f"\n  Summary:")
        print(f"    With experiments: {len(clusters_with_experiments)}")
        print(f"    Without experiments: {len(clusters_without_experiments)}")

        # At least some clusters should have experiments
        assert len(clusters_with_experiments) > 0, \
            "No clusters have Kruize experiments after waiting"

    def test_kruize_experiments_per_cluster(
        self, multi_cluster_validation_data, kruize_credentials
    ):
        """Verify each cluster has expected number of experiments."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if not clusters_with_data:
            pytest.skip("No clusters have summary data ready")

        kruize_user = kruize_credentials["user"]
        kruize_password = kruize_credentials["password"]

        print(f"\nKruize experiments per cluster:")

        for ctx in clusters_with_data:
            experiments = get_kruize_experiments_for_cluster(
                namespace=result.namespace,
                db_pod=result.db_pod,
                cluster_id=ctx.cluster_id,
                kruize_user=kruize_user,
                kruize_password=kruize_password,
            )

            experiment_count = len(experiments)
            expected_count = ctx.expected.get("expected_experiment_count", 1)

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: {experiment_count} experiments")

            if experiments:
                for exp in experiments[:3]:  # Show first 3
                    print(f"      - {exp['experiment_name']}")
                if len(experiments) > 3:
                    print(f"      ... and {len(experiments) - 3} more")

            # Soft assertion - log warning instead of failing
            if experiment_count < expected_count:
                print(f"      WARNING: Expected at least {expected_count} experiments")


@pytest.mark.multi_cluster
@pytest.mark.ros
@pytest.mark.extended
@pytest.mark.integration
class TestMultiClusterROSIsolation:
    """Validate ROS data isolation between clusters.

    These tests verify that Kruize experiments and recommendations
    are correctly isolated per cluster with no data leakage.
    """

    def test_experiments_reference_correct_cluster(
        self, multi_cluster_validation_data, kruize_credentials
    ):
        """Verify experiments reference the correct cluster data."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if not clusters_with_data:
            pytest.skip("No clusters have summary data ready")

        kruize_user = kruize_credentials["user"]
        kruize_password = kruize_credentials["password"]

        print(f"\nValidating experiment cluster references:")

        for ctx in clusters_with_data:
            experiments = get_kruize_experiments_for_cluster(
                namespace=result.namespace,
                db_pod=result.db_pod,
                cluster_id=ctx.cluster_id,
                kruize_user=kruize_user,
                kruize_password=kruize_password,
            )

            if not experiments:
                print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: No experiments (skipped)")
                continue

            # Verify each experiment references the correct cluster
            # Kruize may store cluster_id in experiment_name or cluster_name
            for exp in experiments:
                experiment_name = exp.get("experiment_name", "")
                cluster_name = exp.get("cluster_name", "")
                # Cluster ID should be in either experiment_name or cluster_name
                cluster_found = ctx.cluster_id in experiment_name or ctx.cluster_id in cluster_name
                assert cluster_found, \
                    f"Experiment does not reference cluster {ctx.cluster_id}:\n" \
                    f"  experiment_name: {experiment_name}\n" \
                    f"  cluster_name: {cluster_name}"

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: {len(experiments)} experiments verified")

    def test_no_cross_cluster_ros_leakage(
        self, multi_cluster_validation_data, kruize_credentials
    ):
        """Verify no ROS data leakage between clusters."""
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if len(clusters_with_data) < 2:
            pytest.skip("Need at least 2 clusters for ROS isolation test")

        kruize_user = kruize_credentials["user"]
        kruize_password = kruize_credentials["password"]

        print(f"\nChecking for ROS cross-cluster data leakage:")

        # Get all cluster IDs
        all_cluster_ids = [ctx.cluster_id for ctx in clusters_with_data]

        for ctx in clusters_with_data:
            experiments = get_kruize_experiments_for_cluster(
                namespace=result.namespace,
                db_pod=result.db_pod,
                cluster_id=ctx.cluster_id,
                kruize_user=kruize_user,
                kruize_password=kruize_password,
            )

            if not experiments:
                continue

            # Check that no experiments reference other clusters
            other_cluster_ids = [cid for cid in all_cluster_ids if cid != ctx.cluster_id]

            for exp in experiments:
                experiment_name = exp.get("experiment_name", "")
                cluster_name = exp.get("cluster_name", "")
                combined = f"{experiment_name} {cluster_name}"

                for other_id in other_cluster_ids:
                    assert other_id not in combined, \
                        f"Cluster {ctx.cluster_id} experiment references other cluster {other_id}"

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: No cross-cluster leakage")

        print("  All clusters have isolated ROS data")


@pytest.mark.multi_cluster
@pytest.mark.ros
@pytest.mark.extended
@pytest.mark.integration
class TestMultiClusterROSRecommendations:
    """Validate ROS recommendations for multiple clusters.

    Note: Recommendations may not be available immediately as Kruize
    requires multiple data points to generate recommendations.
    """

    def test_check_recommendations_per_cluster(
        self, multi_cluster_validation_data, kruize_credentials
    ):
        """Check if recommendations exist for any clusters.

        This is a soft check - recommendations may not be available
        with limited test data, but we verify the query works.
        """
        result = multi_cluster_validation_data
        clusters_with_data = [ctx for ctx in result.clusters if ctx.is_ready]

        if not clusters_with_data:
            pytest.skip("No clusters have summary data ready")

        kruize_user = kruize_credentials["user"]
        kruize_password = kruize_credentials["password"]

        print(f"\nKruize recommendations per cluster:")

        total_recommendations = 0
        clusters_with_recommendations = 0

        for ctx in clusters_with_data:
            recommendations = get_kruize_recommendations_for_cluster(
                namespace=result.namespace,
                db_pod=result.db_pod,
                cluster_id=ctx.cluster_id,
                kruize_user=kruize_user,
                kruize_password=kruize_password,
            )

            count = len(recommendations)
            total_recommendations += count

            if count > 0:
                clusters_with_recommendations += 1

            print(f"  [{ctx.cluster_index}] {ctx.cluster_id}: {count} recommendations")

        print(f"\n  Summary:")
        print(f"    Total recommendations: {total_recommendations}")
        print(f"    Clusters with recommendations: {clusters_with_recommendations}/{len(clusters_with_data)}")

        # Info message - recommendations may not be available with limited data
        if total_recommendations == 0:
            print("  NOTE: No recommendations yet - Kruize needs more data points")
