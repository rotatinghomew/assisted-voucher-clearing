"""离线文本报告：只读取本地资料并打印当前对账结论。"""

from __future__ import annotations

from pathlib import Path

from .engine import Reconciliation


def render(recon: Reconciliation) -> str:
    lines: list[str] = []
    lines.append("== 票间真实消耗上下界 ==")
    for voucher in recon.data.vouchers:
        low, high = recon.attribution.voucher_bounds[voucher.id]
        lines.append(f"{voucher.id} {voucher.issuer} {voucher.service} 容量{voucher.units} 消耗[{low},{high}]")

    lines.append("")
    lines.append("== 申报核对 ==")
    for claim in recon.data.claims:
        item = recon.claims.evaluations[claim.id]
        lines.append(
            f"{claim.id} {claim.provider} {claim.service} 报{claim.units} "
            f"可核{item.eligible_units} 留置{item.held_units} 拒{item.rejected_units} "
            f"状态{item.status}"
        )

    lines.append("")
    lines.append("== 结算三桶（禁止合并为剩余额度） ==")
    for bucket, rows in recon.ledger.buckets.items():
        for (provider, service), units in sorted(rows.items()):
            lines.append(f"{bucket} {provider} {service} {units}份")

    lines.append("")
    lines.append("== 报送与回执 ==")
    for batch_id, batch in sorted(recon.board.batches.items()):
        marker = "整批received" if batch.received else "未收到整批回执"
        lines.append(f"批次 {batch_id} {marker}")
        for line_id, line in sorted(batch.lines.items()):
            lines.append(
                f"  {line_id} {line.transmission.claim} {line.status} "
                f"已付{line.paid_units}份/{line.paid_cents}分 驳回{line.rejected_units}份"
            )
    if recon.board.releasable:
        lines.append(f"驳回后仍合资格可再送: {recon.board.releasable}")
    if recon.board.frozen_claims:
        lines.append(f"冻结的未送审申报: {recon.board.frozen_claims}")

    lines.append("")
    lines.append("== 现在可送审（确定可核份额） ==")
    for item in recon.audit.submission_plan:
        lines.append(
            f"{item.claim} {item.units}份 {item.service} -> {item.suggested_voucher} "
            f"{item.amount_cents}分（{item.basis}）"
        )
    if not recon.audit.submission_plan:
        lines.append("（无：确定性份额均已支付、报送或冻结）")

    lines.append("")
    lines.append("== 留置待定（不得被另一结算单领走） ==")
    for item in recon.audit.held_items:
        lines.append(
            f"{item.claim} {item.units}份 {item.service} 候选票{item.candidate_vouchers}：{item.reason}"
        )
    if not recon.audit.held_items:
        lines.append("（无）")

    lines.append("")
    lines.append("== 驳回释放与再送资格 ==")
    for item in recon.audit.releasable_items:
        lines.append(f"{item.claim} 释放{item.units}份（来自{item.returned_from_line}）：{item.note}")
    if not recon.audit.releasable_items:
        lines.append("（无）")

    lines.append("")
    lines.append("== 差额复核 ==")
    for entry in recon.audit.difference_review:
        flag = "可申请" if entry["eligible_for_application"] else "人工处理"
        linked = f" 关联争议{entry['linked_disputes']}" if entry["linked_disputes"] else ""
        lines.append(
            f"{entry['correction']} {entry['kind']} {flag} {entry['detail']} "
            f"差额{entry['difference_cents']}分{linked}"
        )
    return "\n".join(lines)


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures"
    recon = Reconciliation.from_directory(root)
    print(render(recon))


if __name__ == "__main__":
    main()
