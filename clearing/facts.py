"""更正与作废的生效处理。

更正按 ``recorded`` 顺序应用，后者覆盖前者；只改被指向的事实，付款等
下游单据不回写。作废只约束其生效日之后发生的使用，发生在生效日之前的
交付对应关系仍然有效。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .loader import Dataset
from .models import Correction, Delivery, Relation


@dataclass(frozen=True)
class AppliedFacts:
    deliveries: dict[str, Delivery]
    relations: dict[str, Relation]
    corrections: list[Correction]
    # 每条更正在业务上属于哪一类
    kind: dict[str, str]


@dataclass(frozen=True)
class BarredRelation:
    relation: Relation
    reason: str  # voided / excluded / unknown-voucher


def apply_corrections(data: Dataset) -> AppliedFacts:
    """按记录时间应用更正，得到当前事实版本。"""
    deliveries = {d.id: d for d in data.deliveries}
    relations = {r.id: r for r in data.relations}
    ordered = sorted(data.corrections, key=lambda c: (c.recorded, c.id))
    kinds: dict[str, str] = {}

    for correction in ordered:
        target = correction.supersedes
        if target in relations and correction.field == "voucher":
            original = relations[target]
            relations[target] = replace(original, voucher=correction.value)
            kinds[correction.id] = "relation-voucher"
        elif target in deliveries:
            original = deliveries[target]
            if correction.field == "provider":
                deliveries[target] = replace(original, provider=correction.value)
                kinds[correction.id] = "delivery-provider"
            elif correction.field == "units":
                deliveries[target] = replace(original, units=int(correction.value))
                kinds[correction.id] = "delivery-units"
        # 资料合成保证 supersedes 指向现存事实；未知目标保留原始形态。

    return AppliedFacts(deliveries, relations, ordered, kinds)


def is_after_void(delivery_occurred: str, void_effective: str) -> bool:
    """交付是否发生在作废生效当日或之后。"""
    return delivery_occurred >= void_effective


def usable_relations(facts: AppliedFacts, data: Dataset) -> tuple[list[Relation], list[BarredRelation]]:
    """剔除排除项与受作废约束的对应，返回可用对应与禁用清单。"""
    void_by_voucher = {v.voucher: v for v in data.voids}
    usable: list[Relation] = []
    barred: list[BarredRelation] = []

    for relation in facts.relations.values():
        delivery = facts.deliveries.get(relation.delivery)
        if delivery is None or delivery.units <= 0:
            barred.append(BarredRelation(relation, "delivery-vanished"))
            continue
        if relation.state == "excluded":
            barred.append(BarredRelation(relation, "excluded"))
            continue
        if relation.voucher not in {v.id for v in data.vouchers}:
            barred.append(BarredRelation(relation, "unknown-voucher"))
            continue
        void = void_by_voucher.get(relation.voucher)
        if void is not None and is_after_void(delivery.occurred, void.effective):
            barred.append(BarredRelation(relation, "voided"))
            continue
        usable.append(relation)

    usable.sort(key=lambda r: r.id)
    return usable, barred
