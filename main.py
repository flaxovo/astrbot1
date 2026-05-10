from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    get_astrbot_data_path = None


PLUGIN_NAME = "astrbot_plugin_gpt_img_2"
DEFAULT_KEYWORDS = ["画图", "绘图", "生成图片", "gptimg", "gpt-img-2"]
DEFAULT_BASE_URL = "https://api.xbyjs.top"
DEFAULT_ENDPOINT = "/v1/images/generations"
DEFAULT_MODEL = "gpt-image-2"


@register(
    PLUGIN_NAME,
    "flaw",
    "通过关键词调用 OpenAI 兼容图片生成接口，根据用户描述生成图片。",
    "1.0.0",
)
class GptImg2Plugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}
        if get_astrbot_data_path is None:
            self.output_dir = Path(__file__).resolve().parent / "generated"
        else:
            self.output_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "generated"
            )

    async def initialize(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @filter.command("gptimg", alias=["画图", "绘图", "生成图片", "gpt-img-2"])
    async def gptimg(self, event: AstrMessageEvent):
        """根据提示词生成图片。用法：/gptimg 一只穿宇航服的猫"""
        prompt = self._extract_command_prompt(event)
        async for result in self._handle_generation(event, prompt):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_generate(self, event: AstrMessageEvent):
        """关键词触发图片生成，例如：画图 一张水彩风格的山间小屋"""
        message = event.message_str.strip()
        if message.startswith("/"):
            return
        if not self._matches_keyword(message):
            return

        prompt = self._extract_keyword_prompt(message)
        async for result in self._handle_generation(event, prompt):
            yield result

    async def _handle_generation(self, event: AstrMessageEvent, prompt: str):
        event.stop_event()
        prompt = prompt.strip()
        if not prompt:
            yield event.plain_result(
                "请在关键词后输入图片描述，例如：/gptimg 一张电影感的雪山日出。"
            )
            return

        api_key = self._get_config_str("api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            yield event.plain_result("请先在插件配置中填写 api_key，或设置 OPENAI_API_KEY。")
            return

        try:
            image_ref = await self._generate_image(api_key, prompt)
        except Exception as exc:
            logger.exception("gpt-img-2 image generation failed")
            yield event.plain_result(f"图片生成失败：{exc}")
            return

        yield event.image_result(image_ref)

    async def _generate_image(self, api_key: str, prompt: str) -> str:
        response = await asyncio.to_thread(self._request_image_generation, api_key, prompt)
        data = response.get("data") or []
        if not data:
            raise RuntimeError("接口没有返回图片数据")

        image_data = data[0]
        if image_data.get("url"):
            return image_data["url"]

        b64_json = image_data.get("b64_json")
        if not b64_json:
            raise RuntimeError("接口响应中没有 url 或 b64_json")

        output_format = self._get_config_str("output_format", "png").lower()
        extension = self._extension_for_format(output_format)
        filename = f"{uuid.uuid4().hex}.{extension}"
        output_path = self.output_dir / filename
        output_path.write_bytes(base64.b64decode(b64_json))
        return str(output_path)

    def _request_image_generation(self, api_key: str, prompt: str) -> dict[str, Any]:
        base_url = self._get_config_str("base_url", DEFAULT_BASE_URL).rstrip("/")
        endpoint = self._get_config_str("endpoint", DEFAULT_ENDPOINT)
        url = f"{base_url}/{endpoint.lstrip('/')}"

        payload = {
            "model": self._get_config_str("model", DEFAULT_MODEL),
            "prompt": prompt,
            "size": self._get_config_str("size", "1024x1024"),
            "n": 1,
        }

        quality = self._get_config_str("quality", "auto")
        if quality:
            payload["quality"] = quality

        output_format = self._get_config_str("output_format", "png")
        if output_format:
            payload["output_format"] = output_format

        extra_body = self.config.get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)

        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        timeout = self._get_config_int("timeout", 120)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_api_error(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接图片接口：{exc.reason}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("图片接口返回了无法解析的 JSON") from exc

        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("图片接口返回格式不正确")

    def _extract_command_prompt(self, event: AstrMessageEvent) -> str:
        message = event.message_str.strip()
        for keyword in self._get_keywords_longest_first():
            prefixes = (f"/{keyword}", keyword)
            for prefix in prefixes:
                if message.startswith(prefix):
                    return self._strip_prompt_separator(message[len(prefix) :])
        if message.startswith("/"):
            parts = message.split(maxsplit=1)
            if len(parts) == 2:
                return parts[1].strip()
        return message

    def _matches_keyword(self, message: str) -> bool:
        return self._split_keyword_prompt(message) is not None

    def _extract_keyword_prompt(self, message: str) -> str:
        prompt = self._split_keyword_prompt(message)
        return message if prompt is None else prompt

    def _split_keyword_prompt(self, message: str) -> str | None:
        for keyword in self._get_keywords_longest_first():
            if not message.startswith(keyword):
                continue

            rest = message[len(keyword) :]
            if not rest:
                return ""
            if rest[0].isspace() or rest[0] in ":：":
                return self._strip_prompt_separator(rest)
        return None

    def _strip_prompt_separator(self, value: str) -> str:
        value = value.strip()
        if value.startswith((":", "：")):
            return value[1:].strip()
        return value

    def _get_keywords(self) -> list[str]:
        configured = self.config.get("keywords")
        if isinstance(configured, list):
            keywords = [str(item).strip() for item in configured if str(item).strip()]
            if keywords:
                return keywords
        return DEFAULT_KEYWORDS

    def _get_keywords_longest_first(self) -> list[str]:
        return sorted(self._get_keywords(), key=len, reverse=True)

    def _get_config_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_config_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _extension_for_format(self, output_format: str) -> str:
        mime_type = f"image/{output_format}"
        extension = mimetypes.guess_extension(mime_type)
        if extension:
            return extension.lstrip(".")
        return "png"

    def _format_api_error(self, status_code: int, body: str) -> str:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return f"接口返回 HTTP {status_code}: {body[:300]}"

        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or str(error)
            return f"接口返回 HTTP {status_code}: {message}"
        return f"接口返回 HTTP {status_code}: {body[:300]}"

    async def terminate(self):
        pass
