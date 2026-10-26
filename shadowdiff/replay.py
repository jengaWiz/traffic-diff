"""Async replay of recorded exchanges against a target."""

import asyncio
import time
from dataclasses import dataclass

import httpx

from shadowdiff.proxy import HOP_BY_HOP, STRIP_FROM_RESPONSE, strip_headers
from shadowdiff.store import Exchange

DEFAULT_TIMEOUT = 10.0


@dataclass
class Replayed:
    exchange_id: int
    status: int | None
    headers: dict[str, str]
    body: bytes | None
    latency_ms: float | None
    error: str | None = None


async def replay_exchanges(
    exchanges: list[Exchange],
    target: str,
    concurrency: int = 20,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Replayed]:
    """Send each recorded request to TARGET; failures are captured, never raised."""
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        base_url=target.rstrip("/"), timeout=timeout, limits=limits, transport=transport
    ) as client:

        async def one(ex: Exchange) -> Replayed:
            url = ex.path + (f"?{ex.query}" if ex.query else "")
            async with sem:
                start = time.perf_counter()
                try:
                    resp = await client.request(
                        ex.method,
                        url,
                        headers=strip_headers(ex.req_headers, HOP_BY_HOP),
                        content=ex.req_body or None,
                    )
                except httpx.TimeoutException:
                    return Replayed(ex.id, None, {}, None, None, f"timeout after {timeout:g}s")
                except httpx.HTTPError as exc:
                    return Replayed(ex.id, None, {}, None, None, f"{type(exc).__name__}: {exc}")
                latency_ms = (time.perf_counter() - start) * 1000
            return Replayed(
                ex.id,
                resp.status_code,
                strip_headers(resp.headers, STRIP_FROM_RESPONSE),
                resp.content,
                latency_ms,
            )

        return await asyncio.gather(*(one(ex) for ex in exchanges))
