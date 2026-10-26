import asyncio

import httpx

from shadowdiff.replay import replay_exchanges
from shadowdiff.store import Exchange


def _ex(i: int, path: str, query: str = "") -> Exchange:
    return Exchange(
        id=i,
        session="s",
        method="GET",
        path=path,
        query=query,
        req_headers={"host": "old-host", "x-trace": "abc"},
        req_body=b"",
        status=200,
        resp_headers={},
        resp_body=b"",
        latency_ms=1.0,
    )


def test_replay_captures_responses_and_errors():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/slow":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"path": request.url.path, "q": request.url.query.decode()})

    exchanges = [_ex(1, "/a", "x=1"), _ex(2, "/down"), _ex(3, "/slow")]
    results = asyncio.run(
        replay_exchanges(exchanges, "http://target", transport=httpx.MockTransport(handler))
    )

    ok, down, slow = results
    assert ok.status == 200 and ok.error is None and ok.latency_ms is not None
    assert ok.body == b'{"path":"/a","q":"x=1"}'
    assert down.status is None and "ConnectError" in down.error
    assert slow.status is None and "timeout" in slow.error
    # Recorded hop-by-hop headers are not replayed; the rest are.
    assert seen[0].headers["host"] == "target"
    assert seen[0].headers["x-trace"] == "abc"


def test_replay_respects_concurrency_limit():
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    exchanges = [_ex(i, f"/{i}") for i in range(30)]
    results = asyncio.run(
        replay_exchanges(
            exchanges, "http://t", concurrency=5, transport=httpx.MockTransport(handler)
        )
    )
    assert len(results) == 30
    assert peak <= 5
