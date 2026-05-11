# gpt-img-2 调用

AstrBot 图片生成插件。用户发送关键词和图片描述后，插件会调用 OpenAI 兼容的图片生成接口，并把生成结果以图片消息发回。

## 触发方式

- `/gptimg 一张电影感的雪山日出`
- `/画图 赛博朋克城市夜景`
- `/绘图 水彩风格的山间小屋`
- `/生成图片 极简产品海报`
- `/gpt-img-2 一只穿宇航服的猫`
- `画图 一张电影感的雪山日出`
- `绘图西瓜`
- `gptimg 一只穿宇航服的猫`
- `宝宝帮我生成一个无畏契约的图片好吗`
- `宝宝我想看烟花`
- `宝宝我要一张赛博朋克城市夜景图`
- `发送图片 + /自拍参考 设置`
- `/自拍参考 查看`
- `/自拍 日常自拍照，窗边自然光，微笑`
- `/状态图 开启`
- `/状态图 立即`
- `宝宝让我看看你` -> 机器人追问 `要看照片吗？` -> 回复 `要看`
- `看看今天的穿搭` -> 机器人追问确认后生成穿搭自拍

## 配置

在 AstrBot 插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| `api_key` | api.xbyjs.top 的 API Key。也可以设置环境变量 `OPENAI_API_KEY`，不要提交到 git。 |
| `base_url` | API 基础地址，默认 `https://api.xbyjs.top`。 |
| `endpoint` | 图片生成接口路径，默认 `/v1/images/generations`。 |
| `model` | 图片模型，默认 `gpt-image-2`；可选 `gpt-image-2-2k`、`gpt-image-2-4k`。 |
| `size` | 图片尺寸，默认 `auto`，按提示词自动选择方图、竖图或横图。 |
| `quality` | 图片质量，默认 `auto`。 |
| `output_format` | 输出格式，默认 `png`。 |
| `keywords` | 免斜杠关键词列表，默认 `画图`、`绘图`、`生成图片`、`gptimg`、`gpt-img-2`。 |
| `natural_image_enabled` | 是否启用自然语言文生图触发，默认开启。 |
| `extra_body` | 额外请求体参数，用于兼容不同服务商。 |
| `proactive.enabled` | 是否启用主动状态图后台任务，默认关闭。 |
| `proactive.interval_seconds` | 主动状态图发送间隔，默认 `3600` 秒。 |
| `proactive.initial_delay_seconds` | 插件启动后多久开始第一次后台检查，默认 `300` 秒。 |
| `proactive.targets` | 固定目标会话列表；也可在聊天里用 `/状态图 开启` 自动保存。 |
| `proactive.caption` | 主动状态图附带文字。 |
| `proactive.prompt_templates` | 状态图提示词模板列表，留空使用内置生活感报备模板。 |
| `selfie.enabled` | 是否启用自拍参考照模式，默认开启。 |
| `selfie.reference_images` | WebUI 上传的自拍参考图；不填时可用命令保存。 |
| `selfie.edit_endpoint` | 自拍使用的图片编辑接口，默认 `/v1/images/edits`。 |
| `selfie.model` | 自拍模型，默认 `gpt-image-2`。 |
| `selfie.max_reference_images` | 最多提交参考图数量，默认 `2`。 |
| `selfie.recent_image_ttl_seconds` | 先发图片再设置参考照时，最近图片缓存多久，默认 `600` 秒。 |
| `selfie.natural_confirm_enabled` | 是否启用自然语言待确认意图，默认开启。 |
| `selfie.natural_confirm_mode` | 自然语言确认模式，默认 `passive`：插件只记录意图并放行给主 Agent 自然回复；`plugin`：插件直接发送确认句。 |
| `selfie.confirm_ttl_seconds` | 等待用户确认的时间，默认 `180` 秒。 |
| `selfie.identity_strength` | 身份参考强度，默认 `balanced`；如果结果太像参考图，可改成 `loose`。 |
| `selfie.variation_instruction` | 变化约束，默认要求换背景、姿势、构图和镜头距离。 |

## 自拍参考照

1. 发送一张清晰人像图，并在同一条消息或随后输入：

```text
/自拍参考 设置
```

2. 生成自拍：

```text
/自拍 日常自拍照，窗边自然光，微笑
/自拍 黑色外套，楼梯间，低头看镜头
```

也可以自然表达：

```text
宝宝让我看看你
拍张今天照片给我看看
看看今天的穿搭
```

默认 `selfie.natural_confirm_mode=passive` 时，插件会记录“用户可能想看照片/穿搭”的意图，然后放行给主 Agent，让它结合记忆和人设自然回复。用户在有效时间内回复 `要看`、`看看`、`来一张`、`好`、`OK` 等确认词后，插件再生成自拍图。

### LLM Tool

插件提供 `gpt_img_2_generate` 给主 Agent 调用。主 Agent 可以结合当前聊天、记忆和用户偏好判断：

- 用户只是含蓄地想看 Bot 照片或穿搭：可以直接自然追问；如果需要插件代发确认句，调用 `mode=ask_selfie` 或 `ask_first=true`。
- 用户已经确认要看：调用 `mode=selfie`，直接生成自拍参考图。
- 用户只是要画普通图片：调用 `mode=text`。

这样用户不需要主动输入 `/自拍` 或 `/gptimg`，自然聊天里也能触发插件能力。

3. 查看或删除参考照：

```text
/自拍参考 查看
/自拍参考 删除
```

说明：2

- WebUI 的 `selfie.reference_images` 优先于命令保存的参考照。
- 可以先单独发送图片，再在缓存时间内输入 `/自拍参考 设置`。
- 第一张参考图只用于人物身份、五官、脸型和气质参考，不应该复刻参考图的尺寸、构图、姿势、背景、光线或拍摄角度。
- 当前 `/自拍` 消息里附带的图片只作为额外服装、姿势、构图或场景的松散灵感，默认也不会照搬。
- 自拍模式调用 OpenAI 兼容 `images/edits` 接口。如果你的网关不支持该接口，会返回接口错误。
- `size=auto` 时，自拍/穿搭/人物默认走竖图，风景/街景/宽屏默认走横图，其它内容走方图。

## 主动状态图

1. 在插件配置里打开：

```text
proactive.enabled = true
```

2. 在要接收状态图的聊天里发送：

```text
/状态图 开启
```

3. 常用命令：

```text
/状态图 立即
/状态图 状态
/状态图 关闭
```

说明：

- 主动发送需要 AstrBot 的 `unified_msg_origin`，所以目标聊天至少要执行一次 `/状态图 开启`。
- 默认每 1 小时生成一张“正在做什么/生活状态报备”的图片。
- `prompt_templates` 可自定义，比如“正在看书”“在桌前工作”“夜里整理东西”等不同状态。

## 说明

- 插件不引入额外 Python 依赖，只使用标准库发起 HTTP 请求。
- 接口返回 `b64_json` 时，图片会优先保存到 AstrBot 的 `data/plugin_data/astrbot_plugin_gpt_img_2/generated/` 再发送；旧版本环境取不到数据目录时，会回退到插件目录下的 `generated/`。
- 接口返回 `url` 时，插件会直接发送该图片 URL。
