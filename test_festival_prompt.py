import importlib
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch


def _install_astrbot_stubs():
    atoma = types.ModuleType("atoma")
    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_event = types.ModuleType("astrbot.api.event")
    astrbot_star = types.ModuleType("astrbot.api.star")
    bs4 = types.ModuleType("bs4")

    class _Logger:
        def debug(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    class _Star:
        def __init__(self, context):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(_name):
            return Path(".")

    def _register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    class _Filter:
        @staticmethod
        def command(_name):
            def decorator(func):
                return func

            return decorator

    class _MessageChain:
        def message(self, text):
            return text

    class _BeautifulSoup:
        def __init__(self, text, _parser):
            self._text = text

        def get_text(self, separator=" ", strip=True):
            return self._text.strip() if strip else self._text

    astrbot_api.AstrBotConfig = dict
    astrbot_api.logger = _Logger()
    astrbot_event.AstrMessageEvent = object
    astrbot_event.MessageChain = _MessageChain
    astrbot_event.filter = _Filter
    astrbot_star.Context = object
    astrbot_star.Star = _Star
    astrbot_star.StarTools = _StarTools
    astrbot_star.register = _register
    bs4.BeautifulSoup = _BeautifulSoup

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = astrbot_event
    sys.modules["astrbot.api.star"] = astrbot_star
    sys.modules["atoma"] = atoma
    sys.modules["bs4"] = bs4


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _AsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url):
        self.requested_url = url
        return _Response(self._payload)


class _EmptyAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _LLMResponse:
    completion_text = "generated"


class FestivalPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_news_text_replaces_time_with_normalized_festival_json(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="provider"),
            llm_generate=AsyncMock(return_value=_LLMResponse()),
        )
        plugin = main.DailyAINewsPlugin(
            context,
            {
                "generate_news_umo": "umo",
                "festival_api_url": "https://timor.tech/api/holiday/info/",
            },
        )

        payload = {
            "code": 0,
            "type": {"type": 2, "name": "国庆节", "week": 4},
            "holiday": {
                "holiday": True,
                "name": "国庆节",
                "wage": 3,
            },
        }

        with patch.object(
            main.httpx, "AsyncClient", return_value=_AsyncClient(payload)
        ):
            result = await plugin._generate_news_text("新闻内容" * 30)

        self.assertEqual(result, "generated")
        prompt = context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn('"type": "节日"', prompt)
        self.assertIn('"week": "周四"', prompt)
        self.assertIn('"holiday": true', prompt)
        self.assertNotIn("{time}", prompt)
        self.assertNotIn("{content}", prompt)

    async def test_fetch_rss_batch_configures_http_timeout(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        plugin = main.DailyAINewsPlugin(
            types.SimpleNamespace(),
            {
                "rss_urls": [],
            },
        )

        with patch.object(
            main.httpx, "AsyncClient", return_value=_EmptyAsyncClient()
        ) as async_client:
            result = await plugin._fetch_rss_batch()

        self.assertIsNone(result)
        async_client.assert_called_once_with(timeout=main.RSS_FETCH_TIMEOUT)

    async def test_prepare_news_result_does_not_reuse_content_only_cache(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        plugin = main.DailyAINewsPlugin(
            types.SimpleNamespace(),
            {
                "generate_news_umo": "umo",
                "festival_api_url": "https://timor.tech/api/holiday/info/",
            },
        )
        plugin._fetch_rss_batch = AsyncMock(
            return_value={
                "title": "AI News",
                "content": "新闻内容" * 30,
                "links": ["https://example.com/news-a"],
                "url": "https://example.com/news-a",
            }
        )
        plugin._fetch_festival_info = AsyncMock(return_value='{"type":{"type":"节日"}}')
        plugin._read_summary_cache = AsyncMock(
            return_value={
                "https://example.com/news-a": {
                    "generated_text": "old content-only generated text"
                }
            }
        )
        plugin._save_summary_cache = AsyncMock()
        plugin._generate_news_text = AsyncMock(return_value="new generated text")

        result = await plugin._prepare_news_result()

        self.assertEqual(result["news_key"], "https://example.com/news-a")
        self.assertEqual(result["generated_text"], "new generated text")
        plugin._generate_news_text.assert_awaited_once_with(
            "新闻内容" * 30, '{"type":{"type":"节日"}}'
        )

    async def test_generation_cache_key_includes_local_date(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        plugin = main.DailyAINewsPlugin(types.SimpleNamespace(), {})
        batch = {
            "content": "新闻内容" * 30,
            "links": ["https://example.com/news-a"],
        }

        plugin._current_cache_date = lambda: "2026-06-07"
        first_day_key = plugin._build_generation_cache_key(batch, "")
        plugin._current_cache_date = lambda: "2026-06-08"
        second_day_key = plugin._build_generation_cache_key(batch, "")

        self.assertNotEqual(first_day_key, second_day_key)

    async def test_generated_cache_entry_writes_created_at(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        plugin = main.DailyAINewsPlugin(types.SimpleNamespace(), {})
        plugin._read_summary_cache = AsyncMock(return_value={})
        plugin._save_summary_cache = AsyncMock()
        plugin._generate_news_text = AsyncMock(return_value="generated text")
        plugin._current_cache_datetime = lambda: datetime(
            2026, 6, 7, 20, 30, tzinfo=timezone(timedelta(hours=8))
        )

        await plugin._get_or_create_generated_text(
            {
                "title": "AI News",
                "content": "新闻内容" * 30,
                "url": "https://example.com/news-a",
            },
            "cache-key",
            "",
        )

        saved_cache = plugin._save_summary_cache.await_args.args[0]
        self.assertEqual(
            saved_cache["cache-key"]["created_at"], "2026-06-07T20:30:00+08:00"
        )

    async def test_save_summary_cache_trims_oldest_entries_by_created_at(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        plugin = main.DailyAINewsPlugin(types.SimpleNamespace(), {})
        written = {}

        def capture_write(_filepath, data):
            written.update(data)

        plugin._atomic_write = capture_write
        cache = {
            "missing-created-at": {"generated_text": "legacy"},
            "old-created-at": {
                "generated_text": "old",
                "created_at": "2026-01-01T00:00:00+08:00",
            },
        }
        start_time = datetime(
            2026, 6, 7, 0, 0, tzinfo=timezone(timedelta(hours=8))
        )
        for index in range(main.MAX_CACHE_SIZE):
            cache[f"new-{index:03d}"] = {
                "generated_text": f"new {index}",
                "created_at": (start_time + timedelta(minutes=index)).isoformat(),
            }

        await plugin._save_summary_cache(cache)

        self.assertEqual(len(written), main.MAX_CACHE_SIZE)
        self.assertNotIn("missing-created-at", written)
        self.assertNotIn("old-created-at", written)
        self.assertIn("new-000", written)

    async def test_corrupt_summary_cache_is_replaced_with_empty_cache(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        with TemporaryDirectory() as temp_dir:
            plugin = main.DailyAINewsPlugin(types.SimpleNamespace(), {})
            plugin._cache_file = Path(temp_dir) / "summary_cache.json"
            plugin._cache_file.write_text("{bad json", encoding="utf-8")

            cache = await plugin._read_summary_cache()

            self.assertEqual(cache, {})
            self.assertEqual(
                json.loads(plugin._cache_file.read_text(encoding="utf-8")), {}
            )
            backups = list(Path(temp_dir).glob("summary_cache.json.corrupt.*"))
            self.assertEqual(len(backups), 1)

    async def test_corrupt_sent_news_is_replaced_with_empty_sent_records(self):
        _install_astrbot_stubs()
        main = importlib.import_module("main")

        with TemporaryDirectory() as temp_dir:
            plugin = main.DailyAINewsPlugin(types.SimpleNamespace(), {})
            plugin._sent_file = Path(temp_dir) / "sent_news.json"
            plugin._sent_file.write_text("{bad json", encoding="utf-8")
            plugin._sent_keys = {"already-sent"}

            await plugin._load_sent_news()

            self.assertEqual(plugin._sent_keys, set())
            self.assertEqual(
                json.loads(plugin._sent_file.read_text(encoding="utf-8")),
                {"sent_keys": []},
            )
            backups = list(Path(temp_dir).glob("sent_news.json.corrupt.*"))
            self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
