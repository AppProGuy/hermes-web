#!/usr/bin/env python3
"""Hermes Web — local agent host or secure proxy to a remote Hermes Web host."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import mimetypes
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
import uvicorn
import websockets
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response


APP_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = APP_DIR / "frontend"
REMOTE_URL = os.getenv("HERMES_REMOTE_URL", "").strip().rstrip("/")
REMOTE_TOKEN = os.getenv("HERMES_REMOTE_TOKEN", "").strip()
AUTH_TOKEN = os.getenv("HERMES_WEB_TOKEN", "").strip()
HOST = os.getenv("HERMES_WEB_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("HERMES_WEB_PORT", "3005"))
MAX_CONVERSATIONS = int(os.getenv("HERMES_WEB_MAX_CONVERSATIONS", "100"))
APPROVAL_TIMEOUT = int(os.getenv("HERMES_APPROVAL_TIMEOUT", "300"))


def _validate_remote_url() -> None:
    if not REMOTE_URL:
        return
    parsed = urlparse(REMOTE_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("HERMES_REMOTE_URL must be an http(s) URL with a hostname")


_validate_remote_url()


def _agent_home() -> Path:
    configured = os.getenv("HERMES_AGENT_HOME", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / "hermes-agent",
        Path.home() / ".hermes" / "hermes-agent",
    ]
    for candidate in candidates:
        if candidate and (candidate / "run_agent.py").is_file():
            return candidate.resolve()
    searched = ", ".join(str(path) for path in candidates if path)
    raise RuntimeError(
        "Hermes Agent was not found. Set HERMES_AGENT_HOME or run in proxy mode with "
        f"HERMES_REMOTE_URL. Searched: {searched}"
    )


def _load_local_runtime():
    home = _agent_home()
    sys.path.insert(0, str(home))
    for path in (home / "venv").glob("lib/python*/site-packages"):
        sys.path.insert(0, str(path))
        break
    os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))
    os.environ.setdefault("HERMES_DISABLE_TELEMETRY", "1")
    from agent.model_metadata import get_model_context_length
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback

    return AIAgent, get_model_context_length, set_approval_callback


def _load_config() -> dict:
    path = Path(os.getenv("HERMES_CONFIG", str(Path.home() / ".hermes" / "config.yaml"))).expanduser()
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


CONFIG = _load_config()
MODEL_CONFIG = CONFIG.get("model", {}) if isinstance(CONFIG, dict) else {}
DEFAULT_MODEL = MODEL_CONFIG.get("default", "")
DEFAULT_PROVIDER = MODEL_CONFIG.get("provider", "")
DEFAULT_BASE_URL = MODEL_CONFIG.get("base_url", "")
DEFAULT_API_KEY = os.getenv(MODEL_CONFIG.get("env_key", ""), "") if MODEL_CONFIG.get("env_key") else ""

DATA_DIR = Path(os.getenv("HERMES_WEB_DATA_DIR", str(Path.home() / ".hermes" / "hermes-web"))).expanduser()
CONV_DIR = DATA_DIR / "conversations"
if not REMOTE_URL:
    CONV_DIR.mkdir(parents=True, exist_ok=True)


def _active_agent_ids() -> set[str]:
    current = globals().get("session")
    return set(current.agents) if current else set()


def load_conversations() -> List[dict]:
    conversations: List[dict] = []
    active_ids = _active_agent_ids()
    for path in CONV_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            messages = data.get("messages", [])
            conversations.append(
                {
                    "id": data["id"],
                    "title": data.get("title", "New Chat"),
                    "model": data.get("model", ""),
                    "provider": data.get("provider", ""),
                    "created_at": data.get("created_at", 0),
                    "updated_at": data.get("updated_at", 0),
                    "message_count": len(messages),
                    "tokens": sum(len(str(message.get("content", "") or "")) for message in messages) // 3,
                    "has_agent": data["id"] in active_ids,
                }
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    conversations.sort(key=lambda item: item["updated_at"], reverse=True)
    return conversations


def _valid_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def load_conversation(conversation_id: str) -> Optional[dict]:
    if not _valid_id(conversation_id):
        return None
    path = CONV_DIR / f"{conversation_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_conversation(data: dict) -> None:
    path = CONV_DIR / f"{data['id']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def delete_conversation(conversation_id: str) -> None:
    if not _valid_id(conversation_id):
        return
    path = CONV_DIR / f"{conversation_id}.json"
    if path.is_file():
        path.unlink()


def create_conversation(model: str, provider: str) -> dict:
    conversations = load_conversations()
    if len(conversations) >= MAX_CONVERSATIONS:
        delete_conversation(conversations[-1]["id"])
    now = time.time()
    conversation = {
        "id": str(uuid.uuid4()),
        "title": "New Chat",
        "model": model,
        "provider": provider,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    save_conversation(conversation)
    return conversation


def add_message(conversation_id: str, role: str, content: str, extra: Optional[dict] = None) -> None:
    conversation = load_conversation(conversation_id)
    if not conversation:
        return
    message = {"role": role, "content": content, "timestamp": time.time()}
    if extra:
        message.update(extra)
    conversation["messages"].append(message)
    conversation["updated_at"] = time.time()
    if role == "user" and len(conversation["messages"]) == 1:
        clean = " ".join(content.strip().split())
        conversation["title"] = clean[:60] + ("…" if len(clean) > 60 else "")
    save_conversation(conversation)


def update_message(conversation_id: str, tool_id: str, updates: dict) -> None:
    conversation = load_conversation(conversation_id)
    if not conversation:
        return
    for message in conversation["messages"]:
        if message.get("tool_id") == tool_id:
            message.update(updates)
            message["timestamp"] = time.time()
            break
    conversation["updated_at"] = time.time()
    save_conversation(conversation)


def _estimate_context(messages: list, config: dict) -> Optional[dict]:
    resolver = globals().get("get_model_context_length")
    if resolver is None:
        return None
    total_chars = 0
    tool_chars = 0
    message_count = 0
    for message in messages:
        content = message.get("content", "") or ""
        if isinstance(content, str):
            total_chars += len(content)
            message_count += 1
            if message.get("role") == "tool":
                tool_chars += len(content)
    estimated_tokens = total_chars // 3
    limit = config.get("_ctx_limit")
    if limit is None:
        try:
            limit = resolver(
                model=config.get("model", ""),
                base_url=config.get("base_url") or "",
                api_key=config.get("api_key") or "",
                provider=config.get("provider") or "",
            )
        except Exception:
            return None
        config["_ctx_limit"] = limit
    percentage = round(estimated_tokens / max(limit, 1) * 100, 1)
    return {
        "tokens": estimated_tokens,
        "limit": limit,
        "pct": percentage,
        "messages": message_count,
        "tool_kb": round(tool_chars / 1024, 1),
    }


@dataclass
class PendingInteraction:
    event: threading.Event = field(default_factory=threading.Event)
    response: Any = None


class LocalSession:
    def __init__(self) -> None:
        self.ws: Optional[WebSocket] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.conversation_id: Optional[str] = None
        self.config = {
            "model": DEFAULT_MODEL,
            "provider": DEFAULT_PROVIDER,
            "base_url": DEFAULT_BASE_URL or None,
            "api_key": DEFAULT_API_KEY or None,
            "max_iterations": 60,
        }
        self.reasoning: Dict[str, str] = {}
        self.thinking: Dict[str, str] = {}
        self.agents: Dict[str, Any] = {}
        self.history: Dict[str, List[dict]] = {}
        self.pending: Dict[str, PendingInteraction] = {}
        self.stop_requested = False

    async def send(self, event_type: str, **payload: Any) -> None:
        if not self.ws:
            return
        try:
            await self.ws.send_json({"type": event_type, **payload})
        except (RuntimeError, WebSocketDisconnect):
            return

    def send_from_worker(self, event_type: str, **payload: Any) -> None:
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.send(event_type, **payload), self.loop)

    def request_human(self, event_type: str, payload: dict, timeout: int = APPROVAL_TIMEOUT) -> Any:
        request_id = str(uuid.uuid4())
        pending = PendingInteraction()
        self.pending[request_id] = pending
        self.send_from_worker(event_type, request_id=request_id, **payload)
        if not pending.event.wait(timeout):
            self.pending.pop(request_id, None)
            return "timeout"
        self.pending.pop(request_id, None)
        return pending.response

    def resolve_human(self, request_id: str, response: Any) -> bool:
        pending = self.pending.get(request_id)
        if not pending:
            return False
        pending.response = response
        pending.event.set()
        return True

    def cancel_pending(self) -> None:
        for pending in self.pending.values():
            pending.response = "cancelled"
            pending.event.set()


session = LocalSession()
AIAgent = None
get_model_context_length = None
set_approval_callback = None
if not REMOTE_URL:
    AIAgent, get_model_context_length, set_approval_callback = _load_local_runtime()


app = FastAPI(title="Hermes Web", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'"
    )
    return response


def _token_ok(candidate: str) -> bool:
    return not AUTH_TOKEN or hmac.compare_digest(candidate, AUTH_TOKEN)


def _require_http_auth(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    candidate = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not _token_ok(candidate):
        raise HTTPException(status_code=401, detail="Authentication required")


def _require_ws_auth(websocket: WebSocket) -> bool:
    candidate = ""
    for protocol in websocket.headers.get("sec-websocket-protocol", "").split(","):
        protocol = protocol.strip()
        if not protocol.startswith("auth."):
            continue
        encoded = protocol.removeprefix("auth.")
        try:
            padding = "=" * (-len(encoded) % 4)
            candidate = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        break
    return _token_ok(candidate)


def _accepted_subprotocol(websocket: WebSocket) -> Optional[str]:
    offered = {item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")}
    return "hermes" if "hermes" in offered else None


def _origin_ok(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if not origin:
        return True
    return urlparse(origin).netloc == host


def _remote_headers() -> dict:
    return {"Authorization": f"Bearer {REMOTE_TOKEN}"} if REMOTE_TOKEN else {}


async def _remote_request(method: str, path: str) -> Response:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        upstream = await client.request(method, f"{REMOTE_URL}{path}", headers=_remote_headers())
    excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    headers = {key: value for key, value in upstream.headers.items() if key.lower() not in excluded}
    return Response(upstream.content, status_code=upstream.status_code, headers=headers)


@app.get("/api/health")
async def health(request: Request):
    _require_http_auth(request)
    if not REMOTE_URL:
        return {"ok": True, "mode": "local", "agent_home": str(_agent_home()), "version": 2}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(f"{REMOTE_URL}/api/conversations", headers=_remote_headers())
            response.raise_for_status()
        return {"ok": True, "mode": "proxy", "remote": urlparse(REMOTE_URL).hostname, "version": 2}
    except Exception:
        return JSONResponse({"ok": False, "mode": "proxy", "error": "Remote Hermes is unavailable"}, status_code=503)


@app.get("/api/conversations")
async def list_conversation_api(request: Request):
    _require_http_auth(request)
    if REMOTE_URL:
        return await _remote_request("GET", "/api/conversations")
    return JSONResponse(load_conversations(), headers={"Cache-Control": "no-store"})


@app.get("/api/conversations/{conversation_id}")
async def get_conversation_api(conversation_id: str, request: Request):
    _require_http_auth(request)
    if not _valid_id(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if REMOTE_URL:
        return await _remote_request("GET", f"/api/conversations/{conversation_id}")
    conversation = load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation_api(conversation_id: str, request: Request):
    _require_http_auth(request)
    if not _valid_id(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    if REMOTE_URL:
        return await _remote_request("DELETE", f"/api/conversations/{conversation_id}")
    delete_conversation(conversation_id)
    session.agents.pop(conversation_id, None)
    session.history.pop(conversation_id, None)
    if session.conversation_id == conversation_id:
        session.conversation_id = None
    return {"ok": True}


def _media_roots() -> list[Path]:
    raw = os.getenv("HERMES_MEDIA_ROOTS", str(Path.home() / ".hermes"))
    return [Path(item.strip()).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip()]


@app.get("/api/media")
async def media(path: str, request: Request):
    _require_http_auth(request)
    if REMOTE_URL:
        from urllib.parse import quote

        return await _remote_request("GET", f"/api/media?path={quote(path, safe='')}")
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file() or not any(candidate.is_relative_to(root) for root in _media_roots()):
        raise HTTPException(status_code=404, detail="Media not found")
    media_type, _ = mimetypes.guess_type(candidate.name)
    if not media_type or not media_type.startswith(("image/", "audio/", "video/")):
        raise HTTPException(status_code=415, detail="Unsupported media type")
    return FileResponse(candidate, media_type=media_type, filename=candidate.name)


async def _proxy_websocket(websocket: WebSocket) -> None:
    parsed = urlparse(REMOTE_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    remote_ws = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/ws/chat"
    await websocket.accept(subprotocol=_accepted_subprotocol(websocket))
    try:
        remote_protocols = ["hermes"]
        if REMOTE_TOKEN:
            encoded = base64.urlsafe_b64encode(REMOTE_TOKEN.encode("utf-8")).decode("ascii").rstrip("=")
            remote_protocols.append(f"auth.{encoded}")
        async with websockets.connect(
            remote_ws,
            max_size=16 * 1024 * 1024,
            subprotocols=remote_protocols if REMOTE_TOKEN else None,
        ) as upstream:
            async def browser_to_remote():
                while True:
                    await upstream.send(await websocket.receive_text())

            async def remote_to_browser():
                async for message in upstream:
                    # Older hermes-web versions included the resolved API key in
                    # config events. Strip it at the bridge boundary.
                    try:
                        event = json.loads(message)
                        if event.get("type") == "config_loaded":
                            event.pop("api_key", None)
                        message = json.dumps(event)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    await websocket.send_text(message)

            tasks = [asyncio.create_task(browser_to_remote()), asyncio.create_task(remote_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        return
    except Exception:
        try:
            await websocket.send_json({"type": "error", "message": "Remote Hermes connection failed"})
        except Exception:
            pass


def _make_callbacks(conversation_id: str):
    def thinking(text: str):
        session.thinking[conversation_id] = session.thinking.get(conversation_id, "") + text
        session.send_from_worker("thinking", content=text, _conv_id=conversation_id)

    def reasoning(text: str):
        session.reasoning[conversation_id] = session.reasoning.get(conversation_id, "") + text
        session.send_from_worker("reasoning", content=text, _conv_id=conversation_id)

    def tool_start(tool_id: str, name: str, args: dict):
        combined = "\n".join(
            part.strip()
            for part in (session.thinking.get(conversation_id, ""), session.reasoning.get(conversation_id, ""))
            if part.strip()
        )
        session.thinking[conversation_id] = ""
        session.reasoning[conversation_id] = ""
        add_message(
            conversation_id,
            "tool",
            "",
            {
                "tool_id": tool_id,
                "tool_name": name,
                "tool_args": args,
                "tool_result": "",
                "status": "running",
                "reasoning": combined,
            },
        )
        session.send_from_worker(
            "tool_start", id=tool_id, name=name, args=args, reasoning=combined, _conv_id=conversation_id
        )

    def tool_complete(tool_id: str, name: str, args: dict, result: str):
        result_text = str(result)
        display_result = result_text[:12000] + ("\n… (truncated)" if len(result_text) > 12000 else "")
        update_message(conversation_id, tool_id, {"tool_result": display_result, "status": "done"})
        conversation = load_conversation(conversation_id)
        context = _estimate_context(conversation.get("messages", []), session.config) if conversation else None
        session.send_from_worker(
            "tool_complete",
            id=tool_id,
            name=name,
            result=display_result,
            context=context,
            _conv_id=conversation_id,
        )

    def stream_delta(delta: str):
        if delta:
            session.send_from_worker("delta", content=delta, _conv_id=conversation_id)

    def clarify(question: str, choices: Any = None):
        response = session.request_human(
            "clarification_required",
            {"question": question, "choices": choices or [], "_conv_id": conversation_id},
        )
        return "" if response in {None, "timeout", "cancelled"} else str(response)

    return thinking, reasoning, tool_start, tool_complete, stream_delta, clarify


def _approval_callback(conversation_id: str):
    def approve(command: str, description: str, **options: Any) -> str:
        response = session.request_human(
            "approval_required",
            {
                "command": command,
                "description": description,
                "allow_permanent": bool(options.get("allow_permanent", True)),
                "allow_session": bool(options.get("allow_session", True)),
                "title": options.get("title") or "Approval required",
                "_conv_id": conversation_id,
            },
        )
        allowed = {"once", "session", "always", "deny", "timeout", "cancelled"}
        return response if response in allowed else "deny"

    return approve


async def _local_websocket(websocket: WebSocket) -> None:
    await websocket.accept(subprotocol=_accepted_subprotocol(websocket))
    session.ws = websocket
    session.loop = asyncio.get_running_loop()
    session.conversation_id = None
    await session.send("config_loaded", conv_id=None, **{k: v for k, v in session.config.items() if k != "api_key"})

    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type", "message")

            if message_type == "configure":
                for key in ("model", "provider", "max_iterations"):
                    if key in data:
                        session.config[key] = data[key]
                session.config.pop("_ctx_limit", None)
                await session.send(
                    "config_loaded",
                    conv_id=session.conversation_id,
                    **{k: v for k, v in session.config.items() if k != "api_key"},
                )
                continue

            if message_type in {"approval_response", "clarification_response"}:
                response = data.get("decision") if message_type == "approval_response" else data.get("response")
                session.resolve_human(data.get("request_id", ""), response)
                continue

            if message_type == "new_chat":
                conversation = create_conversation(session.config["model"], session.config["provider"])
                session.conversation_id = conversation["id"]
                await session.send(
                    "config_loaded",
                    conv_id=session.conversation_id,
                    **{k: v for k, v in session.config.items() if k != "api_key"},
                )
                continue

            if message_type == "switch_chat":
                conversation_id = data.get("conv_id", "")
                conversation = load_conversation(conversation_id)
                if conversation:
                    session.conversation_id = conversation_id
                    history = session.history.get(conversation_id)
                    context = _estimate_context(history, session.config) if history else None
                    await session.send(
                        "load_conversation",
                        messages=conversation.get("messages", []),
                        context=context,
                        _conv_id=conversation_id,
                    )
                continue

            if message_type == "stop":
                session.stop_requested = True
                session.cancel_pending()
                agent = session.agents.get(session.conversation_id or "")
                if agent:
                    try:
                        agent.interrupt()
                    except Exception:
                        pass
                await session.send("stopped", _conv_id=session.conversation_id)
                continue

            message = str(data.get("message", "")).strip()
            if not message:
                continue
            if not session.conversation_id:
                conversation = create_conversation(session.config["model"], session.config["provider"])
                session.conversation_id = conversation["id"]
                await session.send(
                    "config_loaded",
                    conv_id=session.conversation_id,
                    **{k: v for k, v in session.config.items() if k != "api_key"},
                )

            conversation_id = session.conversation_id
            add_message(conversation_id, "user", message)
            await session.send("user_message", content=message, _conv_id=conversation_id)

            agent = session.agents.get(conversation_id)
            if agent is None:
                thinking, reasoning, tool_start, tool_complete, stream_delta, clarify = _make_callbacks(conversation_id)
                agent = AIAgent(
                    model=session.config["model"],
                    provider=session.config["provider"],
                    base_url=session.config.get("base_url"),
                    api_key=session.config.get("api_key"),
                    quiet_mode=True,
                    tool_start_callback=tool_start,
                    tool_complete_callback=tool_complete,
                    stream_delta_callback=stream_delta,
                    thinking_callback=thinking,
                    reasoning_callback=reasoning,
                    clarify_callback=clarify,
                    max_iterations=session.config["max_iterations"],
                )
                session.agents[conversation_id] = agent

            session.stop_requested = False

            def run_agent_turn(turn_agent=agent, turn_message=message, turn_conversation_id=conversation_id) -> None:
                try:
                    set_approval_callback(_approval_callback(turn_conversation_id))
                    result = turn_agent.run_conversation(
                        turn_message, conversation_history=session.history.get(turn_conversation_id)
                    )
                    if session.stop_requested:
                        return
                    session.history[turn_conversation_id] = result.get("messages", [])
                    final = result.get("final_response", "") or ""
                    add_message(turn_conversation_id, "assistant", final)
                    session.reasoning[turn_conversation_id] = ""
                    session.thinking[turn_conversation_id] = ""
                    context = _estimate_context(session.history[turn_conversation_id], session.config)
                    session.send_from_worker("done", content=final, context=context, _conv_id=turn_conversation_id)
                except Exception as error:
                    if not session.stop_requested:
                        session.send_from_worker("error", message=str(error), _conv_id=turn_conversation_id)
                finally:
                    set_approval_callback(None)
                    session.send_from_worker("end", _conv_id=turn_conversation_id)

            threading.Thread(target=run_agent_turn, daemon=True).start()
    except WebSocketDisconnect:
        return
    finally:
        if session.ws is websocket:
            session.ws = None


@app.websocket("/ws/chat")
async def chat_websocket(websocket: WebSocket):
    if not _require_ws_auth(websocket) or not _origin_ok(websocket):
        await websocket.close(code=1008)
        return
    if REMOTE_URL:
        await _proxy_websocket(websocket)
    else:
        await _local_websocket(websocket)


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path:path}")
async def static_files(path: str):
    candidate = (FRONTEND_DIR / path).resolve()
    if candidate.is_relative_to(FRONTEND_DIR.resolve()) and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    mode = f"proxy → {urlparse(REMOTE_URL).hostname}" if REMOTE_URL else "local agent"
    print(f"Hermes Web ({mode}) → http://{HOST}:{port}")
    uvicorn.run(app, host=HOST, port=port, log_level=os.getenv("HERMES_LOG_LEVEL", "info"))
