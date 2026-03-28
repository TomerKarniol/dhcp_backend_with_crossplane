from __future__ import annotations
import logging
from typing import Optional
from fastapi import HTTPException, status
from app.models import DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError, run_ps
from app.services.ps_parsers import assemble_scope_state

logger = logging.getLogger(__name__)


def scope_exists(scope_id: str) -> bool:
    try:
        run_ps(f"Get-DhcpServerv4Scope -ScopeId {scope_id}")
        return True
    except PowerShellError:
        return False


def create_scope(payload: DhcpScopePayload) -> DhcpScopePayload:
    """Create a DHCP scope. Idempotent — if scope already exists, return current state."""
    scope_id = str(payload.network)

    if not scope_exists(scope_id):
        # 1. Create scope
        run_ps(
            f'Add-DhcpServerv4Scope '
            f'-Name "{payload.scopeName}" '
            f'-StartRange {payload.startRange} '
            f'-EndRange {payload.endRange} '
            f'-SubnetMask {payload.subnetMask} '
            f'-State Active '
            f'-LeaseDuration (New-TimeSpan -Days {payload.leaseDurationDays}) '
            f'-Description "{payload.description}"',
            parse_json=False,
        )
    else:
        logger.info("Scope %s already exists — skipping Add-DhcpServerv4Scope", scope_id)

    # 2. Set options (idempotent — Set-* replaces existing values)
    dns_str = ",".join(str(ip) for ip in payload.dnsServers)
    run_ps(
        f"Set-DhcpServerv4OptionValue -ScopeId {scope_id} "
        f"-Router {payload.gateway} "
        f"-DnsServer {dns_str} "
        f'-DnsDomain "{payload.dnsDomain}"',
        parse_json=False,
    )

    # 3. Add exclusion ranges
    for excl in payload.exclusions:
        try:
            run_ps(
                f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
                f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}",
                parse_json=False,
            )
        except PowerShellError as e:
            if "already" not in e.stderr.lower():
                raise

    # 4. Failover setup
    if payload.failover is not None:
        _setup_failover(scope_id, payload.failover)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )

    return assemble_scope_state(scope_id)


def get_scope(scope_id: str) -> DhcpScopePayload:
    """Get current scope state. Raises HTTP 404 if scope does not exist."""
    try:
        return assemble_scope_state(scope_id)
    except PowerShellError as e:
        if _is_not_found_error(e.stderr):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Scope {scope_id} not found",
            )
        raise


def update_scope(scope_id: str, desired: DhcpScopePayload) -> DhcpScopePayload:
    """Apply only the changes between desired and current state (diff-based PUT)."""
    try:
        current = assemble_scope_state(scope_id)
    except PowerShellError as e:
        if _is_not_found_error(e.stderr):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Scope {scope_id} not found",
            )
        raise

    # 1. Scope params diff
    if (
        current.scopeName != desired.scopeName
        or current.leaseDurationDays != desired.leaseDurationDays
        or current.description != desired.description
    ):
        logger.info("Scope %s: updating params (name/lease/description)", scope_id)
        run_ps(
            f"Set-DhcpServerv4Scope -ScopeId {scope_id} "
            f'-Name "{desired.scopeName}" '
            f"-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) "
            f'-Description "{desired.description}"',
            parse_json=False,
        )

    # 2. Options diff
    if (
        current.gateway != desired.gateway
        or current.dnsServers != desired.dnsServers
        or current.dnsDomain != desired.dnsDomain
    ):
        logger.info("Scope %s: updating options (gateway/dns/domain)", scope_id)
        dns_str = ",".join(str(ip) for ip in desired.dnsServers)
        run_ps(
            f"Set-DhcpServerv4OptionValue -ScopeId {scope_id} "
            f"-Router {desired.gateway} "
            f"-DnsServer {dns_str} "
            f'-DnsDomain "{desired.dnsDomain}"',
            parse_json=False,
        )

    # 3. Exclusions diff (set-based; IPv4Address is hashable)
    current_excl = {(e.startAddress, e.endAddress) for e in current.exclusions}
    desired_excl = {(e.startAddress, e.endAddress) for e in desired.exclusions}

    for start, end in current_excl - desired_excl:
        logger.info("Scope %s: removing exclusion %s-%s", scope_id, start, end)
        run_ps(
            f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {start} -EndRange {end}",
            parse_json=False,
        )

    for start, end in desired_excl - current_excl:
        logger.info("Scope %s: adding exclusion %s-%s", scope_id, start, end)
        run_ps(
            f"Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
            f"-StartRange {start} -EndRange {end}",
            parse_json=False,
        )

    # 4. Failover diff
    _handle_failover_diff(scope_id, current.failover, desired.failover)

    return assemble_scope_state(scope_id)


