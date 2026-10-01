"""Ollama client (the model runs on the PC GPU and is reached over the LAN)."""

from __future__ import annotations

import json
import re
import time
from typing import AsyncIterator

import httpx

from . import diag


class LLMError(Exception):
    """A user-facing model error."""


class Ollama:
    def __init__(self, base_url: str, chat_model: str, router_model: str, timeout: float = 180.0,
                 keep_alive: str = "-1") -> None:
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.router_model = router_model
        self.timeout = timeout
        # Ollama takes a number of seconds (-1 = forever, 0 = unload now) or a duration such as "2h"
        value = str(keep_alive).strip() or "-1"
        self.keep_alive: int | str = int(value) if re.fullmatch(r"-?\d+", value) else value
        self._was_online: bool | None = None

    async def loaded(self) -> dict[str, str] | None:
        """Models currently in GPU memory → when Ollama will unload them ('' = never). None if unreachable."""
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                response = await client.get(f"{self.base_url}/api/ps")
                response.raise_for_status()
                return {m.get("name", ""): m.get("expires_at", "") for m in response.json().get("models", []) or []
                        if isinstance(m, dict)}
        except (httpx.HTTPError, ValueError):
            return None

    async def warm(self) -> str:
        """Background job: load the model(s) into GPU memory as soon as Ollama is reachable (e.g. after the PC
        boots or Ollama is restarted after gaming), so the first question doesn't wait for a load."""
        loaded = await self.loaded()
        if loaded is None:
            if self._was_online:
                diag.event("model", "Ollama went offline (PC off or Ollama closed)")
            self._was_online = False
            return "Ollama offline"
        came_back = self._was_online is False
        self._was_online = True
        wanted = [m for m in dict.fromkeys([self.chat_model, self.router_model]) if m]
        missing = [m for m in wanted if m not in loaded and f"{m}:latest" not in loaded]
        if not missing:
            return "model in memory" + (" (Ollama is back)" if came_back else "")
        for model in missing:
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(f"{self.base_url}/api/generate",
                                                 json={"model": model, "keep_alive": self.keep_alive})
                    response.raise_for_status()
            except httpx.HTTPError as error:
                diag.warning("model", f"couldn't load {model}: {type(error).__name__}", error=str(error))
                return f"couldn't load {model}"
            diag.event("model", f"loaded {model} into GPU memory in {round((time.perf_counter() - started) * 1000)} ms",
                       keep_alive=self.keep_alive)
        return f"loaded {', '.join(missing)}"

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                models = [m.get("name") for m in response.json().get("models", []) if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError) as error:
            return {"ok": False, "detail": f"unreachable ({type(error).__name__})"}
        missing = [m for m in {self.chat_model, self.router_model} if m not in models]
        if missing:
            return {"ok": False, "detail": "missing model(s): " + ", ".join(missing), "models": models}
        loaded = await self.loaded() or {}
        in_memory = self.chat_model in loaded or f"{self.chat_model}:latest" in loaded
        state = "in GPU memory" if in_memory else "not loaded yet — loads on first use"
        return {"ok": True, "detail": f"ready ({self.chat_model}, {state})", "models": models}

    @staticmethod
    def _describe(messages: list[dict]) -> dict:
        info: dict = {"messages": len(messages), "prompt_chars": sum(len(m.get("content", "")) for m in messages)}
        if diag.verbose():
            info["prompt"] = [{"role": m.get("role"), "content": m.get("content", "")[:4000]} for m in messages]
        return info

    @staticmethod
    def _stats(data: dict) -> dict:
        stats = {k: data[k] for k in ("prompt_eval_count", "eval_count") if k in data}
        if data.get("total_duration"):
            stats["model_ms"] = round(data["total_duration"] / 1e6)
        if data.get("load_duration") and data["load_duration"] > 1e9:
            stats["load_ms"] = round(data["load_duration"] / 1e6)  # model was (re)loaded into GPU memory
        return stats

    async def chat(self, messages: list[dict], model: str | None = None, json_mode: bool = False) -> str:
        model = model or self.chat_model
        payload: dict = {"model": model, "messages": messages, "stream": False, "keep_alive": self.keep_alive}
        if json_mode:
            payload["format"] = "json"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                content = data.get("message", {}).get("content", "")
        except httpx.HTTPError as error:
            diag.warning("model", f"{model} call failed: {type(error).__name__}", error=str(error),
                         **self._describe(messages))
            raise LLMError(f"Could not reach Ollama at {self.base_url}: {type(error).__name__}") from None
        except ValueError:
            diag.warning("model", f"{model} returned invalid JSON")
            raise LLMError("Ollama returned an invalid response.") from None
        elapsed = round((time.perf_counter() - started) * 1000)
        diag.event("model", f"{model}{' (json)' if json_mode else ''}: {elapsed} ms",
                   output=content[:4000] if diag.verbose() or json_mode else f"{len(content)} chars",
                   **self._stats(data), **self._describe(messages))
        if not isinstance(content, str) or not content.strip():
            diag.warning("model", f"{model} returned an empty response")
            raise LLMError("Ollama returned an empty response.")
        return content.strip()

    async def stream(self, messages: list[dict], model: str | None = None) -> AsyncIterator[str]:
        payload = {"model": model or self.chat_model, "messages": messages, "stream": True,
                   "keep_alive": self.keep_alive}
        started = time.perf_counter()
        first: int | None = None
        output = ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        text = chunk.get("message", {}).get("content", "")
                        if text:
                            if first is None:
                                first = round((time.perf_counter() - started) * 1000)
                            output += text
                            yield text
                        if chunk.get("done"):
                            diag.event("model", f"{payload['model']} streamed answer: {len(output)} chars, first token "
                                       f"after {first} ms", first_token_ms=first,
                                       output=output[:4000] if diag.verbose() else None,
                                       **self._stats(chunk), **self._describe(messages))
                            break
        except httpx.HTTPError as error:
            diag.warning("model", f"{payload['model']} stream failed: {type(error).__name__}", error=str(error),
                         received_chars=len(output))
            raise LLMError(f"Could not reach Ollama at {self.base_url}: {type(error).__name__}") from None
