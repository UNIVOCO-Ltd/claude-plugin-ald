# claude-plugin-ald

A **domain-specific Claude Code plugin** — an ALD (Arbortext Layout Developer) assistant backed by
the ALD RAGLAND knowledge base. It is a thin wrapper around the generic RAGLAND MCP server
(source of truth: the backend repo's `mcp_server/`), specialised with:

- a pinned knowledge base (`RAGLAND_CLIENT_ID` = the ALD client),
- a bundled **persona** (`instructions.md`) delivered to Claude on connect, and
- **style-aware retrieval** — the model picks the RAGLAND style (`jsfom` / `tcode` / `default`,
  discovered live via `list_styles`) that matches what it needs, and each style resolves its own
  namespaces server-side.

Nothing else is domain-specific: `server.py` and `ragland_http.py` are the unmodified generic
engine (copied verbatim from `mcp_server/`).

## Install

Works in **Claude Desktop** (Chat tab), **Claude Code**, and Claude web/Cowork — plugins installed
from a marketplace run in all of them, and this plugin's only component is a local MCP server.

Prerequisite: [`uv`](https://docs.astral.sh/uv/) must be installed and on your `PATH` (the plugin
runs the server with `uv run`). On Claude Desktop, install `uv` before installing the plugin.

**Claude Desktop:** Customize (left sidebar) → **Plugins** → **Add marketplace** →
`UNIVOCO-Ltd/claude-plugin-ald` → **Install** the `ald` plugin.

**Claude Code:**

```
/plugin marketplace add UNIVOCO-Ltd/claude-plugin-ald
/plugin install ald@ald-tools
```

Then authenticate once with **your own** RAGLAND `client_id` + `api_key` (validated against the API
and remembered across sessions — the plugin ships no credentials). `authenticate` is a **tool**, not
a slash command, so:

- **Claude Desktop / web:** just say in chat — *"authenticate with client_id … and api_key …"* — and
  Claude calls the tool.
- **Claude Code (terminal):** the same, or use the bundled command `/ald:authenticate` (which will
  prompt for the two values).

Ask ALD questions normally — the assistant consults the ALD knowledge base first and auto-selects
the right style. The tools appear as `mcp__ald__*`.

## How the specialisation works

| Piece | Where | Effect |
|---|---|---|
| Persona / behaviour | `instructions.md` → `RAGLAND_MCP_INSTRUCTIONS_FILE` in `.mcp.json` | Delivered as the MCP server's `instructions`; tells Claude to prioritise ALD and how to pick a style. |
| Knowledge base | user's `client_id` + `api_key` via the `authenticate` tool | No credentials ship in `.mcp.json`; each user authenticates with their own RAGLAND `client_id`/`api_key` (persisted to `~/.ragland/mcp/credentials.json`). The API key determines the actual scope. |
| Styles / "modes" | RAGLAND client config (`options.styles`) | Not in this repo — the model discovers them at runtime with `list_styles`; each style routes to its own namespaces. |
| Allowed styles | `RAGLAND_MCP_STYLES` in `.mcp.json` (`jsfom,tcode,default`) | Restricts which styles this plugin may use; `list_styles` is filtered and `chat_query` forces any other style into the list. For a **single-language** variant, list only that style and omit the broad `default` (e.g. `jsfom`). |
| Style descriptions | `styles.json` → `RAGLAND_MCP_STYLE_INFO_FILE` | Per-style `description` + `when_to_use` surfaced by `list_styles` so the model can judge which style to query. Edit these to tune routing. |
| Prod URL | `RAGLAND_BASE_URL` (default `https://rag4.univoco.io`) | Overridable via env for local dev. |

## Local dev

```bash
RAGLAND_BASE_URL=http://localhost:8001 claude --plugin-dir /media/joe/LAPTOPXTRA/claude-plugin-ald
# /mcp → "ald" connected;  then authenticate() with a local ALD key
```

## Cloning to another domain (the template)

This directory **is** the template. To make `claude-plugin-<domain>`:

1. Copy this folder; copy fresh `server.py` + `ragland_http.py` from the backend `mcp_server/`
   (keep them byte-identical — never edit the engine per-domain).
2. Edit **three** things only:
   - `.mcp.json` → `RAGLAND_CLIENT_ID` (the new domain's client) and the `mcpServers` key name.
   - `instructions.md` → the new domain's persona + which styles to prefer.
   - `.claude-plugin/plugin.json` + `marketplace.json` → `name` / `description`.
3. Configure that client's `options.styles` in RAGLAND so `list_styles` returns useful modes.

The generic engine, credential handling (stdio + hosted HTTP), persistence, and `list_styles`
all come along unchanged.
