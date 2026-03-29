import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from app.services.dhcp_env import DhcpEnvironmentError
from app.services.ps_executor import PowerShellError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app.

    Handles:
    - DhcpEnvironmentError → HTTP 503 with reason + detail fields
      Raised when the runtime does not support DHCP automation (wrong OS, missing
      PowerShell, missing DHCP cmdlets).  503 is appropriate: the server exists
      but cannot currently service DHCP requests.

    - PowerShellError → HTTP 500 with ps_error field
      Raised when a PowerShell cmdlet exits non-zero during a DHCP operation.
    """

    @app.exception_handler(DhcpEnvironmentError)
    async def dhcp_env_error_handler(
        request: Request, exc: DhcpEnvironmentError
    ) -> JSONResponse:
        """
        Converts DhcpEnvironmentError into HTTP 503.

        Response body:
        - ``detail``: human-readable description (suitable for operator logs/alerts)
        - ``reason``: machine-readable code from DhcpEnvReason (suitable for tooling)
        """
        logger.error(
            "DHCP environment error on %s %s [%s]: %s",
            request.method,
            request.url.path,
            exc.reason,
            exc.detail,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": exc.detail, "reason": exc.reason},
        )

    @app.exception_handler(PowerShellError)
    async def powershell_error_handler(request: Request, exc: PowerShellError) -> JSONResponse:
        """
        Converts unhandled PowerShellError into HTTP 500.

        Response body:
        - ``detail``: human-readable error message
        - ``ps_error``: raw stderr from PowerShell (useful for diagnosing DHCP server issues)
        """
        logger.error(
            "PowerShell error on %s %s: %s",
            request.method,
            request.url.path,
            exc.stderr,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc), "ps_error": exc.stderr},
        )
