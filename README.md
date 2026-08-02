# simplelogin-mcp

An HTTP [MCP](https://modelcontextprotocol.io) server exposing the alias
endpoints of the [SimpleLogin](https://simplelogin.io) API, gated by a
configurable permission level.

## Quick start

```bash
docker run -p 8000:8000 \
  -e SIMPLELOGIN_API_KEY=your-api-key \
  -e SIMPLELOGIN_PERMISSION_LEVEL=read \
  ghcr.io/samanthavbarron/simplelogin-mcp:latest
```

The MCP endpoint is served at `/mcp` over streamable HTTP; `/health` answers
liveness probes.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SIMPLELOGIN_API_KEY` | *(required)* | SimpleLogin API key. One account per deployment. |
| `SIMPLELOGIN_PERMISSION_LEVEL` | `read` | `read`, `create`, `update` or `delete`. |
| `SIMPLELOGIN_API_BASE_URL` | `https://app.simplelogin.io` | Override for self-hosted instances. |
| `SIMPLELOGIN_MAX_AUTO_PAGES` | `10` | Page cap when a list tool is called without `page_id`. |
| `SIMPLELOGIN_REQUEST_TIMEOUT` | `30` | Upstream request timeout, seconds. |
| `MCP_AUTH_TOKEN` | *(unset)* | If set, clients must send `Authorization: Bearer <token>`. |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | Listen address and endpoint path. |

An unrecognised permission level is rejected at startup rather than defaulted,
so a typo cannot silently grant a level you did not intend.

## Permission levels

Levels are cumulative — each includes those below it.

| Level | Adds |
| --- | --- |
| `read` | `get_alias_options`, `list_aliases`, `search_aliases`, `get_alias`, `get_alias_activities`, `list_alias_contacts`, `list_mailboxes` |
| `create` | `create_custom_alias`, `create_random_alias`, `create_alias_contact` |
| `update` | `update_alias`, `toggle_alias` |
| `delete` | *(nothing — see below)* |

Enforcement happens at two independent layers: tools above the configured level
are omitted from `tools/list`, **and** refused by the call handler. A client
that hard-codes or guesses a hidden tool name gains nothing, and the refusal
happens before any request reaches SimpleLogin.

### Why `delete` grants nothing

Alias deletion is not exposed. Deleting an alias is irreversible and permanently
reserves the address, which is a poor trade in an agent-driven context. Use
`toggle_alias` to disable an alias instead — it stops mail forwarding and is
reversible.

`DELETE /api/aliases/:id` was the only destructive endpoint in scope, so no tool
currently requires the `delete` level. The level remains defined so
configuration stays forward-compatible, and the test suite asserts that it
grants nothing beyond `update`. The underlying HTTP client has no delete method
at all, and tests verify that no operation at any level issues a `DELETE`
upstream.

## Tool notes

- **Pagination.** List tools accept an optional 0-based `page_id`. Supply it to
  fetch one page (20 items); omit it to auto-paginate up to
  `SIMPLELOGIN_MAX_AUTO_PAGES`. Responses carry `has_more`, so truncation is
  always visible.
- **Creating a custom alias** needs a `signed_suffix` from `get_alias_options`
  and `mailbox_ids` from `list_mailboxes`. The suffix is cryptographically
  signed and cannot be constructed by hand.
- **`list_mailboxes`** is a read-only addition outside the alias endpoint set,
  included because alias creation is unusable without it.
- **Contacts are premium-gated.** `create_alias_contact` returns SimpleLogin's
  upgrade message on free accounts.

## Development

```bash
uv sync --locked --dev
uv run pytest -m "not image and not live"   # offline: no network, no container
uv run pytest -m "image and not live"       # against the built container image
uv run pytest -m live                       # against the real SimpleLogin API
```

Offline tests run against a stateful in-memory fake modelled on real captured
API responses, so they need neither credentials nor network access.

### Live tests

Live tests need `SI_API_TEST_KEY` and run the built image against the real
service. They are shaped around two measured constraints:

- **Alias creation is heavily rate limited.** An exhausted window was observed
  still refusing after six minutes idle, while reads, `PATCH` and toggle were
  unaffected. The suite therefore shares one durable fixture alias across runs
  and only creates in the two tests that specifically exercise creation. Those
  skip rather than fail when throttled.
- **The account is shared and small.** Everything ephemeral is stamped with the
  run id and removed in a `finally` block. Only stamped aliases are ever
  deleted, so the account's own aliases are never at risk. Teardown uses a
  direct API client, never the server under test.

Deletion in the test harness is intentional and lives only there — see
`tests/e2e/live_harness.py`.

## CI

- Offline tests run on every push and pull request, including from forks.
- Image E2E builds and exercises the container on native `amd64` and `arm64`
  runners.
- Live E2E is **currently limited to manual `workflow_dispatch` runs** while the
  shared test account is throttled on alias creation, so it cannot block
  publishing. Trigger it by hand to check whether the throttle has lifted; see
  the comment in `.github/workflows/ci.yml` for how to re-enable automatic runs.
  When enabled it runs only where secrets are available (forked pull requests
  skip it) and is serialised by a concurrency group, since all runs share one
  account. `pull_request_target` is deliberately not used — it would expose
  secrets to untrusted contributor code.
- Images publish to GHCR as `latest` and `sha-<short>`, built per-architecture
  on native runners and merged into one manifest.

## License

MIT
