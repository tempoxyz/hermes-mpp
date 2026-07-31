# hermes-mpp

`hermes-mpp` makes ordinary HTTPX requests from
[Hermes Agent](https://hermes-agent.nousresearch.com) payment-aware. When an
allowed origin responds with an MPP `402 Payment Required` challenge, the
plugin creates a Tempo credential and retries the request. It does not expose
payment tools to the model.

## Status

This repository is private and experimental. It currently targets Hermes Agent
0.19, HTTPX 0.27–0.28, and Tempo charge payments.

The temporary `tool.uv.sources` pin tracks the pympp changes under review in
[`tempoxyz/pympp#201`](https://github.com/tempoxyz/pympp/pull/201) and
[`tempoxyz/pympp#202`](https://github.com/tempoxyz/pympp/pull/202). Remove it
once those changes ship in pympp 0.10.

## Install

Install the package into the same Python environment as Hermes:

```sh
uv pip uninstall mpp-hermes
uv pip install \
  "pympp[tempo] @ git+https://github.com/tempoxyz/pympp.git@4d47c66667c7dbf69fc3870f3c67cefcc6ba4648"
uv pip install --no-deps \
  "hermes-mpp @ git+ssh://git@github.com/tempoxyz/hermes-mpp.git"
hermes plugins enable mpp
```

The two-step install is temporary: the source commit still reports pympp
0.9.1, while this package correctly declares the forthcoming 0.10 API. Once
pympp 0.10 is released, a normal one-line plugin install will resolve it.
The uninstall prevents the repository's former `mpp-hermes` distribution from
leaving a second plugin registered under the same `mpp` activation key.

Restart Hermes after installation or configuration changes.

## Configure

```sh
export TEMPO_PRIVATE_KEY=0x...
export MPP_ALLOWED_ORIGINS=https://mpp.dev,https://api.example.com
export TEMPO_RPC_URL=https://rpc.moderato.tempo.xyz
```

`MPP_ALLOWED_ORIGINS` is required and accepts exact HTTP or HTTPS origins only.
Paths, queries, fragments, and wildcard hosts are rejected. `TEMPO_RPC_URL` is
optional. The example uses the Tempo Moderato testnet because `mpp.dev`
currently challenges there; fund the dedicated key with testnet tokens.
Use plaintext HTTP only for trusted local development: an on-path attacker can
otherwise inject a payment challenge.

Use a dedicated, low-balance key. Installation does not activate the plugin:
Hermes requires the explicit `hermes plugins enable mpp` step.
Allowlisting an origin authorizes automatic Tempo charge payments from that
origin; this initial version has no separate spend cap or approval prompt.

## Behavior

- Existing and future sync and async HTTPX clients are instrumented.
- Requests outside the origin allowlist pass through untouched.
- Unsupported or malformed challenges remain ordinary `402` responses.
- A paid retry is attempted at most once.
- Concurrent, repeated, and uncertain payment outcomes fail closed to avoid
  accidental double payment.
- Instrumentation is removed at process exit and can be closed explicitly in
  tests.

HTTPX is one Python transport seam. This plugin does not instrument Requests,
aiohttp, urllib3, MCP, or arbitrary subprocess traffic.

Every payment attempt receives a fresh pympp runtime and method. Async callers
stay on their event loop; sync callers bridge only pympp's async credential and
event work. Runtime factories must not reuse loop-bound method instances.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check .
```

Hermes Agent currently supports Python 3.11–3.13, so CI tests those versions.
