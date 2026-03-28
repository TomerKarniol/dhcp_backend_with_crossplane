"""Tests for Pydantic model validation — IP addresses, ranges, field constraints."""
import pytest
from pydantic import ValidationError
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload


# ---------------------------------------------------------------------------
# DhcpExclusion
# ---------------------------------------------------------------------------

def test_exclusion_valid():
    e = DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")
    assert str(e.startAddress) == "10.20.30.1"
    assert str(e.endAddress) == "10.20.30.99"


def test_exclusion_same_start_end_valid():
    # Single IP exclusion is valid
    e = DhcpExclusion(startAddress="10.20.30.5", endAddress="10.20.30.5")
    assert str(e.startAddress) == "10.20.30.5"


def test_exclusion_end_before_start_invalid():
    with pytest.raises(ValidationError) as exc_info:
        DhcpExclusion(startAddress="10.20.30.99", endAddress="10.20.30.1")
    assert "endAddress" in str(exc_info.value)


def test_exclusion_invalid_ip_octet():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="10.20.999.1", endAddress="10.20.30.99")


def test_exclusion_invalid_ip_format():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="not-an-ip", endAddress="10.20.30.99")


def test_exclusion_invalid_ip_too_few_octets():
    with pytest.raises(ValidationError):
        DhcpExclusion(startAddress="10.20.30", endAddress="10.20.30.99")


# ---------------------------------------------------------------------------
# DhcpFailover
# ---------------------------------------------------------------------------

def test_failover_valid(sample_failover):
    assert sample_failover.reservePercent == 5
    assert sample_failover.maxClientLeadTimeMinutes == 60


def test_failover_reserve_percent_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=101,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_reserve_percent_negative():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=-1,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_load_balance_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="LoadBalance",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=101,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_max_lead_time_zero():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=0,
        )


def test_failover_max_lead_time_too_high():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=1441,
        )


def test_failover_empty_partner_server():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_invalid_mode():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="InvalidMode",
            serverRole="Active",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


def test_failover_invalid_server_role():
    with pytest.raises(ValidationError):
        DhcpFailover(
            partnerServer="dhcp02.lab.local",
            relationshipName="rel1",
            mode="HotStandby",
            serverRole="Primary",
            reservePercent=5,
            loadBalancePercent=50,
            maxClientLeadTimeMinutes=60,
        )


# ---------------------------------------------------------------------------
# DhcpScopePayload
# ---------------------------------------------------------------------------

def test_scope_payload_valid(sample_scope_payload):
    assert str(sample_scope_payload.network) == "10.20.30.0"
    assert str(sample_scope_payload.gateway) == "10.20.30.1"


def test_scope_end_range_before_start_range():
    with pytest.raises(ValidationError) as exc_info:
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.200",
            endRange="10.20.30.100",  # before startRange
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53"],
            dnsDomain="lab.local",
            exclusions=[],
        )
    assert "endRange" in str(exc_info.value)


def test_scope_invalid_network_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.999.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=[],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_invalid_gateway_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="not-an-ip",
            dnsServers=[],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_invalid_dns_server_ip():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=["10.0.0.53", "300.0.0.1"],  # invalid IP
            dnsDomain="",
            exclusions=[],
        )


def test_scope_lease_duration_zero():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=0,
            description="",
            gateway="10.20.30.1",
            dnsServers=[],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_lease_duration_too_high():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="Test",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=3651,
            description="",
            gateway="10.20.30.1",
            dnsServers=[],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_empty_name_invalid():
    with pytest.raises(ValidationError):
        DhcpScopePayload(
            scopeName="",
            network="10.20.30.0",
            subnetMask="255.255.255.0",
            startRange="10.20.30.100",
            endRange="10.20.30.200",
            leaseDurationDays=8,
            description="",
            gateway="10.20.30.1",
            dnsServers=[],
            dnsDomain="",
            exclusions=[],
        )


def test_scope_json_serialization_uses_strings(sample_scope_payload):
    """IPv4Address fields must serialize as plain strings in JSON output."""
    data = sample_scope_payload.model_dump(mode="json")
    assert isinstance(data["network"], str)
    assert isinstance(data["gateway"], str)
    assert isinstance(data["subnetMask"], str)
    assert all(isinstance(ip, str) for ip in data["dnsServers"])
    assert isinstance(data["exclusions"][0]["startAddress"], str)
