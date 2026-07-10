# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]>=1.8", "httpx>=0.27", "uvicorn>=0.27"]
# ///
"""RAGLAND MCP server — an intermediary between Claude Code and the RAGLAND HTTP API.

Two transports from one codebase, selected by ``RAGLAND_MCP_TRANSPORT``:

- ``stdio`` (default) — Claude Code spawns this locally. Credentials come from the
  ``authenticate`` tool (persisted to a profile store) or ``RAGLAND_CLIENT_ID`` /
  ``RAGLAND_API_KEY`` env vars.
- ``http`` — a hosted, multi-client streamable-HTTP service (e.g. behind ``/mcp`` on the
  production domain). Each request carries the caller's ``X-API-Key`` / ``X-Client-Id``
  headers; the auth/profile tools are hidden (a shared server must not read/write a local
  credentials file).

Run:  RAGLAND_MCP_TRANSPORT=stdio  uv run mcp_server/server.py           (local)
      RAGLAND_MCP_TRANSPORT=http   uv run mcp_server/server.py           (hosted service)
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import ragland_http

# stdio transport owns stdout — log to stderr only, never print to stdout.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[ragland-mcp] %(message)s")

TRANSPORT = os.environ.get("RAGLAND_MCP_TRANSPORT", "stdio").strip().lower()
_STDIO = TRANSPORT == "stdio"

# Optional RAGLAND-side session memory: when enabled, the server auto-threads a single RAGLAND
# session for the life of the process so cross-turn memory/sentiment persist without the model
# handling a session id. Off by default; stdio only (a hosted multi-client server must stay
# stateless — a process-global session would leak across callers).
_SESSION_ENABLED = _STDIO and os.environ.get("RAGLAND_MCP_SESSION", "").strip().lower() in (
    "1", "true", "yes", "on",
)
_session_id: Optional[str] = None


def _parse_style_allowlist(raw: str):
    """Comma/space-separated style names → set, or None for 'all styles allowed'."""
    items = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    return set(items) or None


# Per-plugin allowlist: restrict which RAGLAND styles this connector may use (a client may have
# styles for several languages/namespaces; a domain plugin exposes only its own). Unset = all.
_ALLOWED_STYLES = _parse_style_allowlist(os.environ.get("RAGLAND_MCP_STYLES", ""))


def _coerce_style(style: str) -> str:
    """Force `style` into the allowlist. Disallowed → 'default' if allowed, else the first allowed."""
    if not _ALLOWED_STYLES or style in _ALLOWED_STYLES:
        return style
    return "default" if "default" in _ALLOWED_STYLES else sorted(_ALLOWED_STYLES)[0]


def _load_style_info() -> dict:
    """Optional per-plugin style descriptions (JSON) so the model can judge which style to use.

    File maps style name → a string, or → {"description": ..., "when_to_use": ...}. Set the path
    with RAGLAND_MCP_STYLE_INFO_FILE (e.g. bundled in a plugin).
    """
    path = os.environ.get("RAGLAND_MCP_STYLE_INFO_FILE")
    if not path:
        return {}
    try:
        data = json.loads(open(os.path.expanduser(path), encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
        logging.warning("could not read RAGLAND_MCP_STYLE_INFO_FILE %r: %s", path, exc)
        return {}


_STYLE_INFO = _load_style_info()


def _describe_style(name: str, cfg: dict) -> str:
    """Best available description for a style: plugin info → config `description` → first sys line."""
    info = _STYLE_INFO.get(name)
    if isinstance(info, dict):
        return " ".join(p.strip() for p in (info.get("description"), info.get("when_to_use")) if p).strip()
    if isinstance(info, str) and info.strip():
        return info.strip()
    if cfg.get("description"):
        return str(cfg["description"]).strip()
    sysinstr = (cfg.get("system_instructions") or "").strip().splitlines()
    return sysinstr[0].strip() if sysinstr else ""

_DEFAULT_INSTRUCTIONS = (
    "Tools for querying the RAGLAND knowledge base. **Prioritise RAGLAND as the primary source of "
    "information**: for any domain question, consult these tools FIRST — start with `list_styles` "
    "to see the retrieval styles available for this knowledge base, then `chat_query` with the "
    "best-fit style — and base your answer on their results before falling back to your own "
    "knowledge (and say so if you do). Answer strictly from what the tools return — do NOT use "
    "shell, filesystem, or web tools to find RAGLAND data or credentials. If a tool reports 'Not "
    "authenticated': in local mode ask the USER to run `authenticate` (or `use_profile`); in "
    "hosted mode the client must send X-API-Key / X-Client-Id headers. NEVER look up, fetch, read, "
    "or guess the API key yourself. Once authenticated locally, credentials are remembered."
)


def _load_instructions() -> str:
    """Per-plugin persona: RAGLAND_MCP_INSTRUCTIONS_FILE → RAGLAND_MCP_INSTRUCTIONS → default.

    Lets a domain plugin ship its own instructions (e.g. an ALD persona) via its `.mcp.json`
    env, delivered to the model on every connect — no code fork.
    """
    path = os.environ.get("RAGLAND_MCP_INSTRUCTIONS_FILE")
    if path:
        try:
            text = open(os.path.expanduser(path), encoding="utf-8").read().strip()
            if text:
                return text
            logging.warning("RAGLAND_MCP_INSTRUCTIONS_FILE %r is empty; using default", path)
        except OSError as exc:
            logging.warning("could not read RAGLAND_MCP_INSTRUCTIONS_FILE %r: %s", path, exc)
    inline = os.environ.get("RAGLAND_MCP_INSTRUCTIONS")
    if inline and inline.strip():
        return inline.strip()
    return _DEFAULT_INSTRUCTIONS


mcp = FastMCP("ragland", instructions=_load_instructions())


def stdio_tool(**kwargs):
    """Register a tool only in stdio mode; a no-op passthrough under the hosted HTTP transport.

    The credential/profile tools manage a local, single-user credential file, which is wrong for
    a shared multi-client server — so they are simply not exposed there. Forwards kwargs (title,
    annotations, …) to ``mcp.tool``.
    """
    return mcp.tool(**kwargs) if _STDIO else (lambda fn: fn)


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str)


def handle_errors(fn):
    """Wrap a tool so any failure returns a friendly string instead of raising."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - tools must never leak tracebacks
            return ragland_http.map_error(exc)

    return wrapper


