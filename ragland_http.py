"""HTTP plumbing + credential store for the RAGLAND MCP server.

Deliberately thin: it knows how to reach the RAGLAND HTTP API, hold the active
``(client_id, api_key)`` pair for the life of the process, persist credential
profiles across sessions, and collapse the chat streaming response into a single
result. No business logic lives here — the MCP tool layer (``server.py``) owns that.

The server is key-agnostic. Credentials come from (in priority order): the
``RAGLAND_CLIENT_ID`` / ``RAGLAND_API_KEY`` env vars, else the persisted profile
store (``~/.ragland/mcp/credentials.json``, override with ``RAGLAND_MCP_CREDENTIALS``),
else the ``authenticate`` tool at runtime. Profiles are keyed by a label so multiple
API keys — even several under the same ``client_id`` — coexist without clobbering.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE_URL = os.environ.get("RAGLAND_BASE_URL", "http://localhost:8001").rstrip("/")

# Generous timeout: chat_query can take a while for long RAG answers.
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


class AuthError(Exception):
    """No credentials cached, or a request was rejected as unauthorized."""


class RaglandError(Exception):
    """An in-stream error event surfaced from the chat endpoint."""


# --------------------------------------------------------------------- creds

# --- active (in-memory, per-process/session) credential pair ---

_client_id: Optional[str] = None
_api_key: Optional[str] = None


def set_credentials(client_id: str, api_key: str) -> None:
    """Set the active credential pair for the life of the process (no persistence)."""
    global _client_id, _api_key
    _client_id, _api_key = client_id, api_key


def clear_credentials() -> None:
    global _client_id, _api_key
    _client_id = _api_key = None


def current_client_id() -> Optional[str]:
    return _client_id


def _http_request_headers():
    """Incoming HTTP request headers when running under an HTTP transport, else None.

    Reads the mcp SDK's per-message request context (set fresh for every request, even inside a
    long-lived streamable-HTTP session), so it reflects the *current* caller. Returns None under
    stdio (no HTTP request) or if the SDK internals move.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx
        rc = request_ctx.get()
    except Exception:  # noqa: BLE001 - best-effort; LookupError when no request in context
        return None
    req = getattr(rc, "request", None)
    return getattr(req, "headers", None)


def require_credentials() -> Tuple[str, str]:
    """Resolve the active credentials.

    Priority: per-request HTTP headers (hosted multi-client mode) → process-global pair
    (stdio: env bootstrap / `authenticate` / saved profile) → error.
    """
    headers = _http_request_headers()
    if headers is not None:
        key = headers.get("x-api-key")
        cid = headers.get("x-client-id")
        if key and cid:
            return cid, key
        if key and not cid:
            raise AuthError(
                "Hosted mode: an X-API-Key was sent but no X-Client-Id — also send the "
                "X-Client-Id header (the RAGLAND client this key belongs to)."
            )
        # No auth headers on this HTTP request → fall through to any process-global creds.

    if not _client_id or not _api_key:
        raise AuthError(
            "Not authenticated. Hosted (HTTP) mode: send X-API-Key and X-Client-Id request "
            "headers. Local (stdio) mode: ask the USER to run `authenticate` (or `use_profile`). "
            "Do NOT look up, fetch, read, or guess the API key yourself."
        )
    return _client_id, _api_key


# --- persistent profile store (labels → credentials) ---

