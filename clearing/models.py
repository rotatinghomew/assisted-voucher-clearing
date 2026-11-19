"""原始资料的行模型。所有数量为整数份，金额单位为分。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Voucher:
    id: str
    issuer: str
    service: str
    units: int
    issued: str


@dataclass(frozen=True)
class Delivery:
    id: str
    provider: str
    service: str
    units: int
    occurred: str


@dataclass(frozen=True)
class Relation:
    id: str
    delivery: str
    voucher: str
    state: str  # verified / possible / excluded
    max_units: int
    recorded: str


@dataclass(frozen=True)
class Void:
    voucher: str
    effective: str
    recorded: str


@dataclass(frozen=True)
class Claim:
    id: str
    provider: str
    bill: str
    service: str
    units: int
    delivery_hint: str  # 空字符串表示未指明


@dataclass(frozen=True)
class Payment:
    id: str
    claim: str
    allocation: str
    units: int
    cents: int
    paid: str


@dataclass(frozen=True)
class Correction:
    id: str
    supersedes: str
    field: str  # voucher / provider / units
    value: str
    recorded: str


@dataclass(frozen=True)
class Term:
    service: str
    unit_cents: int
    same_provider_adjustment: bool
    cross_provider_offset: bool


@dataclass(frozen=True)
class Transmission:
    id: str
    batch: str
    claim: str
    allocation: str
    units: int
    sent: str


@dataclass(frozen=True)
class Receipt:
    id: str
    batch: str
    line: str  # 空字符串表示整批 received
    kind: str  # received / paid / rejected
    occurred: str
    recorded: str
