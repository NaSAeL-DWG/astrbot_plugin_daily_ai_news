# astrbot_plugin_daily_ai_news

每日 AI 资讯自动推送插件 - 为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发。

该插件会抓取 RSS 新闻，生成一份 AI 新闻播报正文，并按计划发送到指定目标会话。

## ✨ 功能

- 📰 **简化的 RSS → AI 新闻正文流程**：核心抓取与生成逻辑参考 `dailytextnews`
- 🤖 **生成与推送职责分离**：`generate_news_umo` 仅用于生成新闻，`push_umo` 仅保留为推送阶段配置语义
- ⏰ **定时自动推送**：使用 `schedule_cron` 结合 `cron_manager` 执行每日播报
- 💾 **缓存与去重**：保留本地生成缓存与已发送记录，避免重复生成与重复推送
- 📅 **当日信息注入**：可通过节日 API 获取当天节假日信息，并注入新闻生成提示词
- 🔄 **手动预览**：发送 `/ainews` 即可立即预览本次新闻内容
- 🧩 **可自定义提示词与包装文本**：提示词支持新闻正文和当日信息占位符，包装文本追加在 AI 正文后

## 📝 指令列表

| 指令 | 说明 |
| --- | --- |
| `/ainews` | 立即抓取并生成一份 AI 新闻预览，仅回复当前会话 |

## ⚙️ 配置说明

安装插件后，在 AstrBot 管理面板中可配置以下选项：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `rss_urls` | RSS 源地址列表，可配置一个或多个订阅源 | 空 |
| `schedule_cron` | 使用 cron 表达式控制自动推送时间 | `30 9 * * *` |
| `target_groups` | 自动推送的目标会话列表 | 空 |
| `generate_news_umo` | 用于解析生成新闻模型的会话 umo | 空 |
| `push_umo` | 推送阶段的会话 umo 语义配置 | 空 |
| `festival_api_url` | 节日/纪念日 API 地址，用于获取当天信息 | `https://timor.tech/api/holiday/info/` |
| `news_prompt` | 自定义新闻生成提示词，支持 `{content}` 和 `{time}` 占位符 | 空 |
| `wrapper_text` | 附加在 AI 正文后的包装文本 | 空 |
| `max_news_count` | 每个 RSS 源最多抓取的新闻条数 | `10` |
| `enable_auto_send` | 是否启用自动推送 | `false` |

## 📌 行为说明

- `generate_news_umo` **只用于生成新闻正文**。
- `push_umo` **不参与新闻生成**；当前 AstrBot `send_message` API 仍按目标会话发送消息，因此该字段目前仅保留为推送阶段配置语义。
- `{content}` 会被替换为 RSS 新闻原文整理结果。
- `{time}` 会被替换为节日 API 返回内容的 JSON 字符串；其中 `type.type` 和 `type.week` 会转为中文描述。
- 最终推送文本格式为：`AI 正文 + 空行 + wrapper_text`。
- 当 `wrapper_text` 为空时，仅发送 AI 正文。
- `/ainews` 使用与定时任务相同的抓取、缓存、生成、渲染流程，但仅返回当前会话预览，不写入已推送记录。
- 当 `enable_auto_send=false` 时，不注册定时推送，但 `/ainews` 仍可使用。

## ❌ fallback 说明

- RSS 获取失败：只返回/记录错误信息，不回退输出简要内容；单次 RSS 请求默认 15 秒超时。
- 节日 API 获取失败：记录错误并以空字符串替换 `{time}`，不影响新闻生成主流程。
- AI 生成失败：只返回/记录错误信息，不回退输出简要内容。
- 定时任务失败：仅记录日志，不向目标会话发送失败提示。

## 💾 持久化说明

插件会继续使用原有数据目录保存：

- `summary_cache.json`：缓存已生成的新闻正文（最多保留 100 条）
- `sent_news.json`：记录已成功推送的新闻键值

生成缓存会按新闻内容、提示词模板、当日信息和本地日期区分，避免跨天复用旧播报。旧版缓存中若存在 `summary` 字段，插件会兼容读取。

## 📦 安装

1. 在 AstrBot 管理面板中搜索 `astrbot_plugin_daily_ai_news` 安装
2. 或手动将本仓库克隆到 `data/plugins/` 目录下
3. 重启 AstrBot 即可生效

## 📄 License

[GPL-3.0](LICENSE)
