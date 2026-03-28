from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from app.services.ps_executor import run_ps

router = APIRouter(tags=["health"])


@router.get(
    "/healthz",
    summary="Health check",
    description="""
Verifies that PowerShell is available and the `DhcpServer` module can be loaded
on the Windows DHCP server running this API.

**Returns 200** when healthy, **503** when PowerShell or the DhcpServer module
is not accessible.
""",
    responses={
        status.HTTP_200_OK: {
            "description": "API is healthy; PowerShell and DhcpServer module are accessible.",
            "content": {"application/json": {"example": {"status": "ok"}}},
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "PowerShell is not reachable or the DhcpServer module is not installed. "
                "The API cannot manage DHCP scopes until this is resolved."
            ),
            "content": {
                "application/json": {
                    "example": {
                        "status": "error",
                        "detail": "PowerShell command failed (rc=1): ...",
                    }
                }
            },
        },
    },
)
def healthz():
    try:
        run_ps("Get-Module -ListAvailable -Name DhcpServer", parse_json=False)
        return {"status": "ok"}
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "detail": str(exc)},
        )
