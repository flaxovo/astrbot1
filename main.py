from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import random
import re
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
    from astrbot.api.event import MessageChain
except Exception:
    MessageChain = None

try:
    from astrbot.core.message.components import File, Record
except Exception:
    File = None
    Record = None

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
DEFAULT_VOLC_TTS_BASE_URL = "https://openspeech.bytedance.com"
DEFAULT_VOLC_TTS_ENDPOINT = "/api/v1/tts"
DEFAULT_VOLC_TTS_V3_ENDPOINT = "/api/v3/tts/unidirectional"
DEFAULT_VOLC_TTS_CLUSTER = "volcano_tts"
DEFAULT_VOLC_TTS_APP_ID = "9694280449"
DEFAULT_VOLC_TTS_RESOURCE_ID = "seed-icl-2.0"
DEFAULT_VOLC_TTS_VOICE_TYPE = "S_RaFCxn8Q1"
OLD_PROACTIVE_CAPTION = "给你报备一下我现在的状态。"
DEFAULT_PROACTIVE_CAPTIONS = [
    "宝宝，{time_period}我刚刚在{activity}，给你偷偷看一眼。",
    "{time_period}刚才忙着{activity}，突然想起你，就顺手拍给你看。",
    "不用急着回我，我就是想让你看看我{time_period}在{activity}。",
    "{time_period}这一会儿在{activity}，像被你抽查到了一样。",
    "我没有消失哦，{time_period}刚刚在{activity}，给你看一下我这边的小现场。",
]