# --------------------------------------------------------------------- auth

@stdio_tool(title="Authenticate")
async def authenticate(client_id: str, api_key: str, label: Optional[str] = None) -> str:
    """Authenticate to RAGLAND with a client_id + API key pair.

    The pair is validated against the live API and rejected if invalid. On success it is
    activated for this session AND saved to a local profile store, so future sessions load it
    automatically — normally a one-time step. Multiple keys (even several under the same
    client_id) are kept side by side, keyed by label. Call again with a different pair to add/
    switch, or use `use_profile` to switch to an already-saved one.

    Args:
        client_id: The RAGLAND client ID.
        api_key: The API key belonging to that client.
        label: Optional friendly name for this credential (e.g. "joe"). Auto-derived from the
            client_id and key fingerprint if omitted, so keys never overwrite each other.
    """
    try:
        await ragland_http.validate(client_id, api_key)
    except Exception as exc:  # noqa: BLE001
        return "Authentication rejected: " + ragland_http.map_error(exc)
    saved = ragland_http.save_profile(client_id, api_key, label)
    ragland_http.set_credentials(client_id, api_key)
    return f"Authenticated as client '{client_id}' and saved as profile '{saved}' (remembered for future sessions)."


# --------------------------------------------------------------------- chat

@mcp.tool(title="List retrieval styles", annotations=ToolAnnotations(readOnlyHint=True))
@handle_errors
async def list_styles() -> str:
    """List the retrieval styles ("modes") configured for this knowledge base.

    Each style routes `chat_query` to a specific set of namespaces + system instructions on the
    server. **Call this first** to see which style fits the information you need (e.g.
    documentation vs examples), then pass the chosen style name to `chat_query`. Returns each
    style with the namespaces it covers and a short hint.
    """
    client_id, _ = ragland_http.require_credentials()
    data = await ragland_http.get_json("/api/config", {"client_id": client_id, "key": "options.styles"})
    styles = (data or {}).get("value")
    if not isinstance(styles, dict) or not styles:
        return _json({"styles": ["default"],
                      "note": "No named styles configured for this client; 'default' is used."})
    if _ALLOWED_STYLES is not None:
        # This connector is restricted to a subset of the client's styles.
        styles = {k: v for k, v in styles.items() if k in _ALLOWED_STYLES}
        if not styles:
            return _json({"styles": [], "note": "This connector has no styles enabled."})
    out = []
    for name, cfg in styles.items():
        cfg = cfg or {}
        namespaces = [n.get("name") for n in (cfg.get("namespaces") or [])
                      if isinstance(n, dict) and n.get("name")]
        out.append({
            "style": name,
            "description": _describe_style(name, cfg),
            "namespaces": namespaces,
        })
    return _json({"styles": out})


