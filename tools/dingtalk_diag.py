#!/usr/bin/env python3
"""钉钉推送一键诊断（在树莓派上运行）。

用途：回答「为什么用户收不到每日照片和报告」。

用法：
    python tools/dingtalk_diag.py                  # 只读自检：查凭证、通道、连通性、接收人
    python tools/dingtalk_diag.py --live           # 额外上传一张测试图并真实发送一条消息
    python tools/dingtalk_diag.py --live --photo data/photos/xxx.jpg   # 用指定图片发送

退出码：
    0 = 推送通道就绪
    1 = 推送不可用（凭证缺失 / 网络不通 / 接口报错）
    2 = 通道可用但存在隐患（没有接收人）—— 消息能发，但没人收得到
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dingtalk.robot_notifier import PushError, RobotNotifier  # noqa: E402
from dingtalk.subscribers import SubscriberStore  # noqa: E402
from utils import load_config  # noqa: E402

ICON = {"not_found": "🚫", "unreachable": "⛔", "unknown": "❓"}


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def mask(name: str, value: str) -> str:
    """只暴露长度，绝不打印凭证内容。"""
    if not value:
        return f"   ❌ {name:<24} 未设置"
    return f"   ✅ {name:<24} 已设置（长度 {len(value)}）"


def make_probe_image(path: Path) -> Path:
    """生成一张小测试图（不碰相机，避免诊断工具依赖硬件）。"""
    try:
        from PIL import Image

        Image.new("RGB", (480, 270), (34, 139, 34)).save(path, "JPEG", quality=85)
    except Exception:  # noqa: BLE001  PIL 不可用时退化为一帧最小 JPEG
        path.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xc0\x00\x0b\x08\x00\x01\x00"
            b"\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff"
            b"\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf\x20\xff\xd9"
        )
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="钉钉推送诊断")
    ap.add_argument("--live", action="store_true", help="真实上传并发送一条测试消息")
    ap.add_argument("--photo", help="发送指定图片（默认生成一张测试图）")
    args = ap.parse_args()

    os.chdir(ROOT)
    cfg = load_config()
    dt_cfg = cfg.get("dingtalk", {})
    push_cfg = cfg.get("push", {})

    notifier = RobotNotifier(dt_cfg)
    store = SubscriberStore(
        dt_cfg.get("subscribers_file", "./data/subscribers.json"),
        preset=dt_cfg.get("user_ids"),
    )
    recipients = store.all()

    print("钉钉推送诊断")
    print(f"配置文件: {ROOT / 'config.yaml'}")

    # ---- 1. 凭证 ----
    section("1. 凭证")
    print(mask(dt_cfg.get("client_id_env", "DINGTALK_CLIENT_ID"), notifier.client_id))
    print(mask(dt_cfg.get("client_secret_env", "DINGTALK_CLIENT_SECRET"), notifier.client_secret))
    print(mask(dt_cfg.get("robot_code_env", "DINGTALK_ROBOT_CODE"), notifier.robot_code))
    webhook = os.environ.get(dt_cfg.get("webhook_env", "DINGTALK_WEBHOOK"), "")
    print(f"   {'✅' if webhook else '➖'} {'DINGTALK_WEBHOOK':<24} "
          f"{'已设置' if webhook else '未设置（webhook 群通道不可用）'}")

    if not notifier.configured:
        section("结论")
        print(f"   ❌ 企业机器人推送不可用，缺少: {', '.join(notifier.missing_credentials())}")
        print("   💡 在 .env 里补齐上述变量（开发者后台 > 应用详情 > 凭证与基础信息 / 机器人）")
        print("   ⚠️ 没有推送通道时，每日照片与报告**发不出去**（不会静默假装成功）")
        return 1

    # ---- 2. 连通性 ----
    section("2. 接口连通性")
    try:
        notifier.access_token()
        print("   ✅ api.dingtalk.com   accessToken 获取成功")
    except PushError as exc:
        print(f"   ❌ {exc} [{exc.kind}]")
        print(f"   💡 {exc.hint}")
        return 1
    try:
        notifier.legacy_access_token()
        print("   ✅ oapi.dingtalk.com  旧版 token 获取成功（media/upload 用）")
    except PushError as exc:
        print(f"   ❌ {exc} [{exc.kind}]")
        print(f"   💡 {exc.hint}")
        return 1

    # ---- 3. 通道 ----
    section("3. 推送通道")
    print("   ✅ 企业机器人单聊   可用 —— 照片走 mediaId，**无需公网 IP**")
    print(f"   {'✅ 自定义机器人 webhook  可用（群推送）' if webhook else '➖ 自定义机器人 webhook  未配置（只影响群推送）'}")

    # ---- 4. 接收人 ----
    section("4. 接收人")
    if recipients:
        print(f"   ✅ 共 {len(recipients)} 位: {', '.join(recipients)}")
    else:
        print("   ❌ 订阅名单为空 —— 消息发得出去，但没人收得到")
        print("   💡 在钉钉里给机器人发一条消息（例如「订阅」），程序会自动记录你的 userId")
        print(f"      （名单文件: {dt_cfg.get('subscribers_file', './data/subscribers.json')}）")

    # ---- 5. 实测 ----
    exit_code = 0 if recipients else 2
    if args.live:
        section("5. 实测发送（--live）")
        if not recipients:
            print("   ⏭ 跳过：没有接收人，先按上面的提示订阅")
            return 2
        photo = Path(args.photo) if args.photo else make_probe_image(
            Path(tempfile.gettempdir()) / "dingtalk_probe.jpg")
        if not photo.exists():
            print(f"   ❌ 图片不存在: {photo}")
            return 1
        print(f"   图片: {photo}")
        res = notifier.send_image(photo, recipients, title="🔧 推送自检")
        if res.get("ok"):
            print(f"   ✅ 图片已发送（{res.get('sent')} 人）media_id={str(res.get('media_id'))[:12]}…")
        else:
            print(f"   ❌ 图片发送失败 [{res.get('error_kind')}] {res.get('errmsg')}")
            print(f"   💡 {res.get('hint')}")
            exit_code = 1
        res2 = notifier.send_markdown("🔧 推送自检", "如果你看到这条消息，说明每日照片+报告推送链路正常。",
                                      recipients)
        if res2.get("ok"):
            print(f"   ✅ 文本已发送（{res2.get('sent')} 人）")
        else:
            print(f"   ❌ 文本发送失败 [{res2.get('error_kind')}] {res2.get('errmsg')}")
            print(f"   💡 {res2.get('hint')}")
            exit_code = 1

    section("结论")
    if exit_code == 0:
        print("   ✅ 推送通道就绪，每日照片与报告可以送达")
    elif exit_code == 2:
        print("   ⚠️ 通道可用，但没有接收人 —— 请在钉钉里给机器人发一条消息完成订阅")
    else:
        print("   ❌ 推送链路存在问题，见上方逐项结论")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
