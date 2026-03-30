# DHCP Backend With Crossplane

A production-oriented FastAPI service for managing Windows DHCP IPv4 scopes through PowerShell, designed for GitOps reconciliation with Crossplane `provider-http`.

## What This Project Does

This repository connects declarative cluster configuration to real DHCP server state:

1. Git stores desired DHCP configuration.
2. Helm renders a Crossplane `Request` resource.
3. Crossplane reconciles by calling this API (`GET`/`POST`/`PUT`/`DELETE`).
4. The API executes Windows DHCP PowerShell cmdlets.
5. Current DHCP state is normalized back into a canonical API shape.

The implementation focuses on:

- Idempotent create/delete behavior
- Deterministic serialization for stable reconciliation
- Strict schema and subnet validation
- Safe PowerShell execution and environment gating

## Architecture

```text
Git desired state
  -> Helm templates
    -> Crossplane provider-http Request
      -> FastAPI DHCP backend
        -> PowerShell cmdlets
          -> Windows DHCP server
```

## Repository Layout

```text
app/
  main.py                    FastAPI app bootstrap
  config.py                  Env-based settings
  logging_config.py          JSON logging config
  exception_handlers.py      Global API exception mapping
  models.py                  Pydantic request/response models
  routers/
    scopes.py                DHCP scope CRUD endpoints
    health.py                Runtime health endpoint
  services/
    dhcp_env.py              Runtime capability validator (OS/PS/cmdlets)
    ps_executor.py           PowerShell command runner
    ps_parsers.py            Parse/normalize PowerShell output
    scope_service.py         Core scope lifecycle logic
  utils/
    ip_utils.py              IP + TimeSpan parsing helpers

helm/hosted-cluster-integration/
  Chart.yaml
  values.yaml
  templates/
    dhcp-scope-request.yaml  Crossplane provider-http Request template
    _dhcp-helpers.tpl        Canonical payload rendering helpers

tests/
  test_endpoints.py
  test_models.py
  test_validation.py
  test_parsers.py
  test_diff.py
  test_dhcp_env.py
  test_parity.py
```

## Runtime Requirements

- Python 3.12+
- Windows host (native Windows, not Linux/macOS/WSL)
- `powershell.exe` available on PATH
- DHCP PowerShell cmdlets (`Get-DhcpServerv4Scope`, etc.)
  - Windows Server: DHCP Server role/tools
  - Windows client: RSAT DHCP tools

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Environment variables:

- `DHCP_API_TOKEN` (default: empty, auth disabled)
- `HOST` (default: `0.0.0.0`)
- `PORT` (default: `8080`)
- `LOG_LEVEL` (default: `INFO`)

You can also use a `.env` file in the repo root.

## Run the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

or:

```bash
python app/main.py
```

## API Endpoints

Base path: `/api/v1`

- `GET /scopes` - List all scopes (canonical payload list)
- `POST /scopes/{scope_id}` - Create/ensure scope (idempotent); used by Crossplane for all lifecycle operations
- `GET /scopes/{scope_id}` - Get canonical current state
- `PUT /scopes/{scope_id}` - Diff-based update
- `DELETE /scopes/{scope_id}` - Delete scope (idempotent)
- `GET /healthz` - Runtime capability check

## Canonical Payload Shape

```json
{
  "scopeName": "cluster-a-workers",
  "network": "10.20.30.0",
  "subnetMask": "255.255.255.0",
  "startRange": "10.20.30.50",
  "endRange": "10.20.30.200",
  "leaseDurationDays": 8,
  "description": "optional text",
  "gateway": "10.20.30.1",
  "dnsServers": ["10.10.1.5", "10.10.1.6"],
  "dnsDomain": "lab.local",
  "exclusions": [{ "startAddress": "10.20.30.1", "endAddress": "10.20.30.10" }],
  "failover": null
}
```

Notes:

- Field order is intentional and tested for parity.
- `failover` is either `null` or a full object.
- Exclusions are returned sorted by IP.
- DNS server order is preserved (primary/secondary semantics).

## Failover Model

Supported modes:

- `HotStandby`
- `LoadBalance`

Normalization rules in model validation:

- `HotStandby`: `serverRole` required, `loadBalancePercent` normalized to `0`
- `LoadBalance`: `loadBalancePercent` required, `serverRole` normalized to `Active`, `reservePercent` normalized to `0`

This prevents GET/PUT drift when Helm includes unused mode fields.

## Crossplane + Helm Integration

The chart under `helm/hosted-cluster-integration` renders a Crossplane `Request` resource that maps:

- `POST` -> `.../api/v1/scopes/{network}`
- `GET` -> `.../api/v1/scopes/{network}`
- `PUT` -> `.../api/v1/scopes/{network}`
- `DELETE` -> `.../api/v1/scopes/{network}`

Template behavior highlights:

- Authorization header can be injected from a Kubernetes secret.
- Failover mode-specific fields are normalized at template render.
- Payload is emitted in canonical API field order.

Render example:

```bash
helm template dhcp-request ./helm/hosted-cluster-integration -f ./helm/hosted-cluster-integration/values.yaml
```

## Reconciliation Contract (Important)

Crossplane repeatedly compares desired payload with `GET` response. Any mismatch can trigger repeated `PUT`s.

To keep reconciliation stable:

- Canonical GET shape must match desired PUT body
- No hidden API defaults
- Deterministic ordering (especially exclusions and field order)
- Path/body scope identity must match on mutating scope endpoints

**Exclusion ordering:** The API always returns exclusions sorted by IP (ascending). Your `values.yaml`
exclusions **must** be listed in ascending IP numerical order. If they are not, Crossplane will
detect a mismatch on every GET and issue a PUT indefinitely.

**Removing failover with layered values files:** When using multiple `-f` values files (e.g.
site defaults + cluster override), use `failover: null` to remove an inherited failover config.
Using `failover: {}` does **not** remove it — Helm deep-merges the empty map with the base map,
leaving the original failover object intact. Only `null` replaces the key.

## Security and Safety

- Optional bearer token auth (`DHCP_API_TOKEN`)
- Runtime environment validation before DHCP operations
- `-ErrorAction Stop` on PowerShell commands
- Sensitive command arguments (shared secrets) redacted in logs
- PowerShell stderr sanitized before returning to clients
- Structured JSON logs

## Testing

Run:

```bash
pytest
```

Test coverage includes:

- Endpoint behavior and status contracts
- Pydantic schema and subnet/failover validation
- PowerShell parsing and deterministic normalization
- Diff-based update semantics
- Runtime environment guards
- GET/PUT parity contract and drift-prevention scenarios

## Operational Notes

- This service is intended to run on a Windows environment with DHCP cmdlets.
- Linux/macOS/WSL requests to scope endpoints are rejected with structured `503` reasons.
- `healthz` remains callable even when DHCP runtime prerequisites are missing.
- Deletion is fail-safe around failover detach to avoid orphaned relationship drift.

## License

No license file is currently present in this repository.
