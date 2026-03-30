from __future__ import annotations
import logging
from typing import Optional
from fastapi import HTTPException, status
from app.models import DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError, run_ps
from app.services.ps_parsers import assemble_scope_state, normalize_list
from app.utils.ip_utils import ip_to_int

logger = logging.getLogger(__name__)


def _ps_str(value: str) -> str:
    """Escape a string for safe insertion inside a PowerShell double-quoted string.

    Escapes: backtick (PS escape char), dollar sign (variable expansion), double-quote (terminator).
    """
    return value.replace("`", "``").replace("$", "`$").replace('"', '`"')


def list_scopes() -> list[DhcpScopePayload]:
    """Return all DHCP scopes, each assembled via the same path as the single-scope GET.

    Scopes are sorted numerically by network address for deterministic output.

    Error strategy: fail-fast. If any scope cannot be assembled canonically (PowerShell
    error, malformed output, validation failure), the entire request raises — callers see
    HTTP 500. Partial-success lists would be misleading in an infrastructure API.
    """
    raw = run_ps("Get-DhcpServerv4Scope")
    entries = normalize_list(raw)  # None → [], single dict → [dict], list → list

    # Extract valid ScopeId values and sort numerically so output is deterministic
    scope_ids: list[str] = sorted(
        (str(e["ScopeId"]) for e in entries if e.get("ScopeId")),
        key=ip_to_int,
    )

    return [assemble_scope_state(scope_id) for scope_id in scope_ids]


def scope_exists(scope_id: str) -> bool:
    """Return True if the scope exists. Raises PowerShellError for non-not-found failures.

    Collapsing all PowerShell errors into False would mask permission errors and
    transient failures, causing unsafe create/delete decisions on existing scopes.
    """
    try:
        run_ps(f"Get-DhcpServerv4Scope -ScopeId {scope_id}")
        return True
    except PowerShellError as e:
        if _is_not_found_error(e.stderr):
            return False
        raise  # permission errors, transient failures — propagate, do not treat as "not found"


