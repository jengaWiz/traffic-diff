import importlib

import pytest
from fastapi.testclient import TestClient


def _client(monkeypatch, version):
    monkeypatch.setenv("VERSION", version)
    import demo.app

    return TestClient(importlib.reload(demo.app).app)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_every_response_has_noise_fields(monkeypatch, version):
    client = _client(monkeypatch, version)
    for path in ["/health", "/products", "/products/1", "/orders/1", "/products/9999"]:
        body = client.get(path).json()
        assert "request_id" in body and "served_at" in body


def test_v1_product_shape(monkeypatch):
    product = _client(monkeypatch, "v1").get("/products/1").json()
    assert isinstance(product["price"], float)
    assert "in_stock" in product


def test_v2_planted_bugs(monkeypatch):
    product = _client(monkeypatch, "v2").get("/products/1").json()
    assert isinstance(product["price"], str)
    assert "in_stock" not in product and "available" in product


def test_missing_product_is_404(monkeypatch):
    assert _client(monkeypatch, "v1").get("/products/9999").status_code == 404
