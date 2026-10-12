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
# 树莓派上必须带 --system-site-packages，否则 venv 看不到 apt 装的 picamera2 / libcamera
python3 -m venv --system-site-packages .venv && source .venv/bin/activate
pip install -r requirements.txt

# 树莓派额外装相机与 GPIO 后端（apt 提供，pip 装不全）
sudo apt install -y python3-picamera2 python3-libcamera python3-gpiod
sudo usermod -aG video,gpio $USER   # 重新登录生效

cp .env.example .env             # 填入 ANTHROPIC_API_KEY 与 DINGTALK_*
```

* 在树莓派上保持 `system.mock_hardware: false`（使用真实相机与 GPIO，硬件缺失会直接报错）；
* 在 PC / Mac 上改为 `true`，硬件操作才走 mock。

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

## 测试

统一使用项目内虚拟环境（`.venv`）运行：

```bash
.venv/bin/python -m pytest                       # 全量（约 3 分钟，慢在用例里的真实 sleep）
.venv/bin/python -m pytest tests/test_camera.py -v   # 单个文件
.venv/bin/python tests/test_camera.py            # 也可以直接执行单个测试文件
```

> **不要用 `python tests/test_camera.py` 之外的解释器**：`python` 若指向系统/其他解释器，
> 会缺 pytest 与项目依赖。请用 `.venv/bin/python`，或先 `source .venv/bin/activate`。
>
> `pytest.ini` 已配置 `pythonpath = . tests`，配合各测试文件顶部的
> `import _bootstrap`，无论从哪个工作目录调用、或用 IDE 的「Run File」都能正确导入项目模块。

`tests/testRelay.py` 是**手动硬件脚本**（继电器点动），不参与自动测试；它在树莓派上单独执行：

```bash
.venv/bin/python tests/testRelay.py    # 仅树莓派，Ctrl-C 退出
```

## 硬件降级策略（重要）

本项目**不做静默降级**。`mock_hardware: false`（树莓派）时，若相机或 GPIO 不可用，
程序会直接抛出带分类原因的 `HardwareError` 并拒绝继续，而不是悄悄生成占位图 / 假装浇水成功。

| 配置 | 默认 | 含义 |
|---|---|---|
| `system.mock_hardware` | `false` | `true` 时全部走模拟（PC/Mac 开发）；`false` 时使用真实硬件 |
| `camera.allow_mock_fallback` | `false` | 仅在"无相机也要跑通流程"时才设 `true`；此时照片名带 `MOCK_` 前缀且日志为 WARNING |
| `safety.allow_mock_fallback` | `false` | 同上，作用于 GPIO；为 `true` 时浇水不会真正开阀，日志会明确告警 |

异常按三类归档，便于快速定位：

* `not_found`：模块/设备不存在（picamera2 未装、libcamera 未检测到相机、GPIO 后端未装）
* `unreachable`：设备存在但拿不到（被占用、权限不足、超时）
* `unknown`：未归类，日志里有完整堆栈

## 相机诊断

拍照结果是绿色 "MOCK GARDEN PHOTO" 占位图时，在树莓派上运行：

```bash
python tools/camera_diag.py          # 环境自检（不动相机）
python tools/camera_diag.py --live   # 额外做一次真实打开 + 拍照 + 画质体检
python tools/camera_diag.py --live --baseline   # 把本次画质指标存为基线，供日后对比
```

脚本会输出：解释器是否在 venv、`picamera2`/`libcamera` 是否可见、`/dev/video*`、
当前用户是否在 `video` 组、`config.txt` 的相机配置，并给出**分类结论 + 修复建议**。
加 `--live` 后还会给出成片的清晰度/对比度/亮度、对焦过程与拍摄元数据（曝光、增益、对焦位置）。

退出码：`0` = 正常，`1` = 相机不可用，**`2` = 相机可用但成片画质异常**（照片能拍但不清晰）。

### 照片能拍出来，但很模糊

> 详细分析见 [docs/camera_image_quality.md](docs/camera_image_quality.md)。

相机是 **Pi Camera v3（IMX708，带自动对焦）**。libcamera 不会在拍照瞬间自动帮你对一次焦，
所以代码必须在拍照前显式触发对焦 —— 早期版本漏了这一步，等于**盲拍**，对焦环停在无穷远，
拍 0.5~2m 外的花架整幅发虚。现已修复（`camera.autofocus: auto`）。

按症状快速分诊：

| 症状 | 原因 | 处置 |
|---|---|---|
| 整幅均匀糊，无一处清晰 | 未合焦 / 镜头保护膜没撕 | 先撕膜；再看 `camera_diag --live` 的 `last_focus` 与 `af_state` |
| 一半清晰一半糊（分层） | 景深不足（L3 顶端到 L1 底层跨 0.3~2m，f/1.8 不够） | 光学限制，用 `lens_position` 锁定中距离，见文档 §6 |
| 叶片有拖影 | 暗光下曝光过长 | 调小 `camera.max_exposure_us`，或设 `exposure_mode: short` |
| 局部模糊斑块 / 亮斑 | 水渍、水雾、结露（本项目会喷水） | 擦净镜头 |

推荐验证流程：

```bash
.venv/bin/python tools/camera_diag.py --live   # 看画质结论与「已生效控件」
.venv/bin/python tests/photoAndAnalyse.py --photo   # 走完整拍照链路，直接打印画质指标
```

> ⚠️ 若 `--live` 输出里出现「未生效控件」，说明当前 libcamera 版本不接受某个控件名
> （如 `AfMode`、`FrameDurationLimits`），对焦/曝光就不会受控 —— 把该行内容贴出来排查。

最常见的两个原因：

1. **venv 看不到 apt 安装的 picamera2**：`picamera2` 来自 `apt install python3-picamera2`，
   装在 `/usr/lib/python3/dist-packages`。用 `python3 -m venv .venv`（不带参数）创建的 venv
   **默认隔离系统包**，于是 `import picamera2` 失败。解决：

   ```bash
   rm -rf .venv && python3 -m venv --system-site-packages .venv
   source .venv/bin/activate && pip install -r requirements.txt
   # 或直接用系统解释器运行：/usr/bin/python3 main.py
   ```

2. **相机未被 libcamera 识别**：先用系统工具确认硬件层是否可见：

   ```bash
   rpicam-hello --list-cameras   # 旧系统用 libcamera-hello --list-cameras
   ```

   完全没输出 → 检查 FPC 排线方向/插紧（Pi 5 排线朝向与 Pi 4 不同），
   确认 `/boot/firmware/config.txt` 中 `camera_auto_detect=1` 且**没有** `start_x=1`（legacy 栈冲突），改完重启。

## 常见问题

* **`picamera2` 装不上？** 仅在树莓派 64-bit OS 上可用；其他环境把 `system.mock_hardware` 设为 `true`。
* **拍照出来是绿色 MOCK 图？** 说明相机初始化失败被降级了。运行 `python tools/camera_diag.py` 定位；
  升级后默认不会再静默降级，会直接报错并给出 `kind` 与修复建议。
* **钉钉收不到 @ 消息？** 检查 `DINGTALK_CLIENT_ID/SECRET` 是否正确、应用版本是否已发布、机器人是否加进群；启动日志里应有「钉钉 Stream 客户端已启动」。
* **水泵不出水？** 先在 `/api/water` 单独触发，并听继电器是否吸合；不吸合多半是 BCM 编号或 12V 电源未接通。
* **启动即报 `HardwareError`？** 这是预期行为（fail-fast）。修好硬件，或在开发机上把 `mock_hardware` 设为 `true`。

## 许可证

MIT
