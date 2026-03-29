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
    serverRole: Optional[Literal["Active", "Standby"]] = Field(
        default=None,
        description=(
            "Role of THIS server. Required when mode is 'HotStandby'. "
            "Normalized to 'Active' for LoadBalance (not used in that mode)."
        ),
    )
    reservePercent: int = Field(
        default=0,
        ge=0,
        le=100,
        description=(
            "Percentage of addresses reserved for the standby server. "
            "Used only in HotStandby mode. Normalized to 0 for LoadBalance."
        ),
    )
    loadBalancePercent: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Percentage of load handled by THIS server. "
            "Required when mode is 'LoadBalance'. "
            "Normalized to 0 for HotStandby (not used in that mode)."
        ),
    )
    maxClientLeadTimeMinutes: int = Field(
        ge=1,
        le=1440,
        description="Max client lead time in minutes (1–1440, i.e. up to 24 hours)",
    )
    sharedSecret: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Shared secret for failover authentication. null = no authentication. "
                    "Empty string is not valid; use null to indicate no authentication.",
    )

    @model_validator(mode="after")
    def enforce_mode_fields(self) -> "DhcpFailover":
        """Normalize and validate mode-specific fields.

        HotStandby: serverRole is required; loadBalancePercent is not used → normalized to 0.
        LoadBalance: loadBalancePercent is required; serverRole and reservePercent are not
                     used → normalized to canonical values ('Active' and 0).

        Normalization (not rejection) is intentional: it prevents GET/PUT parity mismatches
        when Helm values.yaml includes unused-mode fields.
        """
        if self.mode == "HotStandby":
            if self.serverRole is None:
                raise ValueError("serverRole is required when mode is 'HotStandby'")
            self.loadBalancePercent = 0
        else:  # LoadBalance
            if self.loadBalancePercent is None:
                raise ValueError("loadBalancePercent is required when mode is 'LoadBalance'")
            self.serverRole = "Active"
            self.reservePercent = 0
        return self


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

    @model_validator(mode="after")
    def validate_subnet_consistency(self) -> "DhcpScopePayload":
        """Validate that network/subnetMask is a valid subnet and all IPs fall within it."""
        from ipaddress import IPv4Network

        # strict=True: raises if network has host bits set, or mask is non-contiguous
        try:
            subnet = IPv4Network(f"{self.network}/{self.subnetMask}", strict=True)
        except ValueError as exc:
            raise ValueError(
                f"network {self.network} with subnetMask {self.subnetMask} "
                f"is not a valid subnet: {exc}"
            ) from exc

        for field, ip in [
            ("startRange", self.startRange),
            ("endRange", self.endRange),
            ("gateway", self.gateway),
        ]:
            if ip not in subnet:
                raise ValueError(f"{field} {ip} is not within subnet {subnet}")

        for i, excl in enumerate(self.exclusions):
            for attr in ("startAddress", "endAddress"):
                ip = getattr(excl, attr)
                if ip not in subnet:
                    raise ValueError(
                        f"exclusions[{i}].{attr} {ip} is not within subnet {subnet}"
                    )

        # Reject network and broadcast addresses in dynamic-assignment and routing fields.
        # These addresses are reserved: assigning them to gateway/range causes DHCP failure.
        net_addr = subnet.network_address
        bcast_addr = subnet.broadcast_address
        for field, ip in [
            ("gateway", self.gateway),
            ("startRange", self.startRange),
            ("endRange", self.endRange),
        ]:
            if ip == net_addr:
                raise ValueError(
                    f"{field} {ip} must not be the network address {net_addr}"
                )
            if ip == bcast_addr:
                raise ValueError(
                    f"{field} {ip} must not be the broadcast address {bcast_addr}"
                )

        return self