@register(
    PLUGIN_NAME,
    "flaw",
    "通过关键词调用 OpenAI 兼容图片生成接口，根据用户描述生成图片。",
    "1.1.1",
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
        self.voice_dir = self.data_dir / "voices"
        self.selfie_ref_dir = self.data_dir / "selfie_refs"
        self.proactive_targets_path = self.data_dir / "proactive_targets.json"
        self._recent_images_by_user: dict[str, tuple[float, list[bytes]]] = {}
        self._pending_selfie_confirm: dict[str, tuple[float, str]] = {}
        self._proactive_task: asyncio.Task | None = None

    async def initialize(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.selfie_ref_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self._proactive_is_active():
            self._proactive_task = asyncio.create_task(self._proactive_loop())

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

        natural_image_prompt = self._build_natural_image_prompt(message)
        if natural_image_prompt:
            async for result in self._handle_generation(event, natural_image_prompt):
                yield result
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

    @filter.command("状态图")
    async def proactive_status_image(self, event: AstrMessageEvent):
        """管理主动状态图。用法：/状态图 开启、/状态图 关闭、/状态图 立即"""
        action = self._extract_named_command_prompt(event, "状态图").strip()
        async for result in self._handle_proactive_status_command(event, action):
            yield result

    @filter.command("语音", alias=["tts", "说话"])
    async def tts(self, event: AstrMessageEvent):
        """使用火山语音合成文本。用法：/语音 晚点给你看照片。"""
        text = self._extract_command_prompt(event)
        async for result in self._handle_tts(event, text):
            yield result

    @filter.llm_tool(name="gpt_img_2_generate")
    async def gpt_img_2_generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        mode: str = "auto",
        ask_first: bool = False,
        current_state: str = "",
        today_outfit: str = "",
        memory_context: str = "",
    ):
        """生成图片或基于自拍参考照生成 Bot 自拍。

        使用建议：
        - 用户明确要求画一张图、生成某个场景或物品：mode=text，ask_first=false。
        - 用户说“让我看看你”“拍张今天照片”“看看今天穿搭”等，但还没有明确同意看照片：mode=ask_selfie，ask_first=true。
        - 用户已经回复“要看/看看/来一张/好/OK”等确认词：mode=selfie，ask_first=false。
        - 用户在聊 Bot 自己的照片、自拍、穿搭、今天怎么穿：优先使用 selfie，而不是普通文生图。
        - 如果记忆里有“今天安排/现在在做什么/今日穿搭/偏好”，把整理后的内容放进 current_state、today_outfit 或 memory_context。
        - 如果只是要自然地问用户是否想看，可以直接用你的人设和记忆自然回复；插件已记录待确认自拍意图，用户之后确认时会自动生成。
        - 成功后插件会直接把图片发送给用户，模型不要再伪造图片或描述成已经看过真实照片。

        Args:
            prompt(string): 图片提示词。自拍模式下写清楚场景、穿搭、光线、姿势。
            mode(string): auto/text/selfie/ask_selfie。auto 会按提示词语义选择。
            ask_first(boolean): true 表示只追问确认并记录待生成提示词，不立即生成。
            current_state(string): 主 Agent 从记忆中理解出的当前状态/日程，例如“下午在咖啡店看书”。
            today_outfit(string): 主 Agent 从记忆中理解出的今日穿搭。
            memory_context(string): 与图片有关的记忆摘要，例如日程、穿搭、偏好、地点。
        """
        prompt = (prompt or "").strip()
        prompt = self._merge_prompt_with_memory(
            prompt,
            current_state=current_state,
            today_outfit=today_outfit,
            memory_context=memory_context,
        )
        resolved_mode = self._resolve_llm_tool_mode(prompt, mode, ask_first)

        if resolved_mode == "ask_selfie":
            pending_prompt = prompt or self._default_selfie_prompt()
            self._set_pending_selfie(event, pending_prompt)
            yield event.plain_result(
                self._build_selfie_confirmation_message(pending_prompt)
            )
            return

        if resolved_mode == "selfie":
            try:
                image_ref = await self._generate_selfie_for_tool(
                    event,
                    prompt or self._default_selfie_prompt(),
                    memory_context=memory_context,
                )
            except RuntimeError as exc:
                yield event.plain_result(str(exc))
                return
            yield event.image_result(image_ref)
            return

        try:
            image_ref = await self._generate_image_for_tool(prompt)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.image_result(image_ref)

    @filter.llm_tool(name="gpt_img_2_speak")
    async def gpt_img_2_speak(self, event: AstrMessageEvent, text: str):
        """把一句自然回复合成为语音并发送给用户。

        Args:
            text(string): 要合成的中文回复。适合短句，不要放很长的整段说明。
        """
        text = (text or "").strip()
        if not text:
            yield event.plain_result("想让我说什么呀？")
            return
        try:
            audio_path = await self._generate_tts_audio(text)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return
        yield self._voice_result(event, audio_path, text=text)

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

    async def _handle_tts(self, event: AstrMessageEvent, text: str):
        event.stop_event()
        text = text.strip()
        if not text:
            yield event.plain_result("想让我说什么呀？")
            return

        try:
            audio_path = await self._generate_tts_audio(text)
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            return

        yield self._voice_result(event, audio_path, text=text)

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
            memory_context = await self._get_memory_context_for_event(event)
            image_ref = await self._generate_selfie(
                api_key,
                event,
                prompt,
                memory_context=memory_context,
            )
        except Exception as exc:
            logger.exception("gpt-img-2 selfie generation failed")
            yield event.plain_result(self._friendly_generation_error(exc, mode="selfie"))
            return

        yield event.image_result(image_ref)

    async def _generate_image_for_tool(self, prompt: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise RuntimeError("缺少图片描述")

        api_key = self._get_config_str("api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("api_key 未配置")

        try:
            return await self._generate_image(api_key, prompt)
        except Exception as exc:
            logger.exception("gpt-img-2 llm tool image generation failed")
            raise RuntimeError(
                self._friendly_generation_error(exc, mode="image")
            ) from exc

    async def _generate_selfie_for_tool(
        self, event: AstrMessageEvent, prompt: str, memory_context: str = ""
    ) -> str:
        if not self._get_selfie_bool("enabled", True):
            raise RuntimeError("自拍参考照功能已关闭")

        api_key = self._get_config_str("api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("api_key 未配置")

        try:
            if not memory_context:
                memory_context = await self._get_memory_context_for_event(event)
            return await self._generate_selfie(
                api_key,
                event,
                prompt,
                memory_context=memory_context,
            )
        except Exception as exc:
            logger.exception("gpt-img-2 llm tool selfie generation failed")
            raise RuntimeError(
                self._friendly_generation_error(exc, mode="selfie")
            ) from exc

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

    async def _handle_proactive_status_command(
        self, event: AstrMessageEvent, action: str
    ):
        event.stop_event()
        normalized = action.strip() or "帮助"
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()

        if normalized in {"开启", "订阅", "打开", "enable", "on"}:
            if not umo:
                yield event.plain_result("这个会话暂时拿不到发送地址，没法开启主动状态图。")
                return
            targets = self._load_saved_proactive_targets()
            if umo not in targets:
                targets.append(umo)
                self._save_proactive_targets(targets)
            self._ensure_proactive_task()
            interval = self._get_proactive_int("interval_seconds", 3600)
            yield event.plain_result(
                f"好呀，那我隔一段时间就偷偷给你发一张。现在大概 {interval} 秒一次。"
            )
            return

        if normalized in {"关闭", "取消", "停用", "disable", "off"}:
            targets = [target for target in self._load_saved_proactive_targets() if target != umo]
            self._save_proactive_targets(targets)
            yield event.plain_result("好，我先不主动发状态图了。")
            return

        if normalized in {"立即", "现在", "测试", "发一张", "马上"}:
            memory_context = await self._get_memory_context_for_event(event)
            proactive_context = self._build_proactive_context(memory_context)
            try:
                image_ref = await self._generate_proactive_status_image(
                    umo,
                    memory_context=memory_context,
                    proactive_context=proactive_context,
                )
                local_image = await self._ensure_local_image_ref(image_ref)
            except Exception as exc:
                logger.exception("proactive status image immediate generation failed")
                yield event.plain_result(self._friendly_generation_error(exc, mode="image"))
                return
            caption = self._build_proactive_caption(
                memory_context,
                proactive_context=proactive_context,
            )
            result = event.make_result()
            if caption:
                result.message(caption)
            result.file_image(local_image)
            yield result
            return

        if normalized in {"状态", "查看", "status"}:
            targets = self._get_proactive_targets()
            interval = self._get_proactive_int("interval_seconds", 3600)
            enabled = self._get_proactive_bool("enabled", False)
            yield event.plain_result(
                f"主动状态图：配置开关={'开' if enabled else '关'}，间隔={interval} 秒，目标会话={len(targets)} 个。"
            )
            return

        yield event.plain_result(
            "状态图用法：/状态图 开启、/状态图 关闭、/状态图 立即、/状态图 状态。"
        )

    async def _generate_image(self, api_key: str, prompt: str) -> str:
        response = await asyncio.to_thread(self._request_image_generation, api_key, prompt)
        return self._image_ref_from_response(
            response, self._get_config_str("output_format", "png")
        )

    async def _generate_selfie(
        self,
        api_key: str,
        event: AstrMessageEvent,
        prompt: str,
        memory_context: str = "",
    ) -> str:
        reference_images = self._get_selfie_reference_images()
        if not reference_images:
            raise RuntimeError(
                "未设置自拍参考照。请先发送一张清晰人像图，然后输入：/自拍参考 设置"
            )

        extra_images = await self._extract_event_image_bytes(event)
        return await self._generate_selfie_with_references(
            api_key,
            prompt,
            reference_images,
            extra_images,
            memory_context=memory_context,
        )

    async def _generate_selfie_with_references(
        self,
        api_key: str,
        prompt: str,
        reference_images: list[bytes],
        extra_images: list[bytes] | None = None,
        memory_context: str = "",
    ) -> str:
        extra_images = extra_images or []
        final_prompt = self._build_selfie_prompt(
            prompt,
            extra_refs=len(extra_images),
            memory_context=memory_context,
        )
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

    async def _proactive_loop(self) -> None:
        initial_delay = max(0, self._get_proactive_int("initial_delay_seconds", 300))
        if initial_delay:
            await asyncio.sleep(initial_delay)

        while True:
            interval = max(60, self._get_proactive_int("interval_seconds", 3600))
            await self._run_proactive_tick()
            await asyncio.sleep(interval)

    async def _run_proactive_tick(self) -> None:
        if not self._proactive_is_active():
            return

        targets = self._get_proactive_targets()
        if not targets:
            return

        max_targets = max(1, self._get_proactive_int("max_targets_per_tick", 5))
        for umo in targets[:max_targets]:
            try:
                await self._send_proactive_status_image(umo)
            except Exception as exc:
                logger.exception("send proactive status image failed: %s", exc)

    async def _send_proactive_status_image(self, umo: str) -> None:
        memory_context = await self._get_memory_context_for_umo(umo)
        proactive_context = self._build_proactive_context(memory_context)
        image_ref = await self._generate_proactive_status_image(
            umo,
            memory_context=memory_context,
            proactive_context=proactive_context,
        )
        local_image = await self._ensure_local_image_ref(image_ref)
        caption = self._build_proactive_caption(
            memory_context,
            proactive_context=proactive_context,
        )
        chain = self._build_active_image_chain(caption, local_image)
        await self.context.send_message(umo, chain)

    async def _generate_proactive_status_image(
        self,
        umo: str = "",
        memory_context: str | None = None,
        proactive_context: dict[str, str] | None = None,
    ) -> str:
        api_key = self._get_config_str("api_key") or os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("api_key 未配置")
        if memory_context is None:
            memory_context = await self._get_memory_context_for_umo(umo)
        if proactive_context is None:
            proactive_context = self._build_proactive_context(memory_context)
        prompt = self._build_proactive_status_prompt(
            memory_context,
            proactive_context=proactive_context,
        )
        reference_images = self._get_selfie_reference_images()
        if (
            reference_images
            and self._get_selfie_bool("enabled", True)
            and self._get_proactive_bool("use_selfie_reference", True)
        ):
            try:
                return await self._generate_selfie_with_references(
                    api_key,
                    prompt,
                    reference_images,
                    memory_context=memory_context,
                )
            except Exception as exc:
                logger.exception("proactive status selfie generation failed: %s", exc)
        return await self._generate_image(api_key, prompt)

    def _build_proactive_status_prompt(
        self,
        memory_context: str = "",
        proactive_context: dict[str, str] | None = None,
    ) -> str:
        if proactive_context is None:
            proactive_context = self._build_proactive_context(memory_context)
        templates = self._get_proactive_value("prompt_templates", [])
        if isinstance(templates, list):
            candidates = [str(item).strip() for item in templates if str(item).strip()]
        else:
            candidates = []

        activity = proactive_context["activity"]
        outfit = proactive_context["outfit"]
        if not candidates:
            candidates = [
                f"真实生活感查岗自拍：本人正在{activity}，像被亲近的人临时问“在干嘛”后随手拍的一张照片，画面自然、不摆拍，手机随拍感，不要文字。",
                f"真实生活感状态照片：本人正在{activity}，镜头里能看到人物和当前环境的小细节，像认真做自己的事时顺手发给恋人看的照片，真实自然，不要文字。",
                f"真实生活感查岗照片：当前状态是{activity}，画面有一点生活痕迹和即时感，像刚停下来随手拍给对方看一眼，温柔自然，不要文字。",
                f"真实生活感自拍：本人正在{activity}，不刻意摆造型，构图像微信里临时发出的近照，真实照片风格，不要文字。",
            ]

        base_prompt = self._render_context_template(
            random.choice(candidates),
            proactive_context,
        )
        base_prompt = (
            f"{base_prompt}\n"
            f"当前日程/状态：{activity}。"
            f"\n当前本地时间：{proactive_context['current_time']}（{proactive_context['time_period']}）。"
            f"\n时间段画面要求：{proactive_context['time_scene']}。"
        )
        if outfit:
            base_prompt = (
                f"{base_prompt}\n"
                f"今日穿搭：{outfit}。今天生成的自拍和状态照都要尽量保持这套穿搭一致，"
                "除非用户明确要求换衣服。"
            )
        if self._get_proactive_bool("person_visible", True):
            base_prompt = (
                f"{base_prompt}\n"
                "查岗重点：画面里必须能看到本人或本人的一部分，"
                "可以是半身、侧脸、手部、肩膀、腿部、镜中倒影或拿手机的手；"
                "不要只拍桌面、物品或空房间。人物穿着日常得体。"
            )
        return f"{base_prompt}\n画面不要出现可读文字、水印、二维码或具体时间数字。"

    def _select_current_activity(self, memory_context: str = "") -> str:
        memory_activity = self._extract_activity_from_memory(memory_context)
        if memory_activity:
            return memory_activity

        configured = self._get_proactive_value("random_activities", [])
        if isinstance(configured, list):
            activities = [str(item).strip() for item in configured if str(item).strip()]
        else:
            activities = []

        if not activities:
            hour = int(time.strftime("%H"))
            if 6 <= hour < 11:
                activities = [
                    "慢慢收拾早晨的东西，准备开始今天",
                    "靠窗喝点东西，顺便看消息",
                    "整理今天要做的事和随身小物",
                ]
            elif 11 <= hour < 14:
                activities = [
                    "准备吃点东西，顺便休息一会儿",
                    "在外面走走，找个地方坐一下",
                    "刚忙完一段事情，准备吃午饭",
                ]
            elif 14 <= hour < 18:
                activities = [
                    "认真处理下午的事情",
                    "在安静的地方看东西或写东西",
                    "出门办点小事，顺手停下来回消息",
                ]
            elif 18 <= hour < 22:
                activities = [
                    "吃完东西后慢慢放松",
                    "在房间里整理小物和今天的东西",
                    "开着暖灯休息，顺便看消息",
                ]
            else:
                activities = [
                    "夜里还没睡，安静地做一点自己的事",
                    "靠在床边或桌前放空一会儿",
                    "在暖灯下收尾今天的小事情",
                ]

        seed = f"{time.strftime('%Y-%m-%d-%H')}:activity"
        return random.Random(seed).choice(activities)

    def _select_today_outfit(self, memory_context: str = "") -> str:
        if not self._get_proactive_bool("daily_outfit_enabled", True):
            return ""

        memory_outfit = self._extract_outfit_from_memory(memory_context)
        if memory_outfit:
            return memory_outfit

        configured = self._get_proactive_value("daily_outfits", [])
        if isinstance(configured, list):
            outfits = [str(item).strip() for item in configured if str(item).strip()]
        else:
            outfits = []

        if not outfits:
            outfits = [
                "柔软浅色针织开衫，里面搭干净的浅色内搭，下身是高腰半裙或宽松长裤，整体温柔日常",
                "短款外套配简单内搭和浅色牛仔下装，头发自然整理，清爽又有生活感",
                "宽松卫衣或针织上衣配休闲短裙/长裤，袜子和鞋子干净，像今天随手穿得很舒服",
                "奶油色或浅粉色上衣配浅蓝牛仔下装，配一个小发夹或简单耳饰，甜一点但不夸张",
                "柔和色系衬衫或开衫，搭配日常通勤感下装，整个人看起来干净、亲近、自然",
            ]

        seed = f"{time.strftime('%Y-%m-%d')}:outfit"
        return random.Random(seed).choice(outfits)

    def _build_proactive_context(self, memory_context: str = "") -> dict[str, str]:
        time_period = self._current_time_period()
        return {
            "activity": self._select_current_activity(memory_context),
            "outfit": self._select_today_outfit(memory_context),
            "current_time": self._current_time_reference(),
            "time_period": time_period,
            "time_scene": self._current_time_scene_instruction(time_period),
        }

    def _current_time_reference(self) -> str:
        timezone = time.strftime("%Z").strip()
        suffix = f" {timezone}" if timezone else ""
        return f"{time.strftime('%Y-%m-%d %H:%M')}{suffix}"

    def _current_time_period(self) -> str:
        hour = int(time.strftime("%H"))
        if 5 <= hour < 8:
            return "清晨"
        if 8 <= hour < 11:
            return "上午"
        if 11 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 22:
            return "晚上"
        return "深夜"

    def _current_time_scene_instruction(self, period: str | None = None) -> str:
        period = period or self._current_time_period()
        instructions = {
            "清晨": "光线偏柔和清透，可以有刚起床、收拾东西或准备出门的生活感",
            "上午": "光线明亮自然，状态像已经开始今天的事情，不要生成夜景或昏暗灯光",
            "中午": "环境和光线要像午间休息或吃饭前后，避免深夜卧室感",
            "下午": "光线和场景要符合下午，适合工作、学习、外出或短暂休息的状态",
            "晚上": "可以有室内暖光、饭后休息或整理东西的氛围，不要像白天强日光",
            "深夜": "光线应偏安静克制，状态像夜里收尾或准备休息，不要生成白天户外强光",
        }
        return instructions.get(period, "画面状态要符合当前本地时间")

    def _render_context_template(self, template: str, context: dict[str, str]) -> str:
        return (
            template.replace("{activity}", context.get("activity", "现在的事"))
            .replace("{outfit}", context.get("outfit") or "今天这身")
            .replace("{time}", context.get("current_time", "现在"))
            .replace("{time_period}", context.get("time_period", "这会儿"))
        )

    def _build_proactive_caption(
        self,
        memory_context: str = "",
        proactive_context: dict[str, str] | None = None,
    ) -> str:
        if proactive_context is None:
            proactive_context = self._build_proactive_context(memory_context)
        templates = self._get_proactive_value("caption_templates", [])
        if isinstance(templates, list):
            candidates = [str(item).strip() for item in templates if str(item).strip()]
        else:
            candidates = []

        fixed_caption = self._get_proactive_str("caption", "")
        if fixed_caption and fixed_caption != OLD_PROACTIVE_CAPTION:
            candidates.append(fixed_caption)

        if not candidates:
            candidates = DEFAULT_PROACTIVE_CAPTIONS

        caption = random.choice(candidates)
        return self._render_context_template(caption, proactive_context)

    async def _get_memory_context_for_umo(self, umo: str) -> str:
        if not umo or self.context is None:
            return ""

        chunks: list[str] = []
        try:
            manager = getattr(self.context, "conversation_manager", None)
            if manager is not None:
                cid = await manager.get_curr_conversation_id(umo)
                if cid:
                    conv = await manager.get_conversation(umo, cid)
                    history = json.loads(getattr(conv, "history", "[]") or "[]")
                    chunks.extend(self._history_records_to_text(history[-16:]))
        except Exception as exc:
            logger.debug("read conversation memory context failed: %s", exc)

        return "\n".join(chunks)[-6000:]

    async def _get_memory_context_for_event(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return await self._get_memory_context_for_umo(umo)

    def _merge_prompt_with_memory(
        self,
        prompt: str,
        current_state: str = "",
        today_outfit: str = "",
        memory_context: str = "",
    ) -> str:
        parts = [prompt.strip()] if prompt.strip() else []
        current_state = str(current_state or "").strip()
        today_outfit = str(today_outfit or "").strip()
        memory_context = str(memory_context or "").strip()
        if current_state:
            parts.append(f"根据记忆理解的当前状态/日程：{current_state}")
        if today_outfit:
            parts.append(f"根据记忆理解的今日穿搭：{today_outfit}")
        if memory_context:
            parts.append(f"相关记忆摘要：{memory_context}")
        return "\n".join(parts).strip()

    def _history_records_to_text(self, records: list[Any]) -> list[str]:
        chunks: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            role = str(record.get("role", "")).strip()
            content = self._content_to_text(record.get("content"))
            if content:
                chunks.append(f"{role}: {content}")
        return chunks

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts).strip()
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            return str(text).strip() if text else ""
        return ""

    def _extract_activity_from_memory(self, memory_context: str) -> str:
        text = self._normalize_memory_text(memory_context)
        if not text:
            return ""

        patterns = [
            r"(?:今天|现在|这会儿|目前|待会儿|等下|下午|晚上|早上|中午)[^。！？\n]{0,20}(?:要|会|准备|正在|打算|计划|安排)[^。！？\n]{1,45}",
            r"(?:日程|安排|计划|行程|状态)[：:，, ]+([^。！？\n]{2,50})",
            r"(?:在|去|准备去)([^。！？\n]{1,30}(?:上课|上班|看书|学习|工作|吃饭|逛街|散步|买东西|咖啡店|图书馆|学校|公司|房间|卧室|客厅))",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1) if match.lastindex else match.group(0)
                return self._clean_memory_fragment(value)
        return ""

    def _extract_outfit_from_memory(self, memory_context: str) -> str:
        text = self._normalize_memory_text(memory_context)
        if not text:
            return ""

        patterns = [
            r"(?:今天|今日|这身|现在)[^。！？\n]{0,12}(?:穿搭|穿的是|穿了|衣服|搭配)[：:，, ]*([^。！？\n]{2,80})",
            r"(?:穿搭|衣服|搭配)[：:，, ]+([^。！？\n]{2,80})",
            r"((?:米白|奶白|浅粉|粉色|浅蓝|黑色|白色|针织|开衫|吊带|裙|牛仔|衬衫|卫衣|短袜|玛丽珍)[^。！？\n]{2,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return self._clean_memory_fragment(match.group(1))
        return ""

    def _normalize_memory_text(self, memory_context: str) -> str:
        lines = []
        for line in memory_context.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(
                marker in stripped
                for marker in (
                    "记忆",
                    "事实",
                    "偏好",
                    "日程",
                    "安排",
                    "穿搭",
                    "衣服",
                    "搭配",
                    "今天",
                    "现在",
                    "状态",
                )
            ):
                lines.append(stripped)
        return "\n".join(lines[-80:])

    def _clean_memory_fragment(self, value: str) -> str:
        value = re.sub(r"^(user|assistant|system)\s*:\s*", "", value.strip(), flags=re.I)
        value = re.sub(r"[，,。！？!?\s]+$", "", value)
        return value[:90]

    async def _ensure_local_image_ref(self, image_ref: str) -> str:
        if not image_ref.startswith(("http://", "https://")):
            return image_ref
        image_bytes = await asyncio.to_thread(self._download_image_bytes, image_ref)
        ext = self._guess_image_ext(image_bytes)
        path = self.output_dir / f"proactive_{uuid.uuid4().hex}.{ext}"
        path.write_bytes(image_bytes)
        return str(path)

    def _build_active_image_chain(self, caption: str, image_path: str) -> Any:
        if MessageChain is None:
            raise RuntimeError("当前 AstrBot 环境不支持 MessageChain 主动发送")

        chain = MessageChain()
        if caption:
            chain.message(caption)
        chain.file_image(image_path)
        return chain

    def _voice_result(self, event: AstrMessageEvent, audio_path: str, text: str = "") -> Any:
        if self._should_send_voice_as_file(event):
            return self._voice_file_fallback_result(event, audio_path, text)

        if Record is None:
            return self._voice_file_fallback_result(event, audio_path, text)

        result = event.make_result()
        result.chain.append(Record.fromFileSystem(audio_path, text=text))
        return result

    def _voice_file_fallback_result(
        self, event: AstrMessageEvent, audio_path: str, text: str = ""
    ) -> Any:
        filename = Path(audio_path).name
        message = text or "语音合成好了。"

        if File is not None and self._get_tts_bool("send_file_fallback", True):
            result = event.make_result()
            result.message(message)
            result.chain.append(File(name=filename, file=audio_path))
            return result

        return event.plain_result(f"{message}\n语音文件：{audio_path}")

    def _should_send_voice_as_file(self, event: AstrMessageEvent) -> bool:
        mode = self._get_tts_str("send_mode", "auto").lower()
        if mode in {"record", "voice", "语音"}:
            return False
        if mode in {"file", "文件", "text", "文本"}:
            return True

        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "").lower()
        except Exception:
            platform_id = ""

        umo = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return "weixin_oc" in platform_id or "weixin_oc" in umo or "weixin_personal" in umo

    async def _generate_tts_audio(self, text: str) -> str:
        if not self._get_tts_bool("enabled", True):
            raise RuntimeError("语音合成功能暂时关着。")

        app_id = self._get_tts_str("app_id") or os.getenv(
            "VOLC_TTS_APP_ID", DEFAULT_VOLC_TTS_APP_ID
        )
        access_token = self._get_tts_str("access_token") or os.getenv(
            "VOLC_TTS_ACCESS_TOKEN", ""
        )
        if not app_id or not access_token:
            raise RuntimeError("我这边还没配好语音接口，等填好火山 App ID 和 Token 再说给你听。")

        text = self._prepare_tts_text(text)
        try:
            response = await asyncio.to_thread(
                self._request_volc_tts,
                app_id,
                access_token,
                text,
            )
            audio_bytes = self._audio_bytes_from_tts_response(response)
        except Exception as exc:
            logger.exception("volc tts generation failed")
            raise RuntimeError(self._friendly_tts_error(exc)) from exc

        encoding = self._get_tts_str("encoding", "mp3").lower() or "mp3"
        ext = "wav" if encoding == "wav" else "mp3"
        output_path = self.voice_dir / f"{uuid.uuid4().hex}.{ext}"
        output_path.write_bytes(audio_bytes)
        return str(output_path)

    def _request_volc_tts(
        self,
        app_id: str,
        access_token: str,
        text: str,
    ) -> dict[str, Any]:
        api_version = self._get_tts_str("api_version", "v3").lower()
        if api_version in {"v3", "3", "http_v3", "unidirectional"}:
            return self._request_volc_tts_v3(app_id, access_token, text)
        return self._request_volc_tts_v1(app_id, access_token, text)

    def _request_volc_tts_v1(
        self,
        app_id: str,
        access_token: str,
        text: str,
    ) -> dict[str, Any]:
        base_url = self._get_tts_str("base_url", DEFAULT_VOLC_TTS_BASE_URL).rstrip("/")
        endpoint = self._get_tts_str("endpoint", DEFAULT_VOLC_TTS_ENDPOINT)
        url = self._build_api_url(base_url, endpoint)

        encoding = self._get_tts_str("encoding", "mp3").lower() or "mp3"
        payload = {
            "app": {
                "appid": app_id,
                "token": access_token,
                "cluster": self._get_tts_str("cluster", DEFAULT_VOLC_TTS_CLUSTER),
            },
            "user": {
                "uid": self._get_tts_str("uid", "astrbot_gpt_img_2")
            },
            "audio": {
                "voice_type": self._get_tts_str(
                    "voice_type", DEFAULT_VOLC_TTS_VOICE_TYPE
                ),
                "encoding": encoding,
                "speed_ratio": self._get_tts_float("speed_ratio", 1.0),
                "volume_ratio": self._get_tts_float("volume_ratio", 1.0),
                "pitch_ratio": self._get_tts_float("pitch_ratio", 1.0),
            },
            "request": {
                "reqid": uuid.uuid4().hex,
                "text": text,
                "text_type": "plain",
                "operation": "query",
            },
        }

        extra_body = self._get_tts_value("extra_body", {})
        if isinstance(extra_body, dict):
            self._deep_update(payload, extra_body)

        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer;{access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        timeout = self._get_tts_int("timeout", self._get_config_int("timeout", 120))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_api_error(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接语音接口：{exc.reason}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("语音接口返回了无法解析的 JSON") from exc

        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("语音接口返回格式不正确")

    def _request_volc_tts_v3(
        self,
        app_id: str,
        access_token: str,
        text: str,
    ) -> dict[str, Any]:
        base_url = self._get_tts_str("base_url", DEFAULT_VOLC_TTS_BASE_URL).rstrip("/")
        endpoint = self._get_tts_str("v3_endpoint", DEFAULT_VOLC_TTS_V3_ENDPOINT)
        url = self._build_api_url(base_url, endpoint)
        encoding = self._get_tts_str("encoding", "mp3").lower() or "mp3"
        voice_type = self._get_tts_str("voice_type", DEFAULT_VOLC_TTS_VOICE_TYPE)

        payload = {
            "req_params": {
                "text": text,
                "speaker": voice_type,
                "audio_params": {
                    "format": encoding,
                    "sample_rate": self._get_tts_int("sample_rate", 24000),
                },
            }
        }

        extra_body = self._get_tts_value("extra_body", {})
        if isinstance(extra_body, dict):
            self._deep_update(payload, extra_body)

        resource_id = self._get_tts_str("resource_id", DEFAULT_VOLC_TTS_RESOURCE_ID)
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "X-Api-App-Id": app_id,
                "X-Api-Access-Key": access_token,
                "X-Api-Resource-Id": resource_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        timeout = self._get_tts_int("timeout", self._get_config_int("timeout", 120))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(self._format_api_error(exc.code, body)) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接语音接口：{exc.reason}") from exc

        return self._parse_volc_tts_v3_body(body)

    def _parse_volc_tts_v3_body(self, body: str) -> dict[str, Any]:
        audio_parts: list[bytes] = []
        last_message = ""

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue

            code = parsed.get("code")
            if code not in (None, 0, 3000):
                last_message = parsed.get("message") or parsed.get("msg") or str(parsed)
                continue

            data = parsed.get("data")
            if isinstance(data, str) and data:
                audio_parts.append(base64.b64decode(data))
            elif isinstance(data, dict):
                audio = data.get("audio") or data.get("data")
                if isinstance(audio, str) and audio:
                    audio_parts.append(base64.b64decode(audio))

        if audio_parts:
            return {"code": 3000, "data": base64.b64encode(b"".join(audio_parts)).decode()}

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            if last_message:
                raise RuntimeError(f"语音接口返回错误：{last_message}") from exc
            raise RuntimeError("语音接口没有返回音频数据") from exc
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("语音接口返回格式不正确")

    def _audio_bytes_from_tts_response(self, response: dict[str, Any]) -> bytes:
        code = response.get("code")
        if code not in (None, 0, 3000):
            message = response.get("message") or response.get("msg") or str(response)
            raise RuntimeError(f"语音接口返回错误：{message}")

        audio_data = (
            response.get("data")
            or response.get("audio")
            or response.get("result", {}).get("data")
        )
        if not audio_data:
            raise RuntimeError("语音接口没有返回音频数据")
        if isinstance(audio_data, str):
            return base64.b64decode(audio_data)
        raise RuntimeError("语音接口音频数据格式不正确")

    def _prepare_tts_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        max_chars = self._get_tts_int("max_chars", 300)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
        return text

    def _friendly_tts_error(self, exc: Exception) -> str:
        text = str(exc)
        lowered = text.lower()
        if "api" in lowered or "token" in lowered or "authorization" in lowered or "401" in text:
            return "我这边语音接口还没连好，等钥匙配对了再说给你听。"
        if "3031" in text or "Init Engine Instance failed" in text:
            return "这次没说出来，音色和接口像是没对上。我换一条新的语音通道再试。"
        if "timeout" in lowered or "超时" in text:
            return "刚刚这句话没说出来，像是卡了一下。等会儿我再试试。"
        if "HTTP 500" in text or "HTTP 502" in text or "HTTP 503" in text:
            return "这次语音没合出来，接口那边有点不稳。"
        return "这次没说出来，我等会儿换个方式再试。"

    def _deep_update(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[str(key)] = value

    def _ensure_proactive_task(self) -> None:
        if self._proactive_task is None or self._proactive_task.done():
            self._proactive_task = asyncio.create_task(self._proactive_loop())

    def _proactive_is_active(self) -> bool:
        return self._get_proactive_bool("enabled", False) or bool(
            self._load_saved_proactive_targets()
        )

    def _get_proactive_targets(self) -> list[str]:
        targets: list[str] = []
        configured = self._get_proactive_value("targets", [])
        if isinstance(configured, list):
            targets.extend(str(item).strip() for item in configured if str(item).strip())
        targets.extend(self._load_saved_proactive_targets())
        return list(dict.fromkeys(targets))

    def _load_saved_proactive_targets(self) -> list[str]:
        if not self.proactive_targets_path.exists():
            return []
        try:
            data = json.loads(self.proactive_targets_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def _save_proactive_targets(self, targets: list[str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        unique_targets = list(dict.fromkeys(str(item).strip() for item in targets if str(item).strip()))
        self.proactive_targets_path.write_text(
            json.dumps(unique_targets, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

    def _build_natural_image_prompt(self, message: str) -> str:
        if not self._get_config_bool("natural_image_enabled", True):
            return ""

        text = self._normalize_chat_request_text(message)
        if not text:
            return ""

        trigger_patterns = (
            r"(?:帮我|给我|替我|麻烦你)?(?:生成|画|绘制|做|整)(?:一张|一个|张|个)?(?P<prompt>.+)",
            r"(?:帮我|给我|替我|麻烦你)?(?:来一张|来张|整一张|整张)(?P<prompt>.+)",
            r"(?:我想要|想要|我要)(?:一张|一个|张|个)?(?P<prompt>.+)",
            r"(?:我想看|想看|想看看)(?P<prompt>.+)",
        )

        for pattern in trigger_patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            prompt = self._clean_natural_image_prompt(match.group("prompt"))
            if self._is_valid_natural_image_prompt(prompt):
                return prompt
        return ""

    def _normalize_chat_request_text(self, message: str) -> str:
        text = re.sub(r"\s+", "", message.strip())
        text = re.sub(r"^(宝宝|宝贝|亲爱的|老婆|老公|bot|机器人)[，,。.!！?？]*", "", text)
        return text

    def _clean_natural_image_prompt(self, value: str) -> str:
        text = value.strip()
        text = re.sub(r"^(一张|一个|张|个|幅|一幅)", "", text)
        text = re.sub(r"(好吗|好不好|可以吗|行吗|可不可以|吗|嘛|吧|呗|呀|啦|呢)[。.!！?？]*$", "", text)
        text = re.sub(r"(的)?(图片|图像|图|照片|壁纸|头像|海报)$", "", text)
        text = text.strip(" ，,。.!！?？：:")
        return text

    def _is_valid_natural_image_prompt(self, prompt: str) -> bool:
        if not prompt:
            return False
        if len(prompt) > 160:
            return False
        if prompt in {"你", "你自己", "你的", "照片", "图片", "图"}:
            return False
        if any(marker in prompt for marker in ("看看你", "你的照片", "你的自拍", "你长什么样")):
            return False
        return True

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

    def _build_selfie_prompt(
        self, prompt: str, extra_refs: int, memory_context: str = ""
    ) -> str:
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
        activity = self._extract_activity_from_memory(memory_context)
        if activity:
            user_prompt = f"{user_prompt}\n根据记忆理解的当前状态/日程：{activity}。"
        time_period = self._current_time_period()
        user_prompt = (
            f"{user_prompt}\n"
            f"当前本地时间：{self._current_time_reference()}（{time_period}）。"
            f"自拍的光线、环境和状态要符合这个时间段：{self._current_time_scene_instruction(time_period)}。"
            "不要在画面里出现可读时间、文字、水印或二维码。"
        )
        outfit = self._select_today_outfit(memory_context)
        if outfit:
            user_prompt = (
                f"{user_prompt}\n"
                f"今日穿搭：{outfit}。今天的自拍、查岗照和状态照都尽量保持这套穿搭一致，"
                "除非用户明确要求换衣服或换风格。"
            )
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

    def _extract_named_command_prompt(self, event: AstrMessageEvent, command: str) -> str:
        message = event.message_str.strip()
        for prefix in (f"/{command}", command):
            if message.startswith(prefix):
                return message[len(prefix) :].strip()
        if message.startswith("/"):
            parts = message.split(maxsplit=1)
            if len(parts) == 2:
                return parts[1].strip()
            return ""
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

    def _get_config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
        return bool(value)

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

    def _get_proactive_config(self) -> dict[str, Any]:
        value = self.config.get("proactive", {})
        return value if isinstance(value, dict) else {}

    def _get_proactive_value(self, key: str, default: Any = None) -> Any:
        return self._get_proactive_config().get(key, default)

    def _get_proactive_str(self, key: str, default: str = "") -> str:
        value = self._get_proactive_value(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_proactive_int(self, key: str, default: int) -> int:
        value = self._get_proactive_value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_proactive_bool(self, key: str, default: bool) -> bool:
        value = self._get_proactive_value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
        return bool(value)

    def _get_tts_config(self) -> dict[str, Any]:
        value = self.config.get("tts", {})
        return value if isinstance(value, dict) else {}

    def _get_tts_value(self, key: str, default: Any = None) -> Any:
        return self._get_tts_config().get(key, default)

    def _get_tts_str(self, key: str, default: str = "") -> str:
        value = self._get_tts_value(key, default)
        if value is None:
            return default
        return str(value).strip()

    def _get_tts_int(self, key: str, default: int) -> int:
        value = self._get_tts_value(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_tts_float(self, key: str, default: float) -> float:
        value = self._get_tts_value(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _get_tts_bool(self, key: str, default: bool) -> bool:
        value = self._get_tts_value(key, default)
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
        if self._proactive_task is not None:
            self._proactive_task.cancel()
            try:
                await self._proactive_task
            except asyncio.CancelledError:
                pass
