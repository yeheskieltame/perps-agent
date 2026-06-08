"""ShardRouter — consistent hashing so the engine plane scales across workers
(plan/SCALING.md #10).

`shard = route(user_id)` decides which worker owns a user; that worker holds all of
that user's engines, fill streams, and exchange clients (one event loop per worker
→ N cores). Consistent hashing (a hash ring with virtual nodes) means adding or
removing a shard remaps only ~1/N of users, not almost all of them as plain
`hash % N` would — so scaling out doesn't reshuffle every user's grids.

Hashing uses hashlib (NOT the builtin `hash`, which is per-process salted) so every
worker computes the SAME routing for a given user_id.
"""
from __future__ import annotations

import bisect
import hashlib
from typing import Iterable


def _h(key: str) -> int:
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big")


class ShardRouter:
    def __init__(self, nodes: int | Iterable[str], vnodes: int = 256) -> None:
        if isinstance(nodes, int):
            if nodes < 1:
                raise ValueError("need >= 1 shard")
            nodes = [str(i) for i in range(nodes)]
        self.nodes = list(dict.fromkeys(str(n) for n in nodes))  # dedupe, keep order
        if not self.nodes:
            raise ValueError("need >= 1 shard")
        self._vnodes = vnodes
        ring = sorted((_h(f"{n}#{i}"), n) for n in self.nodes for i in range(vnodes))
        self._pos = [p for p, _ in ring]
        self._owner = [n for _, n in ring]

    def route(self, user_id) -> str:
        """The shard that owns this user (first ring point clockwise from the key)."""
        i = bisect.bisect(self._pos, _h(str(user_id))) % len(self._pos)
        return self._owner[i]

    def owns(self, user_id, node) -> bool:
        return self.route(user_id) == str(node)
