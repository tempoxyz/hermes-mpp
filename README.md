# hermes-mpp

`hermes-mpp` makes [Hermes Agent](https://hermes-agent.nousresearch.com) HTTPX
and Requests traffic payment-aware. It answers supported MPP `402` challenges
with a Tempo charge and retries the request through the same client.

V1 targets Hermes Agent 0.19 and the current 0.20 development branch, HTTPX
0.27–0.28, Requests 2.31–2.33, and pympp 0.10.

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

Existing Hermes tools and Python SDKs need no MPP-specific adapter. For example,
point Hermes's OpenAI-compatible model client at a keyless MPP endpoint once:

This live endpoint charges mainnet funds. Use a dedicated, low-balance wallet.

```sh
hermes config set model.provider custom
hermes config set model.base_url https://mpp.orthogonal.com/baseten/v1
hermes config set model.default openai/gpt-oss-120b
hermes chat -q "Reply with exactly: payment aware"
```

The unmodified OpenAI SDK sends its normal HTTPX call. The plugin handles a
supported `402` before the SDK sees it, pays once, and returns the model
response. No provider API key or service-specific plugin code is involved.

The model also gets one generic `mpp_fetch` tool for arbitrary HTTP APIs. Payment
is not a separate tool call: it and in-process HTTPX and Requests clients handle
supported MPP challenges automatically.

## Behavior

- Existing and future HTTPX clients and Requests sessions retain their transport,
  pool, cookies, hooks, redirects, and request options. Response hooks observe
  the final logical response rather than the internal `402`.
- Free responses pass through. Malformed, unsupported, and disallowed
  challenges remain ordinary `402` responses.
- A paid request is retried at most once. Distinct payments are serialized;
  equivalent, repeated, and uncertain attempts fail closed.
- `mpp_fetch` blocks private-network redirects, hides sensitive response headers,
  and truncates large bodies.
- HTTPX and `requests.Session` calls in the Hermes process are instrumented.
  Direct urllib3, aiohttp, browser, subprocess, and shell traffic are not. A
  provider must still be configured well enough to issue its first request;
  transport instrumentation cannot bypass a provider's preflight credential
  gate.

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

CI tests Python 3.11–3.13, HTTPX 0.27–0.28, and Requests 2.31–2.33.
