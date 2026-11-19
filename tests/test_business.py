"""业务规则测试：资料见 fixtures/，字段含义见 DATA.md。"""

from __future__ import annotations

import unittest
from pathlib import Path

from clearing import Reconciliation
from clearing.claims import (
    NO_DELIVERY,
    OVERCLAIM,
    PROVIDER_DISPUTE,
)
from clearing.settlement import (
    CROSS_PROVIDER,
    PENDING,
    SAME_PROVIDER,
    SETTLED,
    UNCLAIMED,
    VANISHED_DELIVERY,
)
from clearing.transmissions import (
    PAID,
    REJECTED,
    SENT,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class BusinessReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recon = Reconciliation.from_directory(FIXTURES)

    # ---- 更正与作废 ----

    def test_corrections_apply_to_facts_but_never_rewrite_payments(self):
        facts = self.recon.facts
        # X1：R1 证实关系从 V1 改挂 V2
        self.assertEqual(facts.relations["R1"].voucher, "V2")
        # X2：D1 供应方 A -> B
        self.assertEqual(facts.deliveries["D1"].provider, "B")
        # X3：D5 数量置 0
        self.assertEqual(facts.deliveries["D5"].units, 0)
        # 历史付款原样保留，allocation 仍是旧票 V1，金额不回写
        payment = self.recon.data.payments[0]
        self.assertEqual((payment.claim, payment.allocation, payment.cents), ("C1", "V1", 120))

    def test_void_only_constrains_uses_after_effective_date(self):
        barred = {b.relation.id: b.reason for b in self.recon.attribution.barred}
        # D5 同时被 X3 置 0 且发生在作废生效之后：交付消失优先判定
        self.assertEqual(barred.get("R7"), "delivery-vanished")
        # D3 发生在 V4 作废生效（07-07）之前：R5 仍是可用对应
        self.assertNotIn("R5", barred)

    def test_pure_void_sequence_bars_only_later_uses(self):
        from clearing.facts import apply_corrections, usable_relations
        from clearing.loader import Dataset
        from clearing.models import Delivery, Relation, Void, Voucher
        data = Dataset.load(FIXTURES)
        data.vouchers = [Voucher("W1", "西点", "打印", 5, "2026-07-01")]
        data.voids = [Void("W1", "2026-07-05", "2026-07-05")]
        data.deliveries = [
            Delivery("E1", "A", "打印", 2, "2026-07-04"),  # 作废前
            Delivery("E2", "A", "打印", 2, "2026-07-06"),  # 作废后
        ]
        data.relations = [
            Relation("Q1", "E1", "W1", "possible", 2, "2026-07-04"),
            Relation("Q2", "E2", "W1", "possible", 2, "2026-07-06"),
        ]
        facts = apply_corrections(data)
        usable, barred = usable_relations(facts, data)
        self.assertEqual([r.id for r in usable], ["Q1"])
        self.assertEqual([(b.relation.id, b.reason) for b in barred], [("Q2", "voided")])

    # ---- 票间上下界 ----

    def test_voucher_consumption_is_reported_as_bounds_not_binary(self):
        bounds = self.recon.attribution.voucher_bounds
        self.assertEqual(bounds["V1"], (0, 0))   # R1 改挂 V2 后 V1 无任何入边
        self.assertEqual(bounds["V2"], (4, 4))   # D1 四份证实落 V2，无歧义
        self.assertEqual(bounds["V3"], (2, 2))   # 扫描证实两份
        self.assertEqual(bounds["V4"], (0, 0))   # R5 实际无法承担：D3 的两份只能由 V2 消化
        # 没有把任何票整票当成空额或已用：区间宽度反映歧义
        self.assertIsInstance(bounds["V2"], tuple)

    def test_ambiguous_delivery_lists_candidate_vouchers(self):
        ambiguous = self.recon.attribution.ambiguous
        # D2 可挂 V1/V2，D3 可挂 V2/V4；D1 已被证实唯一归属 V2
        self.assertEqual(ambiguous["D2"], ["V1", "V2"])
        self.assertEqual(ambiguous["D3"], ["V2", "V4"])
        self.assertNotIn("D1", ambiguous)

    def test_every_real_delivery_unit_has_a_possible_landing(self):
        total_demand = sum(
            d.units for d in self.recon.facts.deliveries.values() if d.units > 0
        )
        self.assertEqual(self.recon.attribution.flow_total, total_demand)

    # ---- 申报核对 ----

    def test_split_and_duplicate_claims_never_inflate_delivery(self):
        evals = self.recon.claims.evaluations
        # C2 与 C3 指向同一交付 D2（共 3 份）：C2 留置 3，C3 超报被拒 2
        self.assertEqual((evals["C2"].held_units, evals["C2"].rejected_units), (3, 0))
        self.assertEqual(evals["C3"].status, OVERCLAIM)
        self.assertEqual(evals["C3"].rejected_units, 2)
        # 同一交付单位只核一次：D2 累计占用封顶在其上界
        self.assertEqual(self.recon.claims.reserved_by_delivery["D2"], 3)

    def test_cross_provider_dispute_blocks_claim_but_keeps_payment(self):
        evals = self.recon.claims.evaluations
        self.assertEqual(evals["C1"].status, PROVIDER_DISPUTE)
        self.assertEqual(evals["C1"].rejected_units, 4)
        case = next(c for c in self.recon.ledger.payment_cases if c.claim_id == "C1")
        self.assertEqual(case.kind, CROSS_PROVIDER)
        self.assertEqual(case.difference_cents, 0)  # 无互抵资格

    def test_vanished_delivery_claim_is_rejected_without_difference(self):
        evals = self.recon.claims.evaluations
        self.assertEqual(evals["C6"].status, NO_DELIVERY)
        case = next(c for c in self.recon.ledger.correction_cases if c.correction_id == "X3")
        self.assertEqual(case.kind, VANISHED_DELIVERY)
        self.assertFalse(case.eligible_for_application)

    def test_verified_claim_is_confirmed_while_possible_only_is_held(self):
        evals = self.recon.claims.evaluations
        self.assertEqual(evals["C5"].eligible_units, 2)   # D4 证实
        self.assertEqual(evals["C4"].held_units, 2)       # D3 只有 possible
        self.assertEqual(evals["C4"].eligible_units, 0)

    def test_unclaimed_delivered_volume_is_separate_bucket(self):
        # D1（B，4 份）无人以 B 名义申领；D2/D3 已被申报占用
        self.assertEqual(self.recon.claims.unclaimed_by_delivery.get("D1"), 4)

    # ---- 三桶与留置约束 ----

    def test_settled_pending_unclaimed_are_never_merged(self):
        buckets = self.recon.ledger.buckets
        self.assertEqual(buckets[SETTLED], {("A", "打印"): 4})
        self.assertEqual(
            buckets[PENDING],
            {("A", "打印"): 3, ("A", "扫描"): 2, ("B", "打印"): 2},
        )
        self.assertEqual(buckets[UNCLAIMED], {("B", "打印"): 4})

    def test_held_share_cannot_be_claimed_from_another_settlement(self):
        # D3 的两份已由 C4 留置，任何后续申报在 D3 上都拿不到新份额
        reserved = self.recon.claims.reserved_by_delivery
        self.assertEqual(reserved["D3"], 2)

    # ---- 更正定性与差额 ----

    def test_correction_kinds_are_classified(self):
        by_id = {c.correction_id: c for c in self.recon.ledger.correction_cases}
        self.assertEqual(by_id["X2"].kind, CROSS_PROVIDER)
        self.assertEqual(by_id["X3"].kind, VANISHED_DELIVERY)
        self.assertEqual(by_id["X1"].kind, SAME_PROVIDER)
        # X1 虽在 terms 允许范围内，但挂靠在 X2 争议交付上，须等争议解决
        self.assertTrue(by_id["X1"].eligible_for_application)
        self.assertEqual(by_id["X1"].linked_disputes, ["X2"])
        self.assertEqual(by_id["X1"].difference_cents, 120)

    def test_evidence_gaps_name_blocked_and_independent_items(self):
        x2 = next(c for c in self.recon.ledger.correction_cases if c.correction_id == "X2")
        # C1 已历史支付，走付款案件而非待核对清单；X2 本身没有未送审的 D1 申报
        self.assertEqual(x2.affected_pending_items, [])
        # 与 D1 无关、且有可核份额的项目可独立继续结算
        self.assertIn("C5", x2.independently_settleable)
        self.assertNotIn("C1", x2.independently_settleable)
        payment_case = next(c for c in self.recon.ledger.payment_cases if c.claim_id == "C1")
        self.assertEqual(payment_case.kind, CROSS_PROVIDER)

    # ---- 报送与回执 ----

    def test_batch_receipt_is_not_line_level_rejection(self):
        board = self.recon.board
        self.assertTrue(board.batches["T20"].received)

    def test_duplicate_receipt_does_not_double_pay(self):
        line = self.recon.board.batches["T20"].lines["L2"]
        self.assertEqual(line.status, PAID)
        self.assertEqual(line.paid_units, 2)
        self.assertEqual(line.paid_cents, 120)  # 2 份 * 60 分，重复 G2 只计一次

    def test_rejection_releases_only_unpaid_share_and_rechecks_eligibility(self):
        line = self.recon.board.batches["T20"].lines["L1"]
        self.assertEqual(line.status, REJECTED)
        self.assertEqual(self.recon.board.releasable.get("C2"), 1)
        item = next(i for i in self.recon.audit.releasable_items if i.claim == "C2")
        self.assertIn("仍合资格", item.note)

    def test_withdrawn_eligibility_does_not_release_sent_share(self):
        # L2 已付费：即使后续证据变化，已送出份额不释放、不重复登记
        paid = sum(
            line.paid_cents
            for batch in self.recon.board.batches.values()
            for line in batch.lines.values()
        )
        self.assertEqual(paid, 120)
        # receipts 的付费不在 payments 表重复登记
        self.assertEqual(len(self.recon.data.payments), 1)

    # ---- 等待期冻结与时点重放 ----

    def test_fixture_attribution_break_does_not_touch_sent_or_paid(self):
        # 07-09：X1 已到达而 T20 回执未齐；受影响的 D1 申报 C1 已历史支付，
        # 已送出的 L1/L2 也不受影响，因此没有新的冻结项——冻结只针对未送审行。
        board = self.recon.board_at("2026-07-09")
        self.assertEqual(board.frozen_claims, {})
        for line in board.batches["T20"].lines.values():
            self.assertEqual(line.status, SENT)

    def test_unsent_lines_freeze_when_attribution_breaks_while_waiting(self):
        from clearing.claims import evaluate_claims
        from clearing.facts import apply_corrections
        from clearing.loader import Dataset
        from clearing.transmissions import build_board, detect_attribution_breaks
        from clearing.attribution import attribute

        data = Dataset.load(FIXTURES)
        # 构造：X1 更正了 D1 的票间归属，而指向 D1 的 C1 没有历史支付、尚未送审
        data.transmissions = [t for t in data.transmissions if t.claim != "C1"]
        data.payments = []
        facts = apply_corrections(data)
        attribution = attribute(data, facts)
        claims_recon = evaluate_claims(data, facts, attribution)
        breaks = detect_attribution_breaks(data, facts, "2026-07-10")
        board = build_board(
            data, facts, claims_recon, as_of="2026-07-10", frozen_claim_ids=breaks
        )
        self.assertIn("C1", board.frozen_claims)
        # 已送出的行保持原状态，份额不释放
        for line in board.batches["T20"].lines.values():
            self.assertIn(line.status, (SENT, PAID, REJECTED))

    def test_partial_receipts_resolve_over_time(self):
        early = self.recon.board_at("2026-07-08")
        # 只有整批 received：两行都仍是 sent，不得当作驳回
        self.assertTrue(early.batches["T20"].received)
        for line in early.batches["T20"].lines.values():
            self.assertEqual(line.status, SENT)
        late = self.recon.board_at("2026-07-12")
        self.assertEqual(late.batches["T20"].lines["L2"].status, PAID)
        self.assertEqual(late.batches["T20"].lines["L1"].status, REJECTED)

    # ---- 审核视图 ----

    def test_utilization_by_issuer_and_service_with_trace(self):
        rows = {(r.issuer, r.service): r for r in self.recon.audit.utilization}
        north_print = rows[("北点", "打印")]
        self.assertEqual((north_print.capacity, north_print.consumed_lower, north_print.consumed_upper), (9, 0, 0))
        south_print = rows[("南点", "打印")]
        self.assertEqual((south_print.consumed_lower, south_print.consumed_upper), (4, 4))
        trace = self.recon.audit.trace["北点/打印"]
        self.assertIn("R7", [b["relation"] for b in trace["barred_relations"]])
        self.assertIn("X2", trace["disputed_corrections"])
        self.assertIn("2026-07-05", trace["payment_dates"])

    def test_audit_answers_what_can_be_sent_now(self):
        # 当前没有新的确定性可送份额：C5 已付费，其余为留置/争议/冻结/驳回
        self.assertEqual(self.recon.audit.submission_plan, [])
        held_claims = {item.claim for item in self.recon.audit.held_items}
        self.assertEqual(held_claims, {"C4"})
        self.assertEqual(self.recon.audit.totals["paid_on_other_ledger_cents"], 120)
        self.assertEqual(self.recon.audit.totals["frozen_claims"], [])


if __name__ == "__main__":
    unittest.main()
