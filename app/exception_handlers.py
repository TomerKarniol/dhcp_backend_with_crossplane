import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from app.services.ps_executor import PowerShellError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app.

    Currently handles:
    - PowerShellError → HTTP 500 with ps_error field
      Used by: any route that calls scope_service functions when PowerShell
      returns a non-zero exit code that isn't caught internally (i.e. not a
      "not found" error that scope_service converts to 404).
    """

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
