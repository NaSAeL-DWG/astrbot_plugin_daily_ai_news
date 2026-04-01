# astrbot_plugin_daily_ai_news

每日 AI 资讯自动推送插件 - 为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发

通过 RSS 订阅 [橘鸦 AI 日报](https://imjuya.github.io/juya-ai-daily/) 获取最新 AI 早报，经 **AI 总结** 后自动推送到 QQ 群 / 私聊。

## ✨ 功能

- 📰 **定时自动推送**：使用 `schedule_cron` 通过 `cron_manager` 定时触发每日 AI 资讯推送
- 🤖 **AI 智能总结**：调用 AstrBot 内置 LLM，将长篇早报精炼为 5-8 条关键要点
- 🔄 **手动获取**：发送 `/ainews` 随时获取最新 AI 资讯
- 📋 **灵活订阅**：支持配置文件填写群号/QQ号 + 群内指令订阅两种方式
- 🧹 **RSS 文本清洗**：沿用 `astrbot_plugin_dailytextnews` 的 RSS 获取与 HTML 清洗方式
- 💾 **缓存去重**：AI 总结结果缓存到本地，并按 RSS 内容键去重推送

## 📝 指令列表

| 指令             | 说明                              |
| ---------------- | --------------------------------- |
| `/ainews`        | 立即获取最新 AI 资讯（AI 总结版） |
| `/ainews_sub`    | 订阅每日推送（群聊/私聊均可使用） |
| `/ainews_unsub`  | 取消每日推送订阅                  |
| `/ainews_status` | 查看推送状态与订阅信息            |

## ⚙️ 配置说明

安装插件后，在 AstrBot 管理面板中可配置以下选项：

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `rss_urls` | RSS 源地址列表，可配置一个或多个订阅源 | 空 |
| `schedule_cron` | 使用 cron 表达式控制自动推送时间 | `0 9 * * *` |
| `target_groups` | 需要推送的目标列表，可包含群聊或私聊目标 | 空 |
| `llm_request_umo` | 用于请求 LLM 的统一消息来源 | 空 |
| `max_news_count` | 每个 RSS 源最多抓取的新闻条数 | `5` |
| `enable_auto_send` | 是否启用自动推送 | `false` |
| `skill_content` | 通过 WebUI 输入的消息前置文本，会插入到最终推送消息顶部 | 空 |

## 📦 安装

1. 在 AstrBot 管理面板中搜索 `astrbot_plugin_daily_ai_news` 安装
2. 或手动将本仓库克隆到 `addons/plugins/` 目录下
3. 重启 AstrBot 即可生效

## 📌 注意事项

- **需要配置 LLM**：AI 总结功能依赖 AstrBot 中已配置的 LLM Provider，请确保至少启用了一个 LLM 服务
- 可通过 `skill_content` 在 WebUI 中直接输入固定文案，作为推送消息前置内容
- 若 AI 总结失败，插件只会记录日志，不再回退输出原文内容
- 插件仅依赖 `schedule_cron` 触发自动推送，不再执行启动补偿或轮询检查
- 配置群号方式需要填写 QQ 群号码（纯数字），指令方式需在群内发送 `/ainews_sub`
- 两种订阅方式（配置文件 + 指令）可同时使用，插件会自动合并去重
- 相同 RSS 聚合内容不会重复推送，内容变化后会生成新的总结与推送
- AI 总结缓存最多保留最近 10 条记录

## 📝 更新日志

- ✨ 新增了多平台的支持。
- ⚙️ 新增了开启/关闭 AI 总结的选项支持。

## 📄 License

[GPL-3.0](LICENSE)
