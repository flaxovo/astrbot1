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

## 配置

在 AstrBot 插件配置页填写：

| 配置项 | 说明 |
| --- | --- |
| `api_key` | api.xbyjs.top 的 API Key。也可以设置环境变量 `OPENAI_API_KEY`，不要提交到 git。 |
| `base_url` | API 基础地址，默认 `https://api.xbyjs.top`。 |
| `endpoint` | 图片生成接口路径，默认 `/v1/images/generations`。 |
| `model` | 图片模型，默认 `gpt-image-2`；可选 `gpt-image-2-2k`、`gpt-image-2-4k`。 |
| `size` | 图片尺寸，默认 `1024x1024`。 |
| `quality` | 图片质量，默认 `auto`。 |
| `output_format` | 输出格式，默认 `png`。 |
| `keywords` | 免斜杠关键词列表，默认 `画图`、`绘图`、`生成图片`、`gptimg`、`gpt-img-2`。 |
| `extra_body` | 额外请求体参数，用于兼容不同服务商。 |

## 说明

- 插件不引入额外 Python 依赖，只使用标准库发起 HTTP 请求。
- 接口返回 `b64_json` 时，图片会优先保存到 AstrBot 的 `data/plugin_data/astrbot_plugin_gpt_img_2/generated/` 再发送；旧版本环境取不到数据目录时，会回退到插件目录下的 `generated/`。
- 接口返回 `url` 时，插件会直接发送该图片 URL。
