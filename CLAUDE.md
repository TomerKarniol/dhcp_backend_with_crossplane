# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitOps-driven DHCP scope management. DHCP scopes are declared as YAML in git, rendered by a Helm chart into Crossplane `Request` CRs, and reconciled by Crossplane provider-http against this FastAPI backend. The backend translates HTTP CRUD into PowerShell cmdlets on a Windows DHCP server.

Full stack:
```
Git repo (DHCP YAML values)
  → CI/CD (helm template → kubectl apply)
    → Crossplane provider-http (reconciles Request CRs)
      → THIS FastAPI backend (translates HTTP → PowerShell)
        → Windows DHCP Server (DhcpServer module)
```

## Build & Test Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the API (development)
python run.py

# Run the API (production)
uvicorn app.main:app --host 0.0.0.0 --port 8080

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_parsers.py -v

# Run a single test
pytest tests/test_parsers.py::test_parse_timespan -v

# Lint
flake8 app/ tests/ --max-line-length=100

# Type check
mypy app/
```

## Project Structure

```
dhcp_fast_api_backend/
├── app/
│   ├── main.py                  # FastAPI app, exception handlers, startup
│   ├── config.py                # Settings (port, log level, bearer token)
│   ├── models.py                # DhcpScopePayload, DhcpExclusion, DhcpFailover
│   ├── routers/
│   │   └── scopes.py            # POST/GET/PUT/DELETE /api/v1/scopes
│   ├── services/
│   │   ├── scope_service.py     # Business logic: create/get/update/delete/diff
│   │   ├── ps_executor.py       # run_ps() subprocess wrapper, PowerShellError
│   │   └── ps_parsers.py        # Parse PowerShell JSON → Pydantic models
│   └── utils/
│       └── ip_utils.py          # ip_to_int(), parse_timespan_days(), parse_timespan_minutes()
├── tests/
│   ├── test_models.py           # Pydantic serialization, field ordering
│   ├── test_parsers.py          # PowerShell output parsing (no PS required)
│   ├── test_diff.py             # PUT diff logic (no PS required)
│   └── test_endpoints.py        # API tests with mocked run_ps()
├── helm/
│   └── dhcp-scope/              # Helm chart rendering Crossplane Request CRs
│       ├── Chart.yaml
│       ├── values.yaml          # Default/example values
│       └── templates/
│           ├── request.yaml     # Crossplane provider-http Request CR
│           └── _helpers.tpl
├── crossplane/
│   └── examples/
│       └── scope-example.yaml   # Example Crossplane Request CR (rendered output)
├── requirements.txt
└── run.py
```

---

## API specification (full)

### Base URL: `/api/v1`

### Endpoints (exactly 4)

#### POST `/scopes`
- **Called when**: Crossplane creates a new Request CR
- **Request body**: Full scope configuration (see DhcpScopePayload model below)
- **Response**: 200 + full current state (same shape as GET response)
- **MUST be idempotent**: If scope already exists, return current state (200), do NOT return 409 or 500. Crossplane can retry POST if the first attempt times out or the external-create-pending annotation write fails. Retries must be harmless.
- **Execution order**:
  1. `Add-DhcpServerv4Scope` (skip if scope already exists)
  2. `Set-DhcpServerv4OptionValue` (gateway, DNS servers, DNS domain)
  3. `Add-DhcpServerv4ExclusionRange` (for each exclusion entry)
  4. Failover setup (if failover is not null):
     - Check if relationship already exists: `Get-DhcpServerv4Failover -Name "{relationshipName}"`
     - If exists: `Add-DhcpServerv4FailoverScope -Name "{name}" -ScopeId {scopeId}`
     - If not exists: `Add-DhcpServerv4Failover` with full parameters
  5. `Invoke-DhcpServerv4FailoverReplication -ScopeId {scopeId}` (if failover was configured)
  6. Assemble current state via GET logic → return

#### GET `/scopes/{scope_id}`
- **Called when**: Every reconciliation cycle (~1 min per scope)
- **Path param**: `scope_id` is the network address (e.g., `10.20.30.0`)
- **Response**: 200 + full current state assembled from multiple cmdlets
- **If scope doesn't exist**: Return 404 (Crossplane interprets this as "resource doesn't exist" and triggers POST)
- **Assembly logic**:
  1. `Get-DhcpServerv4Scope -ScopeId {scope_id}` → scope params
  2. `Get-DhcpServerv4OptionValue -ScopeId {scope_id}` → DNS, gateway, domain
  3. `Get-DhcpServerv4ExclusionRange -ScopeId {scope_id}` → exclusion list
  4. `Get-DhcpServerv4Failover -ScopeId {scope_id}` → failover config (may not exist)
  5. Assemble into the canonical JSON shape (DhcpScopePayload model)

#### PUT `/scopes/{scope_id}`
- **Called when**: GET response differs from desired state in the Request CR
- **Request body**: Full desired scope configuration
- **Response**: 200 + full current state after applying changes
- **Internal diffing**: Do NOT blindly re-apply everything. Compare desired vs. current state section by section:
  - Scope params changed (name, lease, description) → `Set-DhcpServerv4Scope`
  - Options changed (gateway, DNS, domain) → `Set-DhcpServerv4OptionValue`
  - Exclusions changed → remove old exclusions, add new ones
  - Failover changed → `Set-DhcpServerv4Failover` or add/remove failover
- After applying changes: `Invoke-DhcpServerv4FailoverReplication` if failover is configured
- Return updated state via GET logic

#### DELETE `/scopes/{scope_id}`
- **Called when**: Crossplane Request CR is deleted (cluster torn down)
- **Response**: 204 No Content
- **If scope doesn't exist**: Return 204 anyway (idempotent)
- **Execution order (REVERSE of create — ordering is critical)**:
  1. Remove from failover: `Remove-DhcpServerv4FailoverScope -Name "{name}" -ScopeId {scope_id} -Force`
     - If this is the LAST scope in the relationship, also: `Remove-DhcpServerv4Failover -Name "{name}" -Force`
  2. Remove all exclusion ranges: `Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} -StartRange ... -EndRange ...`
  3. Remove scope: `Remove-DhcpServerv4Scope -ScopeId {scope_id} -Force`
     - This implicitly removes scope options

---

## Data models (Pydantic v2)

### DhcpExclusion
```python
class DhcpExclusion(BaseModel):
    startAddress: str    # IPv4 address, e.g. "10.20.30.1"
    endAddress: str      # IPv4 address, e.g. "10.20.30.10"
