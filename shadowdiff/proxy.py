"""Recording reverse proxy.

Forwards every request to an upstream unchanged and records ``GET`` exchanges
with 2xx-4xx responses. Recording happens off the response path: exchanges go
onto an in-memory queue that a background task flushes to SQLite in batches.
"""

import asyncio
import contextlib
import time

import httpx
from fastapi import FastAPI, Request, Response

from shadowdiff.store import Exchange, Store

MAX_BODY_BYTES = 1024 * 1024
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
# httpx transparently decodes compressed bodies, so the encoding header no longer applies.
STRIP_FROM_RESPONSE = HOP_BY_HOP | {"content-encoding"}


def strip_headers(headers, drop: frozenset[str] = HOP_BY_HOP) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def should_record(method: str, status: int, req_body: bytes, resp_body: bytes) -> bool:
    return (
        method == "GET"
        and 200 <= status < 500
        and len(req_body) <= MAX_BODY_BYTES
        and len(resp_body) <= MAX_BODY_BYTES
    )


class Recorder:
    """Buffers exchanges and writes them to the store in batches."""

    _STOP = object()

    def __init__(self, store: Store, batch_size: int = 200):
        self.store = store
        self.batch_size = batch_size
        self.queue: asyncio.Queue = asyncio.Queue()
        self.recorded = 0

    def submit(self, exchange: Exchange) -> None:
        self.queue.put_nowait(exchange)

    def stop(self) -> None:
        """Ask run() to flush everything submitted so far, then return."""
        self.queue.put_nowait(self._STOP)

    async def run(self) -> None:
        # Stopping with a sentinel rather than cancelling means a batch is never
        # abandoned mid-write and nothing submitted before stop() is lost.
        stopping = False
        while not stopping:
            batch = []
            item = await self.queue.get()
            while True:
                if item is self._STOP:
                    stopping = True
                    break
                batch.append(item)
                if len(batch) >= self.batch_size or self.queue.empty():
                    break
                item = self.queue.get_nowait()
            if batch:
                await asyncio.to_thread(self.store.add_exchanges, batch)
                self.recorded += len(batch)


class ClientPool:
    """A growable LIFO pool of single-connection httpx clients.

    One shared client with a large connection pool spends CPU per request
    proportional to the pool size, which added tens of milliseconds of p95
    latency under concurrent load. Single-connection clients avoid that, and
    LIFO reuse keeps the warmest connections busy.
    """

    def __init__(self, factory, max_size: int = 200):
        self.factory = factory
        self.max_size = max_size
        self.idle: asyncio.LifoQueue[httpx.AsyncClient] = asyncio.LifoQueue()
        self.clients: list[httpx.AsyncClient] = []

    @contextlib.asynccontextmanager
    async def client(self):
        if not self.idle.empty():
            client = self.idle.get_nowait()
        elif len(self.clients) < self.max_size:
            client = self.factory()
            self.clients.append(client)
        else:
            client = await self.idle.get()
        try:
            yield client
        finally:
            self.idle.put_nowait(client)

    async def aclose(self) -> None:
        for client in self.clients:
            await client.aclose()


def create_app(
    target: str,
    store: Store,
    session: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    target = target.rstrip("/")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        recorder = Recorder(store)
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        pool = ClientPool(
            lambda: httpx.AsyncClient(
                base_url=target, limits=limits, timeout=30, transport=transport
            )
        )
        app.state.pool = pool
        app.state.recorder = recorder
        writer = asyncio.create_task(recorder.run())
        try:
            yield
        finally:
            recorder.stop()
            await writer
            await pool.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def forward(request: Request, path: str) -> Response:
        pool: ClientPool = request.app.state.pool
        req_body = await request.body()
        req_headers = strip_headers(request.headers)
        query = request.url.query
        url = request.url.path + (f"?{query}" if query else "")

        start = time.perf_counter()
        try:
            async with pool.client() as client:
                upstream = await client.request(
                    request.method, url, headers=req_headers, content=req_body
                )
        except httpx.HTTPError as exc:
            return Response(f"shadowdiff proxy: upstream error: {exc!r}", status_code=502)
        latency_ms = (time.perf_counter() - start) * 1000

        resp_headers = strip_headers(upstream.headers, STRIP_FROM_RESPONSE)
        resp_body = upstream.content
        if should_record(request.method, upstream.status_code, req_body, resp_body):
            request.app.state.recorder.submit(
                Exchange(
                    session=session,
                    method=request.method,
                    path=request.url.path,
                    query=query,
                    req_headers=req_headers,
                    req_body=req_body,
                    status=upstream.status_code,
                    resp_headers=resp_headers,
                    resp_body=resp_body,
                    latency_ms=latency_ms,
                )
            )
        return Response(content=resp_body, status_code=upstream.status_code, headers=resp_headers)

    return app
