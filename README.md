# hermes-mpp

`hermes-mpp` makes [Hermes Agent](https://hermes-agent.nousresearch.com) HTTPX
and MCP traffic payment-aware. It answers supported MPP payment challenges with
a Tempo charge and retries the request through the same client or MCP session.

This version supports Hermes Agent 0.19.0 and 0.20.0, HTTPX 0.27–0.28, and
pympp 0.10. MCP instrumentation is fail-closed on any other Hermes version
because it wraps a private Hermes lifecycle seam.

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

The allowlist also applies to HTTP and SSE MCP server URLs. Stdio MCP servers
have no network origin to allowlist; configure only trusted commands.

## Use

Ask Hermes for the resource normally:

```sh
hermes chat -q "Make a request to https://mpp.dev/api/ping/paid"
```

The model gets one generic `mpp_fetch` tool for arbitrary HTTP APIs. Payment is
not a separate tool call: it and every other in-process HTTPX client handle
supported MPP challenges automatically.

Paid MCP tools work through their normal Hermes names too:

```text
mcp__analytics__premium_report
```

No `mpp_fetch` call or payment-specific MCP tool is required. Hermes-MPP wraps
every newly initialized MCP session before tool discovery, including sessions
created after reconnect.

For HTTP MCP servers, the expected payment realm defaults to the configured URL
authority. For stdio servers, it defaults to the server's config key. Override
the realm or disable automatic payment for one server in Hermes config:

```yaml
mcp_servers:
  analytics:
    url: https://mcp.example.com/mcp
    mpp:
      realm: mcp.example.com
      auto_pay: true
  local_unpaid:
    command: python
    args: [server.py]
    mpp:
      auto_pay: false
```

Hermes-MPP requires the challenge realm to match this configured identity and,
for network transports, requires the configured origin to pass
`MPP_ALLOWED_ORIGINS` when that allowlist is set.

## Behavior

- Existing and future sync and async clients retain their transport, pool,
  cookies, hooks, redirects, extensions, and streaming behavior. Response hooks
  observe the final logical response rather than the internal `402`.
- Free HTTP responses pass through. Malformed, unsupported, and disallowed
  HTTP challenges remain ordinary `402` responses.
- Free MCP tools retain their content, structured content, images, resources,
  and existing error rendering. Paid MCP receipts are added to structured
  content under `org.paymentauth/receipt` without replacing normal output;
  denied MCP challenges fail before any credential is created.
- A paid HTTP request is retried at most once. Distinct HTTP payments are
  serialized; equivalent, repeated, and uncertain attempts fail closed. A paid
  MCP call also retries exactly once, and uncertainty blocks later payments to
  that configured server across reconnects.
- `mpp_fetch` blocks private-network redirects, hides sensitive response headers,
  and truncates large bodies.
- General HTTP payment interception covers HTTPX traffic in the Hermes process;
  configured MCP sessions are covered separately. Shell commands and Requests,
  aiohttp, or urllib3 are not; use `mpp_fetch` for arbitrary HTTP.

If a payment outcome is uncertain, verify the wallet transaction before
restarting Hermes. HTTP payments remain blocked in that process; MCP payments
to the same configured server remain blocked across reconnects.

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

CI tests Python 3.11–3.13 and HTTPX 0.27–0.28. It also runs the suite against
a pinned Hermes main snapshot so private-seam compatibility drift is explicit.
