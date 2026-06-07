import asyncio
import hashlib
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import atoma
import httpx
from bs4 import BeautifulSoup

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

DEFAULT_NEWS_PROMPT = """你是一个专业的 AI 新闻播报编辑。请根据提供的 RSS 新闻原始内容，输出一份适合直接发送给用户的中文 AI 新闻播报稿。

要求：
1. 只保留最值得关注的重点内容。
2. 表达简洁、自然、易读。
3. 突出公司、产品、模型、数据或行业变化等关键信息。
4. 保持内容准确，不要编造原文中不存在的信息。
5. 仅输出最终播报正文，不要输出解释、标题前缀或额外说明。

当日信息:
{time}

新闻原文：
{content}

请直接输出播报正文："""

MAX_CACHE_SIZE = 100
MAX_GENERATION_CONTENT_LENGTH = 80000
RSS_FETCH_TIMEOUT = 15.0
CACHE_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
FESTIVAL_TYPE_NAMES = {
    0: "工作日",
    1: "周末",
    2: "节日",
    3: "调休",
}
WEEKDAY_NAMES = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}


@register(
    "astrbot_plugin_daily_ai_news",
    "NaSAeL",
    "订阅AI日报并进行AI总结 ",
    "1.0.2",
    "https://github.com/NaSAeL-DWG/astrbot_plugin_daily_ai_news",
)
class DailyAINewsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        self._url_list = list(config.get("rss_urls", []))
        self._num = int(config.get("max_news_count", 10))
        self._target_groups = list(config.get("target_groups", []))
        self._schedule_cron = str(config.get("schedule_cron", "0 9 * * *"))
        self._enable_auto_send = bool(config.get("enable_auto_send", False))
        self._generate_news_umo = str(config.get("generate_news_umo", "")).strip()
        self._push_umo = str(config.get("push_umo", "")).strip()
        self._news_prompt = str(config.get("news_prompt", "")).strip()
        self._wrapper_text = str(config.get("wrapper_text", "")).strip()
        self._scheduled_job_id: str | None = None

        self._data_dir = StarTools.get_data_dir("astrbot_plugin_daily_ai_news")
        self._sent_file = self._data_dir / "sent_news.json"
        self._cache_file = self._data_dir / "summary_cache.json"

        self._sent_keys: set[str] = set()
        self._file_lock = asyncio.Lock()

        self._festival_api_url = str(config.get("festival_api_url", "")).strip()

    async def initialize(self):
        self._data_dir.mkdir(parents=True, exist_ok=True)
        await self._load_sent_news()
        await self._register_schedule()

        if self._push_umo:
            logger.info(
                "push_umo 已配置；当前 AstrBot send_message API 仍以目标会话为实际发送目标。"
            )

        logger.info("每日AI资讯推送插件已初始化")

    @filter.command("ainews")
    async def cmd_ainews(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 正在生成最新 AI 新闻预览，请稍候...")

        result = await self._prepare_news_result()
        if not result:
            yield event.plain_result("❌ 获取或生成 AI 新闻失败，请检查日志。")
            return

        yield event.plain_result(result["rendered_text"])

    async def _register_schedule(self):
        if not self._enable_auto_send:
            logger.info("自动发送未启用，跳过定时任务注册")
            return

        job_name = "daily_ai_news_broadcast"

        existing_jobs = await self.context.cron_manager.list_jobs(job_type="basic")
        duplicate_job_ids = {
            str(job.job_id)
            for job in existing_jobs
            if getattr(job, "name", None) == job_name
        }

        if self._scheduled_job_id:
            duplicate_job_ids.add(self._scheduled_job_id)

        for job_id in duplicate_job_ids:
            await self.context.cron_manager.delete_job(job_id)
            logger.info(f"删除已存在的重复定时任务: {job_id}")

        self._scheduled_job_id = None

        job = await self.context.cron_manager.add_basic_job(
            name=job_name,
            cron_expression=self._schedule_cron,
            handler=self.run_daily_news,
            description="每日 AI 资讯播报定时任务",
            timezone="Asia/Shanghai",
            enabled=True,
            persistent=False,
        )
        self._scheduled_job_id = job.job_id
        logger.info("每日 AI 资讯定时任务注册成功")

    async def run_daily_news(self):
        await self._push_latest_news()

    async def _push_latest_news(self):
        result = await self._prepare_news_result()
        if not result:
            logger.error("定时任务执行失败：获取或生成新闻内容失败")
            return

        news_key = result["news_key"]
        if news_key in self._sent_keys:
            logger.info(f"News batch already sent: {news_key}")
            return

        targets = self._get_push_targets()
        if not targets:
            logger.info("没有任何推送目标，跳过推送")
            return

        success_count = 0
        rendered_text = result["rendered_text"]
        for umo in targets:
            try:
                chain = MessageChain().message(rendered_text)
                await self.context.send_message(umo, chain)
                logger.info(f"已推送至: {umo}")
                success_count += 1
            except Exception as exc:
                logger.error(f"推送到 {umo} 失败: {exc}")

        if success_count > 0:
            self._sent_keys.add(news_key)
            await self._save_sent_news()
            logger.info(
                f"每日AI资讯推送完成，成功推送到 {success_count}/{len(targets)} 个目标"
            )
        else:
            logger.warning("所有推送目标均失败，不标记已推送，后续将重试")

    def _get_push_targets(self) -> list[str]:
        return sorted(set(self._target_groups))

    async def _prepare_news_result(self) -> dict[str, str] | None:
        batch = await self._fetch_rss_batch()
        if not batch:
            return None

        news_key = self._build_news_key(batch)
        festival_info = await self._fetch_festival_info()
        generation_cache_key = self._build_generation_cache_key(batch, festival_info)
        generated_text = await self._get_or_create_generated_text(
            batch, generation_cache_key, festival_info
        )
        if not generated_text:
            return None

        return {
            "news_key": news_key,
            "generated_text": generated_text,
            "rendered_text": self._render_message(generated_text),
        }

    async def get_pure_text(self, html_str: str) -> str:
        if not html_str:
            return ""
        try:
            soup = BeautifulSoup(html_str, "html.parser")
            return soup.get_text(separator=" ", strip=True)
        except Exception as exc:
            logger.error(f"[DailyAINews] HTML parse failed: {exc}")
            return html_str

    async def _fetch_rss_batch(self) -> dict[str, object] | None:
        items: list[dict[str, str]] = []

        async with httpx.AsyncClient(timeout=RSS_FETCH_TIMEOUT) as client:
            for url in self._url_list:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    feed = atoma.parse_rss_bytes(response.content)
                    for item in feed.items[: self._num]:
                        title = (item.title or "").strip()
                        summary = await self.get_pure_text(
                            getattr(item, "description", "")
                        )
                        link = str(getattr(item, "link", "") or "").strip()
                        if not title and not summary:
                            continue
                        items.append(
                            {
                                "title": title,
                                "summary": summary,
                                "link": link,
                            }
                        )
                        logger.debug(f"[DailyAINews] 成功获取新闻 {title}")
                except Exception as exc:
                    logger.error(f"[DailyAINews] Failed to fetch RSS from {url}: {exc}")

        if not items:
            return None

        normalized_items = sorted(
            items,
            key=lambda item: (
                item["link"],
                item["title"],
                item["summary"],
            ),
        )

        lines: list[str] = []
        links: list[str] = []
        for item in normalized_items:
            lines.append(f"标题: {item['title']}")
            lines.append(f"正文摘要: {item['summary']}")
            if item["link"]:
                lines.append(f"原文链接: {item['link']}")
                links.append(item["link"])

        primary_title = normalized_items[0]["title"] or "AI News"
        primary_link = links[0] if links else ""
        return {
            "title": primary_title,
            "content": "\n".join(lines),
            "links": links,
            "url": primary_link,
        }

    def _build_news_key(self, batch: dict[str, object]) -> str:
        links = sorted(
            {str(link).strip() for link in batch.get("links", []) if str(link).strip()}
        )
        if links:
            return "|".join(links)

        content = "\n".join(
            line.strip()
            for line in str(batch.get("content", "")).splitlines()
            if line.strip()
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _build_generation_cache_key(
        self, batch: dict[str, object], festival_info: str
    ) -> str:
        prompt_template = self._news_prompt or DEFAULT_NEWS_PROMPT
        cache_identity = {
            "news_key": self._build_news_key(batch),
            "prompt_template": prompt_template,
            "festival_info": festival_info,
            "cache_date": self._current_cache_date(),
        }
        cache_identity_text = json.dumps(
            cache_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(cache_identity_text.encode("utf-8")).hexdigest()
        return f"generated:{digest}"

    async def _get_or_create_generated_text(
        self, batch: dict[str, object], cache_key: str, festival_info: str
    ) -> str | None:
        cache = await self._read_summary_cache()
        cached = cache.get(cache_key)
        cached_text = self._extract_cached_generated_text(cached)
        if cached_text:
            logger.info(f"使用缓存的 AI 新闻内容 ({cache_key})")
            return cached_text

        generated_text = await self._generate_news_text(
            str(batch["content"]), festival_info
        )
        if not generated_text:
            logger.error(f"AI 新闻生成失败，跳过内容输出: {cache_key}")
            return None

        cache[cache_key] = {
            "title": str(batch.get("title", "")),
            "url": str(batch.get("url", "")),
            "generated_text": generated_text,
            "created_at": self._current_cache_datetime().isoformat(),
        }
        await self._save_summary_cache(cache)
        return generated_text

    def _current_cache_datetime(self) -> datetime:
        return datetime.now(CACHE_TIMEZONE)

    def _current_cache_date(self) -> str:
        return self._current_cache_datetime().date().isoformat()

    def _extract_cached_generated_text(
        self, cached: dict[str, object] | None
    ) -> str | None:
        if not isinstance(cached, dict):
            return None

        generated_text = str(cached.get("generated_text", "")).strip()
        if generated_text:
            return generated_text

        legacy_summary = str(cached.get("summary", "")).strip()
        if legacy_summary:
            return legacy_summary

        return None

    async def _fetch_festival_info(self) -> str:
        if not self._festival_api_url:
            return ""

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self._festival_api_url)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            logger.error(f"[DailyAINews] Failed to fetch festival info: {exc}")
            return ""

        if not isinstance(payload, dict):
            logger.warning("[DailyAINews] Festival API returned non-object payload")
            return ""

        if payload.get("code") not in (0, "0", None):
            logger.warning(f"[DailyAINews] Festival API returned error: {payload}")
            return ""

        normalized_payload = self._normalize_festival_payload(payload)
        return json.dumps(normalized_payload, ensure_ascii=False)

    def _normalize_festival_payload(self, payload: dict[str, object]) -> dict[str, object]:
        normalized_payload = dict(payload)

        type_info = payload.get("type")
        if isinstance(type_info, dict):
            normalized_type = dict(type_info)
            type_value = self._to_int(normalized_type.get("type"))
            week_value = self._to_int(normalized_type.get("week"))
            if type_value in FESTIVAL_TYPE_NAMES:
                normalized_type["type"] = FESTIVAL_TYPE_NAMES[type_value]
            if week_value in WEEKDAY_NAMES:
                normalized_type["week"] = WEEKDAY_NAMES[week_value]
            normalized_payload["type"] = normalized_type

        holiday_info = payload.get("holiday")
        if isinstance(holiday_info, dict):
            normalized_payload["holiday"] = dict(holiday_info)

        return normalized_payload

    def _to_int(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _generate_news_text(
        self, content: str, festival_info: str | None = None
    ) -> str | None:
        if not content or len(content.strip()) < 50:
            logger.warning("文章内容过短，跳过 AI 新闻生成")
            return None

        if not self._generate_news_umo:
            logger.error("未配置 generate_news_umo，无法生成 AI 新闻")
            return None

        prompt_template = self._news_prompt or DEFAULT_NEWS_PROMPT
        trimmed_content = content.strip()
        if len(trimmed_content) > MAX_GENERATION_CONTENT_LENGTH:
            trimmed_content = (
                trimmed_content[:MAX_GENERATION_CONTENT_LENGTH]
                + "\n...(内容过长已截断)"
            )

        if festival_info is None:
            festival_info = await self._fetch_festival_info()
        prompt = (
            prompt_template.replace("{content}", trimmed_content).replace(
                "{time}", festival_info
            )
        )

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=self._generate_news_umo
            )
            llm_response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            logger.debug("[DailyAINews] 成功获取AI总结新闻")
        except Exception as exc:
            logger.error(f"AI 新闻生成失败: {exc}")
            return None

        if llm_response and llm_response.completion_text:
            return llm_response.completion_text.strip()

        logger.warning("LLM 返回结果为空")
        return None

    def _render_message(self, generated_text: str) -> str:
        clean_generated_text = generated_text.strip()
        if not self._wrapper_text:
            return clean_generated_text
        return f"{clean_generated_text}\n\n{self._wrapper_text.strip()}"

    def _atomic_write(self, filepath: Path, data: dict):
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=filepath.parent,
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                json.dump(data, temp_file, ensure_ascii=False, indent=2)
                temp_path = Path(temp_file.name)
            temp_path.replace(filepath)
        except Exception as exc:
            logger.error(f"原子写入 {filepath} 失败: {exc}")
            if "temp_path" in locals() and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

    def _replace_corrupt_json_file(self, filepath: Path, replacement: dict):
        if filepath.exists():
            timestamp = self._current_cache_datetime().strftime("%Y%m%d%H%M%S%f")
            backup_path = filepath.with_name(f"{filepath.name}.corrupt.{timestamp}")
            try:
                filepath.replace(backup_path)
                logger.warning(f"已备份损坏的 JSON 文件: {backup_path}")
            except Exception as exc:
                logger.error(f"备份损坏 JSON 文件失败 {filepath}: {exc}")

        self._atomic_write(filepath, replacement)

    async def _load_sent_news(self):
        async with self._file_lock:
            self._sent_keys = set()
            try:
                if self._sent_file.exists():
                    data = json.loads(self._sent_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("sent news JSON root is not an object")
                    self._sent_keys.update(data.get("sent_keys", []))
                    self._sent_keys.update(data.get("sent_links", []))
                    self._sent_keys.update(
                        item
                        for item in data.get("sent_ids", [])
                        if not re.match(r"\d{4}-\d{2}-\d{2}$", item)
                    )
                    logger.info(f"已加载 {len(self._sent_keys)} 个已发送内容键")
            except Exception as exc:
                logger.error(f"加载已推送记录失败: {exc}")
                self._sent_keys = set()
                self._replace_corrupt_json_file(self._sent_file, {"sent_keys": []})

    async def _save_sent_news(self):
        async with self._file_lock:
            try:
                self._atomic_write(
                    self._sent_file,
                    {"sent_keys": sorted(self._sent_keys)},
                )
            except Exception as exc:
                logger.error(f"保存已推送记录失败: {exc}")

    async def _read_summary_cache(self) -> dict[str, dict]:
        async with self._file_lock:
            try:
                if self._cache_file.exists():
                    data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("summary cache JSON root is not an object")
                    return data
            except Exception as exc:
                logger.error(f"读取总结缓存失败: {exc}")
                self._replace_corrupt_json_file(self._cache_file, {})
            return {}

    async def _save_summary_cache(self, cache: dict[str, dict]):
        async with self._file_lock:
            try:
                if len(cache) > MAX_CACHE_SIZE:
                    sorted_items = sorted(
                        cache.items(),
                        key=lambda item: (self._cache_created_at(item[1]), item[0]),
                    )
                    cache = dict(sorted_items[-MAX_CACHE_SIZE:])
                self._atomic_write(self._cache_file, cache)
            except Exception as exc:
                logger.error(f"保存总结缓存失败: {exc}")

    def _cache_created_at(self, cached: object) -> datetime:
        if not isinstance(cached, dict):
            return datetime.min.replace(tzinfo=timezone.utc)

        created_at = cached.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            return datetime.min.replace(tzinfo=timezone.utc)

        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=CACHE_TIMEZONE)
        return parsed

    async def terminate(self):
        if self._scheduled_job_id:
            await self.context.cron_manager.delete_job(self._scheduled_job_id)
            self._scheduled_job_id = None
        logger.info("每日AI资讯推送插件已停用")
