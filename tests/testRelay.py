"""继电器测试脚本 — 使用 gpiod (兼容 Pi 5)。"""
import gpiod
from time import sleep

CHIP = "/dev/gpiochip0"
PIN = 17  # GPIO17, BCM 编号

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