```

### DhcpFailover
```python
class DhcpFailover(BaseModel):
    partnerServer: str                # FQDN, e.g. "dhcp02.lab.local"
    relationshipName: str             # e.g. "mce1-failover"
    mode: Literal["HotStandby", "LoadBalance"]
    serverRole: Literal["Active", "Standby"]  # Only relevant for HotStandby
    reservePercent: int               # 0-100, only relevant for HotStandby
    loadBalancePercent: int           # 0-100, only relevant for LoadBalance
    maxClientLeadTimeMinutes: int     # Typically 60
    sharedSecret: Optional[str]       # null = no authentication
```

### DhcpScopePayload (the canonical shape — used for POST body, PUT body, AND GET response)
```python
class DhcpScopePayload(BaseModel):
    scopeName: str                    # Display name
    network: str                      # Network address (scope ID), e.g. "10.20.30.0"
    subnetMask: str                   # e.g. "255.255.255.0"
    startRange: str                   # First IP in range
    endRange: str                     # Last IP in range
    leaseDurationDays: int            # Converted to TimeSpan for PowerShell
    description: str                  # Scope description
    gateway: str                      # Default gateway (router option)
    dnsServers: list[str]             # Ordered list of DNS server IPs
    dnsDomain: str                    # DNS domain suffix
    exclusions: list[DhcpExclusion]   # Sorted by startAddress
    failover: Optional[DhcpFailover]  # null = no failover configured
```

**CRITICAL**: This single model defines the contract between Crossplane and this API. The GET endpoint must return JSON that is byte-for-byte comparable to the PUT/POST body. Use `model.model_dump(mode="json")` consistently. Sort exclusions by `startAddress` in both GET assembly and any comparison logic.

---

## Critical constraint: GET/PUT shape parity

**This is the #1 cause of failure in Crossplane provider-http integrations.**

Crossplane provider-http detects drift by comparing the GET response body with the PUT request body. If they differ in ANY way — key ordering, data types, null handling, array ordering — Crossplane triggers a PUT on every single reconciliation cycle (every ~1 minute), creating an infinite update loop.

Rules:
1. GET and PUT MUST use the exact same Pydantic response model
2. All JSON keys must be in the same order (use Pydantic model field ordering)
3. Data types must match exactly: `8` (int) ≠ `"8"` (string)
4. Empty arrays must be `[]`, not `null`
5. `null` failover must be `null`, not `{}` or omitted
6. DNS servers array must maintain order
7. Exclusions array must be sorted by `startAddress` consistently

---

## PowerShell execution layer

### Design principles

1. **Wrap every cmdlet call in a helper function** that handles:
   - Building the PowerShell command string
   - Executing via `subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=30)`
   - Parsing stdout (PowerShell objects → JSON via `ConvertTo-Json`)
   - Handling stderr and non-zero return codes
   - Logging the command and result

2. **Always use `-ErrorAction Stop`** on PowerShell cmdlets so errors become exceptions (non-zero exit code) rather than silent failures.

3. **Always use `ConvertTo-Json -Depth 5`** when returning data from GET cmdlets to ensure nested objects serialize properly.

4. **Always use `-Force`** on destructive operations (Remove-*) to suppress confirmation prompts.

### PowerShell helper pattern

```python
import subprocess
import json
import logging

