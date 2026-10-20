# 每日照片与报告的钉钉推送

> **结论先行**：定时任务一直在跑、报告也能生成，但**照片从来没有送到钉钉**；
> 更糟的是，**没有推送凭证时整条链路是静默的** —— 调用方以为推成功，
> 用户什么都没收到，日志里也没有报错。

## 1. 根因

### 根因 A：照片压根没进推送逻辑

改造前 `assistant.take_photo_and_report()` 只发了两条消息：

```text
📷 已拍摄花园照片: garden_20261012_130000.jpg，正在分析…
🌿 花园生长报告 …（纯文本 markdown）
```

第一条**只有文件名，没有图片**；`DingTalkBot.send_image()` 从未被调用过。

### 根因 B：唯一实现的通道是 webhook，而内网发不出图片

`DingTalkBot` 当时只实现了自定义机器人 **webhook** 通道，且没有任何
`DINGTALK_WEBHOOK` 配置时**只打一行日志就返回 `errcode=0`**，调用方完全看不出失败。

即便配上 webhook，也解决不了照片问题：

> 钉钉 markdown 消息里的 `![](url)` 是**钉钉服务端去抓取**的，
> 必须是公网可访问的 URL。本项目部署在家庭内网、没有公网 IP，钉钉抓不到。

于是「照片显示不出来」在两套配置下都会发生：

| 配置状态 | 报告文字 | 照片 | 用户感知 |
|---|---|---|---|
| 无 webhook（实际状态） | ❌ 只打了 mock 日志 | ❌ | 什么都没收到 |
| 配了 webhook | ✅ 发到群里 | ❌ 图片裂开 | 只看到文字，没有照片 |

## 2. 方案：改用企业内部机器人单聊

钉钉官方文档《消息发送与接收类型》里有一条决定性说明：

> **图片类型 `sampleImageMsg`** 的 `photoURL` 可填写图片的完整 URL 路径，
> **也可填写图片的 mediaId**。mediaId 可通过**上传媒体文件**接口获取。

这就绕开了公网限制。完整链路（三跳，全部只需一个企业应用凭证）：

```text
① POST https://api.dingtalk.com/v1.0/oauth2/accessToken
     {appKey, appSecret}                    -> accessToken

② POST https://oapi.dingtalk.com/media/upload?access_token=…&type=image
     multipart: media=<本地 jpg>             -> media_id (@lADP…)

③ POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend
     header: x-acs-dingtalk-access-token
     body:   {robotCode, userIds, msgKey:"sampleImageMsg",
              msgParam:"{\"photoURL\":\"<media_id>\"}"}
```

**不需要公网 IP、不需要图床、不需要 AgentId**，用已有的
`CLIENT_ID / CLIENT_SECRET / ROBOT_CODE` 三件套即可。

### 通道选型对比

| 通道 | 发照片 | 依赖 | 本场景 |
|---|---|---|---|
| **企业机器人单聊** | ✅ mediaId 直发 | 企业应用凭证 | ✅ 采用 |
| 自定义机器人 webhook | ⚠️ 需公网 URL | webhook + 公网 | 仅作备选 |
| 工作通知 `asyncsend_v2` | ✅ media_id | 额外需要 **AgentId + userid** | 不需要，凭证更多 |

## 3. `userId` 从哪来：自动订阅

单聊机器人必须指定 `userIds`，而 userId 不是手机号、用户很难自己查到。
更关键的是钉钉的一条硬性约束：

> **机器人不能凭空发起单聊**。用户必须先和机器人说过话，否则接口返回
> `robot.oto.notExist` / `session.notExisted`。

所以本项目把这两件事合并解决：

* Stream 收到任何消息时，`WateringChatbotHandler` 会把 `senderStaffId`
  自动写进 `data/subscribers.json`（原子写入，去重）；
* 用户在钉钉里发一句 **「订阅」**，会收到明确回执并列出当前接收人数量；
* 也可以在 `config.yaml` 的 `dingtalk.user_ids` 里直接预置。

## 4. 推送长什么样

