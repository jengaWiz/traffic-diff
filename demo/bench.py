"""Measure the latency the recording proxy adds.

Sends the same GET requests to the demo API directly and through the proxy,
then prints p50/p95/p99 for each and the difference.

    python demo/bench.py --direct http://localhost:8001 --proxy http://localhost:8080
"""

import argparse
import asyncio
import statistics
import time

import httpx

from demo.traffic import make_paths


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return values[k]


async def measure(base_url: str, paths: list[str], concurrency: int) -> list[float]:
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=10, limits=limits) as client:

        async def one(path: str) -> None:
            async with sem:
                start = time.perf_counter()
                await client.get(path)
                latencies.append((time.perf_counter() - start) * 1000)

        # Warm up connections and code paths before measuring.
        await asyncio.gather(*(one(p) for p in paths[:100]))
        latencies.clear()
        await asyncio.gather(*(one(p) for p in paths))
    return latencies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", default="http://localhost:8001")
    parser.add_argument("--proxy", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    paths = make_paths(args.requests, seed=11)
    direct: list[float] = []
    proxied: list[float] = []
    # Alternate rounds so drift (thermal, background load) hits both sides equally.
    for _ in range(args.rounds):
        direct += asyncio.run(measure(args.direct, paths, args.concurrency))
        proxied += asyncio.run(measure(args.proxy, paths, args.concurrency))

    print(f"{len(direct)} requests per side, concurrency {args.concurrency}")
    print(f"{'':8}{'p50':>9}{'p95':>9}{'p99':>9}{'mean':>9}  (ms)")
    for name, xs in [("direct", direct), ("proxied", proxied)]:
        row = [percentile(xs, 50), percentile(xs, 95), percentile(xs, 99), statistics.mean(xs)]
        print(f"{name:8}" + "".join(f"{v:9.2f}" for v in row))
    print(
        f"added   {percentile(proxied, 50) - percentile(direct, 50):9.2f}"
        f"{percentile(proxied, 95) - percentile(direct, 95):9.2f}"
        f"{percentile(proxied, 99) - percentile(direct, 99):9.2f}"
        f"{statistics.mean(proxied) - statistics.mean(direct):9.2f}"
    )


if __name__ == "__main__":
    main()
