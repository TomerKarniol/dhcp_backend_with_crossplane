from __future__ import annotations
import hmac
import logging
from ipaddress import IPv4Address, AddressValueError
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from app.config import settings
from app.models import DhcpScopePayload
from app.services import dhcp_env, scope_service

logger = logging.getLogger(__name__)

def _require_dhcp_env() -> None:
    """Route-level dependency: fail fast before any business logic if the runtime
    does not support DHCP automation.  Centralised here so every scope route
    is protected without per-route boilerplate.  The check is cached — only the
    first request per process incurs the subprocess overhead."""
    dhcp_env.validate_dhcp_environment()


router = APIRouter(
    prefix="/api/v1",
    tags=["scopes"],
    dependencies=[Depends(_require_dhcp_env)],
)

# ---------------------------------------------------------------------------
# Shared path type + validators
# ---------------------------------------------------------------------------

ScopeIdPath = Annotated[
    str,
    Path(
        description="Scope ID — IPv4 network address of the DHCP scope",
        examples=["10.20.30.0"],
        # No regex pattern here — _validate_scope_id owns all validation and returns a
        # consistent HTTP 400 for any invalid scope_id. Having both a pattern= and a custom
        # validator produces inconsistent status codes: non-IP strings get 422 (framework
        # pattern rejection) while out-of-range octets get 400 (custom validator). Clients
        # and Crossplane must handle only one contract: 400 for invalid scope_id.
    ),
]

# Shared response descriptions re-used across multiple routes
_RESPONSES_COMMON = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": (
            "Runtime environment cannot support DHCP automation. "
            "Returned when the server is running on an unsupported OS (Linux, macOS, WSL), "
            "PowerShell is missing or non-functional, or DHCP cmdlets are unavailable. "
            "The 'reason' field identifies the specific failure: "
            "unsupported_os, wsl_detected, powershell_not_found, "
            "powershell_exec_failed, or dhcp_cmdlets_unavailable."
        ),
        "content": {
            "application/json": {
                "example": {
                    "detail": "WSL (Windows Subsystem for Linux) is not a supported runtime.",
                    "reason": "wsl_detected",
                }
            }
        },
    },
    status.HTTP_401_UNAUTHORIZED: {
        "description": (
            "Missing or invalid bearer token. "
            "Only returned when the DHCP_API_TOKEN environment variable is set."
        ),
        "content": {
            "application/json": {
                "example": {"detail": "Unauthorized"}
            }
        },
    },
    status.HTTP_422_UNPROCESSABLE_ENTITY: {
        "description": (
            "Request body or path parameter failed validation. "
            "Includes details about which field is invalid and why "
            "(e.g. IP octet out of range, endRange < startRange, percent > 100)."
        ),
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "description": (
            "A PowerShell cmdlet on the Windows DHCP server returned a non-zero exit code. "
            "The response body contains 'detail' (human-readable message) and "
            "'ps_error' (raw stderr from PowerShell) to aid diagnosis."
        ),
        "content": {
            "application/json": {
                "example": {
                    "detail": "PowerShell command failed (rc=1): Access is denied.",
                    "ps_error": "Access is denied.",
                }
            }
        },
    },
}


def _verify_token(authorization: str = Header(default="")) -> None:
    """Bearer token auth — only enforced when DHCP_API_TOKEN env var is set."""
    if not settings.DHCP_API_TOKEN:
        return
    expected = f"Bearer {settings.DHCP_API_TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


def _validate_scope_id(scope_id: ScopeIdPath) -> str:
    """Validate that the scope_id path parameter is a well-formed IPv4 address (0–255 per octet)."""
    try:
        IPv4Address(scope_id)
    except (AddressValueError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid scope ID '{scope_id}': each octet must be 0–255",
        )
    return scope_id


# ---------------------------------------------------------------------------
# GET /scopes  — list all scopes
# ---------------------------------------------------------------------------

