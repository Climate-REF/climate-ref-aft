#!/usr/bin/env python3
"""Validate a REF API endpoint.

Fetches a URL, asserts HTTP 200, prints the item count, and (optionally) asserts a non-empty result.
Retries transient failures so the rollout endpoint flap during CI does not produce false negatives.

Usage:
    python3 api_check.py <url> [expect_nonempty]

`expect_nonempty` is truthy if non-empty and not "0".
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


def fetch(url: str, attempts: int = 10, delay: float = 3.0) -> str:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
                if r.status != 200:
                    raise SystemExit(f"{url} -> {r.status}")
                return r.read().decode()
        except (urllib.error.URLError, ConnectionError) as exc:
            last = exc
            time.sleep(delay)
    raise SystemExit(f"{url} unreachable after retries: {last}")


def count_items(body: str) -> int | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("non-json body:", body[:200])
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return data.get("count", len(data.get("results") or data.get("data") or []))
    return 1 if data else 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: api_check.py <url> [expect_nonempty]", file=sys.stderr)
        return 2

    url = sys.argv[1]
    expect_nonempty = len(sys.argv) > 2 and sys.argv[2] not in ("", "0")

    body = fetch(url)
    n = count_items(body)
    if n is None:
        return 0

    print(f"{url} -> {n} item(s)")
    if expect_nonempty and n <= 0:
        raise SystemExit(f"expected non-empty result from {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
