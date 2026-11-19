"""从本地 CSV 目录装载合成资料，不做任何外部访问。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .models import (
    Claim,
    Correction,
    Delivery,
    Payment,
    Receipt,
    Relation,
    Term,
    Transmission,
    Voucher,
    Void,
)

_BOOL = {"yes": True, "no": False}


def _rows(directory: Path, name: str) -> list[dict[str, str]]:
    with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


@dataclass
class Dataset:
    vouchers: list[Voucher]
    deliveries: list[Delivery]
    relations: list[Relation]
    voids: list[Void]
    claims: list[Claim]
    payments: list[Payment]
    corrections: list[Correction]
    terms: list[Term]
    transmissions: list[Transmission]
    receipts: list[Receipt]

    @classmethod
    def load(cls, directory: str | Path) -> "Dataset":
        root = Path(directory)
        return cls(
            vouchers=[
                Voucher(r["id"], r["issuer"], r["service"], int(r["units"]), r["issued"])
                for r in _rows(root, "vouchers")
            ],
            deliveries=[
                Delivery(r["id"], r["provider"], r["service"], int(r["units"]), r["occurred"])
                for r in _rows(root, "deliveries")
            ],
            relations=[
                Relation(
                    r["id"], r["delivery"], r["voucher"], r["state"],
                    int(r["max_units"]), r["recorded"],
                )
                for r in _rows(root, "relations")
            ],
            voids=[Void(r["voucher"], r["effective"], r["recorded"]) for r in _rows(root, "voids")],
            claims=[
                Claim(
                    r["id"], r["provider"], r["bill"], r["service"],
                    int(r["units"]), r["delivery_hint"],
                )
                for r in _rows(root, "claims")
            ],
            payments=[
                Payment(
                    r["id"], r["claim"], r["allocation"], int(r["units"]),
                    int(r["cents"]), r["paid"],
                )
                for r in _rows(root, "payments")
            ],
            corrections=[
                Correction(r["id"], r["supersedes"], r["field"], r["value"], r["recorded"])
                for r in _rows(root, "corrections")
            ],
            terms=[
                Term(
                    r["service"], int(r["unit_cents"]),
                    _BOOL[r["same_provider_adjustment"]],
                    _BOOL[r["cross_provider_offset"]],
                )
                for r in _rows(root, "terms")
            ],
            transmissions=[
                Transmission(
                    r["id"], r["batch"], r["claim"], r["allocation"],
                    int(r["units"]), r["sent"],
                )
                for r in _rows(root, "transmissions")
            ],
            receipts=[
                Receipt(
                    r["id"], r["batch"], r["line"], r["kind"],
                    r["occurred"], r["recorded"],
                )
                for r in _rows(root, "receipts")
            ],
        )
