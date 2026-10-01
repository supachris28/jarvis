"""Shared httpx clients.

Every outgoing call used to build its own `httpx.AsyncClient`, i.e. a fresh TCP + TLS handshake per request
(one per Gmail message during a backfill, one per Home Assistant poll…). `shared()` hands out one long-lived
client per (timeout, verify) pair so connections are pooled and kept alive. The diagnostics hook on
`httpx.AsyncClient.send` still sees every request. Call `aclose_all()` on shutdown.
"""

from __future__ import annotations

import asyncio

import httpx

_clients: dict[tuple, httpx.AsyncClient] = {}
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=60)


def shared(timeout: float | httpx.Timeout = 30, verify: bool | str = True, **kwargs) -> httpx.AsyncClient:
    """A pooled client for plain request/response calls. Don't close it; don't use it as a context manager.

    Clients are tied to the event loop whose connections they hold, so the cache is keyed by loop as well
    (tests spin up a fresh loop per case)."""
    try:
        loop_key: object = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_key = None
    key = (loop_key, timeout if not isinstance(timeout, httpx.Timeout) else repr(timeout), verify,
           tuple(sorted(kwargs.items())))
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, verify=verify, limits=LIMITS, **kwargs)
        _clients[key] = client
    return client


async def aclose_all() -> None:
    clients = list(_clients.values())
    _clients.clear()
    await asyncio.gather(*(c.aclose() for c in clients if not c.is_closed), return_exceptions=True)
