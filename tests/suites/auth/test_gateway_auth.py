"""
Gateway JWT authentication tests.

Tests for JWT authentication on the centralized API gateway.
The gateway handles all external API traffic with Keycloak JWT validation.

Tests cover:
- JWT authentication (valid/invalid/missing tokens)
- Health endpoint exemptions (no auth required)
- Rate limiting configuration
- X-Rh-Identity header injection
"""

import subprocess
import tempfile

import pytest
import requests


def _check_gateway_reachable(gateway_url: str, http_session: requests.Session) -> bool:
    """Check if gateway service is reachable."""
    try:
        # Try the ingress ready endpoint through the gateway (exempt from auth)
        response = http_session.get(f"{gateway_url}/ingress/ready", timeout=5)
        return response.status_code != 503
    except requests.exceptions.RequestException:
        return False


def _generate_fake_jwt() -> str | None:
    """Generate a fake JWT with valid structure but wrong signature."""
    try:
        import base64
        import json
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            key_file = f.name

        subprocess.run(
            ["openssl", "genrsa", "-out", key_file, "2048"],
            capture_output=True,
            check=True,
        )

        header = {"alg": "RS256", "typ": "JWT", "kid": "fake-key"}
        payload = {
            "sub": "attacker",
            "iss": "https://fake-issuer.com",
            "aud": "cost-management-operator",
            "exp": 9999999999,
        }

        def b64url_encode(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        header_b64 = b64url_encode(json.dumps(header).encode())
        payload_b64 = b64url_encode(json.dumps(payload).encode())

        message = f"{header_b64}.{payload_b64}"
        sign_result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file],
            input=message.encode(),
            capture_output=True,
            check=True,
        )
        signature_b64 = b64url_encode(sign_result.stdout)

        os.unlink(key_file)

        return f"{header_b64}.{payload_b64}.{signature_b64}"

    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


