# hermes-mpp

`hermes-mpp` makes [Hermes Agent](https://hermes-agent.nousresearch.com) HTTP
requests payment-aware. When an endpoint returns an MPP `402`, the plugin signs
a Tempo charge with a local private key and retries the same request through the
same HTTPX client.

V1 targets Hermes Agent 0.19, HTTPX 0.27–0.28, and pympp 0.10.

## Install

After [installing Hermes](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart),
run:

```sh
uvx hermes-mpp install
```

The installer adds the plugin to Hermes's managed environment, enables it, and
prompts you to import a Tempo private key or generate one. The key is stored in
`~/.hermes/.env` with owner-only permissions, and the wallet address is printed
so you can fund it with the assets required by the services you use.

Use a dedicated, low-balance key. By default, any origin can charge it through
a valid MPP challenge without a separate prompt or spend cap. To restrict
automatic payments, set an exact, comma-separated allowlist:

```sh
MPP_ALLOWED_ORIGINS=https://mpp.dev,https://api.example.com
```

Paths and wildcards are rejected. The challenge's Tempo chain ID selects the
corresponding public RPC, so no RPC configuration or network switch is needed.
Use plaintext `http://` origins only for trusted local development: an on-path
attacker can inject a payment challenge into an unencrypted response.

## Use

Ask Hermes for the resource normally:

```sh
hermes chat -q "Make a request to https://mpp.dev/api/ping/paid"
```

The plugin exposes one generic `mpp_fetch` tool so the model can discover and
call arbitrary HTTP APIs. Payment is not a separate tool call: `mpp_fetch` and
all other in-process HTTPX clients automatically handle supported MPP `402`
challenges.

## Behavior

- Existing and future sync and async HTTPX clients are instrumented.
- With no `MPP_ALLOWED_ORIGINS`, valid MPP challenges from any origin may charge
  the configured wallet. When set, requests outside the allowlist pass through
  untouched.
- Unsupported or malformed challenges remain ordinary `402` responses.
- The original client's transport, connection pool, cookies, hooks, redirects,
  extensions, and streaming behavior are retained.
- A paid request is never retried more than once. Concurrent, repeated, and
  uncertain outcomes fail closed to avoid accidental double payment.
- Instrumentation is restored at process exit.

Only HTTPX traffic in the Hermes Python process can be instrumented. Shell
commands such as `curl`, and libraries such as Requests, aiohttp, and urllib3,
run outside this seam; Hermes should use `mpp_fetch` for arbitrary paid HTTP
calls.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
```

CI tests Python 3.11–3.13 and HTTPX 0.27–0.28.
