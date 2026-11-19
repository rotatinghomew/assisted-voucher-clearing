"""对外门面：一次装载即可得到核销、清算、报送与审核的完整视图。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attribution import Attribution, attribute
from .claims import ClaimReconciliation, evaluate_claims
from .facts import AppliedFacts, apply_corrections
from .loader import Dataset
from .audit import AuditView, build_audit_view
from .settlement import SettlementLedger, build_ledger
from .transmissions import (
    TransmissionBoard,
    build_board,
    detect_attribution_breaks,
)


@dataclass
class Reconciliation:
    data: Dataset
    facts: AppliedFacts
    attribution: Attribution
    claims: ClaimReconciliation
    ledger: SettlementLedger
    board: TransmissionBoard
    audit: AuditView
    as_of: str | None

    @classmethod
    def from_directory(cls, directory: str | Path, as_of: str | None = None) -> "Reconciliation":
        data = Dataset.load(directory)
        facts = apply_corrections(data)
        attribution = attribute(data, facts)
        claims = evaluate_claims(data, facts, attribution)
        ledger = build_ledger(data, facts, claims)
        breaks = detect_attribution_breaks(data, facts, as_of) if as_of else set()
        board = build_board(data, facts, claims, as_of=as_of, frozen_claim_ids=breaks)
        view = build_audit_view(data, facts, attribution, claims, board, ledger)
        return cls(data, facts, attribution, claims, ledger, board, view, as_of)

    def board_at(self, as_of: str) -> TransmissionBoard:
        """按指定日期重放报送与回执，回答“那时点可以送什么”。"""
        breaks = detect_attribution_breaks(self.data, self.facts, as_of)
        return build_board(
            self.data, self.facts, self.claims,
            as_of=as_of, frozen_claim_ids=breaks,
        )
