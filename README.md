# hermes-mpp

`hermes-mpp` makes ordinary in-process HTTPX requests from
[Hermes Agent](https://hermes-agent.nousresearch.com) payment-aware. When an
allowed origin returns an MPP `402`, the plugin signs a Tempo charge with a
local private key and retries through the same HTTPX client. It exposes no
model-facing payment tool.

V1 targets Hermes Agent 0.19, HTTPX 0.27–0.28, and pympp 0.10.

## Install

The standard Hermes installer creates a managed virtual environment. Install
the plugin there, then enable its entry point without granting tool overrides:

```sh
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
uv pip install --python "$HERMES_ROOT/venv/bin/python" \
  "hermes-mpp @ git+ssh://git@github.com/tempoxyz/hermes-mpp.git"
hermes plugins enable mpp --no-allow-tool-override
```

Restart Hermes after installation or configuration changes.

## Configure

```sh
export TEMPO_PRIVATE_KEY=0x...
export MPP_ALLOWED_ORIGINS=https://mpp.boutique
export TEMPO_RPC_URL=https://rpc.moderato.tempo.xyz
```

Use a dedicated, low-balance key. `MPP_ALLOWED_ORIGINS` accepts comma-separated,
exact HTTP or HTTPS origins; paths and wildcards are rejected. Allowlisting an
origin authorizes automatic charges from that origin with no separate spend cap
or approval prompt.

`TEMPO_RPC_URL` is optional in general, but the Moderato URL above is needed for
the MPP Boutique testnet charge.

## Smoke test MPP Boutique

Fund the key with Moderato pathUSD, export the variables above, then run:

```sh
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
"$HERMES_ROOT/venv/bin/python" - <<'PY'
from uuid import uuid4

import httpx
from hermes_cli.plugins import PluginManager

PluginManager().discover_and_load()
response = httpx.post(
    "https://mpp.boutique/api/buy?item=mpp-cap",
    json={"name": f"Hermes MPP {uuid4()}"},
    timeout=60,
)
response.raise_for_status()
print(response.json())
PY
```

The request starts as an ordinary HTTPX call. A successful result is a paid
`201` response for the Tempo Hat.

## Behavior

- Existing and future sync and async HTTPX clients are instrumented.
- Requests outside the origin allowlist pass through untouched.
- Unsupported or malformed challenges remain ordinary `402` responses.
- The original client's transport, connection pool, cookies, hooks, redirects,
  extensions, and streaming behavior are retained.
- A paid request is never retried more than once. Concurrent, repeated, and
  uncertain outcomes fail closed to avoid accidental double payment.
- Instrumentation is restored at process exit.

Only in-process HTTPX traffic is covered. Requests, aiohttp, urllib3, and
subprocess traffic are not instrumented.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
```

CI tests Python 3.11–3.13 and HTTPX 0.27–0.28.
