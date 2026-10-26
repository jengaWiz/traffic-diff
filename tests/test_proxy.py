import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from shadowdiff.proxy import MAX_BODY_BYTES, create_app
from shadowdiff.store import Store

upstream = FastAPI()


@upstream.get("/items/{item_id}")
def get_item(item_id: int, q: str = ""):
    if item_id == 404:
        return JSONResponse({"error": "missing"}, status_code=404)
    if item_id == 500:
        return JSONResponse({"error": "boom"}, status_code=500)
    return {"id": item_id, "q": q}


@upstream.get("/big")
def big():
    return PlainTextResponse("x" * (MAX_BODY_BYTES + 1))


@upstream.post("/items")
async def create_item():
    return {"created": True}


@upstream.get("/echo-headers")
def echo_headers(request: Request):
    return dict(request.headers)


def _proxy(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    app = create_app("http://upstream", store, "s1", transport=httpx.ASGITransport(app=upstream))
    return store, TestClient(app)


def test_forwards_and_records_get(tmp_path):
    store, client = _proxy(tmp_path)
    with client:
        resp = client.get("/items/7?q=hello")
        assert resp.status_code == 200
        assert resp.json() == {"id": 7, "q": "hello"}
    [ex] = store.exchanges("s1")
    assert (ex.method, ex.path, ex.query, ex.status) == ("GET", "/items/7", "q=hello", 200)
    assert ex.resp_body == resp.content


def test_record_filters(tmp_path):
    store, client = _proxy(tmp_path)
    with client:
        assert client.get("/items/404").status_code == 404
        assert client.get("/items/500").status_code == 500
        assert client.post("/items").json() == {"created": True}
        assert len(client.get("/big").content) == MAX_BODY_BYTES + 1
    recorded = [(e.path, e.status) for e in store.exchanges("s1")]
    assert recorded == [("/items/404", 404)]


def test_strips_hop_by_hop_headers(tmp_path):
    _, client = _proxy(tmp_path)
    with client:
        headers = client.get("/echo-headers", headers={"x-custom": "1"}).json()
    assert headers["x-custom"] == "1"
    assert headers["host"] == "upstream"


def test_upstream_down_returns_502(tmp_path):
    store = Store(str(tmp_path / "test.db"))

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    app = create_app("http://upstream", store, "s1", transport=httpx.MockTransport(refuse))
    with TestClient(app) as client:
        assert client.get("/anything").status_code == 502
    assert store.exchanges("s1") == []


def test_client_pool_reuses_and_caps_clients():
    import asyncio

    from shadowdiff.proxy import ClientPool

    async def scenario():
        made = []
        pool = ClientPool(lambda: made.append(object()) or made[-1], max_size=2)
        async with pool.client() as a:
            async with pool.client() as b:
                assert a is not b
        async with pool.client() as c:
            assert c is a or c is b
        assert len(made) == 2

        # At the cap, a third concurrent user waits for a client to be returned.
        order = []

        async def use(name, hold):
            async with pool.client():
                order.append(name)
                await asyncio.sleep(hold)

        await asyncio.gather(use("x", 0.02), use("y", 0.02), use("z", 0))
        assert sorted(order) == ["x", "y", "z"] and order[-1] == "z"
        assert len(made) == 2

    asyncio.run(scenario())


def test_recorder_flushes_everything_on_stop_even_mid_write():
    import asyncio
    import threading
    import time as _time

    from shadowdiff.proxy import Recorder

    class SlowStore:
        def __init__(self):
            self.rows = []
            self.lock = threading.Lock()

        def add_exchanges(self, batch):
            _time.sleep(0.01)  # a write still in flight when shutdown starts
            with self.lock:
                self.rows.extend(batch)

    async def scenario():
        store = SlowStore()
        recorder = Recorder(store, batch_size=7)
        writer = asyncio.create_task(recorder.run())
        for i in range(50):
            recorder.submit(i)
            if i % 10 == 0:
                await asyncio.sleep(0)  # let the writer start a batch
        recorder.stop()
        await writer
        return store.rows, recorder.recorded

    rows, recorded = asyncio.run(scenario())
    assert rows == list(range(50))
    assert recorded == 50
