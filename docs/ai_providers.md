# AI 多模型配置与排障

本项目的照片分析不绑定任何一家大模型。换模型只改 `config.yaml` 的 `ai:` 段，
不用动代码。支持两类 API：

| api_style | 协议 | 覆盖范围 |
|---|---|---|
| `openai` | OpenAI Chat Completions（`/chat/completions`） | DeepSeek / OpenAI / 通义 / Moonshot / 智谱 / Ollama / vLLM / 自建网关 —— 事实上所有主流厂商都提供这一兼容端点 |
| `anthropic` | Anthropic Messages（`/v1/messages`） | Claude 官方；也可指向兼容端点（如 DeepSeek 的 `https://api.deepseek.com/anthropic`） |

一键自检：

```bash
python tools/ai_diag.py           # 只读：查配置、Key、模型与视觉能力（不联网、不花钱）
python tools/ai_diag.py --live    # 额外真实调用一次，确认链路通
```

---

## 1. 内置 provider 对照表

| provider | 默认模型 | 需要的环境变量 | 视觉 | 备注 |
|---|---|---|---|---|
| `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` | ✅ 仅 `deepseek-flash` | `deepseek-v4-pro` 是纯文本，不能看图 |
| `anthropic` | `claude-opus-4-7` | `ANTHROPIC_API_KEY` | ✅ | Claude 3 及以后都支持图片 |
| `openai` | `gpt-4o` | `OPENAI_API_KEY` | ✅ | |
| `openai_compatible` | 必须自填 | `OPENAI_API_KEY`（可改） | 由你声明 | 其它厂商一律走这个 |
| `ollama` | `llama3.2-vision` | 不需要 | 以本地模型为准 | 本地推理，`http://localhost:11434/v1` |
| `mock` | —— | 不需要 | ❌ | 显式占位模式，仅用于本地跑通流程 |

> 表里的模型名只是**当下可用的常见默认**，厂商会不断迭代。生产环境建议在
> `config.yaml` 里显式写明 `ai.model`，并以厂商文档为准。

---

## 2. 配置字段说明

```yaml
ai:
  provider: deepseek              # 上表的 provider 名，或自填（配合 base_url）
  model: deepseek-flash           # 留空 = 用 provider 默认模型
  api_key_env: DEEPSEEK_API_KEY   # 读哪个环境变量当 Key（值写在 .env）
  base_url: null                  # 留空 = 用 provider 默认；自定义端点在此填
  supports_vision: null           # 留空 = 由预设自动判定（deepseek 按模型白名单）
  max_tokens: 1500
  temperature: null               # 留空则不下发（DeepSeek 思考模式不支持该参数）
  timeout_sec: 60
  image_detail: high              # high=原图；low=缩到 512×512，更快更省 token
  extra_body: null                # 厂商私有参数，原样并入请求体
  allow_mock_fallback: false      # true = 失败时降级为占位报告（不推荐）
  system_prompt: |
    你是一名严谨的园艺师，只依据画面中可见的信息作答，不要编造看不到的细节。
  prompt: |
    你是一名资深园艺师。请基于这张花园照片……
```

---

## 3. 各厂商示例

### DeepSeek（当前默认，项目里已配好）

```yaml
ai:
  provider: deepseek
  model: deepseek-flash
  api_key_env: DEEPSEEK_API_KEY
```

`.env`：`DEEPSEEK_API_KEY=sk-...`

> **只有 `deepseek-flash` 能看图**。`deepseek-v4-pro` 是纯文本模型 —— 它收到图片时
> **不报错**，只会把图片替换成 `[Unsupported Image]` 之类的占位文字，然后**凭空编造**
> 一份看起来很正常的分析。本项目会在发送前就把这种情况拦下来（见 §4）。

### Claude

```yaml
ai:
  provider: anthropic
  model: claude-opus-4-7
  api_key_env: ANTHROPIC_API_KEY
```

### OpenAI

```yaml
ai:
  provider: openai
  model: gpt-4o
  api_key_env: OPENAI_API_KEY
```

### 通义千问 / Moonshot / 智谱 等（OpenAI 兼容）

```yaml
ai:
  provider: openai_compatible
  model: <该厂商的视觉模型名>          # 以厂商文档为准
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1   # 通义
  # base_url: https://api.moonshot.cn/v1                        # Moonshot
  # base_url: https://open.bigmodel.cn/api/paas/v4              # 智谱
  api_key_env: DASHSCOPE_API_KEY
  supports_vision: true                # 请确认所选模型确实支持图片
```

### 本地 Ollama

```yaml
ai:
  provider: ollama
  model: llama3.2-vision               # 以 `ollama list` 实际拉取的模型为准
```

### 走 DeepSeek 的 Anthropic 兼容端点（演示 api_style 覆盖）

```yaml
ai:
  provider: openai_compatible
  api_style: anthropic                  # 用 Anthropic Messages 协议
  base_url: https://api.deepseek.com/anthropic
  model: deepseek-flash
  api_key_env: DEEPSEEK_API_KEY
```

---

## 4. 视觉能力：为什么必须显式判定

这是本项目**最危险的静默失败**，值得单独说明。

纯文本模型收到图片时，绝大多数网关**不会返回错误**，而是：

