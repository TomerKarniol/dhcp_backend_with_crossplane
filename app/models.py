from __future__ import annotations
from ipaddress import IPv4Address
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


class DhcpExclusion(BaseModel):
    startAddress: IPv4Address = Field(
        description="First IP address in the exclusion range",
        examples=["10.20.30.1"],
    )
    endAddress: IPv4Address = Field(
        description="Last IP address in the exclusion range",
        examples=["10.20.30.99"],
    )

    @model_validator(mode="after")
    def end_gte_start(self) -> "DhcpExclusion":
        if int(self.endAddress) < int(self.startAddress):
            raise ValueError(
                f"endAddress {self.endAddress} must be >= startAddress {self.startAddress}"
            )
        return self


class DhcpFailover(BaseModel):
    partnerServer: str = Field(
        min_length=1,
        max_length=255,
        description="FQDN of the partner DHCP server",
        examples=["dhcp02.lab.local"],
    )
    relationshipName: str = Field(
        min_length=1,
        max_length=64,
        description="Failover relationship name (unique per DHCP server pair)",
        examples=["mce1-failover"],
    )
    mode: Literal["HotStandby", "LoadBalance"] = Field(
        description="Failover mode: HotStandby (active/standby) or LoadBalance"
    )
    serverRole: Literal["Active", "Standby"] = Field(
        description="Role of THIS server in HotStandby mode"
    )
    reservePercent: int = Field(
        ge=0,
        le=100,
        description="Percentage of addresses reserved for the standby server (HotStandby only)",
    )
    loadBalancePercent: int = Field(
        ge=0,
        le=100,
        description="Percentage of load handled by THIS server (LoadBalance only)",
    )
    maxClientLeadTimeMinutes: int = Field(
        ge=1,
        le=1440,
        description="Max client lead time in minutes (1–1440, i.e. up to 24 hours)",
    )
    sharedSecret: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Shared secret for failover authentication. null = no authentication.",
    )


class DhcpScopePayload(BaseModel):
    # Field ordering is CRITICAL — Crossplane compares GET response to PUT body byte-for-byte.
    # Do NOT reorder these fields.
    scopeName: str = Field(
        min_length=1,
        max_length=256,
        description="Human-readable display name for the scope",
        examples=["Cluster-A Management"],
    )
    network: IPv4Address = Field(
        description="Network address — also the DHCP scope ID used in all PowerShell cmdlets",
        examples=["10.20.30.0"],
    )
    subnetMask: IPv4Address = Field(
        description="Subnet mask",
        examples=["255.255.255.0"],
    )
    startRange: IPv4Address = Field(
        description="First IP address in the DHCP distribution range",
        examples=["10.20.30.100"],
    )
    endRange: IPv4Address = Field(
        description="Last IP address in the DHCP distribution range",
        examples=["10.20.30.200"],
    )
    leaseDurationDays: int = Field(
        ge=1,
        le=3650,
        description="Lease duration in days (1–3650)",
        examples=[8],
    )
    description: str = Field(
        default="",
        max_length=1024,
        description="Optional scope description",
    )
    gateway: IPv4Address = Field(
        description="Default gateway sent to clients (DHCP option 3)",
        examples=["10.20.30.1"],
    )
    dnsServers: list[IPv4Address] = Field(
        default_factory=list,
        description="Ordered list of DNS server IPs sent to clients (DHCP option 6)",
        examples=[["10.0.0.53", "10.0.0.54"]],
    )
    dnsDomain: str = Field(
        default="",
        max_length=256,
        description="DNS domain suffix sent to clients (DHCP option 15)",
        examples=["lab.local"],
    )
    exclusions: list[DhcpExclusion] = Field(
        default_factory=list,
        description="IP ranges excluded from distribution, sorted by startAddress",
    )
    failover: Optional[DhcpFailover] = Field(
        default=None,
        description="Failover configuration. null = no failover configured.",
    )

    @model_validator(mode="after")
    def end_range_gte_start_range(self) -> "DhcpScopePayload":
        if int(self.endRange) < int(self.startRange):
            raise ValueError(
                f"endRange {self.endRange} must be >= startRange {self.startRange}"
            )
        return self
