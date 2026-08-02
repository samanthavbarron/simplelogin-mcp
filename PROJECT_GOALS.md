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
DELETE /api/aliases/:alias_id: Delete an alias.
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
