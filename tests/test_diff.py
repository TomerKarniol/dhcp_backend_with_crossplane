"""Tests for the PUT diff logic in scope_service.update_scope."""
from unittest.mock import patch, call, MagicMock
import pytest
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload
from app.services.ps_executor import PowerShellError


def _make_scope(**overrides):
    defaults = dict(
        scopeName="Cluster-A",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="desc",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=None,
    )
    defaults.update(overrides)
    return DhcpScopePayload(**defaults)


def _run_update(current_scope, desired_scope):
    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        # First call returns current, second call returns "fresh" state after update
        mock_assemble.side_effect = [current_scope, desired_scope]
        scope_service.update_scope(current_scope.network, desired_scope)
        return mock_ps.call_args_list


def test_no_op_when_identical():
    scope = _make_scope()
    calls = _run_update(scope, scope)
    assert calls == [], "No PowerShell calls expected when desired == current"


def test_scope_name_changed():
    current = _make_scope(scopeName="Old Name")
    desired = _make_scope(scopeName="New Name")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_lease_changed():
    current = _make_scope(leaseDurationDays=8)
    desired = _make_scope(leaseDurationDays=14)
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_description_changed():
    current = _make_scope(description="old")
    desired = _make_scope(description="new")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4Scope" in cmd for cmd in ps_commands)


def test_gateway_changed():
    current = _make_scope(gateway="10.20.30.1")
    desired = _make_scope(gateway="10.20.30.2")
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


def test_dns_changed():
    current = _make_scope(dnsServers=["10.0.0.53"])
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Set-DhcpServerv4OptionValue" in cmd for cmd in ps_commands)


def test_exclusion_added():
    current = _make_scope(exclusions=[])
    desired = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Add-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


def test_exclusion_removed():
    current = _make_scope(exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")])
    desired = _make_scope(exclusions=[])
    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls]
    assert any("Remove-DhcpServerv4ExclusionRange" in cmd for cmd in ps_commands)


def _make_failover(**overrides):
    defaults = dict(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        loadBalancePercent=50,
        maxClientLeadTimeMinutes=60,
        sharedSecret=None,
    )
    defaults.update(overrides)
    return DhcpFailover(**defaults)


def test_failover_add_new_relationship():
    """current=None, desired=failover, relationship doesn't exist → Add-DhcpServerv4Failover"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # First run_ps in _setup_failover (Get-DhcpServerv4Failover) raises → new relationship
        mock_ps.side_effect = [
            PowerShellError("Get-DhcpServerv4Failover", "Not found", 1),  # relationship check
            None,  # Add-DhcpServerv4Failover
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


def test_failover_add_existing_relationship():
    """current=None, desired=failover, relationship exists → Add-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=None)
    desired = _make_scope(failover=_make_failover())

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        # Get-DhcpServerv4Failover returns existing relationship
        mock_ps.side_effect = [
            {"Name": "mce1-failover", "ScopeId": "10.20.20.0"},  # existing relationship
            None,  # Add-DhcpServerv4FailoverScope
            None,  # Invoke-DhcpServerv4FailoverReplication
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Add-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


def test_failover_remove():
    """current=failover, desired=None → Remove-DhcpServerv4FailoverScope"""
    current = _make_scope(failover=_make_failover())
    desired = _make_scope(failover=None)

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.side_effect = [
            None,  # Remove-DhcpServerv4FailoverScope
            PowerShellError("Get-DhcpServerv4Failover", "gone", 1),  # check remaining
        ]
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Remove-DhcpServerv4FailoverScope" in cmd for cmd in ps_commands)


def test_failover_params_updated():
    """Failover params changed → Set-DhcpServerv4Failover + Replication"""
    current = _make_scope(failover=_make_failover(reservePercent=5))
    desired = _make_scope(failover=_make_failover(reservePercent=10))

    from app.services import scope_service
    with (
        patch("app.services.scope_service.assemble_scope_state") as mock_assemble,
        patch("app.services.scope_service.run_ps") as mock_ps,
    ):
        mock_assemble.side_effect = [current, desired]
        mock_ps.return_value = None
        scope_service.update_scope(current.network, desired)

    ps_commands = [c.args[0] for c in mock_ps.call_args_list if c.args]
    assert any("Set-DhcpServerv4Failover" in cmd for cmd in ps_commands)
    assert any("Invoke-DhcpServerv4FailoverReplication" in cmd for cmd in ps_commands)


def test_failover_unchanged_no_calls():
    """Identical failover config → no failover cmdlets at all"""
    failover = _make_failover()
    current = _make_scope(failover=failover)
    desired = _make_scope(failover=_make_failover())  # same values, new object

    calls = _run_update(current, desired)
    ps_commands = [c.args[0] for c in calls if c.args]
    failover_cmds = [c for c in ps_commands if "Failover" in c]
    assert failover_cmds == []
