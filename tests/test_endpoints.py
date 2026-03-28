"""API endpoint tests using FastAPI TestClient with mocked service layer."""
import json
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import DhcpExclusion, DhcpScopePayload
from app.services.ps_executor import PowerShellError

client = TestClient(app, raise_server_exceptions=False)


def _make_scope_dict(**overrides):
    base = {
        "scopeName": "Cluster-A Management",
        "network": "10.20.30.0",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.30.100",
        "endRange": "10.20.30.200",
        "leaseDurationDays": 8,
        "description": "Cluster A management network",
        "gateway": "10.20.30.1",
        "dnsServers": ["10.0.0.53", "10.0.0.54"],
        "dnsDomain": "lab.local",
        "exclusions": [{"startAddress": "10.20.30.1", "endAddress": "10.20.30.99"}],
        "failover": None,
    }
    base.update(overrides)
    return base


def _make_scope(**overrides):
    return DhcpScopePayload(**_make_scope_dict(**overrides))


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_get_existing_scope():
    scope = _make_scope()
    with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
        r = client.get("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 200
    data = r.json()
    assert data["network"] == "10.20.30.0"
    assert data["leaseDurationDays"] == 8


def test_get_missing_scope():
    with patch(
        "app.services.scope_service.assemble_scope_state",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
    ):
        r = client.get("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

def test_post_create_new_scope():
    created = _make_scope()
    with patch("app.services.scope_service.create_scope", return_value=created):
        r = client.post("/api/v1/scopes", json=_make_scope_dict())
    assert r.status_code == 200
    assert r.json()["network"] == "10.20.30.0"


def test_post_idempotent_existing():
    """POST on existing scope must return 200, never 409."""
    existing = _make_scope()
    with patch("app.services.scope_service.create_scope", return_value=existing):
        r = client.post("/api/v1/scopes", json=_make_scope_dict())
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------

def test_put_update_scope():
    updated = _make_scope(scopeName="Updated Name")
    with patch("app.services.scope_service.update_scope", return_value=updated):
        r = client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict(scopeName="Updated Name"))
    assert r.status_code == 200
    assert r.json()["scopeName"] == "Updated Name"


def test_put_scope_not_found():
    from fastapi import HTTPException
    with patch(
        "app.services.scope_service.update_scope",
        side_effect=HTTPException(status_code=404, detail="Scope 10.20.30.0 not found"),
    ):
        r = client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def test_delete_scope():
    with patch("app.services.scope_service.delete_scope", return_value=None):
        r = client.delete("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 204
    assert r.content == b""


def test_delete_idempotent():
    """DELETE on non-existent scope must return 204."""
    with patch("app.services.scope_service.delete_scope", return_value=None):
        r = client.delete("/api/v1/scopes/10.99.99.99")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_powershell_error_500():
    with patch(
        "app.services.scope_service.create_scope",
        side_effect=PowerShellError("Add-DhcpServerv4Scope", "Access denied", 1),
    ):
        r = client.post("/api/v1/scopes", json=_make_scope_dict())
    assert r.status_code == 500
    body = r.json()
    assert "ps_error" in body
    assert body["ps_error"] == "Access denied"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_auth_required_when_token_set():
    import app.routers.scopes as scopes_mod
    original = scopes_mod.settings.DHCP_API_TOKEN
    scopes_mod.settings.DHCP_API_TOKEN = "secret-token"
    try:
        r = client.get("/api/v1/scopes/10.20.30.0")
        assert r.status_code == 401
    finally:
        scopes_mod.settings.DHCP_API_TOKEN = original


def test_auth_passes_with_correct_token():
    import app.routers.scopes as scopes_mod
    scope = _make_scope()
    original = scopes_mod.settings.DHCP_API_TOKEN
    scopes_mod.settings.DHCP_API_TOKEN = "secret-token"
    try:
        with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            r = client.get(
                "/api/v1/scopes/10.20.30.0",
                headers={"Authorization": "Bearer secret-token"},
            )
        assert r.status_code == 200
    finally:
        scopes_mod.settings.DHCP_API_TOKEN = original


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_healthz_endpoint():
    with patch("app.routers.health.run_ps", return_value=None):
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Critical: GET/PUT roundtrip test
# ---------------------------------------------------------------------------

def test_invalid_scope_id_returns_400():
    r = client.get("/api/v1/scopes/10.20.999.0")
    assert r.status_code == 400
    assert "Invalid scope ID" in r.json()["detail"]


def test_invalid_scope_id_not_ip_returns_422():
    # Pattern validation on the path param catches non-IP strings before our validator
    r = client.get("/api/v1/scopes/not-an-ip")
    assert r.status_code in (400, 422)


def test_get_put_roundtrip(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    """
    CRITICAL: The JSON that Crossplane sends as the PUT body must be byte-for-byte
    identical to the JSON returned by the GET endpoint.

    This test catches type mismatches (int vs str), key ordering differences,
    null handling, and array ordering issues.
    """
    from app.services.ps_parsers import assemble_scope_state as real_assemble
    from app.services.ps_executor import PowerShellError

    # Build the "desired" payload — this is what Crossplane would send as PUT body
    put_payload = DhcpScopePayload(
        scopeName="Cluster-A Management",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=None,
    )
    put_json = put_payload.model_dump(mode="json")

    # Simulate GET response assembled from PowerShell output
    def fake_run_ps(cmd, parse_json=True):
        if "Get-DhcpServerv4Scope" in cmd:
            return mock_ps_scope_raw
        if "Get-DhcpServerv4OptionValue" in cmd:
            return mock_ps_options_raw
        if "Get-DhcpServerv4ExclusionRange" in cmd:
            return mock_ps_exclusions_raw
        if "Get-DhcpServerv4Failover" in cmd:
            raise PowerShellError(cmd, "No failover configured", 1)
        return None

    with patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        get_payload = real_assemble("10.20.30.0")
    get_json = get_payload.model_dump(mode="json")

    # Must be byte-for-byte identical — no sort_keys so field order matters too
    put_str = json.dumps(put_json, ensure_ascii=False)
    get_str = json.dumps(get_json, ensure_ascii=False)

    assert put_str == get_str, (
        f"GET/PUT mismatch!\nPUT: {put_str}\nGET: {get_str}"
    )