@pytest.mark.auth
@pytest.mark.integration
class TestGatewayJWTAuthentication:
    """Tests for JWT authentication on the centralized API gateway.

    The gateway is a centralized Envoy proxy that:
    - Validates JWT tokens from Keycloak
    - Injects X-Rh-Identity headers for backend services
    - Routes requests to appropriate backends based on path and method
    """

    @pytest.mark.smoke
    def test_gateway_reachable(self, gateway_url: str, http_session: requests.Session):
        """Verify the API gateway is reachable."""
        try:
            # Test ingress ready endpoint through gateway
            response = http_session.get(f"{gateway_url}/ingress/ready", timeout=10)
        except requests.exceptions.RequestException as e:
            pytest.skip(f"Cannot reach gateway service: {e}")

        if response.status_code == 503:
            pytest.skip("Gateway service returning 503 - pods may not be ready yet")

        assert response.status_code in [200, 401, 403], (
            f"Gateway not reachable: {response.status_code}"
        )

    def test_request_without_token_rejected(
        self, gateway_url: str, http_session: requests.Session
    ):
        """Verify requests without JWT token are rejected with 401."""
        if not _check_gateway_reachable(gateway_url, http_session):
            pytest.skip("Gateway service not available")

        response = http_session.post(f"{gateway_url}/ingress/v1/upload", timeout=10)

        assert response.status_code == 401, (
            f"Expected 401 for unauthenticated request, got {response.status_code}"
        )

    def test_malformed_token_rejected(
        self, gateway_url: str, http_session: requests.Session
    ):
        """Verify requests with malformed JWT token are rejected."""
        if not _check_gateway_reachable(gateway_url, http_session):
            pytest.skip("Gateway service not available")

        response = http_session.post(
            f"{gateway_url}/ingress/v1/upload",
            headers={"Authorization": "Bearer invalid.malformed.token"},
            timeout=10,
        )

        assert response.status_code == 401, (
            f"Expected 401 for malformed token, got {response.status_code}"
        )

    def test_fake_signature_token_rejected(
        self, gateway_url: str, http_session: requests.Session
    ):
        """Verify JWT tokens with invalid signatures are rejected."""
        if not _check_gateway_reachable(gateway_url, http_session):
            pytest.skip("Gateway service not available")

        fake_jwt = _generate_fake_jwt()
        if fake_jwt is None:
            pytest.skip("OpenSSL not available to generate fake JWT")

        response = http_session.post(
            f"{gateway_url}/ingress/v1/upload",
            headers={"Authorization": f"Bearer {fake_jwt}"},
            timeout=10,
        )

        assert response.status_code in [401, 403], (
            f"Expected 401/403 for fake signature, got {response.status_code}. "
            "CRITICAL: JWT with fake signature may have been accepted!"
        )

    def test_valid_token_accepted(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify requests with valid JWT token are accepted (auth passes)."""
        response = http_session.get(
            f"{gateway_url}/ingress/ready",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        assert response.status_code not in [401, 403], (
            f"Valid JWT token was rejected: {response.status_code}"
        )

    def test_cost_management_api_accessible(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify Cost Management API is accessible through gateway with valid JWT."""
        response = http_session.get(
            f"{gateway_url}/cost-management/v1/status/",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        # Accept 200 (success), 404 (endpoint may not exist), but not 401/403
        assert response.status_code not in [401, 403], (
            f"Valid JWT token was rejected for cost-management API: {response.status_code}"
        )

    def test_sources_api_accessible(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify Sources API is accessible through gateway with valid JWT."""
        response = http_session.get(
            f"{gateway_url}/sources/v1.0/source_types",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        # Accept 200 (success), 404 (endpoint may not exist), but not 401/403
        assert response.status_code not in [401, 403], (
            f"Valid JWT token was rejected for sources API: {response.status_code}"
        )


@pytest.mark.auth
@pytest.mark.integration
class TestHealthEndpointExemptions:
    """Tests for health check endpoints that bypass JWT authentication.

    These endpoints are configured as exempt in both the jwt_authn filter
    and the Lua filter to allow Kubernetes probes and health checks.
    """

    def test_ingress_ready_without_token(
        self, gateway_url: str, http_session: requests.Session
    ):
        """Verify /api/ingress/ready is accessible without JWT token.

        This endpoint is exempt from authentication to allow:
        - Kubernetes readiness/liveness probes
        - Load balancer health checks
        - Basic connectivity verification
        """
        response = http_session.get(
            f"{gateway_url}/ingress/ready",
            timeout=10,
        )

        # Should not return 401/403 - this endpoint is exempt from auth
        assert response.status_code not in [401, 403], (
            f"Ingress ready endpoint should be exempt from auth, got {response.status_code}. "
            "Check jwt_authn rules and Lua filter is_exempt_path() function."
        )



@pytest.mark.auth
@pytest.mark.integration
class TestRateLimiting:
    """Tests for gateway rate limiting configuration.

    The gateway uses a local rate limiter with:
    - 1000 max tokens
    - 100 tokens refilled per second

    Note: Rate limiting header is only present when the feature is deployed
    and may only appear when rate limiting is actively triggered.
    """

    def test_requests_succeed_under_limit(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify multiple requests succeed when under rate limit.

        The rate limit is 1000 tokens with 100/s refill, so a small
        burst of requests should succeed without being throttled.
        """
        success_count = 0
        rate_limited_count = 0

        for _ in range(5):
            response = http_session.get(
                f"{gateway_url}/cost-management/v1/status/",
                headers=jwt_token.authorization_header,
                timeout=10,
            )
            if response.status_code == 429:
                rate_limited_count += 1
            elif response.status_code not in [401, 403]:
                success_count += 1

        # Skip if auth failed on all requests
        if success_count == 0 and rate_limited_count == 0:
            pytest.skip("Authentication failed - cannot verify rate limiting")

        assert success_count >= 4, (
            f"At least 4 of 5 requests should succeed under rate limit, "
            f"but only {success_count} succeeded ({rate_limited_count} rate limited)"
        )

    def test_no_unexpected_rate_limiting(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify normal request patterns are not rate limited.

        A single request should never be rate limited under normal conditions.
        """
        response = http_session.get(
            f"{gateway_url}/cost-management/v1/status/",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        # Skip if auth failed
        if response.status_code in [401, 403]:
            pytest.skip("Authentication failed - cannot verify rate limiting")

        assert response.status_code != 429, (
            "Single request should not be rate limited. "
            "Check if rate limit configuration is too restrictive."
        )


@pytest.mark.auth
@pytest.mark.integration
class TestIdentityHeaderInjection:
    """Tests for X-Rh-Identity header injection by the Lua filter.

    The Lua filter extracts claims from the JWT token (org_id, account_number,
    username, email) and constructs an X-Rh-Identity header required by Koku.
    """

    def test_authenticated_request_reaches_backend(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify authenticated requests reach the backend successfully.

        If X-Rh-Identity header is not properly injected, Koku will reject
        the request with 401/403.
        """
        response = http_session.get(
            f"{gateway_url}/cost-management/v1/sources/",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        # If X-Rh-Identity wasn't injected, Koku would return 401/403
        assert response.status_code not in [401, 403], (
            f"Request failed with {response.status_code}. "
            "X-Rh-Identity header may not be injected correctly by Lua filter."
        )

    def test_ros_api_accessible_with_identity(
        self, gateway_url: str, jwt_token, http_session: requests.Session
    ):
        """Verify ROS recommendations API is accessible with identity header.

        The ROS API also requires X-Rh-Identity for tenant isolation.
        """
        response = http_session.get(
            f"{gateway_url}/cost-management/v1/recommendations/openshift/",
            headers=jwt_token.authorization_header,
            timeout=10,
        )

        # Accept 200 or 404 (no recommendations yet), but not 401/403
        assert response.status_code not in [401, 403], (
            f"ROS API rejected request with {response.status_code}. "
            "X-Rh-Identity may not be properly injected."
        )