logger = logging.getLogger(__name__)

class PowerShellError(Exception):
    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"PowerShell command failed (rc={returncode}): {stderr}")

def run_ps(command: str, parse_json: bool = True) -> dict | list | None:
    """Execute a PowerShell command and optionally parse JSON output."""
    full_cmd = f"{command} -ErrorAction Stop"
    if parse_json:
        full_cmd += " | ConvertTo-Json -Depth 5 -Compress"

    logger.info(f"PS> {command}")

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_cmd],
        capture_output=True,
        text=True,
        timeout=60
    )

    if result.returncode != 0:
        logger.error(f"PS FAILED: {result.stderr.strip()}")
        raise PowerShellError(command, result.stderr.strip(), result.returncode)

    if not parse_json or not result.stdout.strip():
        return None

    return json.loads(result.stdout)
```

### Cmdlet reference table

**Scope management:**
| Action | Cmdlet | Key parameters |
|--------|--------|----------------|
| Create scope | `Add-DhcpServerv4Scope` | `-Name`, `-StartRange`, `-EndRange`, `-SubnetMask`, `-State Active`, `-LeaseDuration (New-TimeSpan -Days N)`, `-Description` |
| Get scope | `Get-DhcpServerv4Scope -ScopeId {id}` | Returns: Name, SubnetMask, StartRange, EndRange, LeaseDuration, State, Description |
| Update scope | `Set-DhcpServerv4Scope -ScopeId {id}` | Same params as Add, but for existing scope |
| Delete scope | `Remove-DhcpServerv4Scope -ScopeId {id} -Force` | Implicitly removes options |

**Scope options (DNS, gateway):**
| Action | Cmdlet | Key parameters |
|--------|--------|----------------|
| Set options | `Set-DhcpServerv4OptionValue -ScopeId {id}` | `-Router {gateway}`, `-DnsServer {ip1},{ip2}`, `-DnsDomain {domain}` |
| Get options | `Get-DhcpServerv4OptionValue -ScopeId {id}` | Returns option IDs: 3=Router, 6=DNS, 15=Domain |

**Important**: `Get-DhcpServerv4OptionValue` returns options by OptionId:
- OptionId 3 = Router (gateway)
- OptionId 6 = DNS Server
- OptionId 15 = DNS Domain Name

Parse the output to extract these specific option values.

**Exclusion ranges:**
| Action | Cmdlet | Key parameters |
|--------|--------|----------------|
| Add exclusion | `Add-DhcpServerv4ExclusionRange -ScopeId {id}` | `-StartRange`, `-EndRange` |
| Get exclusions | `Get-DhcpServerv4ExclusionRange -ScopeId {id}` | Returns array of StartRange/EndRange |
| Remove exclusion | `Remove-DhcpServerv4ExclusionRange -ScopeId {id}` | `-StartRange`, `-EndRange` |

**Failover:**
| Action | Cmdlet | Key parameters |
|--------|--------|----------------|
| Create relationship | `Add-DhcpServerv4Failover` | `-Name`, `-PartnerServer`, `-ScopeId`, `-ServerRole`, `-ReservePercent` (HotStandby) or `-LoadBalancePercent` (LoadBalance), `-MaxClientLeadTime (New-TimeSpan -Minutes N)`, `-SharedSecret` (optional), `-Force` |
| Add scope to existing | `Add-DhcpServerv4FailoverScope` | `-Name {relationshipName}`, `-ScopeId {id}` |
| Get failover for scope | `Get-DhcpServerv4Failover -ScopeId {id}` | Returns relationship config. May return error if scope has no failover. |
| Update failover | `Set-DhcpServerv4Failover -Name {name}` | Same params as Add (minus ScopeId and PartnerServer) |
| Remove scope from failover | `Remove-DhcpServerv4FailoverScope -Name {name} -ScopeId {id} -Force` | Detaches scope, doesn't delete relationship |
| Delete relationship | `Remove-DhcpServerv4Failover -Name {name} -Force` | Only if no scopes remain |
| Replicate to partner | `Invoke-DhcpServerv4FailoverReplication -ScopeId {id} -Force` | MUST call after any failover change |

---

## GET response assembly logic (detailed)

This is the most critical function. It must produce JSON that exactly matches the Pydantic model.

```python
async def assemble_scope_state(scope_id: str) -> DhcpScopePayload:
    """
    Query Windows DHCP via multiple PowerShell cmdlets and assemble
    into the canonical DhcpScopePayload shape.
    """

    # 1. Get scope basic info
    scope = run_ps(f'Get-DhcpServerv4Scope -ScopeId {scope_id}')
    # scope contains: Name, SubnetMask, StartRange, EndRange, LeaseDuration, Description, State
    # LeaseDuration is a TimeSpan string like "8.00:00:00" → parse to days

    # 2. Get scope options
    options = run_ps(f'Get-DhcpServerv4OptionValue -ScopeId {scope_id}')
    # options is an array of objects with OptionId, Value, Name
    # Parse: OptionId 3 → gateway, OptionId 6 → dnsServers, OptionId 15 → dnsDomain

    # 3. Get exclusion ranges
    exclusions_raw = run_ps(f'Get-DhcpServerv4ExclusionRange -ScopeId {scope_id}')
    # May return null/empty if no exclusions. Handle gracefully.
    # Returns array of {StartRange, EndRange}
    # MUST sort by StartRange for consistent comparison

    # 4. Get failover (may not exist)
    failover = None
    try:
        failover_raw = run_ps(f'Get-DhcpServerv4Failover -ScopeId {scope_id}')
        # Parse into DhcpFailover model
        # MaxClientLeadTime is a TimeSpan → parse to minutes
    except PowerShellError:
        # Scope has no failover configured — this is normal, not an error
        failover = None

    # 5. Assemble into model
    # IMPORTANT: Parse LeaseDuration TimeSpan "8.00:00:00" → int 8
    # IMPORTANT: Sort exclusions by startAddress
    # IMPORTANT: dnsServers must maintain order from OptionId 6

    return DhcpScopePayload(
        scopeName=scope["Name"],
        network=scope_id,
        subnetMask=scope["SubnetMask"],  # May need .IPAddressToString or similar
        startRange=scope["StartRange"],
        endRange=scope["EndRange"],
        leaseDurationDays=parse_timespan_to_days(scope["LeaseDuration"]),
        description=scope.get("Description", ""),
        gateway=extract_option(options, 3),
        dnsServers=extract_option_list(options, 6),
        dnsDomain=extract_option(options, 15),
        exclusions=sorted(
            [DhcpExclusion(startAddress=e["StartRange"], endAddress=e["EndRange"])
             for e in (exclusions_raw or [])],
            key=lambda x: ip_to_int(x.startAddress)
        ),
        failover=parse_failover(failover_raw) if failover_raw else None
    )
