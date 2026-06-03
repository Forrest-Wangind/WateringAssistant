# 多路浇花助理 🌿

基于树莓派的 4 路智能浇花系统：

* **4 路独立水泵**：分别为热带 / 多肉 / 中生 / 水生植物供水；
* **定时 + 远程**：APScheduler 按需触发，钉钉群里发指令也能控制；
* **AI 生长报告**：摄像头拍照后调用 Claude 视觉模型，生成 Markdown 图文报告；
* **钉钉双向通信**：自定义机器人推送图片 / 报告，outgoing 机器人接收用户指令；
* **铝型材花架**：3 层结构，30 盆容量，集水槽收集渗水至下水道。

## 系统架构

```
            ┌────────────┐  Stream WS  ┌──────────────────────┐
            │  钉钉用户  │ ──────────▶│ dingtalk-stream      │
            │            │ ◀──────────│ ChatbotHandler       │
            └────────────┘   push      └─────────┬────────────┘
                  ▲                              │
                  │ webhook(图片/报告)           ▼
                  │                       ┌──────────────┐
                  └───────────────────────│ Assistant 编排│
                                          └────┬─────┬───┘
                       ┌─────────────────────┘     └─────────┐
              定时触发 ▼                                    ▼
        ┌──────────────┐  GPIO  ┌─────────┐   ┌────────────────┐
        │ APScheduler  │ ─────▶ │ 4路水泵 │   │ Pi Camera +    │
        └──────────────┘        └─────────┘   │ Claude Vision  │
                                              └────────────────┘
```

## 目录结构

```
WateringAssistant/
├── main.py                       # 主入口：调度 + Web
├── assistant.py                  # 业务编排
├── utils.py                      # 配置 / 日志
├── config.yaml                   # 通道、cron、AI、钉钉
├── .env.example                  # 密钥模板
├── requirements.txt
├── hardware/
│   ├── pump_controller.py        # 4 路 GPIO 水泵
│   └── camera.py                 # picamera2 / mock
├── ai/plant_analyzer.py          # Claude 多模态报告
├── dingtalk/bot.py               # webhook 推送 + Stream 接收 + 指令解析
├── scheduler/watering_scheduler.py
├── web/server.py                 # FastAPI: /api/water /api/report
└── docs/hardware_design.md       # 花架与水路设计
```

## 安装

```bash
git clone <repo> && cd WateringAssistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # 填入 ANTHROPIC_API_KEY 与 DINGTALK_*
```

* 在树莓派上把 `config.yaml` 中 `mock_hardware` 改为 `false`；
* 在 PC / Mac 上保持 `true`，硬件操作会走 mock。

## 运行

```bash
python main.py
```

启动后：

* `GET  /health`、`GET  /api/status` 查看状态；
* `POST /api/water  {"channel_id":1,"volume_ml":500}` 远程浇水；
* `POST /api/report` 立即拍照 + AI 报告；
* 钉钉群里 @机器人 或发关键词消息（通过 Stream 长连接接收，无需公网回调）。

## 钉钉配置

钉钉指令接收使用 **Stream 模式**（长连接），无需公网穿透：

1. 群设置 → 智能群助手 → **添加自定义机器人（推送）**
   * 安全设置选「加签」，把 webhook 与签名分别填入 `.env` 的 `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`，用于推送图片/报告/状态；
2. [钉钉开放平台](https://open-dev.dingtalk.com/) → 应用开发 → **创建企业内部应用**
   * 在「凭证与基础信息」拿到 `AppKey` / `AppSecret`，分别填入 `.env` 的 `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET`；
   * 「机器人」页签开启机器人能力并发布；
   * 「事件订阅」选 **Stream 模式**（默认即可，不用配公网回调地址）；
   * 把机器人加入目标群聊；
3. （可选）`config.yaml` 的 `dingtalk.allowed_users` 加白名单 userId。

> 实现参考 [open-dingtalk/dingtalk-stream-sdk-python](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)。

### 群里支持的指令

| 指令示例           | 含义 |
|--------------------|------|
| `浇水 多肉`        | 给多肉默认浇水量 |
| `浇水 通道3 600`   | 给 3 号通道浇 600ml |
| `报告`             | 拍照 + 生成生长报告 |
| `拍照`             | 仅拍照 |
| `状态`             | 查看 4 路状态 |
| `帮助`             | 查看指令说明 |

## 安全特性

* **冷却时间**：同一通道两次启动需间隔 30s（默认）；
* **最大单次时长**：单泵最长 180s，避免水箱抽干干转；
* **全局互锁**：同一时刻仅允许一路水泵运行；
* **GPIO 初始置高**：低电平触发的继电器开机即关闭，断电也关。

## 常见问题

* **`picamera2` 装不上？** 仅在树莓派 64-bit OS 上可用；其他环境保持 `mock_hardware: true` 即可。
* **钉钉收不到 @ 消息？** 检查 `DINGTALK_CLIENT_ID/SECRET` 是否正确、应用版本是否已发布、机器人是否加进群；启动日志里应有「钉钉 Stream 客户端已启动」。
* **水泵不出水？** 先在 `/api/water` 单独触发，并听继电器是否吸合；不吸合多半是 BCM 编号或 12V 电源未接通。

## 许可证

MIT
