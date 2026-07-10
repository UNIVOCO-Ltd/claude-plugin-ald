---
description: Authenticate to the ALD (RAGLAND) knowledge base with your client_id and api_key
argument-hint: [client_id] [api_key]
---

The user wants to authenticate to the ALD RAGLAND knowledge base so the `list_styles` and
`chat_query` tools work.

Arguments provided (may be empty): $ARGUMENTS

- If both a `client_id` and an `api_key` are present in the arguments above, call the `authenticate`
  tool with them immediately.
- Otherwise, ask the user to paste their RAGLAND `client_id` and `api_key`, then call the
  `authenticate` tool with those two values.

`authenticate` is an MCP tool — call it directly. Never run a shell command, and never guess,
fetch, or invent credentials. On success, tell the user they are authenticated and that it is
remembered for future sessions.

> Note: this `/ald:authenticate` command works in Claude Code (terminal) only. In the Claude
> Desktop / web chat, just say "authenticate with client_id … and api_key …" and the same tool runs.