def delete_scope(scope_id: str) -> None:
    """Delete a DHCP scope. Idempotent — returns normally if scope doesn't exist.

    Deletion order is the reverse of creation (critical for DHCP failover):
    1. Remove from failover relationship
    2. Remove exclusion ranges
    3. Remove scope (implicitly removes options)
    """
    if not scope_exists(scope_id):
        logger.info("Scope %s does not exist — nothing to delete", scope_id)
        return

    try:
        current = assemble_scope_state(scope_id)
    except PowerShellError:
        return  # Already gone

    # 1. Remove from failover
    if current.failover is not None:
        rel_name = current.failover.relationshipName
        try:
            run_ps(
                f'Remove-DhcpServerv4FailoverScope -Name "{rel_name}" '
                f"-ScopeId {scope_id} -Force",
                parse_json=False,
            )
            try:
                rel = run_ps(f'Get-DhcpServerv4Failover -Name "{rel_name}"')
                if rel and not rel.get("ScopeId"):
                    run_ps(
                        f'Remove-DhcpServerv4Failover -Name "{rel_name}" -Force',
                        parse_json=False,
                    )
            except PowerShellError:
                pass
        except PowerShellError as e:
            logger.warning("Failed to remove failover scope: %s", e.stderr)

    # 2. Remove exclusion ranges
    for excl in current.exclusions:
        try:
            run_ps(
                f"Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} "
                f"-StartRange {excl.startAddress} -EndRange {excl.endAddress}",
                parse_json=False,
            )
        except PowerShellError as e:
            logger.warning("Failed to remove exclusion %s: %s", excl.startAddress, e.stderr)

    # 3. Remove scope (implicitly removes options)
    run_ps(f"Remove-DhcpServerv4Scope -ScopeId {scope_id} -Force", parse_json=False)
    logger.info("Scope %s deleted", scope_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_not_found_error(stderr: str) -> bool:
    lower = stderr.lower()
    return any(kw in lower for kw in ("not found", "does not exist", "no dhcp scope"))


def _setup_failover(scope_id: str, failover: DhcpFailover) -> None:
    """Add a scope to a failover relationship, creating it if it doesn't exist yet."""
    existing = None
    try:
        existing = run_ps(f'Get-DhcpServerv4Failover -Name "{failover.relationshipName}"')
    except PowerShellError:
        pass

    if existing:
        run_ps(
            f'Add-DhcpServerv4FailoverScope -Name "{failover.relationshipName}" '
            f"-ScopeId {scope_id}",
            parse_json=False,
        )
    else:
        _create_failover_relationship(scope_id, failover)


def _create_failover_relationship(scope_id: str, failover: DhcpFailover) -> None:
    cmd = (
        f'Add-DhcpServerv4Failover '
        f'-Name "{failover.relationshipName}" '
        f'-PartnerServer {failover.partnerServer} '
        f'-ScopeId {scope_id} '
        f'-Mode {failover.mode} '
        f'-ServerRole {failover.serverRole} '
        f'-MaxClientLeadTime (New-TimeSpan -Minutes {failover.maxClientLeadTimeMinutes}) '
        f'-Force'
    )
    if failover.mode == "HotStandby":
        cmd += f" -ReservePercent {failover.reservePercent}"
    else:
        cmd += f" -LoadBalancePercent {failover.loadBalancePercent}"

    if failover.sharedSecret:
        cmd += f' -SharedSecret "{failover.sharedSecret}"'

    run_ps(cmd, parse_json=False)


def _handle_failover_diff(
    scope_id: str,
    current: Optional[DhcpFailover],
    desired: Optional[DhcpFailover],
) -> None:
    if current is None and desired is None:
        return

    if current is None and desired is not None:
        _setup_failover(scope_id, desired)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )
        return

    if current is not None and desired is None:
        rel_name = current.relationshipName
        run_ps(
            f'Remove-DhcpServerv4FailoverScope -Name "{rel_name}" -ScopeId {scope_id} -Force',
            parse_json=False,
        )
        try:
            rel = run_ps(f'Get-DhcpServerv4Failover -Name "{rel_name}"')
            if rel and not rel.get("ScopeId"):
                run_ps(
                    f'Remove-DhcpServerv4Failover -Name "{rel_name}" -Force',
                    parse_json=False,
                )
        except PowerShellError:
            pass
        return

    # Both exist — update params if changed
    if (
        current.mode != desired.mode
        or current.reservePercent != desired.reservePercent
        or current.loadBalancePercent != desired.loadBalancePercent
        or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
    ):
        logger.info("Scope %s: updating failover params", scope_id)
        cmd = (
            f'Set-DhcpServerv4Failover -Name "{desired.relationshipName}" '
            f"-Mode {desired.mode} "
            f"-MaxClientLeadTime (New-TimeSpan -Minutes {desired.maxClientLeadTimeMinutes})"
        )
        if desired.mode == "HotStandby":
            cmd += f" -ReservePercent {desired.reservePercent}"
        else:
            cmd += f" -LoadBalancePercent {desired.loadBalancePercent}"
        run_ps(cmd, parse_json=False)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )
