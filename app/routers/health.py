from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from app.services.dhcp_env import DhcpEnvironmentError, validate_dhcp_environment

router = APIRouter(tags=["health"])


@router.get(
    "/healthz",
    summary="Health check",
    description="""
Verifies that the runtime environment is capable of executing DHCP automation:

1. Native Windows OS (not WSL / Linux / macOS)
2. `powershell.exe` present and executable
3. DHCP cmdlets available (`Get-DhcpServerv4Scope` discoverable)

This endpoint is intentionally **not** protected by the DHCP environment dependency
so that it remains callable in broken environments and can report exactly what is wrong.

**Returns 200** when all checks pass.
**Returns 503** with a structured `reason` field when any check fails.
""",
    responses={
        status.HTTP_200_OK: {
            "description": "Runtime environment supports DHCP automation.",
            "content": {"application/json": {"example": {"status": "ok"}}},
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "Runtime environment cannot support DHCP automation. "
                "The 'reason' field identifies the specific failure: "
                "unsupported_os, wsl_detected, powershell_not_found, "
                "powershell_exec_failed, or dhcp_cmdlets_unavailable."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "detail": "DHCP PowerShell cmdlets are not available on this machine...",
                        "reason": "dhcp_cmdlets_unavailable",
                    }
                }
            },
        },
    },
)
def healthz():
    try:
        validate_dhcp_environment()
        return {"status": "ok"}
    except DhcpEnvironmentError as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "detail": exc.detail, "reason": exc.reason},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "detail": str(exc)},
        )