1. 把图片悄悄换成占位文字（DeepSeek 实测为 `[Unsupported Image]`）；
2. 照常返回 **HTTP 200** 和一段流畅的中文；
3. 模型看不到任何画面，于是**编造**出「叶片饱满」「叶尖略黄」这类结论。

整条链路零报错：格式正常、评分正常、钉钉推送正常 —— **只有内容是假的**。

因此本项目在**发送前**就做判定：

* 预设里为 DeepSeek 配了视觉模型白名单（`deepseek-flash` 命中，`deepseek-v4-pro` 不命中）；
* 判定为不支持图片时，直接抛 `AIError(kind="vision_unsupported")`，**根本不发请求**；
* 也可用 `ai.supports_vision` 手动覆盖（换了新模型而预设还没更新时用）。

`ai_diag.py` 在模型不支持图片时返回退出码 **2**，提示你换模型。

---

## 5. 思考模式与 token 取舍（DeepSeek）

DeepSeek 的**思考模式默认开启**（effort=high），模型会先输出一段思维链再给正文。
对本项目「看图写报告」的任务，这带来三个副作用：

* 延迟明显变长；
* 输出 token 消耗成倍增加；
* **`max_tokens` 太小会导致正文为空** —— token 全被思维链吃掉，
  表现为 `content: ""` 且 `finish_reason: "length"`。本项目会把这种情况
  报成 `truncated` 并明确提示「输出 token 全部消耗在思考内容上」，而不是含糊的"空响应"。

关掉思考模式（`extra_body` 原样透传，属于厂商私有参数）：

```yaml
ai:
  extra_body:
    thinking:
      type: disabled
```

补充两点：

* 思考模式下 `temperature` **不生效**（传了也不报错），所以默认不下发；
* 图片细节级别 `image_detail: low` 会把图缩到 512×512 再送模型，更快更省 token。
  判断整体长势够用；要数叶片细节就用默认的 `high`。

---

## 6. 排障分诊表

| 现象 / 错误分类 | 含义 | 处置 |
|---|---|---|
| `no_api_key` | `ai.api_key_env` 指的变量没值 | 在 `.env` 里补上；跑 `ai_diag.py` 确认 |
| `auth_failed` | Key 无效 / 过期 / 无权限 | 去厂商控制台核对 Key，确认与 `api_key_env` 对应 |
| `rate_limited` | 限流或余额不足 | 稍后重试或换 provider/model |
| `vision_unsupported` | 模型不支持图片 | 换视觉模型（DeepSeek → `deepseek-flash`） |
| `model_not_found` | 模型名不对或不可用 | 核对 `ai.model` 拼写（DeepSeek 现为 `deepseek-flash` / `deepseek-v4-pro`） |
| `unreachable` | 连不上接口 | 查网络/代理，核对 `ai.base_url` |
| `bad_response` | 返回格式不对 | `base_url` 可能不是兼容端点 |
| `empty_response` | 模型返回空内容 | 提高 `max_tokens`，或关闭思考模式 |
| `truncated` | 输出被 `max_tokens` 截断 | 提高 `max_tokens` / 关闭思考模式 |
| `invalid_config` | 配置缺项 | 跑 `ai_diag.py`，它会指出缺哪一项 |

`python tools/ai_diag.py` 退出码：`0` 正常 / `1` 不可用 / `2` 可用但模型不支持图片。

---

## 7. 降级策略（`allow_mock_fallback`）

默认 **`false`**：没有 Key、调用失败、模型不支持图片时**直接抛 `AIError`**，
由编排层转成一条钉钉告警（「❌ …任务失败：原因」），不会生成报告。

打开后（**不推荐**，仅用于本地跑通全流程）：

* 生成占位报告，**报告抬头带 `⚠️ [MOCK]` 横幅**、`score` 置空；
* 横幅会写进 `.md` 文件，离线查看时同样一眼可辨；
* 钉钉状态页会标注「allow_mock_fallback=true —— 报告是占位模板，内容不可信」。

三种硬件/服务各自的降级开关是独立且默认关闭的，与「绝不静默失败」原则一致：

| 开关 | 作用 |
|---|---|
| `camera.allow_mock_fallback` | 相机不可用时是否出占位图 |
| `safety.allow_mock_fallback` | GPIO 不可用时是否假装浇水成功 |
| `ai.allow_mock_fallback` | AI 不可用时是否出占位报告 |

---

## 8. 代码结构

```
ai/
  errors.py          AIError（分类 kind + 可执行 hint），与 hardware/errors.py 同构
  providers.py       ProviderPreset 预设表 / OpenAICompatProvider / AnthropicProvider /
                     MockProvider / UnavailableProvider / build_provider 工厂
  plant_analyzer.py  PlantAnalyzer：编排「读图 → 调 provider → 落盘报告 → 标注降级」
```

新增一家厂商通常**不用改代码**：只要它提供 OpenAI 兼容端点，
在 `config.yaml` 里用 `provider: openai_compatible` + `base_url` 即可。
若想让它出现在 `ai_diag.py` 的预设列表里，在 `ai/providers.py::PRESETS`
加一条 `ProviderPreset` 即可。
