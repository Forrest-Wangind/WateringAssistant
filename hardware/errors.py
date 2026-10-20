"""硬件层公共异常。

统一「绝不静默失败」的约定：凡是硬件不可用，都必须抛出带分类（kind）与修复建议（hint）
的 HardwareError，由上层显式呈现，而不是悄悄降级为模拟实现。
"""
from __future__ import annotations

from typing import Dict


class HardwareError(RuntimeError):
    """硬件不可用。

    kind 取值：
      * ``not_found``   —— 模块/设备不存在（未安装、未检测到）
      * ``unreachable`` —— 设备存在但当前拿不到（被占用 / 权限 / 超时）
      * ``unknown``     —— 未归类的错误
    """

    def __init__(self, message: str, *, kind: str = "unknown", hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint

    def as_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "message": str(self), "hint": self.hint}
