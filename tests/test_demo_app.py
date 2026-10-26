import pytest
from fastapi.testclient import TestClient

from demo.app import create_app


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_every_response_has_noise_fields(version):
    client = TestClient(create_app(version))
    for path in ["/health", "/products", "/products/1", "/orders/1", "/products/9999"]:
        body = client.get(path).json()
        assert "request_id" in body and "served_at" in body


def test_v1_product_shape():
    product = TestClient(create_app("v1")).get("/products/1").json()
    assert isinstance(product["price"], float)
    assert "in_stock" in product


def test_v2_planted_bugs():
    product = TestClient(create_app("v2")).get("/products/1").json()
    assert isinstance(product["price"], str)
    assert "in_stock" not in product and "available" in product


def test_v2_list_endpoint_is_unchanged():
    items = TestClient(create_app("v2")).get("/products").json()["items"]
    assert all(isinstance(p["price"], float) and "in_stock" in p for p in items)


def test_missing_product_is_404():
    assert TestClient(create_app("v1")).get("/products/9999").status_code == 404
