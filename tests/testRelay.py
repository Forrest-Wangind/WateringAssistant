"""继电器测试脚本 — 使用 gpiod (兼容 Pi 5)。

⚠️ 手动在树莓派上执行的硬件脚本，不是单元测试。
必须在文件顶层加 `if __name__ == "__main__"` 守卫，否则 `pytest` 收集本文件时
会在导入阶段就进入死循环，导致整个测试套件挂起。

用法：python tests/testRelay.py   (仅限树莓派，Ctrl-C 退出)
"""
from time import sleep

CHIP = "/dev/gpiochip0"
PIN = 17  # GPIO17, BCM 编号


def main() -> None:
    import gpiod

    # 高电平触发继电器: ACTIVE=HIGH(吸合), INACTIVE=LOW(断开)
    settings = gpiod.LineSettings(
        direction=gpiod.line.Direction.OUTPUT,
        output_value=gpiod.line.Value.INACTIVE,  # 初始 LOW = 继电器断开
    )
    req = gpiod.request_lines(CHIP, config={PIN: settings}, consumer="test-relay")

    try:
        while True:
            req.set_value(PIN, gpiod.line.Value.ACTIVE)    # HIGH → 继电器吸合
            print("Relay ON")
            sleep(3)

            req.set_value(PIN, gpiod.line.Value.INACTIVE)  # LOW → 继电器断开
            print("Relay OFF")
            sleep(3)
    except KeyboardInterrupt:
        req.set_value(PIN, gpiod.line.Value.INACTIVE)  # 确保继电器断开
        req.release()
        print("程序已退出")


if __name__ == "__main__":
    main()
