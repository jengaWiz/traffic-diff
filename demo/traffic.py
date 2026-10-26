"""Send realistic GET traffic to the demo shop (normally through the recorder).

python demo/traffic.py --base-url http://localhost:8080 --requests 2000
"""

import argparse
import asyncio
import random
import time
from collections import Counter

import httpx

CATEGORIES = ["books", "games", "kitchen", "garden", "toys"]


def make_paths(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    paths = []
    for _ in range(n):
        r = rng.random()
        if r < 0.45:
            # A few IDs past the catalogue end produce realistic 404s.
            paths.append(f"/products/{rng.randint(1, 110)}")
        elif r < 0.75:
            params = {"limit": rng.choice([5, 10, 20])}
            if rng.random() < 0.6:
                params["category"] = rng.choice(CATEGORIES)
            if rng.random() < 0.3:
                params["offset"] = rng.choice([0, 5, 10])
            paths.append("/products?" + "&".join(f"{k}={v}" for k, v in params.items()))
        elif r < 0.95:
            paths.append(f"/orders/{rng.randint(1, 55)}")
        else:
            paths.append("/health")
    return paths


async def send(base_url: str, paths: list[str], concurrency: int) -> Counter:
    sem = asyncio.Semaphore(concurrency)
    statuses: Counter = Counter()
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:

        async def one(path: str) -> None:
            async with sem:
                try:
                    statuses[(await client.get(path)).status_code] += 1
                except httpx.HTTPError as exc:
                    statuses[type(exc).__name__] += 1

        await asyncio.gather(*(one(p) for p in paths))
    return statuses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    paths = make_paths(args.requests, args.seed)
    start = time.perf_counter()
    statuses = asyncio.run(send(args.base_url, paths, args.concurrency))
    elapsed = time.perf_counter() - start
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items(), key=str))
    print(f"Sent {len(paths)} requests to {args.base_url} in {elapsed:.2f}s ({summary})")


if __name__ == "__main__":
    main()