```

### PowerShell output parsing gotchas

1. **IP addresses**: PowerShell returns IP addresses as `IPAddress` objects. When converted to JSON, they serialize as strings like `"10.20.30.0"`. Verify this is consistent.

2. **TimeSpan parsing**: `LeaseDuration` from `Get-DhcpServerv4Scope` returns as a TimeSpan string `"8.00:00:00"` (8 days). Parse with: `int(timespan_str.split(".")[0])` for the days component. `MaxClientLeadTime` from failover returns as `"1:00:00"` (1 hour = 60 minutes). Parse appropriately.

3. **Single vs. array returns**: When PowerShell returns a single object (e.g., one exclusion range), `ConvertTo-Json` outputs a JSON object, not an array. When it returns multiple, it outputs an array. **Always normalize to a list** in Python:
   ```python
   if isinstance(result, dict):
       result = [result]
   ```

4. **Option values**: `Get-DhcpServerv4OptionValue` returns option values in a `Value` array property. DNS servers (OptionId 6) will have multiple values in the array. The router (OptionId 3) will have one value. The domain (OptionId 15) will have one string value.

5. **Null/empty handling**: If a scope has no exclusions, `Get-DhcpServerv4ExclusionRange` may return empty output or an error. Handle both. If a scope has no failover, `Get-DhcpServerv4Failover -ScopeId` will throw an error. Catch it and set failover to `None`.

---

## PUT diff logic (detailed)

The PUT endpoint must NOT blindly re-apply the entire config. It must diff desired vs. current and only run cmdlets for what changed. This minimizes unnecessary DHCP server operations and avoids disrupting active leases.

```python
async def update_scope(scope_id: str, desired: DhcpScopePayload) -> DhcpScopePayload:
    current = await assemble_scope_state(scope_id)

    # 1. Scope params diff
    if (current.scopeName != desired.scopeName or
        current.leaseDurationDays != desired.leaseDurationDays or
        current.description != desired.description):
        run_ps(f'Set-DhcpServerv4Scope -ScopeId {scope_id} '
               f'-Name "{desired.scopeName}" '
               f'-LeaseDuration (New-TimeSpan -Days {desired.leaseDurationDays}) '
               f'-Description "{desired.description}"')

    # 2. Options diff
    if (current.gateway != desired.gateway or
        current.dnsServers != desired.dnsServers or
        current.dnsDomain != desired.dnsDomain):
        dns_str = ",".join(desired.dnsServers)
        run_ps(f'Set-DhcpServerv4OptionValue -ScopeId {scope_id} '
               f'-Router {desired.gateway} '
               f'-DnsServer {dns_str} '
               f'-DnsDomain "{desired.dnsDomain}"')

    # 3. Exclusions diff (set-based comparison)
    current_excl = {(e.startAddress, e.endAddress) for e in current.exclusions}
    desired_excl = {(e.startAddress, e.endAddress) for e in desired.exclusions}

    for start, end in (current_excl - desired_excl):  # Remove old
        run_ps(f'Remove-DhcpServerv4ExclusionRange -ScopeId {scope_id} '
               f'-StartRange {start} -EndRange {end}')

    for start, end in (desired_excl - current_excl):  # Add new
        run_ps(f'Add-DhcpServerv4ExclusionRange -ScopeId {scope_id} '
               f'-StartRange {start} -EndRange {end}')

    # 4. Failover diff (most complex)
    # Cases:
    # a) current=None, desired=None → no-op
    # b) current=None, desired=config → add failover
    # c) current=config, desired=None → remove failover
    # d) current=config, desired=config → compare and update
    handle_failover_diff(scope_id, current.failover, desired.failover)

    # Return fresh state
    return await assemble_scope_state(scope_id)
