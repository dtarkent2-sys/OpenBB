# OpenBB SharkQuant Flow Extension

Proxies the full SuperQuant analytics backend through `openbb.sharkquant.ai`.

**Why:** so your Workspace users only need to connect one backend (SharkQuant)
and still get equity flow, COT positioning, macro predictions, short-interest
factors, and security-master data.

**How:** at import time the extension reads SuperQuant's public widgets.json,
auto-generates one proxy route per upstream endpoint, and returns
`list[Data]` with the original shape preserved.

**Endpoints** land under `/api/v1/sharkquant_flow/{endpoint_name}` and show up
in Workspace automatically via OpenBB's widgets.json generation.

## Environment

Optional `SHARKQUANT_FLOW_UPSTREAM` env var lets you point the extension at
the XTech mirror (`analytics.openbb.x-tech.ai`) or a self-hosted cache
instead of the default SuperQuant backend.