@router.get(
    "/scopes",
    response_model=list[DhcpScopePayload],
    status_code=status.HTTP_200_OK,
    summary="List all DHCP scopes",
    description="""
Returns every DHCP scope on the Windows DHCP server as a list of canonical
scope objects. Each element is structurally identical to the object returned
by `GET /api/v1/scopes/{scope_id}` for that same scope.

**Ordering**: scopes are sorted numerically by network address.

**Error handling**: if any single scope cannot be assembled (PowerShell error,
malformed cmdlet output, validation failure), the entire request fails with
HTTP 500. Partial results are not returned; that would be misleading in an
infrastructure API.

**Read-only**: this route does not modify any DHCP state.
""",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "List of all scopes. Each element has the same shape as the "
                "single-scope GET response. Returns an empty list if no scopes exist."
            ),
        },
        **_RESPONSES_COMMON,
    },
)
def list_scopes(
    _: None = Depends(_verify_token),
) -> list[DhcpScopePayload]:
    logger.info("GET /scopes")
    return scope_service.list_scopes()


# ---------------------------------------------------------------------------
# POST /scopes
# ---------------------------------------------------------------------------

@router.post(
    "/scopes",
    response_model=DhcpScopePayload,
    status_code=status.HTTP_200_OK,
    summary="Create a DHCP scope",
    description="""
Create a new DHCP scope on the Windows DHCP server.

**Idempotent**: If the scope already exists, this endpoint returns the current state
with HTTP 200 — it does NOT return 409. Crossplane may retry POST if the first attempt
times out before it can write the `external-create-pending` annotation; retries must
be harmless.

**Execution order**:
1. `Add-DhcpServerv4Scope` (skipped if scope already exists)
2. `Set-DhcpServerv4OptionValue` — sets gateway, DNS servers, DNS domain
3. `Add-DhcpServerv4ExclusionRange` — one call per exclusion entry
4. Failover setup (if `failover` is not null)
5. `Invoke-DhcpServerv4FailoverReplication` (if failover was configured)
6. Returns full current state (same shape as GET response)
""",
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Scope created successfully, or already existed (idempotent). "
                "Returns the full current scope state from the DHCP server."
            ),
        },
        **_RESPONSES_COMMON,
    },
)
def create_scope(
    payload: DhcpScopePayload,
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    logger.info("POST /scopes network=%s", payload.network)
    return scope_service.create_scope(payload)


# ---------------------------------------------------------------------------
# POST /scopes/{scope_id}  — used by Crossplane provider-http
# ---------------------------------------------------------------------------
# provider-http issues all lifecycle operations (observe/create/update/delete)
# to the SAME URL: /api/v1/scopes/{network}. This alias lets POST work there.

@router.post(
    "/scopes/{scope_id}",
    response_model=DhcpScopePayload,
    status_code=status.HTTP_200_OK,
    summary="Create a DHCP scope (scope-specific URL)",
    description="""
Idempotent create via the scope-specific URL.

Used by **Crossplane provider-http**, which issues all lifecycle operations
(GET observe / POST create / PUT update / DELETE delete) to the same URL
(`/api/v1/scopes/{network}`).

Behaves identically to `POST /api/v1/scopes`: creates the scope if it does not
exist, returns the current state if it already exists.

**Validation**: `scope_id` in the path must match the `network` field in the body.
""",
    responses={
        status.HTTP_200_OK: {
            "description": "Scope created or already existed. Returns current state.",
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "scope_id is not a valid IPv4 address, "
                "or scope_id does not match the network field in the request body."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "detail": "scope_id '10.20.30.0' does not match network '10.20.40.0' in body"
                    }
                }
            },
        },
        **_RESPONSES_COMMON,
    },
)
def create_scope_by_id(
    payload: DhcpScopePayload,
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    if str(payload.network) != scope_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope_id '{scope_id}' does not match network '{payload.network}' in body",
        )
    logger.info("POST /scopes/%s (scope-specific URL)", scope_id)
    return scope_service.create_scope(payload)


# ---------------------------------------------------------------------------
# GET /scopes/{scope_id}
# ---------------------------------------------------------------------------