到点（默认每天 13:00，`config.yaml` 的 `push.daily_report` 可改）依次收到：

1. **图片消息**——今日花园照片（`📷 今日花园`）
2. **文本**——`🔍 正在分析今日照片…`（AI 分析要几十秒，先给个交代）
3. **Markdown**——`🌿 花园生长报告  健康评分 N/5` + 完整报告

顺序是刻意的：**照片先于报告**。另外，任何一环失败都会把原因写进报告抬头：

```text
⚠️ [推送失败:no_session] 照片未能送达钉钉：钉钉接口返回: robot.oto.notExist …
💡 请先在钉钉客户端里找到该机器人并发送一条消息（例如「帮助」），建立单聊会话后机器人才能主动推送。
```

## 5. 不再静默失败

按项目既定原则，推送链路的每一处失败都必须可见：

| 环节 | 旧行为 | 新行为 |
|---|---|---|
| 无任何通道 | 返回 `errcode=0, errmsg=mock`，调用方以为成功 | `mock_hardware=true` 才允许本地打印；生产环境返回 `not_configured` + hint |
| 启动时通道缺失 | 无提示 | `main.py` 打 **ERROR** 日志，列出缺哪个凭证 |
| 照片推送失败 | 无感知 | 报告抬头插入 `⚠️ [推送失败:分类]` 横幅 |
| 定时任务崩了 | 只打日志 | 额外推一条 `❌ 每日照片/报告任务失败：<原因>` 给用户 |
| 接收人无效 | 无感知 | 返回 `invalid` 列表 + warning 日志 |
| 内容过长被钉钉拒收 | 整个消息发不出去 | 超过 `max_markdown_chars` 自动截断并标注 |
| 订阅名单文件损坏 | —— | 显式 ERROR，且**不清空**原文件 |

失败分类沿用与硬件异常同一套口径（`not_configured` / `no_session` /
`no_permission` / `unreachable` / `not_found` / `too_large` / `unknown`），
每一类都带可执行的 `hint`。

## 6. 部署步骤

```bash
# 1. 确认 .env 里有三项（缺一不可）
#    DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_ROBOT_CODE

# 2. 自检（只读，不发消息）
.venv/bin/python tools/dingtalk_diag.py

# 3. 在钉钉里给机器人发一条「订阅」，然后实测一条真实消息
.venv/bin/python tools/dingtalk_diag.py --live

# 4. 重启主程序
.venv/bin/python main.py
```

`tools/dingtalk_diag.py` 退出码：`0` 就绪 ／ `1` 不可用 ／
`2` 通道可用但**没有接收人**。

## 7. 排障表

| 现象 | 分类 | 处置 |
|---|---|---|
| 从未收到任何消息 | `not_configured` | 补 `.env` 三项凭证 |
| `robot.oto.notExist` / `session.notExisted` | `no_session` | 在钉钉里给机器人发一条消息 |
| `Forbidden` / 无权限 | `no_permission` | 开发者后台申请「企业内机器人发送消息权限」并**发布应用** |
| 连不上钉钉 | `unreachable` | 检查树莓派出网/代理是否放行 `api.dingtalk.com`、`oapi.dingtalk.com` |
| 自检 `0` 但没人收到 | —— | `dingtalk.user_ids` 与 `data/subscribers.json` 是否为空 |
| 报告被截断 | `too_long` | 调小 `ai.max_tokens` 或调大 `dingtalk.max_markdown_chars` |

## 8. 已知限制

* **必须先建立单聊会话**：钉钉不允许机器人主动发起单聊，用户需先说过一句话。
  因此"全新部署、从未对话过"时，第一次推送会失败 —— 这是钉钉的产品约束，不是 bug。
* **图片上限 20MB**：当前 `2304×1296` JPEG 约 1MB，远低于上限。
* **`media_id` 会复用**：每次推送重新上传，避免过期风险。
* **报告长度上限**：钉钉对超长 markdown 直接拒收，已做 3500 字截断保护
  （`dingtalk.max_markdown_chars` 可调）。
