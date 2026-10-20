"""推送订阅名单单元测试。"""
import json

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行
from dingtalk.subscribers import SubscriberStore


def test_add_and_persist(tmp_path):
    path = tmp_path / "subs.json"
    store = SubscriberStore(path)
    assert len(store) == 0

    assert store.add("user_a") is True
    assert store.add("user_a") is False          # 去重
    assert store.add("  user_b  ") is True       # 去空白
    assert store.add("") is False                # 空值忽略
    assert store.add(None) is False

    assert store.all() == ["user_a", "user_b"]
    # 落盘后新实例能读回来
    assert SubscriberStore(path).all() == ["user_a", "user_b"]


def test_preset_merges_and_persists(tmp_path):
    path = tmp_path / "subs.json"
    store = SubscriberStore(path, preset=["cfg_user"])
    assert store.all() == ["cfg_user"]
    store.add("stream_user")
    assert set(SubscriberStore(path).all()) == {"cfg_user", "stream_user"}


def test_corrupt_file_does_not_wipe_silently(tmp_path, caplog):
    """名单损坏时不能当成"没有订阅者"糊弄过去，必须显式报错。"""
    path = tmp_path / "subs.json"
    path.write_text("{ not json at all", encoding="utf-8")
    store = SubscriberStore(path)
    assert store.all() == []
    assert any("订阅名单读取失败" in r.message for r in caplog.records)

    # 文件不应被破坏性覆盖
    assert path.read_text(encoding="utf-8") == "{ not json at all"


def test_contains_supports_membership(tmp_path):
    store = SubscriberStore(tmp_path / "s.json")
    store.add("u1")
    assert "u1" in store
    assert "u2" not in store


def test_atomic_write_produces_valid_json(tmp_path):
    path = tmp_path / "subs.json"
    store = SubscriberStore(path)
    store.add("u1")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"user_ids": ["u1"]}
    # 不留临时文件
    assert list(tmp_path.glob("*.tmp")) == []


if __name__ == "__main__":
    _bootstrap.run(__file__)