```

### Failover diff edge cases

```python
def handle_failover_diff(scope_id: str, current: Optional[DhcpFailover], desired: Optional[DhcpFailover]):
    if current is None and desired is None:
        return  # No-op

    if current is None and desired is not None:
        # Add failover
        existing = None
        try:
            existing = run_ps(f'Get-DhcpServerv4Failover -Name "{desired.relationshipName}"')
        except PowerShellError:
            pass

        if existing:
            # Relationship exists, just add this scope
            run_ps(f'Add-DhcpServerv4FailoverScope -Name "{desired.relationshipName}" -ScopeId {scope_id}')
        else:
            # Create new relationship
            create_failover(scope_id, desired)

        run_ps(f'Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force')
        return

    if current is not None and desired is None:
        # Remove from failover
        run_ps(f'Remove-DhcpServerv4FailoverScope -Name "{current.relationshipName}" -ScopeId {scope_id} -Force')
        # Check if relationship is now empty
        try:
            rel = run_ps(f'Get-DhcpServerv4Failover -Name "{current.relationshipName}"')
            # If ScopeId is empty, delete the relationship
            if not rel.get("ScopeId"):
                run_ps(f'Remove-DhcpServerv4Failover -Name "{current.relationshipName}" -Force')
        except PowerShellError:
            pass
        return

    # Both exist — compare params
    if (current.mode != desired.mode or
        current.reservePercent != desired.reservePercent or
        current.loadBalancePercent != desired.loadBalancePercent or
        current.maxClientLeadTimeMinutes != desired.maxClientLeadTimeMinutes):
        update_failover_params(desired)
        run_ps(f'Invoke-DhcpServerv4FailoverReplication -ScopeId {scope_id} -Force')
