"""代办凭证束的核销差异与离线清算。

模块只消费本地合成资料（``fixtures/``），不连接银行或真实采购系统。
主要入口见 :class:`clearing.engine.Reconciliation`。
"""

from .engine import Reconciliation

__all__ = ["Reconciliation"]
