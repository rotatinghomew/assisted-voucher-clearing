"""真实消耗在票间的归属与上下界。

证实（verified）对应给出可核下界；可能（possible）对应只能给出上界。
当一笔交付可落在多张票之间且无法唯一归属时，通过整网最大流判断各票的
真实消耗区间，而不是把整票视为空额或已用。同一交付单位最多核一次。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .facts import AppliedFacts, BarredRelation, usable_relations
from .loader import Dataset
from .models import Relation


class _MaxFlow:
    """整数容量的 Edmonds-Karp 最大流，容量规模很小，无需更复杂算法。"""

    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[dict]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> None:
        needed = max(u, v) + 1
        while len(self.graph) < needed:
            self.graph.append([])
        self.n = len(self.graph)
        forward = {"to": v, "cap": cap, "rev": len(self.graph[v])}
        backward = {"to": u, "cap": 0, "rev": len(self.graph[u])}
        self.graph[u].append(forward)
        self.graph[v].append(backward)

    def max_flow(self, source: int, sink: int) -> int:
        total = 0
        while True:
            parent_edge = [None] * self.n
            parent_node = [-1] * self.n
            parent_edge[source] = {}
            queue = deque([source])
            while queue and parent_node[sink] == -1:
                u = queue.popleft()
                for edge in self.graph[u]:
                    v = edge["to"]
                    if edge["cap"] > 0 and parent_node[v] == -1 and v != source:
                        parent_node[v] = u
                        parent_edge[v] = edge
                        queue.append(v)
            if parent_node[sink] == -1:
                return total
            pushed = 10**9
            v = sink
            while v != source:
                pushed = min(pushed, parent_edge[v]["cap"])
                v = parent_node[v]
            v = sink
            while v != source:
                edge = parent_edge[v]
                edge["cap"] -= pushed
                self.graph[edge["to"]][edge["rev"]]["cap"] += pushed
                v = parent_node[v]
            total += pushed


@dataclass(frozen=True)
class Attribution:
    # 每张票真实消耗的下界（证实可核）与上界（含可能归属）
    voucher_bounds: dict[str, tuple[int, int]]
    # 每笔交付的可核下界与可归属上界
    delivery_bounds: dict[str, tuple[int, int]]
    # 无法在票间唯一归属的交付，及其候选票
    ambiguous: dict[str, list[str]]
    # 整网可分配的最大总量（用于验证全部真实交付都有落点）
    flow_total: int
    # 每条可用对应贡献的证实下界
    verified_units: dict[str, int]
    barred: list[BarredRelation]

    def range_for_issuer_service(self, vouchers: list) -> dict[tuple[str, str], tuple[int, int]]:
        out: dict[tuple[str, str], tuple[int, int]] = {}
        for voucher in vouchers:
            key = (voucher.issuer, voucher.service)
            low, high = self.voucher_bounds[voucher.id]
            base = out.get(key, (0, 0))
            out[key] = (base[0] + low, base[1] + high)
        return out


def _build_network(
    data: Dataset,
    facts: AppliedFacts,
    usable: list[Relation],
):
    """构造 source->交付->票->sink 的流网，返回节点索引。"""
    deliveries = sorted(d.id for d in facts.deliveries.values() if d.units > 0)
    voucher_ids = sorted(v.id for v in data.vouchers)
    d_index = {d: i for i, d in enumerate(deliveries)}
    v_index = {v: i + len(deliveries) for i, v in enumerate(voucher_ids)}
    source = len(deliveries) + len(voucher_ids)
    sink = source + 1
    flow = _MaxFlow(sink + 1)

    for delivery_id in deliveries:
        flow.add_edge(source, d_index[delivery_id], facts.deliveries[delivery_id].units)
    for voucher in data.vouchers:
        flow.add_edge(v_index[voucher.id], sink, voucher.units)
    for relation in usable:
        flow.add_edge(d_index[relation.delivery], v_index[relation.voucher], relation.max_units)
    return flow, source, sink, d_index, v_index


def _voucher_can_take(
    data: Dataset,
    facts: AppliedFacts,
    usable: list[Relation],
    voucher_id: str,
    required: int,
    total_demand: int,
) -> bool:
    """是否存在一种全网分配，既覆盖全部真实交付，又让该票至少承担 required 份。

    增加需求节点 demand：票 -> demand -> sink，demand 只收 required 份，
    全网最大流达到 total_demand 且 demand->sink 饱和时，required 份确实
    由该票承担，其余交付也仍有落点。
    """
    if required == 0:
        return True
    flow, source, sink, _, v_index = _build_network(data, facts, usable)
    demand = flow.n
    flow.add_edge(v_index[voucher_id], demand, required)
    flow.add_edge(demand, sink, required)
    sink_edge = flow.graph[demand][-1]
    achieved = flow.max_flow(source, sink)
    saturated = sink_edge["cap"] == 0
    return achieved >= total_demand and saturated


def attribute(data: Dataset, facts: AppliedFacts) -> Attribution:
    usable, barred = usable_relations(facts, data)
    voucher_units = {v.id: v.units for v in data.vouchers}

    verified: dict[str, int] = {}
    lower: dict[str, int] = {v.id: 0 for v in data.vouchers}
    delivery_lower: dict[str, int] = {
        d.id: 0 for d in facts.deliveries.values() if d.units > 0
    }
    for relation in usable:
        if relation.state != "verified":
            continue
        units = min(relation.max_units, facts.deliveries[relation.delivery].units)
        verified[relation.id] = units
        lower[relation.voucher] += units
        delivery_lower[relation.delivery] += units

    base_flow, source, sink, _, _ = _build_network(data, facts, usable)
    flow_total = base_flow.max_flow(source, sink)
    total_demand = sum(d.units for d in facts.deliveries.values() if d.units > 0)

    upper: dict[str, int] = {}
    for voucher_id in sorted(voucher_units):
        lo, hi = lower[voucher_id], voucher_units[voucher_id]
        best = lo
        while lo <= hi:
            mid = (lo + hi) // 2
            if _voucher_can_take(data, facts, usable, voucher_id, mid, total_demand):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        upper[voucher_id] = best

    # 候选票与歧义：一笔交付挂多张票即无法唯一归属。
    candidates: dict[str, set[str]] = {}
    for relation in usable:
        candidates.setdefault(relation.delivery, set()).add(relation.voucher)
    ambiguous = {
        delivery_id: sorted(vouchers)
        for delivery_id, vouchers in candidates.items()
        if len(vouchers) > 1
    }

    delivery_bounds: dict[str, tuple[int, int]] = {}
    for delivery_id, delivery in facts.deliveries.items():
        if delivery.units <= 0:
            delivery_bounds[delivery_id] = (0, 0)
            continue
        cap = sum(r.max_units for r in usable if r.delivery == delivery_id)
        delivery_bounds[delivery_id] = (
            delivery_lower.get(delivery_id, 0),
            min(delivery.units, cap),
        )

    return Attribution(
        voucher_bounds={v.id: (lower[v.id], upper[v.id]) for v in data.vouchers},
        delivery_bounds=delivery_bounds,
        ambiguous=ambiguous,
        flow_total=flow_total,
        verified_units=verified,
        barred=barred,
    )