def _creds_path() -> Path:
    override = os.environ.get("RAGLAND_MCP_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ragland" / "mcp" / "credentials.json"


def fingerprint(api_key: str) -> str:
    """A short, non-reversible id for a key — lets us show *which* key without the key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:6]


def _default_store() -> Dict[str, Any]:
    return {"active": None, "profiles": {}}


def _load_store() -> Dict[str, Any]:
    try:
        with _creds_path().open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            return _default_store()
        data.setdefault("active", None)
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default_store()


def _save_store(store: Dict[str, Any]) -> None:
    path = _creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)  # atomic; inherits 0600


def _derive_label(client_id: str, api_key: str) -> str:
    return f"{client_id[:8]}-{fingerprint(api_key)}"


def save_profile(client_id: str, api_key: str, label: Optional[str] = None) -> str:
    """Upsert a credential profile and mark it active. Returns the label used.

    Keyed by ``label`` (auto-derived from client_id + key fingerprint when omitted), so a
    second key for the same client never overwrites the first.
    """
    store = _load_store()
    label = label or _derive_label(client_id, api_key)
    store["profiles"][label] = {
        "client_id": client_id,
        "api_key": api_key,
        "fingerprint": fingerprint(api_key),
    }
    store["active"] = label
    _save_store(store)
    return label


def list_profiles() -> List[Dict[str, Any]]:
    """Saved profiles — labels, client_ids, fingerprints, and which is active. No key material."""
    store = _load_store()
    active = store.get("active")
    return [
        {
            "label": label,
            "client_id": p.get("client_id"),
            "fingerprint": p.get("fingerprint"),
            "active": label == active,
        }
        for label, p in store.get("profiles", {}).items()
    ]


def use_profile(label: str) -> str:
    """Activate a saved profile for this session (and persist it as active). Returns client_id."""
    store = _load_store()
    p = store.get("profiles", {}).get(label)
    if not p:
        known = ", ".join(store.get("profiles", {})) or "(none)"
        raise AuthError(f"No saved profile '{label}'. Saved profiles: {known}.")
    set_credentials(p["client_id"], p["api_key"])
    store["active"] = label
    _save_store(store)
    return p["client_id"]


def forget_profile(label: str) -> None:
    """Delete a saved profile. Clears the active session creds if it was the active one."""
    store = _load_store()
    if label not in store.get("profiles", {}):
        known = ", ".join(store.get("profiles", {})) or "(none)"
        raise AuthError(f"No saved profile '{label}'. Saved profiles: {known}.")
    del store["profiles"][label]
    if store.get("active") == label:
        store["active"] = None
        clear_credentials()
    _save_store(store)


def _bootstrap() -> None:
    """Load the active credential at startup: env vars → active profile → sole profile → none."""
    cid = os.environ.get("RAGLAND_CLIENT_ID")
    key = os.environ.get("RAGLAND_API_KEY")
    if cid and key:
        set_credentials(cid, key)
        print(f"[ragland-mcp] using credentials for client {cid} from env", file=sys.stderr)
        return

    # Hosted HTTP mode is multi-client and authenticates per request via headers — do NOT let a
    # stray local profile file become a shared fallback identity for every caller.
    if os.environ.get("RAGLAND_MCP_TRANSPORT", "stdio").strip().lower() == "http":
        return

    store = _load_store()
    profiles = store.get("profiles", {})
    active = store.get("active")
    chosen = active if active in profiles else (next(iter(profiles)) if len(profiles) == 1 else None)
    if chosen:
        p = profiles[chosen]
        set_credentials(p["client_id"], p["api_key"])
        print(f"[ragland-mcp] loaded saved profile '{chosen}' (client {p['client_id']})", file=sys.stderr)


_bootstrap()


# --------------------------------------------------------------------- errors

def map_error(exc: Exception) -> str:
    """Turn an exception into a short, user-facing string (never a stack trace)."""
    if isinstance(exc, (AuthError, RaglandError)):
        return f"Error: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return "Error: the RAGLAND API timed out. Try again or narrow the request."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = _detail(exc.response)
        if code == 401:
            return f"Error: unauthorized (401) — invalid or missing API key. {detail}".strip()
        if code == 403:
            return f"Error: forbidden (403) — this key is not authorized for that client. {detail}".strip()
        if code == 404:
            return f"Error: not found (404). {detail}".strip()
        return f"Error: RAGLAND API returned {code}. {detail}".strip()
    if isinstance(exc, httpx.RequestError):
        return f"Error: could not reach the RAGLAND API at {BASE_URL} ({exc.__class__.__name__})."
    return f"Error: {exc}"


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------- requests

def _headers(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _clean(params: Optional[dict]) -> Optional[dict]:
    """Drop None-valued query params so optional args aren't sent as 'None'."""
    if not params:
        return params
    return {k: v for k, v in params.items() if v is not None}


async def validate(client_id: str, api_key: str) -> None:
    """Confirm a ``(client_id, api_key)`` pair against the live API.

    Probes a cheap self-scoped endpoint. Raises ``httpx.HTTPStatusError`` on
    401/403 (invalid pair), or another ``httpx`` error on transport failure.
    """
    url = f"{BASE_URL}/api/chat/{client_id}/sessions"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params={"page": 1, "page_size": 1}, headers=_headers(api_key))
        resp.raise_for_status()


async def get_json(path: str, params: Optional[dict] = None) -> Any:
    _, api_key = require_credentials()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=_clean(params), headers=_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def post_json(path: str, json_body: dict) -> Any:
    _, api_key = require_credentials()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{BASE_URL}{path}", json=json_body, headers=_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def collapse_chat_stream(json_body: dict) -> dict:
    """POST /api/chat/query and collapse the NDJSON stream into ``{answer, metrics}``.

    Concatenates ``type == "answer"`` chunks; captures the ``type == "metrics"``
    object; raises ``RaglandError`` on an ``type == "error"`` event. Ignores
    metadata/other passthrough chunks.
    """
    _, api_key = require_credentials()
    answer_parts: list[str] = []
    metrics: dict = {}
    url = f"{BASE_URL}/api/chat/query"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", url, json=json_body, headers=_headers(api_key)) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj_type = obj.get("type")
                if obj_type == "answer":
                    content = obj.get("content")
                    if isinstance(content, str):
                        answer_parts.append(content)
                elif obj_type == "metrics":
                    metrics = obj.get("content") or {}
                elif obj_type == "error":
                    raise RaglandError(str(obj.get("content", "unknown streaming error")))
                if obj.get("done"):
                    break
    return {"answer": "".join(answer_parts), "metrics": metrics}
