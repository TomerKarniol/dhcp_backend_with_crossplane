import pytest
from app.models import DhcpExclusion, DhcpFailover, DhcpScopePayload


@pytest.fixture
def sample_failover():
    return DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="mce1-failover",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        loadBalancePercent=50,
        maxClientLeadTimeMinutes=60,
        sharedSecret=None,
    )


@pytest.fixture
def sample_scope_payload(sample_failover):
    return DhcpScopePayload(
        scopeName="Cluster-A Management",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99"),
        ],
        failover=sample_failover,
    )


@pytest.fixture
def sample_scope_payload_no_failover():
    return DhcpScopePayload(
        scopeName="Cluster-A Management",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[
            DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99"),
        ],
        failover=None,
    )


# ---------------------------------------------------------------------------
# Mock PowerShell output fixtures (mimic ConvertTo-Json output)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ps_scope_raw():
    return {
        "Name": "Cluster-A Management",
        "SubnetMask": "255.255.255.0",
        "StartRange": "10.20.30.100",
        "EndRange": "10.20.30.200",
        "LeaseDuration": "8.00:00:00",
        "Description": "Cluster A management network",
        "State": "Active",
        "ScopeId": "10.20.30.0",
    }


@pytest.fixture
def mock_ps_options_raw():
    return [
        {"OptionId": 3, "Value": ["10.20.30.1"], "Name": "Router"},
        {"OptionId": 6, "Value": ["10.0.0.53", "10.0.0.54"], "Name": "DNS Servers"},
        {"OptionId": 15, "Value": ["lab.local"], "Name": "DNS Domain Name"},
    ]


@pytest.fixture
def mock_ps_exclusions_raw():
    return [
        {"StartRange": "10.20.30.1", "EndRange": "10.20.30.99"},
    ]


@pytest.fixture
def mock_ps_failover_raw():
    return {
        "Name": "mce1-failover",
        "PartnerServer": "dhcp02.lab.local",
        "Mode": "HotStandby",
        "ServerRole": "Active",
        "ReservePercent": 5,
        "LoadBalancePercent": 50,
        "MaxClientLeadTime": "1:00:00",
        "SharedSecret": None,
    }
