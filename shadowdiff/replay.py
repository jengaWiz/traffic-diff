"""Async replay of recorded exchanges against a target, plus run orchestration."""

import asyncio
import contextlib
import time
from dataclasses import dataclass

import httpx

from shadowdiff.config import Config
from shadowdiff.diff import compare
from shadowdiff.proxy import HOP_BY_HOP, STRIP_FROM_RESPONSE, strip_headers
from shadowdiff.store import Exchange, Result, Store

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
    # One single-connection client per concurrency slot. A single httpx client
    # with a large pool spends CPU per request proportional to the pool size,
    # which capped replay at ~250 req/s at concurrency 50; small pools scale.
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    async with contextlib.AsyncExitStack() as stack:
        free = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=target.rstrip("/"), timeout=timeout, limits=limits, transport=transport
                )
            )
            for _ in range(min(concurrency, len(exchanges)))
        ]

        async def one(ex: Exchange) -> Replayed:
            url = ex.path + (f"?{ex.query}" if ex.query else "")
            async with sem:
                # The semaphore guarantees a free client is available.
                client = free.pop()
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
                finally:
                    free.append(client)
                latency_ms = (time.perf_counter() - start) * 1000
            return Replayed(
                ex.id,
                resp.status_code,
                strip_headers(resp.headers, STRIP_FROM_RESPONSE),
                resp.content,
                latency_ms,
            )

        return await asyncio.gather(*(one(ex) for ex in exchanges))


@dataclass
class RunSummary:
    run_id: int
    total: int
    passed: int
    failed: int
    replay_seconds: float

    @property
    def requests_per_second(self) -> float:
        return self.total / self.replay_seconds if self.replay_seconds else 0.0


def run_replay(
    store: Store,
    session: str,
    target: str,
    config: Config,
    concurrency: int = 20,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RunSummary:
    """Replay a recorded session against TARGET, diff every response and store the run."""
    exchanges = store.exchanges(session)
    if not exchanges:
        raise ValueError(f"no recorded exchanges for session {session!r}")
    run_id = store.create_run(session, target, concurrency, total=len(exchanges))

    start = time.perf_counter()
    try:
        replayed = asyncio.run(replay_exchanges(exchanges, target, concurrency, timeout, transport))
    except Exception:
        store.finish_run(run_id, "failed", done=0, passed=0, failed=0)
        raise
    replay_seconds = time.perf_counter() - start

    results = []
    for ex, rep in zip(exchanges, replayed, strict=True):
        if rep.error is not None:
            diffs, breaking = [], True
        else:
            diffs = compare(ex.status, ex.resp_body, rep.status, rep.body, config)
            breaking = any(d.breaking for d in diffs)
        results.append(
            Result(
                exchange_id=ex.id,
                status=rep.status,
                resp_body=rep.body,
                latency_ms=rep.latency_ms,
                error=rep.error,
                diffs=[d.to_dict() for d in diffs],
                breaking=breaking,
            )
        )
    store.add_results(run_id, results)

    failed = sum(r.breaking for r in results)
    passed = len(results) - failed
    store.finish_run(run_id, "done", done=len(results), passed=passed, failed=failed)
    return RunSummary(run_id, len(results), passed, failed, replay_seconds)
