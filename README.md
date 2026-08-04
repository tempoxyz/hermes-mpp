# hermes-mpp

`hermes-mpp` makes [Hermes Agent](https://hermes-agent.nousresearch.com) HTTPX
traffic payment-aware. It answers supported MPP `402` challenges with a Tempo
charge and retries the request through the same client.

V1 targets Hermes Agent 0.19, HTTPX 0.27–0.28, and pympp 0.10.

## Install

After [installing Hermes](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart),
run:

```sh
uvx hermes-mpp install
```

The installer adds and enables the plugin, then prompts for a Tempo private key;
leave it blank to generate one. It stores the key in `~/.hermes/.env` with
owner-only permissions and prints the address to fund.

The installer discovers standard, root, and `PATH`-based Hermes installations.
For a custom environment, run `uvx hermes-mpp install --hermes-python PATH` or
set `HERMES_PYTHON`.

Generated wallets start empty. For testnet resources, fund the printed address
with the [Tempo faucet](https://docs.tempo.xyz/guide/use-accounts/add-funds).

Use a dedicated, low-balance key. By default, any origin presenting a valid MPP
challenge can charge it without a prompt or spend cap. To restrict payments,
set an exact, comma-separated allowlist:

```sh
MPP_ALLOWED_ORIGINS=https://mpp.dev,https://api.example.com
```

Paths and wildcards are rejected. Use plaintext `http://` only for trusted local
development; an on-path attacker can inject a payment challenge.

## Use

Ask Hermes for the resource normally:

```sh
hermes chat -q "Make a request to https://mpp.dev/api/ping/paid"
```

The model gets one generic `mpp_fetch` tool for arbitrary HTTP APIs. Payment is
not a separate tool call: it and every other in-process HTTPX client handle
supported MPP challenges automatically.

## Behavior

- Existing and future sync and async clients retain their transport, pool,
  cookies, hooks, redirects, extensions, and streaming behavior. Response hooks
  observe the final logical response rather than the internal `402`.
- Free responses pass through. Malformed, unsupported, and disallowed
  challenges remain ordinary `402` responses.
- A paid request is retried at most once. Distinct payments are serialized;
  equivalent, repeated, and uncertain attempts fail closed.
- `mpp_fetch` blocks private-network redirects, hides sensitive response headers,
  and truncates large bodies.
- Only HTTPX traffic in the Hermes process is instrumented. Shell commands and
  Requests, aiohttp, or urllib3 are not; use `mpp_fetch` for arbitrary HTTP.

If a payment outcome is uncertain, verify the wallet transaction before
restarting Hermes; later payments remain blocked in that process.

## Manage

```sh
hermes plugins list
uvx --refresh hermes-mpp install  # update or reconfigure
uvx hermes-mpp uninstall
```

Uninstalling leaves the private key in Hermes's `.env` file.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
```

CI tests Python 3.11–3.13 and HTTPX 0.27–0.28.
