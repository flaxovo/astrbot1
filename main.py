from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
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
DEFAULT_EDIT_ENDPOINT = "/v1/images/edits"
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
            self.data_dir = Path(__file__).resolve().parent
        else:
            self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.output_dir = self.data_dir / "generated"
        self.selfie_ref_dir = self.data_dir / "selfie_refs"
        self._recent_images_by_user: dict[str, tuple[float, list[bytes]]] = {}
        self._pending_selfie_confirm: dict[str, tuple[float, str]] = {}

    async def initialize(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.selfie_ref_dir.mkdir(parents=True, exist_ok=True)

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

        pending_prompt, confirmation_handled = self._resolve_pending_selfie(
            event, message
        )
        if confirmation_handled:
            if pending_prompt:
                async for result in self._handle_selfie(event, pending_prompt):
                    yield result
            else:
                event.stop_event()
                yield event.plain_result("好，那我先不发照片。")
            return

        natural_selfie_prompt = self._build_natural_selfie_prompt(message)
        if natural_selfie_prompt:
            self._set_pending_selfie(event, natural_selfie_prompt)
            if self._should_plugin_send_natural_confirm():
                event.stop_event()
                yield event.plain_result(
                    self._build_selfie_confirmation_message(natural_selfie_prompt)
                )
            return

        if not self._matches_keyword(message):
            await self._remember_recent_images(event)
            return

        prompt = self._extract_keyword_prompt(message)
        async for result in self._handle_generation(event, prompt):
            yield result

    @filter.command("自拍")
    async def selfie(self, event: AstrMessageEvent):
        """基于自拍参考照生成新的自拍图。用法：/自拍 窗边自然光，微笑"""
        prompt = self._extract_command_prompt(event)
        async for result in self._handle_selfie(event, prompt):
            yield result

    @filter.command("自拍参考")
    async def selfie_reference(self, event: AstrMessageEvent):
        """管理自拍参考照。用法：发送图片 + /自拍参考 设置"""
        action = self._extract_command_prompt(event).strip()
        async for result in self._handle_selfie_reference(event, action):
            yield result

    @filter.llm_tool(name="gpt_img_2_generate")
    async def gpt_img_2_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        mode: str = "auto",
        ask_first: bool = False,
    ):
        """生成图片或基于自拍参考照生成 Bot 自拍。

        使用建议：
        - 用户明确要求画一张图、生成某个场景或物品：mode=text，ask_first=false。
        - 用户说“让我看看你”“拍张今天照片”“看看今天穿搭”等，但还没有明确同意看照片：mode=ask_selfie，ask_first=true。
        - 用户已经回复“要看/看看/来一张/好/OK”等确认词：mode=selfie，ask_first=false。
        - 用户在聊 Bot 自己的照片、自拍、穿搭、今天怎么穿：优先使用 selfie，而不是普通文生图。
        - 如果只是要自然地问用户是否想看，可以直接用你的人设和记忆自然回复；插件已记录待确认自拍意图，用户之后确认时会自动生成。
        - 成功后插件会直接把图片发送给用户，模型不要再伪造图片或描述成已经看过真实照片。

        Args:
            prompt(string): 图片提示词。自拍模式下写清楚场景、穿搭、光线、姿势。
            mode(string): auto/text/selfie/ask_selfie。auto 会按提示词语义选择。
            ask_first(boolean): true 表示只追问确认并记录待生成提示词，不立即生成。
        """
        prompt = (prompt or "").strip()
        resolved_mode = self._resolve_llm_tool_mode(prompt, mode, ask_first)

        if resolved_mode == "ask_selfie":
            event.stop_event()
            pending_prompt = prompt or self._default_selfie_prompt()
            self._set_pending_selfie(event, pending_prompt)
            yield event.plain_result(
                self._build_selfie_confirmation_message(pending_prompt)
            )
            return

        if resolved_mode == "selfie":
            async for result in self._handle_selfie(
                event, prompt or self._default_selfie_prompt()
            ):
                yield result
            return

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
            yield event.plain_result(self._friendly_generation_error(exc, mode="image"))
            return

        yield event.image_result(image_ref)

    async def _handle_selfie(self, event: AstrMessageEvent, prompt: str):
        event.stop_event()
        if not self._get_selfie_bool("enabled", True):
            yield event.plain_result("自拍参考照功能已关闭。")
            return

        prompt = prompt.strip() or self._default_selfie_prompt()
        api_key = self._get_config_str("api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            yield event.plain_result("请先在插件配置中填写 api_key，或设置 OPENAI_API_KEY。")
            return

        try:
            image_ref = await self._generate_selfie(api_key, event, prompt)
        except Exception as exc:
            logger.exception("gpt-img-2 selfie generation failed")
            yield event.plain_result(self._friendly_generation_error(exc, mode="selfie"))
            return

        yield event.image_result(image_ref)

    async def _handle_selfie_reference(self, event: AstrMessageEvent, action: str):
        event.stop_event()
        if not self._get_selfie_bool("enabled", True):
            yield event.plain_result("自拍参考照功能已关闭。")
            return

        normalized = action.strip() or "帮助"
        if normalized in {"设置", "set", "save"}:
            try:
                count = await self._save_selfie_reference_from_event(event)
            except Exception as exc:
                logger.exception("save selfie reference failed")
                yield event.plain_result(f"自拍参考照设置失败：{exc}")
                return
            yield event.plain_result(f"已保存 {count} 张自拍参考照。")
            return

        if normalized in {"查看", "show", "list"}:
            paths = self._get_selfie_reference_paths()
            if not paths:
                yield event.plain_result(
                    "还没有自拍参考照。请先发送图片并输入：/自拍参考 设置"
                )
                return
            yield event.image_result(str(paths[0]))
            yield event.plain_result(f"当前共有 {len(paths)} 张自拍参考照。")
            return

        if normalized in {"删除", "delete", "clear"}:
            count = self._delete_saved_selfie_references()
            yield event.plain_result(f"已删除 {count} 张命令保存的自拍参考照。")
            return

        yield event.plain_result(
            "自拍参考照用法：发送图片 + /自拍参考 设置；/自拍参考 查看；/自拍参考 删除；/自拍 日常自拍照，窗边自然光。"
        )

    async def _generate_image(self, api_key: str, prompt: str) -> str:
        response = await asyncio.to_thread(self._request_image_generation, api_key, prompt)
        return self._image_ref_from_response(
            response, self._get_config_str("output_format", "png")
        )

    async def _generate_selfie(
        self, api_key: str, event: AstrMessageEvent, prompt: str
    ) -> str:
        reference_images = self._get_selfie_reference_images()
        if not reference_images:
            raise RuntimeError(
                "未设置自拍参考照。请先发送一张清晰人像图，然后输入：/自拍参考 设置"
            )

        extra_images = await self._extract_event_image_bytes(event)
        final_prompt = self._build_selfie_prompt(prompt, extra_refs=len(extra_images))
        request_images = [*reference_images, *extra_images]
        max_images = self._get_selfie_int("max_reference_images", 2)
        request_images = request_images[: max(1, max_images)]

        response = await asyncio.to_thread(
            self._request_image_edit,
            api_key,
            final_prompt,
            request_images,
        )
        output_format = self._get_selfie_str("output_format")
        if not output_format:
            output_format = self._get_config_str("output_format", "png")
        return self._image_ref_from_response(response, output_format)

    def _image_ref_from_response(
        self, response: dict[str, Any], output_format: str
    ) -> str:
        data = response.get("data") or []
        if not data:
            raise RuntimeError("接口没有返回图片数据")

        image_data = data[0]
        if image_data.get("url"):
            return image_data["url"]

        b64_json = image_data.get("b64_json")
        if not b64_json:
            raise RuntimeError("接口响应中没有 url 或 b64_json")

        extension = self._extension_for_format(output_format.lower())
        filename = f"{uuid.uuid4().hex}.{extension}"
        output_path = self.output_dir / filename
        output_path.write_bytes(base64.b64decode(b64_json))
        return str(output_path)

    def _request_image_generation(self, api_key: str, prompt: str) -> dict[str, Any]:
        base_url = self._get_config_str("base_url", DEFAULT_BASE_URL).rstrip("/")
        endpoint = self._get_config_str("endpoint", DEFAULT_ENDPOINT)
        url = self._build_api_url(base_url, endpoint)

        payload = {
            "model": self._get_config_str("model", DEFAULT_MODEL),
            "prompt": prompt,
            "size": self._resolve_size(prompt, self._get_config_str("size", "auto")),
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

    def _request_image_edit(
        self, api_key: str, prompt: str, images: list[bytes]
    ) -> dict[str, Any]:
        if not images:
            raise RuntimeError("缺少自拍参考图片")

        base_url = self._get_config_str("base_url", DEFAULT_BASE_URL).rstrip("/")
        endpoint = self._get_selfie_str("edit_endpoint", DEFAULT_EDIT_ENDPOINT)
        url = self._build_api_url(base_url, endpoint)

        model = self._get_selfie_str("model")
        if not model:
            model = self._get_config_str("model", DEFAULT_MODEL)
        size = self._get_selfie_str("size")
        if not size:
            size = self._get_config_str("size", "auto")

        fields = {
            "model": model,
            "prompt": prompt,
            "size": self._resolve_size(prompt, size),
            "n": "1",
        }

        quality = self._get_selfie_str("quality")
        if not quality:
            quality = self._get_config_str("quality", "auto")
        if quality:
            fields["quality"] = quality

        output_format = self._get_selfie_str("output_format")
        if not output_format:
            output_format = self._get_config_str("output_format", "png")
        if output_format:
            fields["output_format"] = output_format

        extra_body = self._get_selfie_value("extra_body", {})
        if isinstance(extra_body, dict):
            for key, value in extra_body.items():
                if value is not None:
                    fields[str(key)] = str(value)

        image_field = self._get_selfie_str("image_field", "image")
        files = [
            (
                image_field,
                f"reference_{index + 1}.{self._guess_image_ext(image_bytes)}",
                image_bytes,
                self._guess_image_mime(image_bytes),
            )
            for index, image_bytes in enumerate(images)
        ]
        body, content_type = self._build_multipart_body(fields, files)

        request = urllib.request.Request(
            url=url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
            method="POST",
        )

        timeout = self._get_config_int("timeout", 120)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_api_error(exc.code, body_text)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接图片编辑接口：{exc.reason}") from exc

        try:
            parsed = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("图片编辑接口返回了无法解析的 JSON") from exc

        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("图片编辑接口返回格式不正确")

    def _build_multipart_body(
        self,
        fields: dict[str, str],
        files: list[tuple[str, str, bytes, str]],
    ) -> tuple[bytes, str]:
        boundary = f"----astrbot-gpt-img-2-{uuid.uuid4().hex}"
        chunks: list[bytes] = []

        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "utf-8"
                )
            )
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")

        for name, filename, content, content_type in files:
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8")
            )
            chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            chunks.append(content)
            chunks.append(b"\r\n")

        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _build_api_url(self, base_url: str, endpoint: str) -> str:
        base = base_url.rstrip("/")
        path = endpoint.lstrip("/")
        if base.endswith("/v1") and path.startswith("v1/"):
            path = path[3:]
        return f"{base}/{path}"

    async def _save_selfie_reference_from_event(self, event: AstrMessageEvent) -> int:
        images = await self._extract_event_image_bytes(event)
        if not images:
            images = self._get_recent_images(event)
        if not images:
            raise RuntimeError(
                "当前消息里没有读到图片。请先发送图片，再输入 /自拍参考 设置。"
            )

        max_images = self._get_selfie_int("max_reference_images", 2)
        images = images[: max(1, max_images)]
        self.selfie_ref_dir.mkdir(parents=True, exist_ok=True)

        self._delete_saved_selfie_references()
        for index, image_bytes in enumerate(images):
            ext = self._guess_image_ext(image_bytes)
            path = self.selfie_ref_dir / f"selfie_ref_{index + 1}.{ext}"
            path.write_bytes(image_bytes)
        return len(images)

    async def _remember_recent_images(self, event: AstrMessageEvent) -> None:
        images = await self._extract_event_image_bytes(event)
        if not images:
            return
        self._recent_images_by_user[self._event_user_key(event)] = (time.time(), images)

    def _get_recent_images(self, event: AstrMessageEvent) -> list[bytes]:
        key = self._event_user_key(event)
        cached = self._recent_images_by_user.get(key)
        if not cached:
            return []

        cached_at, images = cached
        ttl = max(30, self._get_selfie_int("recent_image_ttl_seconds", 600))
        if time.time() - cached_at > ttl:
            self._recent_images_by_user.pop(key, None)
            return []
        return images

    def _event_user_key(self, event: AstrMessageEvent) -> str:
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        parts = [
            str(getattr(event, "unified_msg_origin", "") or ""),
            sender_id,
        ]
        return "::".join(part for part in parts if part) or "default"

    def _set_pending_selfie(self, event: AstrMessageEvent, prompt: str) -> None:
        self._pending_selfie_confirm[self._event_user_key(event)] = (
            time.time(),
            prompt or self._default_selfie_prompt(),
        )

    def _should_plugin_send_natural_confirm(self) -> bool:
        mode = self._get_selfie_str("natural_confirm_mode", "passive").lower()
        return mode in {"plugin", "direct", "template"}

    def _build_selfie_confirmation_message(self, prompt: str) -> str:
        text = prompt.strip()
        if any(marker in text for marker in ("穿搭", "ootd", "outfit")):
            return "我可以按今天的感觉搭一身拍给你看。要看吗？"
        if "今天" in text:
            return "我可以拍一张今天状态的照片给你。要看吗？"
        if any(marker in text for marker in ("自拍", "照片", "photo", "selfie")):
            return "我可以拍一张给你看。要看吗？"
        return "要不要我拍一张给你看？"

    def _resolve_pending_selfie(
        self, event: AstrMessageEvent, message: str
    ) -> tuple[str, bool]:
        key = self._event_user_key(event)
        pending = self._pending_selfie_confirm.get(key)
        if not pending:
            return "", False

        created_at, prompt = pending
        ttl = max(30, self._get_selfie_int("confirm_ttl_seconds", 180))
        if time.time() - created_at > ttl:
            self._pending_selfie_confirm.pop(key, None)
            return "", False

        if self._is_selfie_confirm_yes(message):
            self._pending_selfie_confirm.pop(key, None)
            return prompt, True
        if self._is_selfie_confirm_no(message):
            self._pending_selfie_confirm.pop(key, None)
            return "", True
        return "", False

    def _is_selfie_confirm_yes(self, message: str) -> bool:
        text = message.strip().lower()
        yes_words = {
            "要",
            "要看",
            "看看",
            "看",
            "发",
            "发来",
            "发我",
            "拍",
            "拍一张",
            "来一张",
            "来张",
            "可以",
            "好",
            "好的",
            "行",
            "嗯",
            "ok",
            "yes",
        }
        return text in yes_words or any(
            marker in text
            for marker in ("要看", "看看", "发来", "发我", "拍一张", "来一张")
        )

    def _is_selfie_confirm_no(self, message: str) -> bool:
        text = message.strip().lower()
        no_words = {"不要", "不用", "别", "算了", "不看", "no"}
        return text in no_words or any(
            marker in text for marker in ("不要", "不用", "算了", "不看")
        )

    def _build_natural_selfie_prompt(self, message: str) -> str:
        if not self._get_selfie_bool("natural_confirm_enabled", True):
            return ""
        if not self._get_selfie_bool("enabled", True):
            return ""

        text = message.strip()
        lowered = text.lower()
        if not text:
            return ""

        selfie_markers = (
            "看看你",
            "看下你",
            "看一下你",
            "让我看看你",
            "给我看看你",
            "你长什么样",
            "你的照片",
            "你的自拍",
            "你自己的照片",
            "拍张照片",
            "拍张今天照片",
            "今天照片",
            "自拍给我",
            "bot自拍",
            "机器人自拍",
        )
        outfit_markers = (
            "今天的穿搭",
            "今天穿搭",
            "看看穿搭",
            "看下穿搭",
            "穿搭给我看",
            "ootd",
            "outfit",
        )
        english_markers = (
            "your selfie",
            "your photo",
            "your picture",
            "your face",
            "show me you",
            "send me a selfie",
        )

        if any(marker in text for marker in outfit_markers) or any(
            marker in lowered for marker in ("ootd", "outfit")
        ):
            return (
                "今天穿搭自拍照，真实照片风格，展示上半身或全身穿搭，"
                "自然光，生活感，构图清晰"
            )
        if any(marker in text for marker in selfie_markers) or any(
            marker in lowered for marker in english_markers
        ):
            if "今天" in text:
                return "今天的日常自拍照，真实照片风格，自然光，生活感"
            return self._default_selfie_prompt()
        return ""

    def _resolve_llm_tool_mode(
        self, prompt: str, mode: str, ask_first: bool
    ) -> str:
        raw_mode = (mode or "auto").strip().lower()
        if ask_first or raw_mode in {"ask", "ask_selfie", "confirm", "confirm_selfie"}:
            return "ask_selfie"
        if raw_mode in {"selfie", "selfie_ref", "ref", "photo_of_bot"}:
            return "selfie"
        if raw_mode in {"text", "draw", "image", "txt2img"}:
            return "text"
        if raw_mode != "auto":
            return "text"

        natural_prompt = self._build_natural_selfie_prompt(prompt)
        if natural_prompt:
            return "selfie"
        return "text"

    def _default_selfie_prompt(self) -> str:
        return "日常自拍照，真实照片风格，自然光，亲近自然的表情"

    def _delete_saved_selfie_references(self) -> int:
        if not self.selfie_ref_dir.exists():
            return 0
        count = 0
        for path in self.selfie_ref_dir.iterdir():
            if path.is_file():
                path.unlink()
                count += 1
        return count

    def _get_selfie_reference_paths(self) -> list[Path]:
        configured = self._get_selfie_value("reference_images", [])
        paths: list[Path] = []
        if isinstance(configured, list):
            for value in configured:
                path = self._resolve_reference_path(str(value))
                if path and path.is_file():
                    paths.append(path)

        if paths:
            return paths

        if not self.selfie_ref_dir.exists():
            return []
        return sorted(
            path
            for path in self.selfie_ref_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )

    def _get_selfie_reference_images(self) -> list[bytes]:
        images: list[bytes] = []
        for path in self._get_selfie_reference_paths():
            try:
                image_bytes = path.read_bytes()
            except OSError:
                continue
            if image_bytes:
                images.append(image_bytes)
        return images

    def _resolve_reference_path(self, value: str) -> Path | None:
        text = value.strip()
        if not text:
            return None

        path = Path(text)
        if path.is_absolute():
            return path

        candidates = [
            self.data_dir / text,
            Path(__file__).resolve().parent / text,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return self.data_dir / text

    async def _extract_event_image_bytes(self, event: AstrMessageEvent) -> list[bytes]:
        images: list[bytes] = []
        for segment in self._get_event_segments(event):
            image_bytes = await self._segment_to_image_bytes(segment)
            if image_bytes:
                images.append(image_bytes)
        return images

    def _get_event_segments(self, event: AstrMessageEvent) -> list[Any]:
        try:
            segments = event.get_messages()
            if isinstance(segments, list):
                return segments
        except Exception:
            pass

        message_obj = getattr(event, "message_obj", None)
        segments = getattr(message_obj, "message", None)
        return segments if isinstance(segments, list) else []

    async def _segment_to_image_bytes(self, segment: Any) -> bytes | None:
        if not self._looks_like_image_segment(segment):
            return None

        convert_to_base64 = getattr(segment, "convert_to_base64", None)
        if callable(convert_to_base64):
            try:
                encoded = await convert_to_base64()
                return self._decode_image_base64(str(encoded))
            except Exception as exc:
                logger.warning("convert image segment to base64 failed: %s", exc)

        for attr in ("path", "file"):
            value = str(getattr(segment, attr, "") or "").strip()
            if value.startswith("file://"):
                value = value[7:]
            if value and Path(value).is_file():
                return await asyncio.to_thread(Path(value).read_bytes)

        url = str(getattr(segment, "url", "") or "").strip()
        if url.startswith(("http://", "https://")):
            return await asyncio.to_thread(self._download_image_bytes, url)
        return None

    def _looks_like_image_segment(self, segment: Any) -> bool:
        if segment is None:
            return False
        class_name = segment.__class__.__name__.lower()
        if class_name == "image":
            return True
        if callable(getattr(segment, "convert_to_base64", None)):
            return True
        return any(getattr(segment, attr, None) for attr in ("url", "file", "path"))

    def _download_image_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-gpt-img-2"})
        timeout = min(self._get_config_int("timeout", 120), 30)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "image" not in content_type and "octet-stream" not in content_type:
                raise RuntimeError(f"URL 不是图片：{content_type}")
            image_bytes = response.read(20 * 1024 * 1024 + 1)
        if len(image_bytes) > 20 * 1024 * 1024:
            raise RuntimeError("图片超过 20MB")
        return image_bytes

    def _decode_image_base64(self, value: str) -> bytes:
        text = value.strip()
        if text.startswith("data:image/"):
            _, _, text = text.partition(",")
        if text.startswith("base64://"):
            text = text[len("base64://") :]
        return base64.b64decode(text, validate=False)

    def _build_selfie_prompt(self, prompt: str, extra_refs: int) -> str:
        prefix = self._get_selfie_str("prompt_prefix", "")
        if not prefix:
            identity_strength = self._get_selfie_str("identity_strength", "balanced")
            prefix = (
                "请生成一张全新的照片，而不是复刻参考图。\n"
                f"身份参考强度：{identity_strength}。\n"
                "第一张参考图只用于识别人脸身份、五官比例、脸型和整体气质；"
                "不要沿用参考图的尺寸、构图、镜头距离、姿势、手势、衣服、背景、光线、色调或拍摄角度。\n"
                "必须根据用户这次的要求重新设计场景、穿搭、姿势、构图和光线；"
                "让画面看起来像同一个人在另一天、另一个地点、另一个姿势重新拍了一张照片。\n"
                "如果有额外参考图，它们只能作为服装、姿势、构图或场景的松散灵感，不能照搬。\n"
                "输出真实照片风格，不要拼图，不要水印，不要文字。"
            )

        user_prompt = prompt.strip() or "日常自拍照，真实照片风格，自然光"
        variety = self._get_selfie_str("variation_instruction", "")
        if not variety:
            variety = (
                "请明显区别于参考图：换一个新背景、新姿势、新构图和新镜头距离。"
            )
        user_prompt = f"{user_prompt}\n{variety}"
        if extra_refs > 0:
            return f"{prefix}\n\n用户要求：{user_prompt}\n额外参考图数量：{extra_refs}"
        return f"{prefix}\n\n用户要求：{user_prompt}"

    def _guess_image_ext(self, image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "webp"
        if image_bytes.startswith(b"GIF"):
            return "gif"
        return "png"

    def _guess_image_mime(self, image_bytes: bytes) -> str:
        ext = self._guess_image_ext(image_bytes)
        if ext == "jpg":
            return "image/jpeg"
        return f"image/{ext}"

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
            if self._is_cjk_keyword(keyword):
                return rest.strip()
        return None

    def _is_cjk_keyword(self, keyword: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in keyword)

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

    def _resolve_size(self, prompt: str, configured_size: str) -> str:
        size = str(configured_size or "").strip().lower()
        if size and size not in {"auto", "自动"}:
            return configured_size

        text = prompt.lower()
        portrait_markers = (
            "自拍",
            "人像",
            "人物",
            "全身",
            "半身",
            "穿搭",
            "ootd",
            "outfit",
            "portrait",
            "selfie",
            "手机壁纸",
            "海报",
        )
        landscape_markers = (
            "风景",
            "山水",
            "城市",
            "街景",
            "全景",
            "横版",
            "宽屏",
            "桌面壁纸",
            "landscape",
            "panorama",
            "banner",
        )

        if any(marker in text for marker in landscape_markers):
            return "1536x1024"
        if any(marker in text for marker in portrait_markers):
            return "1024x1536"
        return "1024x1024"

    def _get_selfie_config(self) -> dict[str, Any]:
        value = self.config.get("selfie", {})
        return value if isinstance(value, dict) else {}

    def _get_selfie_value(self, key: str, default: Any = None) -> Any:
        return self._get_selfie_config().get(key, default)

    def _get_selfie_str(self, key: str, default: str = "") -> str:
        value = self._get_selfie_value(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_selfie_int(self, key: str, default: int) -> int:
        value = self._get_selfie_value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_selfie_bool(self, key: str, default: bool) -> bool:
        value = self._get_selfie_value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
        return bool(value)

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

    def _friendly_generation_error(self, exc: Exception, *, mode: str) -> str:
        text = str(exc)
        lowered = text.lower()
        is_selfie = mode == "selfie"

        if any(marker in text for marker in ("违反", "政策", "敏感", "不允许", "违规")):
            if is_selfie:
                return "这张我拍得不太合适，先不给你看啦。下次换个感觉再拍给你。"
            return "这张图的感觉不太合适，我换个说法再画会更稳一点。"

        if "未设置自拍参考照" in text:
            return "我还没准备好参考照呢。你先发一张想当参考的照片，再跟我说“自拍参考 设置”。"

        if "api_key" in lowered or "authorization" in lowered or "401" in text:
            return "我这边还没连好出图接口，等配置好钥匙再给你看。"

        if "timeout" in lowered or "超时" in text:
            return "刚刚拍得有点久，像是卡住了。等会儿我再给你补一张。"

        if "HTTP 500" in text or "HTTP 502" in text or "HTTP 503" in text:
            if is_selfie:
                return "这次没拍出来，可能状态不太好。下次给你看吧。"
            return "这次没画出来，接口那边有点不稳定。等下我再试试。"

        if is_selfie:
            return "这次照片没拍好，我先不发啦。下次给你看一张更好看的。"
        return "这次图片没生成好，我换个方式再试会更稳。"

    async def terminate(self):
        pass
