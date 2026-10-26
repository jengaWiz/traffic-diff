"""Demo e-commerce API used to show shadowdiff end to end.

Run v1:  VERSION=v1 uvicorn demo.app:app --port 8001
Run v2:  VERSION=v2 uvicorn demo.app:app --port 8002

Every response carries noise (a fresh ``request_id`` UUID and a ``served_at``
timestamp), and ``GET /products`` returns its items in random order. v2 ships
two planted bugs on ``GET /products/{id}``: ``price`` is a string, and
``in_stock`` is renamed to ``available``.
"""

import os
import random
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

CATEGORIES = ["books", "games", "kitchen", "garden", "toys"]

_rng = random.Random(42)
PRODUCTS = {
    i: {
        "id": i,
        "name": f"Product {i}",
        "category": _rng.choice(CATEGORIES),
        "price": round(_rng.uniform(3, 250), 2),
        "in_stock": _rng.random() > 0.2,
        "tags": sorted(_rng.sample(["new", "sale", "popular", "gift", "eco"], k=2)),
        "updated_at": "2026-10-01T12:00:00Z",
    }
    for i in range(1, 101)
}
ORDERS = {
    i: {
        "id": i,
        "status": _rng.choice(["pending", "shipped", "delivered"]),
        "lines": [
            {"product_id": pid, "qty": _rng.randint(1, 4)}
            for pid in _rng.sample(list(PRODUCTS), k=_rng.randint(1, 4))
        ],
        "created_at": "2026-10-02T08:30:00Z",
    }
    for i in range(1, 51)
}
for order in ORDERS.values():
    order["total"] = round(
        sum(PRODUCTS[line["product_id"]]["price"] * line["qty"] for line in order["lines"]), 2
    )


def _noisy(body: dict, version: str, status_code: int = 200) -> JSONResponse:
    body = {
        **body,
        "request_id": str(uuid.uuid4()),
        "served_at": datetime.now(UTC).isoformat(),
    }
    return JSONResponse(body, status_code=status_code, headers={"x-demo-version": version})


def create_app(version: str = "v1") -> FastAPI:
    app = FastAPI(title=f"shadowdiff demo shop ({version})")

    def not_found(kind: str, item_id: int) -> JSONResponse:
        return _noisy({"error": f"{kind} {item_id} not found"}, version, status_code=404)

    @app.get("/health")
    async def health():
        return _noisy({"ok": True}, version)

    @app.get("/products")
    async def list_products(category: str | None = None, limit: int = 20, offset: int = 0):
        items = [p for p in PRODUCTS.values() if category is None or p["category"] == category]
        page = items[offset : offset + limit]
        # Simulate a backend that doesn't guarantee ordering (e.g. a hash-partitioned store).
        page = random.sample(page, k=len(page))
        return _noisy(
            {"items": page, "total": len(items), "limit": limit, "offset": offset}, version
        )

    @app.get("/products/{product_id}")
    async def get_product(product_id: int):
        product = PRODUCTS.get(product_id)
        if product is None:
            return not_found("product", product_id)
        product = dict(product)
        if version == "v2":
            # Planted bug 1: price serialized as a string.
            product["price"] = f"{product['price']:.2f}"
            # Planted bug 2: in_stock renamed to available.
            product["available"] = product.pop("in_stock")
        return _noisy(product, version)

    @app.get("/orders/{order_id}")
    async def get_order(order_id: int):
        order = ORDERS.get(order_id)
        if order is None:
            return not_found("order", order_id)
        return _noisy(order, version)

    return app


app = create_app(os.environ.get("VERSION", "v1"))