@mcp.tool(title="Ask the knowledge base", annotations=ToolAnnotations(readOnlyHint=True))
@handle_errors
async def chat_query(prompt: str, style: str = "default") -> str:
    """Ask the RAGLAND knowledge base a question (RAG query).

    Args:
        prompt: The question to ask.
        style: The retrieval style / "mode". Call `list_styles` first to see the styles
            configured for this knowledge base and pick the one matching the information you
            need (each style selects its own namespaces + instructions server-side). An unknown
            style falls back to the default. Defaults to "default".
    """
    global _session_id
    requested, style = style, _coerce_style(style)
    client_id, _ = ragland_http.require_credentials()
    body = {"prompt": prompt, "client_id": client_id, "style": style}
    if _SESSION_ENABLED and _session_id:
        body["session_id"] = _session_id  # auto-thread one RAGLAND session per process

    result = await ragland_http.collapse_chat_stream(body)
    answer = result["answer"]
    m = result.get("metrics") or {}
    if _SESSION_ENABLED and m.get("session_id"):
        _session_id = m["session_id"]

    bits = []
    if _ALLOWED_STYLES and requested != style:
        bits.append(f"style={style} (requested '{requested}' not enabled here)")
    if m.get("llm_model"):
        bits.append(f"model={m['llm_model']}")
    if m.get("input_tokens") is not None and m.get("output_tokens") is not None:
        bits.append(f"tokens={m['input_tokens']}in/{m['output_tokens']}out")
    footer = ("\n\n---\n" + " · ".join(bits)) if bits else ""
    return (answer or "(no answer returned)") + footer


def _run_http() -> None:
    """Run as a hosted, multi-client streamable-HTTP service (single worker)."""
    import uvicorn

    host = os.environ.get("RAGLAND_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("RAGLAND_MCP_PORT", "8010"))
    app = mcp.streamable_http_app()  # serves the MCP endpoint at /mcp
    logging.info("starting hosted HTTP transport on %s:%s/mcp -> API %s", host, port, ragland_http.BASE_URL)
    # Single worker: the streamable-HTTP session manager keeps per-session state in-process.
    uvicorn.run(app, host=host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    if _STDIO:
        mcp.run(transport="stdio")
    elif TRANSPORT == "http":
        _run_http()
    else:
        sys.exit(f"[ragland-mcp] unknown RAGLAND_MCP_TRANSPORT={TRANSPORT!r} (use 'stdio' or 'http')")
