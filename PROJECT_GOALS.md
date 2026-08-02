# SimpleLogin MCP

Your task is as follows: Create an HTTP-based MCP server for (a limited subset of) the SimpleLogin API.

The SimpleLogin API docs are available here: https://github.com/simple-login/app/blob/master/docs/api.md

The only endpoints for the API that will be in scope are the alias endpoints. I.e. All of the following:

```
GET /api/v5/alias/options: Get alias options. Used by create alias process.
POST /api/v3/alias/custom/new: Create new alias.
POST /api/alias/random/new: Random an alias.
GET /api/v2/aliases: Get user's aliases.
GET /api/aliases/:alias_id: Get alias information.
DELETE /api/aliases/:alias_id: Delete an alias.   [EXCLUDED - see Design Decisions]
POST /api/aliases/:alias_id/toggle: Enable/disable an alias.
GET /api/aliases/:alias_id/activities: Get alias activities.
PATCH /api/aliases/:alias_id: Update alias information.
GET /api/aliases/:alias_id/contacts: Get alias contacts.
POST /api/aliases/:alias_id/contacts: Create a new contact for an alias.
```

There are credentials for an actual SimpleLogin account in `.env` for your testing. Note that this account exists solely for testing purposes, the account was just created and doesn't have anything in it yet.

# Requirements

- The MCP must have a setting to configure the permission level, each of which includes the levels below it.
    - The levels are:
        - Read: Read only for everything
        - Create: May create new entities
        - Update: May update existing entities
        - Delete: May delete entities.
    - For example, a deployment in "Create" mode is essentially append only, and can also read existing entities. Similarly, "Update" can read/create/update entities, but not delete anything, etc.
- The project must ultimately be tested and built in GH Actions.
- The result should be a container image published to GHCR which can be deployed to both amd64/arm64 hosts.
- Tests should be comprehensive and include complex e2e tests with multi-step user interactions, etc. Testing should also include that deployments in a specific permission level cannot perform actions that they were not intended to be able to perform, etc. E.g. A deployment in "Update" mode cannot delete entities, etc.
- Comprehensive E2E testing is critical, and should be applied to the built image itself.

# Design Decisions

Resolved during project scoping. These refine the requirements above; where they conflict, these win.

## Stack and packaging

- **Python + FastMCP**, streamable HTTP transport.
- **One tool per endpoint** (no consolidated action-parameter tools) — keeps the permission mapping unambiguous.
- **Base image:** `python:3.13-alpine`. Viable because the expected dependency set (mcp, pydantic, httpx, uvicorn, starlette) ships `musllinux` wheels for both architectures. If a dependency without a musl wheel is ever added, revisit — a source build on Alpine pulls in a full toolchain.
- **Multi-arch build:** native runners (`ubuntu-latest` + `ubuntu-24.04-arm`) in parallel, merged into a single manifest. Avoids QEMU emulation entirely.
- **GHCR tags:** `latest` and `sha-<short>`. No semver release process for now.
- **Health endpoint** exposed for container orchestration.

## Authentication

- **SimpleLogin API key:** server-side environment variable, single tenant. One key per deployment, matching the one-permission-level-per-deployment model.
- **MCP endpoint:** optional static bearer token via `MCP_AUTH_TOKEN`. If unset, no auth is enforced — deployments are expected to sit behind a proxy or on a private network.

## Permission levels

Hierarchy is unchanged: Read ⊂ Create ⊂ Update ⊂ Delete.

- Enforced at **two layers**: disallowed tools are omitted from `tools/list`, *and* the handler rejects them if called anyway. Both layers are tested independently — a client that guesses a hidden tool name must still be refused.
- `POST /api/aliases/:alias_id/toggle` requires **Update**. It mutates existing state and is reversible.
- Classification follows the *operation*, not the HTTP verb. `GET /api/v2/aliases` also accepts POST (for its search-query body); it remains **Read**.

### Delete tooling is excluded

`DELETE /api/aliases/:alias_id` is **not exposed as a tool**. Alias deletion is irreversible and permanently reserves the address — too much risk for too little benefit in an agent-driven context. Disabling via `toggle` (Update) covers the practical need to stop an alias receiving mail, non-destructively.

Consequences, accepted knowingly:

- It was the only delete operation in scope, so **no tool currently requires the Delete level**. The level stays defined in the enum so configuration and the hierarchy remain forward-compatible. The permission test suite asserts that Delete grants no tools beyond Update, rather than asserting a deletion that cannot happen.
- **Test teardown does not go through MCP.** The harness deletes fixtures by calling the SimpleLogin API directly, bypassing the server under test. Cleanup must not depend on the code being tested.

## Scope extensions

Read-only additions are permitted where a scoped endpoint is otherwise unusable.

- **`GET /api/v2/mailboxes`** (Read) — required because `POST /api/v3/alias/custom/new` takes `mailbox_ids` and `PATCH /api/aliases/:alias_id` takes `mailbox_id`/`mailbox_ids`, with no other way to discover valid IDs on an account with no existing aliases.
- Further gaps may be closed the same way: **strictly read-only**, or escalate for a decision.

## Tool behaviour

- **Pagination** (`page_id`, 20/page on aliases, activities, contacts): `page_id` is optional. When supplied the model pages explicitly; when omitted the server auto-paginates up to a cap. Responses report `has_more`.
- **`hostname`** exposed as an optional parameter on alias options and both create tools. It drives `prefix_suggestion` and the `recommendation` field, which surfaces an alias already used for that site.
- **Random alias `mode`** (`uuid`/`word`) is *not* exposed — inherit the account setting.

## Testing

E2E runs against the **live SimpleLogin API**, with these mitigations:

- **Concurrency group** serializing workflow runs so two runs cannot race on the shared account.
- **Run-scoped identifiers** on every created entity, with teardown in a `finally` block so a mid-test failure still cleans up.
- **Pre-flight quota check** against `max_alias_free_plan`, plus an orphan sweep for leftovers from prior failed runs.
- **Random aliases for bulk tests.** Custom alias addresses are reserved permanently once deleted; random ones avoid burning names.
- **Fork PRs skip live E2E** and run unit + mock suites only. Secrets are unavailable to forks. Live E2E runs on push to `main` and via `workflow_dispatch`. Deliberately *not* using `pull_request_target`, which would expose secrets to untrusted contributor code.

### Account tier

The test account is on a 7-day premium trial (expiring ~2026-08-09) but is to be treated as **free tier**.

`POST /api/aliases/:alias_id/contacts` is premium-gated and returns `403 {"error": "Please upgrade to create a reverse-alias"}` on free accounts. Contact-creation tests probe `GET /api/user_info` and **skip with a stated reason** when `is_premium` is false, so the suite stays green after the trial lapses rather than failing misleadingly.
