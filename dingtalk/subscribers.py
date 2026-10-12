"""推送订阅名单：记录要给哪些钉钉 userId 推送每日照片与报告。

为什么需要这个
--------------
企业内部机器人主动推送（``oToMessages/batchSend``）必须指定 ``userIds``。
但 userId 不是手机号、也不是昵称，用户很难自己查到。最省事的办法是——
**让机器人从收到的消息里自动把 senderStaffId 记下来**：
用户在钉钉里给机器人发一句话（例如「帮助」），这里就把他的 userId 存下来，
之后每日主动推送即可送达。

注意：钉钉不允许机器人凭空发起单聊，用户必须先和机器人说过话；
所以「自动记录」不只是省事，也是该通道能工作的前提。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


class SubscriberStore:
    """线程安全的订阅名单存储（Stream 线程写、调度线程读）。"""

    def __init__(self, path: str | os.PathLike[str], preset: Optional[Iterable[str]] = None) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._ids: List[str] = []
        self._load()
        # 配置里预置的 userId 永远有效（与自动订阅取并集）
        for uid in preset or []:
            self.add(str(uid))

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 名单损坏不清空：显式报错，避免"以为订阅着其实丢了"
            logger.error("订阅名单读取失败（%s）：%s —— 本次以空名单启动，"
                         "请检查该文件或删除后重新订阅", self.path, exc)
            return
        ids = raw.get("user_ids") if isinstance(raw, dict) else raw
        if isinstance(ids, list):
            self._ids = [str(x) for x in ids if str(x).strip()]
            logger.info("已加载 %d 个推送订阅者: %s", len(self._ids), self.path)

    def _save(self) -> None:
        """原子写入：先写临时文件再 replace，避免进程被杀时留下半个文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {"user_ids": self._ids}
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.error("订阅名单写入失败 %s: %s", self.path, exc)

    # ---------- 接口 ----------
    def add(self, user_id: Optional[str]) -> bool:
        """记录一个 userId。返回 True 表示新增。"""
        uid = (user_id or "").strip()
        if not uid:
            return False
        with self._lock:
            if uid in self._ids:
                return False
            self._ids.append(uid)
            self._save()
        logger.info("新增推送订阅者: %s（共 %d 人）", uid, len(self._ids))
        return True

    def all(self) -> List[str]:
        with self._lock:
            return list(self._ids)

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, user_id: object) -> bool:
        return str(user_id) in self.all()