@router.get(
    "/scopes/{scope_id}",
    response_model=DhcpScopePayload,
    status_code=status.HTTP_200_OK,
    summary="Get current DHCP scope state",
    description="""
Returns the current state of a DHCP scope assembled from the Windows DHCP server
via four PowerShell cmdlets (scope info, options, exclusions, failover).

Called by Crossplane provider-http on every reconciliation cycle (~1 minute per scope).
Crossplane compares this response to the desired state in the `Request` CR body;
any difference triggers a PUT.

**Returns 404** if the scope does not exist — Crossplane interprets this as
"resource not yet created" and issues a POST.
""",
    responses={
        status.HTTP_200_OK: {
            "description": "Scope found. Returns full current state assembled from DHCP server.",
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "scope_id is not a valid IPv4 address (e.g. an octet > 255).",
            "content": {
                "application/json": {
                    "example": {"detail": "Invalid scope ID '10.20.999.0': each octet must be 0–255"}
                }
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "Scope does not exist on the DHCP server. "
                "Crossplane will respond by issuing POST to create it."
            ),
            "content": {
                "application/json": {
                    "example": {"detail": "Scope 10.20.30.0 not found"}
                }
            },
        },
        **_RESPONSES_COMMON,
    },
)
def get_scope(
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    logger.info("GET /scopes/%s", scope_id)
    return scope_service.get_scope(scope_id)


# ---------------------------------------------------------------------------
# PUT /scopes/{scope_id}
# ---------------------------------------------------------------------------

@router.put(
    "/scopes/{scope_id}",
    response_model=DhcpScopePayload,
    status_code=status.HTTP_200_OK,
    summary="Update a DHCP scope (diff-based)",
    description="""
Updates a DHCP scope by comparing the desired state (request body) to the
current state on the DHCP server and applying **only the changed fields**.

**Diff logic** (only runs cmdlets for sections that changed):
- Scope params (name, lease, description) → `Set-DhcpServerv4Scope`
- Options (gateway, DNS servers, domain) → `Set-DhcpServerv4OptionValue`
- Exclusions (set difference) → `Add-` / `Remove-DhcpServerv4ExclusionRange`
- Failover → add, remove, or update relationship as needed

Called by Crossplane when the GET response differs from the desired state in the CR.
""",
    responses={
        status.HTTP_200_OK: {
            "description": "Scope updated. Returns full current state after applying changes.",
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "scope_id is not a valid IPv4 address.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "Scope does not exist on the DHCP server. "
                "Crossplane should not send PUT for a scope that was never created."
            ),
            "content": {
                "application/json": {
                    "example": {"detail": "Scope 10.20.30.0 not found"}
                }
            },
        },
        **_RESPONSES_COMMON,
    },
)
def update_scope(
    payload: DhcpScopePayload,
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    if str(payload.network) != scope_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope_id '{scope_id}' does not match network '{payload.network}' in body",
        )
    logger.info("PUT /scopes/%s", scope_id)
    return scope_service.update_scope(scope_id, payload)


# ---------------------------------------------------------------------------
# DELETE /scopes/{scope_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/scopes/{scope_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a DHCP scope",
    description="""
Deletes a DHCP scope and all its configuration from the Windows DHCP server.

**Idempotent**: Returns 204 even if the scope does not exist.

**Deletion order** (reverse of creation — critical for failover):
1. `Remove-DhcpServerv4FailoverScope` (if scope is in a failover relationship)
2. `Remove-DhcpServerv4Failover` (if this was the last scope in the relationship)
3. `Remove-DhcpServerv4ExclusionRange` for each exclusion
4. `Remove-DhcpServerv4Scope` (implicitly removes scope options)

Called by Crossplane when the `Request` CR is deleted from Kubernetes.
""",
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": (
                "Scope deleted, or did not exist (idempotent). "
                "Response body is empty."
            ),
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "scope_id is not a valid IPv4 address.",
        },
        **_RESPONSES_COMMON,
    },
)
def delete_scope(
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> Response:
    logger.info("DELETE /scopes/%s", scope_id)
    scope_service.delete_scope(scope_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
