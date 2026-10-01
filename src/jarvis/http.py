"""Shared httpx clients.

Every outgoing call used to build its own `httpx.AsyncClient`, i.e. a fresh TCP + TLS handshake per request
(one per Gmail message during a backfill, one per Home Assistant poll…). `shared()` hands out one long-lived
client per (timeout, verify) pair so connections are pooled and kept alive. The diagnostics hook on
`httpx.AsyncClient.send` still sees every request. Call `aclose_all()` on shutdown.
"""

from __future__ import annotations

import asyncio
import weakref

import httpx

# Clients hold connections bound to one event loop, so they are kept per loop; a weak key lets a finished loop
# (tests run one per case) take its clients with it instead of a reused id() handing them to a new loop.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, httpx.AsyncClient]]" = \
    weakref.WeakKeyDictionary()
_loopless: dict[tuple, httpx.AsyncClient] = {}
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60)


def _pool() -> dict[tuple, httpx.AsyncClient]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _loopless
    pool = _clients.get(loop)
    if pool is None:
        pool = _clients[loop] = {}
    return pool


def shared(timeout: float | httpx.Timeout = 30, verify: bool | str = True, **kwargs) -> httpx.AsyncClient:
    """A pooled client for plain request/response calls. Don't close it; don't use it as a context manager."""
    pool = _pool()
    key = (repr(timeout) if isinstance(timeout, httpx.Timeout) else timeout, verify, tuple(sorted(kwargs.items())))
    client = pool.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, verify=verify, limits=LIMITS, **kwargs)
        pool[key] = client
    return client


async def aclose_all() -> None:
    """Close this loop's clients (called from the app lifespan)."""
    pool = _pool()
    clients = list(pool.values())
    pool.clear()
    await asyncio.gather(*(c.aclose() for c in clients if not c.is_closed), return_exceptions=True)
