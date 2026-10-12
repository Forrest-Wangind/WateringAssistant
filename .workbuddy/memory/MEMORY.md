# 项目长期约定 — WateringAssistant

## 架构要点
- 编排入口 `assistant.py::WateringAssistant`；硬件层 `hardware/`（`valve_controller.py` 是**实际使用**的 4 路电磁阀控制器，按秒计时；`pump_controller.py` 已无引用且字段与 config.yaml 不兼容，属待删死代码）。
- `main.py` 启动顺序：加载配置 → 构造 Assistant（硬件 fail-fast）→ 注册定时任务 → 钉钉 Stream 客户端 → FastAPI(:8080)。
- 钉钉走 Stream 长连接接收指令，webhook 仅用于主动推送。

## 硬性约定：绝不静默失败
- 硬件（相机 / GPIO / 钉钉 webhook）不可用时**必须显式报错**，禁止悄悄降级为 mock 后继续跑，否则会出现"看起来正常但完全失真"的结果（曾发生：树莓派上照片实为 mock 占位图，却照常生成生长报告）。
- 异常统一用 `hardware/errors.py::HardwareError`，必须带分类 `kind`（`not_found` / `unreachable` / `unknown`）+ 可执行的 `hint`。
- 允许降级必须显式配置：`camera.allow_mock_fallback`、`safety.allow_mock_fallback`（默认均为 `false`）。降级时日志为 WARNING、文件名带 `MOCK_` 前缀、消息带 `⚠️ [MOCK]` 横幅。
- `system.mock_hardware: true` 时才走纯模拟（PC/Mac 开发）；树莓派保持 `false`。
- 排障入口：`python tools/camera_diag.py [--live]`。

## 树莓派相机环境
- picamera2 / libcamera / gpiod 均由 **apt** 提供（`python3-picamera2`、`python3-libcamera`、`python3-gpiod`），装在 `/usr/lib/python3/dist-packages`。
- venv 必须用 `python3 -m venv --system-site-packages .venv` 创建，否则 venv 看不到这些包（这是"拍照一直是 mock"的头号原因）。
- 用户需加入 `video`、`gpio` 组；`config.txt` 需 `camera_auto_detect=1` 且不能有 legacy `start_x=1`。

## 相机拍摄质量：IMX708 必须显式驱动对焦
- 相机是 **Pi Camera v3 = IMX708（VCM 自动对焦）**。libcamera **不会**在 `capture_file()` 瞬间自动对一次焦 —— 只 `start()` + `sleep()` 就拍等于**盲拍**，对焦环可能停在无穷远，拍 0.5~2m 的花架整幅发虚。**拍照前必须显式跑一次对焦**（`hardware/camera.py::Camera._wait_for_focus` → `autofocus_cycle()`）。
- ⚠️ **Picamera2 在 `AfMode=Continuous` 下调用 `autofocus_cycle()` 会抛 RuntimeError**。静止花架用 `autofocus: auto`（单次对焦）+ 显式触发；`continuous` 只适合移动目标。
- **IMX708 原生 sensor mode 只有 4608×2592 / 2304×1296 / 1536×864**，`1920×1080` 不在其中 → ISP 需重采样，画面偏软。config 里已改为 `2304×1296`（全视野 2:1 binning，16:9 同视角）。
- 曝光：IMX708 最长可达 **1.72s**，清晨/傍晚拍照 AEC 会拉满 → 水叶拖影（运动模糊）。已用 `FrameDurationLimits` 限制到 `max_exposure_us: 66666`（1/15s）。
- **景深是光学限制，不是 bug**：相机在 L3 顶端俯角 30°，同一画面跨 0.3m（L3）~2m（L1），f/1.8 无法全清晰，表现为「一半清晰一半糊」。处置：用 `lens_position` 锁定中距离（屈光度，0=无穷远，1=1m）。详见 `docs/camera_image_quality.md`。
- 画质可观测性：`assess_image()`（纯 PIL，sharpness = 高频残余方差）+ `judge_quality()` 分诊；`Camera.last_quality` 会一路带到 `status()` 与钉钉报告抬头（`assistant.py::_quality_banner`）。**糊图照样能喂给 AI 并得到一本正经的结论，这是本项目最危险的静默失败**，故必须显式告警。
- 阈值 `quality_guard.min_sharpness: 8.0` 来自合成图标定（清晰 ≈100、模糊 1.6px ≈28、4px ≈2），**绝对值与场景强相关**，应在树莓派上拍一张满意照片后用 `camera_diag.py --live --baseline` 存基线，之后比基线而非比绝对值。
- `tools/camera_diag.py` 退出码：`0` 正常 / `1` 相机不可用 / **`2` 相机可用但成片画质异常**。

## 定时浇水配置（config.yaml）
- 计划浇水时长**只由** `channels[].default_duration_sec` 决定：`main.py` 注册的是 `assistant.water_channel(cid, notify=True)`，`duration_sec=None` → `ValveController.water()` 回落到 `default_duration_sec`。改计划时长不必动 safety。
- `schedule_cron` 用 5 段标准 crontab，但经 APScheduler `CronTrigger.from_crontab` 解析，**星期字段 0=周一**（与常规 crontab 的 0=周日相反），`7` 会抛 ValueError。**星期一律写英文名 `mon`..`sun`**，避免语义歧义。
- 单位换算：crontab 的 day-of-month 段写 `*/n` 表示"每 n 天"（如 `0 8 */7 * *`）。
- 已知隐患：`scheduler/watering_scheduler.py::schedule_channels` 里 cron 解析失败只 `logger.error` + `continue`，任务不注册而进程照常启动 —— 与"绝不静默失败"原则冲突，配置写错会变成静默不浇水。

## 其他
- 运行测试用项目内 venv：`.venv/bin/python -m pytest`（全量约 3 分钟，慢在真实 `time.sleep`）；
  单文件也可直接 `.venv/bin/python tests/test_camera.py`（依赖 `tests/_bootstrap.py` + `pytest.ini` 的 `pythonpath`，勿删）。
  系统/其他解释器没有 pytest 与项目依赖，必须用 `.venv/bin/python`。
- `.env` 明文存放钉钉凭证，勿提交。
