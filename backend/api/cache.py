"""Shared short-TTL stale-while-revalidate cache (issue #888).

In-process cache for read-mostly list endpoints (inbox, prs): every
Now/Review tab switch pays a full synchronous gh fan-out (~1.2-2.1s
measured) without it. Same idiom as the briefing router's SWR entry
(TTL + max-stale + background revalidate + cached/stale/generated_at
markers), factored once so the second and third users share it. The
briefing module keeps its bespoke entry (narrator coupling); new
list endpoints use this.

Contract:
- read(key, fetch): fresh entries serve instantly; stale entries
  serve instantly and refresh on a daemon thread; entries past
  max-stale (or missing) recompute synchronously via fetch() so a
  broken refresher can never pin ancient data. Every response is a
  copy carrying freshness markers; the stored entry is never mutated.
- fetch exceptions on a cold/must-revalidate read propagate to the
  caller (routes convert them to HTTP errors, nothing is cached);
  background refresh failures keep the stale entry.
- peek(key): stored data without triggering a build, for rollups
  that compose cached per-repo entries. Read-only — never mutate
  the returned dict.
- invalidate(*keys) / clear(): mutation paths drop entries so the
  operator never sees their own action vanish into stale cache.

Tests reset instances via clear(), and patch _spawn_daemon with an
inline runner for deterministic revalidate assertions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


def _spawn_daemon(target: Callable, args: tuple) -> None:
    """Injectable seam: background refresh thread (tests run inline)."""
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


class SWRCache:
    """Thread-safe short-TTL SWR cache keyed by repo name."""

    def __init__(self, ttl: float = 45.0, max_stale: float = 300.0):
        self.ttl = ttl
        self.max_stale = max_stale
        self._entries: dict[str, tuple[float, dict]] = {}
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()

    def read(self, key: str, fetch: Callable[[], dict]) -> dict:
        """Serve key from cache, building via fetch() on miss/staleness."""
        now = time.time()
        hit = self._entries.get(key)
        if hit is not None:
            age = now - hit[0]
            if age < self.ttl:
                return {**hit[1], "cached": True, "stale": False,
                        "generated_at": hit[0]}
            if age < self.max_stale:
                self._revalidate(key, fetch)
                return {**hit[1], "cached": True, "stale": True,
                        "generated_at": hit[0]}
        data = fetch()
        ts = self.store(key, data)
        return {**data, "cached": False, "stale": False,
                "generated_at": ts}

    def peek(self, key: str) -> dict | None:
        """Stored data for key, or None. Never builds, never refreshes."""
        hit = self._entries.get(key)
        return hit[1] if hit is not None else None

    def store(self, key: str, data: dict) -> float:
        """Store data under key; returns the store timestamp."""
        ts = time.time()
        with self._lock:
            self._entries[key] = (ts, data)
            self._refreshing.discard(key)
        return ts

    def invalidate(self, *keys: str) -> None:
        """Drop listed keys; unknown keys are a no-op. Never raises."""
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop every entry. Never raises."""
        with self._lock:
            self._entries.clear()

    def _revalidate(self, key: str, fetch: Callable[[], dict]) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
        _spawn_daemon(self._refresh, (key, fetch))

    def _refresh(self, key: str, fetch: Callable[[], dict]) -> None:
        """Background SWR refresh: failures keep the stale entry."""
        try:
            data = fetch()
        except Exception:
            with self._lock:
                self._refreshing.discard(key)
            return
        self.store(key, data)