def create_scope(payload: DhcpScopePayload) -> DhcpScopePayload:
    """Create a DHCP scope. Idempotent — if scope already exists, return current state."""
    scope_id = str(payload.network)

    if not scope_exists(scope_id):
        # 1. Create scope
        run_ps(
            f'Add-DhcpServerv4Scope '
            f'-Name "{_ps_str(payload.scopeName)}" '
            f'-StartRange {payload.startRange} '
            f'-EndRange {payload.endRange} '
            f'-SubnetMask {payload.subnetMask} '
            f'-State Active '
            f'-LeaseDuration (New-TimeSpan -Days {payload.leaseDurationDays}) '
            f'-Description "{_ps_str(payload.description)}"',
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
        f'-DnsDomain "{_ps_str(payload.dnsDomain)}"',
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
            if not _is_already_exists_error(e.stderr):
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
            f'-Name "{_ps_str(desired.scopeName)}" '
            f"-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) "
            f'-Description "{_ps_str(desired.description)}"',
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
            f'-DnsDomain "{_ps_str(desired.dnsDomain)}"',
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

    # 1. Remove from failover.
    # Fail-hard: if we cannot detach the scope from its failover relationship we must NOT
    # proceed to delete the scope.  Proceeding would leave an orphaned failover relationship
    # that references a now-deleted scope — manual DHCP server cleanup would then be required
    # before Crossplane can recreate the scope.  Raising here lets Crossplane retry the DELETE
    # on the next reconciliation cycle, which is the safe behavior.
    if current.failover is not None:
        _remove_scope_from_failover(scope_id, current.failover.relationshipName)

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
    return any(kw in lower for kw in ("not found", "does not exist", "no dhcp scope", "cannot find"))


def _is_already_exists_error(stderr: str) -> bool:
    """Detect idempotent-safe 'already exists' errors by stable keyword set.

    Substring matching on stderr is inherently fragile (locale, PS version). Keep the
    set narrow and err on the side of propagating unrecognised errors rather than
    silently swallowing them.
    """
    lower = stderr.lower()
    return any(kw in lower for kw in ("already exists", "already been added", "already in use"))


def _remove_scope_from_failover(scope_id: str, rel_name: str) -> None:
    """Remove a scope from a failover relationship, deleting the relationship if now empty."""
    run_ps(
        f'Remove-DhcpServerv4FailoverScope -Name "{_ps_str(rel_name)}" '
        f"-ScopeId {scope_id} -Force",
        parse_json=False,
    )
    try:
        rel = run_ps(f'Get-DhcpServerv4Failover -Name "{_ps_str(rel_name)}"')
        if rel and not rel.get("ScopeId"):
            run_ps(
                f'Remove-DhcpServerv4Failover -Name "{_ps_str(rel_name)}" -Force',
                parse_json=False,
            )
    except PowerShellError:
        pass  # relationship already gone — idempotent


def _setup_failover(scope_id: str, failover: DhcpFailover) -> None:
    """Add a scope to a failover relationship, creating it if it doesn't exist yet."""
    existing = None
    try:
        existing = run_ps(f'Get-DhcpServerv4Failover -Name "{_ps_str(failover.relationshipName)}"')
    except PowerShellError:
        pass

    if existing:
        try:
            run_ps(
                f'Add-DhcpServerv4FailoverScope -Name "{_ps_str(failover.relationshipName)}" '
                f"-ScopeId {scope_id}",
                parse_json=False,
            )
        except PowerShellError as e:
            if not _is_already_exists_error(e.stderr):
                raise  # scope already in relationship is idempotent-safe; other errors are not
    else:
        _create_failover_relationship(scope_id, failover)


def _create_failover_relationship(scope_id: str, failover: DhcpFailover) -> None:
    cmd = (
        f'Add-DhcpServerv4Failover '
        f'-Name "{_ps_str(failover.relationshipName)}" '
        f'-PartnerServer "{_ps_str(failover.partnerServer)}" '
        f'-ScopeId {scope_id} '
        f'-Mode {failover.mode} '
        f'-MaxClientLeadTime (New-TimeSpan -Minutes {failover.maxClientLeadTimeMinutes}) '
        f'-Force'
    )
    if failover.mode == "HotStandby":
        # -ServerRole and -ReservePercent are only valid for HotStandby.
        # The Windows cmdlet does not accept -ServerRole for LoadBalance mode.
        cmd += f" -ServerRole {failover.serverRole}"
        cmd += f" -ReservePercent {failover.reservePercent}"
    else:
        cmd += f" -LoadBalancePercent {failover.loadBalancePercent}"

    if failover.sharedSecret:
        cmd += f' -SharedSecret "{_ps_str(failover.sharedSecret)}"'

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
        _remove_scope_from_failover(scope_id, current.relationshipName)
        return

    # ---- Step 1: mode change is always remove + recreate ------------------
    # Every other field (serverRole, reservePercent, loadBalancePercent) has
    # mode-specific semantics.  Comparing them across modes is meaningless:
    #   • serverRole is normalized to "Active" for LoadBalance — comparing it
    #     against a HotStandby value of "Standby" would fire the wrong check.
    #   • reservePercent is normalized to 0 for LoadBalance — comparing it
    #     against a HotStandby value is noise.
    #   • loadBalancePercent is normalized to 0 for HotStandby — same problem.
    # Isolating the mode check here prevents all those cross-mode false signals.
    if current.mode != desired.mode:
        logger.info(
            "Scope %s: failover mode changed %s→%s — removing relationship '%s' and recreating",
            scope_id, current.mode, desired.mode, current.relationshipName,
        )
        _remove_scope_from_failover(scope_id, current.relationshipName)
        _setup_failover(scope_id, desired)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )
        return

    # ---- Step 2: same-mode structural identity ----------------------------
    # relationshipName, partnerServer, and (for HotStandby) serverRole cannot
    # be changed in-place; Set-DhcpServerv4Failover does not accept -ServerRole.
    # serverRole is only meaningful for HotStandby — LoadBalance always normalises
    # it to "Active" so comparing it there adds no signal.
    identity_changed = (
        current.relationshipName != desired.relationshipName
        or current.partnerServer != desired.partnerServer
        or (current.mode == "HotStandby" and current.serverRole != desired.serverRole)
    )
    if identity_changed:
        logger.info(
            "Scope %s: failover identity fields changed — removing relationship '%s' and recreating",
            scope_id, current.relationshipName,
        )
        _remove_scope_from_failover(scope_id, current.relationshipName)
        _setup_failover(scope_id, desired)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )
        return

    # ---- Step 3: same-mode mutable params ---------------------------------
    # Compare only the fields that are meaningful for the current mode.
    # Comparing cross-mode fields (e.g. reservePercent when mode is LoadBalance)
    # would always be 0==0, adding noise and masking real differences.
    if current.mode == "HotStandby":
        mutable_changed = (
            current.reservePercent != desired.reservePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
            or current.sharedSecret != desired.sharedSecret
        )
    else:  # LoadBalance
        mutable_changed = (
            current.loadBalancePercent != desired.loadBalancePercent
            or current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes
            or current.sharedSecret != desired.sharedSecret
        )
    if mutable_changed:
        logger.info("Scope %s: updating failover params", scope_id)
        cmd = (
            f'Set-DhcpServerv4Failover -Name "{_ps_str(current.relationshipName)}" '
            f"-MaxClientLeadTime (New-TimeSpan -Minutes {desired.maxClientLeadTimeMinutes})"
        )
        if desired.mode == "HotStandby":
            cmd += f" -ReservePercent {desired.reservePercent}"
        else:
            cmd += f" -LoadBalancePercent {desired.loadBalancePercent}"
        if desired.sharedSecret is not None:
            cmd += f' -SharedSecret "{_ps_str(desired.sharedSecret)}"'
        elif current.sharedSecret is not None:
            cmd += ' -SharedSecret ""'  # clear existing secret
        run_ps(cmd, parse_json=False)
        run_ps(
            f"Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force",
            parse_json=False,
        )
