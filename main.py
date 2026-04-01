import asyncio
import hashlib
import json
import os
import re
import tempfile

import atoma
import httpx
from bs4 import BeautifulSoup

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

# AI 总结 prompt
SUMMARY_PROMPT = """你是一个专业的 AI 资讯编辑。请将以下 AI 早报内容进行精炼总结，要求：
1. 提取最重要的 5-8 条新闻要点
2. 每条用一句话概括，突出关键信息（公司、产品、技术、数据）
3. 使用简洁的中文表述
4. 在开头加上日期
5. 保持新闻的时效性和准确性

原文内容：
{content}

请输出总结："""


@register(
    "astrbot_plugin_rss_ai_news",
    "NaSAeL",
    "订阅AI日报并进行AI总结 ",
    "1.0.1",
    "https://github.com/NaSAeL-DWG/astrbot_plugin_daily_ai_news",
)
class DailyAINewsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self.context = context
        self._url_list = config["rss_urls"]
        self._num = int(config["max_news_count"])
        self._target_groups = config["target_groups"]
        self._schedule_cron = config["schedule_cron"]
        self._llm_request_umo = config["llm_request_umo"]
        self._enable_auto_send = config.get("enable_auto_send", False)
        self._skill_content = str(config.get("skill_content", "")).strip()
        self._scheduled_job_id: str | None = None

        # 使用框架规范的数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_daily_ai_news")
        self._subscriptions_file = self._data_dir / "subscriptions.json"
        self._sent_file = self._data_dir / "sent_news.json"
        self._cache_file = self._data_dir / "summary_cache.json"

        # 通过指令订阅的 unified_msg_origin 集合
        self._cmd_subscriptions: set[str] = set()
        self._sent_keys: set[str] = set()

        # 文件读写互斥锁
        self._file_lock = asyncio.Lock()

    async def initialize(self):
        """插件初始化：加载持久化数据并注册定时任务。"""
        os.makedirs(self._data_dir, exist_ok=True)
        await self._load_subscriptions()
        await self._load_sent_news()
        await self._register_schedule()
        logger.info("每日AI资讯推送插件已初始化（RSS 订阅 + AI 总结模式）")

    # ==================== 指令处理 ====================

    # TODO:更改指令
    @filter.command("ainews")
    async def cmd_ainews(self, event: AstrMessageEvent):
        """手动获取最新 AI 早报"""
        yield event.plain_result("🔄 正在从 RSS 获取最新 AI 早报，请稍候...")
        batch = await self._fetch_rss_batch()
        if not batch:
            yield event.plain_result("😞 暂时未能获取到 AI 早报，请稍后再试。")
            return

        news_key = self._build_news_key(batch)
        text = await self._get_or_create_summary(batch, news_key)
        if not text:
            logger.error("AI 总结失败，跳过手动输出")
            return
        yield event.plain_result(text)

    @filter.command("ainews_sub")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每日 AI 资讯推送（在群聊中使用）"""
        umo = event.unified_msg_origin
        logger.info(f"订阅状态: {umo}")
        if umo in self._cmd_subscriptions:
            yield event.plain_result("📢 当前会话已订阅每日AI资讯推送。")
            return
        self._cmd_subscriptions.add(umo)
        await self._save_subscriptions()
        yield event.plain_result(
            "✅ 订阅成功！每日将自动推送 AI 早报总结到本群。\n"
            "取消订阅请发送 /ainews_unsub"
        )

    @filter.command("ainews_unsub")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消每日 AI 资讯推送订阅"""
        umo = event.unified_msg_origin
        if umo not in self._cmd_subscriptions:
            yield event.plain_result("ℹ️ 当前会话未通过指令订阅过 AI 资讯推送。")
            return
        self._cmd_subscriptions.discard(umo)
        await self._save_subscriptions()
        yield event.plain_result("✅ 已取消每日AI资讯推送订阅。")

    @filter.command("ainews_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看推送状态"""
        cache = await self._read_summary_cache()
        status_text = (
            "📊 每日AI资讯推送状态\n"
            f"⏰ Cron 表达式：{self._schedule_cron}\n"
            f"🤖 自动发送：{'已启用' if self._enable_auto_send else '已关闭'}\n"
            f"📋 指令订阅数：{len(self._cmd_subscriptions)}\n"
            f"📨 配置目标数：{len(self._target_groups)}\n"
            f"🧾 已发送内容键数：{len(self._sent_keys)}\n"
            f"💾 总结缓存数：{len(cache)}"
        )
        yield event.plain_result(status_text)

    # ==================== 定时推送 ====================

    async def _register_schedule(self):
        """通过 cron_manager 注册每日资讯播报任务。"""
        if self._scheduled_job_id:
            return

        job = await self.context.cron_manager.add_basic_job(
            name="daily_ai_news_broadcast",
            cron_expression=self._schedule_cron,
            handler=self.run_daily_news,
            description="每日 AI 资讯播报定时任务",
            timezone="Asia/Shanghai",
            enabled=True,
            persistent=False,
        )
        self._scheduled_job_id = job.job_id

    async def run_daily_news(self):
        """定时执行一次每日 AI 资讯播报。"""
        await self._push_latest_news()

    def _get_push_targets(self) -> list[str]:
        return sorted(set(self._target_groups) | self._cmd_subscriptions)

    async def _push_latest_news(self):
        """抓取最新 RSS 聚合内容并推送到所有订阅目标。"""
        batch = await self._fetch_rss_batch()
        if not batch:
            logger.info("No RSS batch available for push")
            return

        news_key = self._build_news_key(batch)
        if news_key in self._sent_keys:
            logger.info(f"News batch already sent: {news_key}")
            return

        logger.info(f"开始执行每日AI资讯推送: {batch['title']}")
        text = await self._get_or_create_summary(batch, news_key)
        if not text:
            logger.warning("未能生成 AI 总结，跳过本次推送")
            return

        targets = self._get_push_targets()
        if not targets:
            logger.info("没有任何推送目标，跳过推送")
            return

        success_count = 0
        for umo in targets:
            try:
                chain = MessageChain().message(text)
                await self.context.send_message(umo, chain)
                logger.info(f"已推送至: {umo}")
                success_count += 1
            except Exception as e:
                logger.error(f"推送到 {umo} 失败: {e}")

        if success_count > 0:
            self._sent_keys.add(news_key)
            await self._save_sent_news()
            logger.info(
                f"每日AI资讯推送完成，成功推送到 {success_count}/{len(targets)} 个目标"
            )
        else:
            logger.warning("所有推送目标均失败，不标记已推送，后续将重试")

    async def _get_or_create_summary(
        self, batch: dict[str, object], news_key: str
    ) -> str | None:
        """获取 RSS 聚合内容的 AI 总结，优先使用缓存。"""
        if not self.config.get("enable_ai_summary", True):
            logger.warning("未开启 AI 总结，跳过内容输出")
            return None

        cache = await self._read_summary_cache()
        cached = cache.get(news_key)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({news_key})")
            return self._format_summary(
                cached["title"], cached["url"], cached["summary"]
            )

        summary = await self._summarize_with_ai(str(batch["content"]))
        if summary:
            cache[news_key] = {
                "title": str(batch["title"]),
                "url": str(batch.get("url", "")),
                "summary": summary,
            }
            await self._save_summary_cache(cache)
            return self._format_summary(
                str(batch["title"]), str(batch.get("url", "")), summary
            )

        logger.error(f"AI 总结生成失败，跳过内容输出: {news_key}")
        return None

    # ==================== RSS 获取 ====================
    async def get_pure_text(self, html_str: str) -> str:
        if not html_str:
            return ""
        try:
            soup = BeautifulSoup(html_str, "html.parser")
            return soup.get_text(separator=" ", strip=True)
        except Exception as e:
            logger.error(f"[DailyAINews] HTML parse failed: {e}")
            return html_str

    async def _fetch_rss_batch(self) -> dict[str, object] | None:
        lines: list[str] = []
        links: list[str] = []

        async with httpx.AsyncClient() as client:
            for url in self._url_list:
                try:
                    response = await client.get(url)
                    feed = atoma.parse_rss_bytes(response.content)
                    for item in feed.items[: self._num]:
                        lines.append(f"标题: {item.title}")
                        pure_text = await self.get_pure_text(item.description)
                        lines.append(f"正文摘要: {pure_text}")
                        if getattr(item, "link", None):
                            links.append(item.link)
                except Exception as e:
                    logger.error(f"[DailyAINews] Failed to fetch RSS from {url}: {e}")

        if not lines:
            return None

        primary_link = links[0] if links else ""
        primary_title = lines[0].removeprefix("标题: ") if lines else "AI News"
        return {
            "title": primary_title,
            "content": "\n".join(lines),
            "links": links,
            "url": primary_link,
        }

    def _build_news_key(self, batch: dict[str, object]) -> str:
        links = [link for link in batch.get("links", []) if link]
        if links:
            return "|".join(links)

        content = str(batch.get("content", ""))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ==================== AI 总结 ====================

    async def _summarize_with_ai(self, content: str) -> str | None:
        """使用 AstrBot 内置 LLM 对内容进行总结。"""
        if not content or len(content.strip()) < 50:
            logger.warning("文章内容过短，跳过 AI 总结")
            return None

        try:
            # 内容过长时截断，避免超过模型上下文限制
            max_len = 8000
            if len(content) > max_len:
                content = content[:max_len] + "\n...(内容过长已截断)"

            prompt = SUMMARY_PROMPT.format(content=content)

            # 使用 AstrBot 提供的 LLM 接口
            provider = self.context.get_using_provider()
            if provider is None:
                logger.warning("未配置 LLM provider，无法进行 AI 总结")
                return None

            resp = await provider.text_chat(
                prompt=prompt,
                session_id="ainews_summary",
            )

            if resp and resp.completion_text:
                return resp.completion_text.strip()
            else:
                logger.warning("LLM 返回结果为空")
                return None

        except Exception as e:
            logger.error(f"AI 总结失败: {e}")
            return None

    # ==================== 格式化输出 ====================

    def _format_summary(self, title: str, url: str, summary: str) -> str:
        """格式化 AI 总结后的推送文本。"""
        prefix = f"{self._skill_content}\n\n" if self._skill_content else ""
        return (
            "📰 AI 早报速递\n"
            f"{'=' * 28}\n\n"
            f"{prefix}"
            f"📌 原文：{title}\n\n"
            f"🤖 AI 总结：\n\n"
            f"{summary}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{url}\n"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    # ==================== 持久化（带锁 + 原子写）====================

    def _atomic_write(self, filepath, data: dict):
        """原子写入 JSON 文件：先写临时文件，再 rename 替换。"""
        dir_path = os.path.dirname(str(filepath))
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(filepath))
            except Exception:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error(f"原子写入 {filepath} 失败: {e}")
            raise

    async def _load_subscriptions(self):
        """从文件加载指令订阅列表。"""
        async with self._file_lock:
            try:
                filepath = str(self._subscriptions_file)
                if os.path.exists(filepath):
                    with open(filepath, encoding="utf-8") as f:
                        data = json.load(f)
                    self._cmd_subscriptions = set(data.get("subscriptions", []))
                    logger.info(f"已加载 {len(self._cmd_subscriptions)} 个指令订阅")
            except Exception as e:
                logger.error(f"加载订阅列表失败: {e}")
                self._cmd_subscriptions = set()

    async def _save_subscriptions(self):
        """将指令订阅列表保存到文件。"""
        async with self._file_lock:
            try:
                self._atomic_write(
                    self._subscriptions_file,
                    {"subscriptions": list(self._cmd_subscriptions)},
                )
            except Exception as e:
                logger.error(f"保存订阅列表失败: {e}")

    async def _load_sent_news(self):
        """加载已推送记录。"""
        async with self._file_lock:
            self._sent_keys = set()
            try:
                filepath = str(self._sent_file)
                if os.path.exists(filepath):
                    with open(filepath, encoding="utf-8") as f:
                        data = json.load(f)
                    self._sent_keys.update(data.get("sent_keys", []))
                    self._sent_keys.update(data.get("sent_links", []))
                    self._sent_keys.update(
                        item
                        for item in data.get("sent_ids", [])
                        if not re.match(r"\d{4}-\d{2}-\d{2}$", item)
                    )
                    logger.info(f"已加载 {len(self._sent_keys)} 个已发送内容键")
            except Exception as e:
                logger.error(f"加载已推送记录失败: {e}")
                self._sent_keys = set()

    async def _save_sent_news(self):
        """保存已推送记录。"""
        async with self._file_lock:
            try:
                self._atomic_write(
                    self._sent_file,
                    {"sent_keys": sorted(self._sent_keys)},
                )
            except Exception as e:
                logger.error(f"保存已推送记录失败: {e}")

    async def _read_summary_cache(self) -> dict[str, dict]:
        """从文件读取 AI 总结缓存。"""
        async with self._file_lock:
            try:
                filepath = str(self._cache_file)
                if os.path.exists(filepath):
                    with open(filepath, encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logger.error(f"读取总结缓存失败: {e}")
            return {}

    async def _save_summary_cache(self, cache: dict[str, dict]):
        """保存 AI 总结缓存到文件。"""
        async with self._file_lock:
            try:
                # 仅保留最近 10 条缓存
                if len(cache) > 10:
                    sorted_keys = sorted(cache.keys())
                    cache = {k: cache[k] for k in sorted_keys[-10:]}
                self._atomic_write(self._cache_file, cache)
            except Exception as e:
                logger.error(f"保存总结缓存失败: {e}")

    async def terminate(self):
        """插件卸载时结束清理。"""
        if self._scheduled_job_id:
            await self.context.cron_manager.delete_job(self._scheduled_job_id)
            self._scheduled_job_id = None
        logger.info("每日AI资讯推送插件已停用")
