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

## 其他
- 运行测试用项目内 venv：`.venv/bin/python -m pytest`（全量约 3 分钟，慢在真实 `time.sleep`）；
  单文件也可直接 `.venv/bin/python tests/test_camera.py`（依赖 `tests/_bootstrap.py` + `pytest.ini` 的 `pythonpath`，勿删）。
  系统/其他解释器没有 pytest 与项目依赖，必须用 `.venv/bin/python`。
- `.env` 明文存放钉钉凭证，勿提交。