```

---

## Error handling

### HTTP status codes

| Scenario | Status | Body |
|----------|--------|------|
| Scope created successfully | 200 | Full scope state |
| POST but scope already exists (idempotent) | 200 | Current scope state |
| GET scope found | 200 | Full scope state |
| GET scope not found | 404 | `{"detail": "Scope 10.20.30.0 not found"}` |
| PUT successful | 200 | Updated scope state |
| PUT scope not found | 404 | `{"detail": "Scope 10.20.30.0 not found"}` |
| DELETE successful | 204 | Empty |
| DELETE scope not found (idempotent) | 204 | Empty |
| PowerShell cmdlet failed | 500 | `{"detail": "Failed to execute ...", "ps_error": "..."}` |
| Invalid request body | 422 | Pydantic validation error |

Register a global exception handler for `PowerShellError` that returns 500 with the stderr message. This helps with debugging from Crossplane events.

---

## Security

- Bearer token auth via `Authorization` header — configured via env var `DHCP_API_TOKEN`
- The `sharedSecret` for failover comes from a Kubernetes Secret injected into the Request CR. The API receives it in the payload and passes it to PowerShell. **Never log it.**
- PowerShell runs as the service account — needs DHCP Server admin rights
- TLS should be terminated at a reverse proxy

---

## Logging

- Log every PowerShell command executed (INFO level)
- Log every PowerShell command result (DEBUG level)
- Log every HTTP request received (INFO level)
- Log diff results on PUT: what changed and what was skipped (INFO level)
- Use structured logging (JSON format) for easier parsing

---

## Testing strategy

1. **Unit tests**: Test Pydantic model serialization, TimeSpan parsing, IP sorting, diff logic — all without PowerShell
2. **Mock tests**: Test endpoints with mocked `run_ps()` function returning sample PowerShell outputs
3. **Integration tests**: Test against an actual DHCP server (manual, not CI)

### Critical test: GET/PUT roundtrip

Write a test that:
1. Creates a `DhcpScopePayload` with known values
2. Serializes it to JSON (simulating what Helm/Crossplane sends as PUT body)
3. Simulates the GET assembly from mock PowerShell output
4. Asserts the two JSON strings are byte-for-byte identical

This catches type mismatches, key ordering issues, and null handling differences.

---

## Helm Chart (GitOps Layer)

The `helm/dhcp-scope/` chart takes a `values.yaml` with one or more DHCP scope definitions and renders Crossplane `Request` CRs. Each scope becomes one `Request` CR with GET/POST/PUT/DELETE method blocks.

Example `values.yaml`:
```yaml
scopes:
  - network: "10.20.30.0"
    scopeName: "Cluster-A Management"
    subnetMask: "255.255.255.0"
    startRange: "10.20.30.100"
    endRange: "10.20.30.200"
    leaseDurationDays: 8
    description: "Cluster A management network"
    gateway: "10.20.30.1"
    dnsServers: ["10.0.0.53", "10.0.0.54"]
    dnsDomain: "lab.local"
    exclusions:
      - startAddress: "10.20.30.1"
        endAddress: "10.20.30.99"
    failover: null

apiServer:
  url: "http://dhcp-api.example.com"
  secretRef:
    name: "dhcp-api-credentials"
    namespace: "crossplane-system"
```

Crossplane `Request` CR structure (rendered output):
```yaml
apiVersion: http.crossplane.io/v1alpha2
kind: Request
metadata:
  name: dhcp-scope-10-20-30-0
spec:
  forProvider:
    url: "http://dhcp-api.example.com/api/v1/scopes/10.20.30.0"
    headers:
      Authorization:
        secretKeyRef:
          name: dhcp-api-credentials
          namespace: crossplane-system
          key: token
    body: |
      { "scopeName": "...", "network": "10.20.30.0", ... }
    method: GET   # observe
  # POST/PUT/DELETE methods configured per Crossplane provider-http spec
```

---

## Phase 1 deliverables

1. Pydantic models (`models.py`)
2. PowerShell executor (`ps_executor.py`)
3. PowerShell output parsers (`ps_parsers.py`) with unit tests
4. GET endpoint + assemble_scope_state logic
5. POST endpoint (idempotent create)
6. PUT endpoint with diff logic
7. DELETE endpoint with reverse-order teardown
8. GET/PUT roundtrip test
9. Helm chart for Crossplane Request CRs
10. Example values.yaml

## Phase 2 (after Phase 1 works end-to-end)

1. Bearer token authentication middleware
2. Health check endpoint (`GET /healthz` — verifies PowerShell + DHCP module available)
3. Structured JSON logging
4. Retry logic for transient PowerShell failures
5. Rate limiting (Crossplane already rate-limits, but defense in depth)
