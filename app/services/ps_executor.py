import json
import logging
import subprocess

logger = logging.getLogger(__name__)


class PowerShellError(Exception):
    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"PowerShell command failed (rc={returncode}): {stderr}")


def run_ps(command: str, parse_json: bool = True) -> dict | list | None:
    """Execute a PowerShell command and optionally parse JSON output.

    Always appends -ErrorAction Stop so errors raise PowerShellError instead
    of silently returning empty output.
    """
    full_cmd = f"{command} -ErrorAction Stop"
    if parse_json:
        full_cmd += " | ConvertTo-Json -Depth 5 -Compress"

    logger.info("PS> %s", command)

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        logger.error("PS FAILED (rc=%d): %s", result.returncode, result.stderr.strip())
        raise PowerShellError(command, result.stderr.strip(), result.returncode)

    logger.debug("PS OUT: %s", result.stdout.strip()[:500])

    if not parse_json or not result.stdout.strip():
        return None

    return json.loads(result.stdout)
