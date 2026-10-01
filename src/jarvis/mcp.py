"""Minimal async client for remote MCP servers over Streamable HTTP."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

import httpx

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    """A user-facing MCP error."""


class MCPClient:
    def __init__(self, name: str, endpoint: str, auth: Callable[[], Awaitable[dict]] | None = None,
                 timeout: float = 60.0) -> None:
        self.name = name
        self.endpoint = endpoint
        self.auth = auth
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0
        self._tools: list[dict] | None = None

    async def _post(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.auth:
            headers.update(await self.auth())
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.endpoint, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise MCPError(f"Could not reach {self.name}: {type(error).__name__}") from None
        if response.status_code >= 400:
            raise MCPError(f"{self.name} returned HTTP {response.status_code}: {response.text[:200]}")
        self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
        if notify:
            return None
        raw = response.text
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event.get("id") == body["id"]:
                        return self._result(event)
            raise MCPError(f"{self.name} stream ended without a response.")
        try:
            return self._result(json.loads(raw))
        except ValueError:
            raise MCPError(f"{self.name} returned invalid JSON.") from None

    def _result(self, message: dict) -> dict:
        if "error" in message:
            raise MCPError(str(message["error"].get("message", "MCP request failed")))
        return message.get("result", {})

    async def initialize(self) -> None:
        if self.session_id:
            return
        await self._post("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                        "clientInfo": {"name": "jarvis", "version": "0.2"}})
        await self._post("notifications/initialized", notify=True)

    async def tools(self) -> list[dict]:
        if self._tools is None:
            await self.initialize()
            result = await self._post("tools/list", {})
            self._tools = [t for t in (result or {}).get("tools", []) if isinstance(t, dict)]
        return self._tools

    async def call(self, name: str, arguments: dict) -> dict:
        await self.initialize()
        result = await self._post("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            raise MCPError(text or f"{self.name} tool {name} failed")
        return result


def result_text(result: dict, limit: int = 12000) -> str:
    if result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False, indent=1)
    else:
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    return text[:limit]
