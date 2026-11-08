# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**一个功能完整的 OpenAI 兼容 API 服务，为 Flow 提供统一的接口**

</div>

## ❤️赞助商

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://useapi.net/docs/articles/google-flow-api-zh?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">
    <img src="https://useapi.net/assets/images/sponsor/useapi-banner.png" alt="useapi.net" width="150">
  </a>
</td>
<td valign="middle">
  感谢 <strong>useapi.net</strong> 赞助了本项目！<strong>一个订阅，全部 API</strong>：useapi.net 为你自己的 Google 账号提供托管 API：<strong>Google Flow</strong>（Veo 3.1、带原生音频的 <strong>Omni 1.2 Flash</strong>，3 到 15 秒，可带音色延长；Nano Banana Pro）、<strong>Google Vids</strong>（Omni 1.2 Flash 15 秒视频，一张照片生成 30 秒会说话的数字人视频，文字转语音，<strong>无需验证码</strong>，额度与 Flow 分开，同一账号同时使用 Flow 和 Vids，Omni 产能约翻倍）、<strong>Google Flow Music</strong>（Lyria 3.5 整首歌曲）和 <strong>Gemini Notebook</strong>（NotebookLM），还有 <strong>Meta Muse</strong>（muse.ai，免费账号可用）。无需自己部署。同一订阅还包含 Dreamina、可灵 Kling、PixVerse、海螺 MiniMax、Mureka、Runway 等 API，每月固定 $15。<a href="https://useapi.net/docs/articles/gemini-omni-1-2-flash-api-zh?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Omni 1.2 中文教程</a> · <a href="https://useapi.net/docs/articles/google-flow-api-zh?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Flow 中文教程</a> · <a href="https://useapi.net/docs/articles/google-vids-api-zh?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Vids 中文教程</a> · <a href="https://buy.stripe.com/8x2aEX4Bd8Vh9PMg4qeUU03?client_reference_id=flow2api">订阅</a><br><br>
  Thanks to <strong>useapi.net</strong> for sponsoring this project! <strong>One subscription, every API</strong>: useapi.net runs hosted APIs on your own Google account: <strong>Google Flow</strong> (Veo 3.1, audio-native <strong>Omni 1.2 Flash</strong> at 3 to 15 seconds with extend and voices, Nano Banana Pro), <strong>Google Vids</strong> (Omni 1.2 Flash 15-second videos, 30-second talking avatar videos from a photo, text to speech, <strong>no captcha</strong>, an allowance separate from Flow, so using both on one account roughly doubles your Omni capacity), <strong>Google Flow Music</strong> (full songs with Lyria 3.5), <strong>Gemini Notebook</strong> (NotebookLM) and <strong>Meta Muse</strong> (muse.ai, free plan included). Nothing to self-host. The same flat $15/month also covers Dreamina, Kling, PixVerse, MiniMax, Mureka, Runway and more. <a href="https://useapi.net/docs/articles/gemini-omni-1-2-flash-api?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Omni 1.2 guide</a> · <a href="https://useapi.net/docs/api-google-flow-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Flow docs</a> · <a href="https://useapi.net/docs/api-google-vids-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor">Vids docs</a> · <a href="https://buy.stripe.com/8x2aEX4Bd8Vh9PMg4qeUU03?client_reference_id=flow2api">Subscribe</a>
</td>
</tr>
</table>

<table>
<tr>
<td width="180" align="center" valign="middle">
  <a href="https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme">
    <img src="static/sponsors/proxylane-flow2api.png" alt="ProxyLane" width="150">
  </a>
</td>
<td valign="top">
  感谢 <strong>ProxyLane</strong> 赞助了本项目！ProxyLane 是住宅代理服务，覆盖 195 个国家，95%+ IP 获 Scamalytics 低风险评级，支持 HTTP / SOCKS5 和账号密码认证。在 Token 的「协议代理」「打码代理」或全局「请求代理地址」里填入代理，用户名中写上城市和会话名，每个 Token 最长 72 小时使用同一个住宅 IP，换 IP 也留在同一城市。中文界面，支持 USDT 付款，首次购买使用优惠码 <strong>FLOW2API30</strong> 享 7 折，通过<a href="https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme">此链接</a>注册即可。<br><br>
  Thanks to <strong>ProxyLane</strong> for sponsoring this project! ProxyLane is a residential proxy service in 195 countries, with 95%+ of IPs rated low risk by Scamalytics and HTTP / SOCKS5 with username and password. Put the proxy into a Token's protocol or captcha proxy, or into the global request proxy, with a city and a session name in the username: each Token keeps one residential IP for up to 72 hours, and a replacement IP stays in the same city. New customers get 30% off the first purchase with code <strong>FLOW2API30</strong>. <a href="https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme_en">Sign up here</a> to get started.
</td>
</tr>
</table>

## ✨ 核心特性

- 🎨 **文生图** / **图生图**
- 🎬 **文生视频** / **图生视频**
- 🎞️ **首尾帧视频**
- 🔄 **Cookie 自动刷新** - 定时刷新完整 Google/Flow Cookie 并验证登录态，支持 browser/personal 浏览器兜底；登录失效后需重新同步 Cookie
- 📊 **余额显示** - 实时查询和显示 VideoFX Credits
- 🚀 **负载均衡** - 多 Token 轮询和并发控制
- 🌐 **代理支持** - 支持 HTTP/SOCKS5 代理
- 📱 **Web 管理界面** - 直观的 Token 和配置管理
- 🎨 **图片生成连续对话**
- 🧩 **Gemini 官方请求体兼容** - 支持 `generateContent` / `streamGenerateContent`、`systemInstruction`、`contents.parts.text/inlineData/fileData`
- ✅ **Gemini 官方格式已实测出图** - 已使用真实 Token 验证 `/models/{model}:generateContent` 可正常返回官方 `candidates[].content.parts[].inlineData`

## 🚀 快速开始

### 前置要求

- Docker 和 Docker Compose（推荐）
- 或 Python 3.8+

- 由于 Flow 增加了额外的验证码，你可以自行选择使用浏览器打码或第三方打码：
  - 注册 [YesCaptcha](https://yescaptcha.com/i/13Xd8K) 并获取 API Key，填入系统配置页面的 `YesCaptcha API 密钥`。
  - 注册 [CapSolver](https://dashboard.capsolver.com/passport/register?inviteCode=z95yVf1HvixI) 并获取 API Key，填入系统配置页面的 `CapSolver API 密钥`。
  - 注册 [Captcha.run](https://captcha.run/sso?inviter=36179602-dbd1-4227-8ec8-98f8c0170c59) 并获取 API Key，填入系统配置页面的 `Captcha.run API 密钥`。
  - 注册 [2Captcha](https://2captcha.com/auth/register/?from=24953866) 并获取 API Key，填入系统配置页面的 `2Captcha API 密钥`。
- YesCaptcha 支持在管理页切换 `type`：`RecaptchaV3TaskProxyless`、`RecaptchaV3TaskProxylessM1`、`RecaptchaV3TaskProxylessM1S7`、`RecaptchaV3TaskProxylessM1S9`；当前默认推荐 `M1S9`，S7/S9 会强制提交 `minScore` 0.7/0.9。
- 默认 `docker-compose.yml` 建议搭配第三方打码（yescaptcha/captcharun/twocaptcha/capmonster/ezcaptcha/capsolver）。
如需 Docker 内有头打码（browser/personal），请使用下方 `docker-compose.headed.yml`。

- 自动推送新版 Flow 登录态的浏览器扩展：[Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)。

### 方式一：Docker 部署（推荐）

#### 标准模式（不使用代理）

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

> 说明：Compose 已默认挂载 `./tmp:/app/tmp`。如果把缓存超时设为 `0`，语义是“不自动过期删除”；若希望容器重建后仍保留缓存文件，也需要保留这个 `tmp` 挂载。

#### WARP 模式（使用代理）

```bash
# 使用 WARP 代理启动
docker-compose -f docker-compose.proxy.yml up -d

# 查看日志
docker-compose -f docker-compose.proxy.yml logs -f
```

#### Docker 有头打码模式（browser / personal）

> 适用于你有虚拟化桌面需求、希望在容器里启用有头浏览器打码的场景。  
> 该模式默认启动 `Xvfb + Fluxbox` 实现容器内部可视化，并设置 `ALLOW_DOCKER_HEADED_CAPTCHA=true`。  
> 仅开放应用端口，不提供任何远程桌面连接端口。
> `personal` 内置浏览器现在默认按有头模式启动；如需临时切回无头，可额外设置环境变量 `PERSONAL_BROWSER_HEADLESS=true`。

```bash
# 启动有头模式（首次建议带 --build）
docker compose -f docker-compose.headed.yml up -d --build

# 查看日志
docker compose -f docker-compose.headed.yml logs -f
```

- API 端口：`8000`
- 进入管理后台后，将验证码方式设为 `browser` 或 `personal`

### 方式二：本地部署

```bash
# 克隆项目
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

### 首次访问

服务启动后,访问管理后台: **http://localhost:8000**,首次登录后请立即修改密码!

- **用户名**: `admin`
- **密码**: `admin`

## 📈 监控接口

- `GET /health`：公开健康检查，返回服务是否存活、活跃 Token 数、即将过期 Token 数、已过期 Token 数、429 禁用数等摘要
- `GET /metrics`：Prometheus 指标接口
- `GET /api/tokens`：管理接口，返回 `at_expires`、`at_expired`、`at_expiring_within_1h`、`ban_reason`、`consecutive_error_count` 等 Token 状态

Prometheus 可直接抓 `/metrics`。如果部署到 Kubernetes，建议只在集群内抓取，并在 Ingress/Gateway 层单独限制 `/metrics` 的外部访问。

### 模型测试页面

访问 **http://localhost:8000/test** 可打开内置的模型测试页面，支持：

- 按分类浏览所有可用模型（图片生成、文/图生视频、多图视频、视频放大等）
- 输入提示词一键测试，流式显示生成进度
- 图生图 / 图生视频场景支持上传图片
- 生成完成后直接预览图片或视频

## 🌐 界面语言 / Interface language

登录页、管理后台和模型测试页均支持 **简体中文 / English**。使用页面右上角的语言选择器切换；首次访问会匹配浏览器语言，手动选择后保存在当前浏览器，跨页面及重新访问时继续生效。切换语言不会刷新页面，也不会清空表单、上传图片或中断生成任务。

The sign-in page, admin console, and model tester support **Simplified Chinese and English**. Use the language selector in the top-right corner. The initial language follows your browser, and your selection is saved locally. Switching languages preserves form inputs, uploaded images, and active generation tasks.

浏览器验证码扩展的设置页也提供中英文切换，偏好保存在扩展本地存储中，切换时保留未保存的配置。The captcha extension's settings page also supports both languages and stores its preference separately, preserving unsaved settings when switching.

管理接口的提示和错误支持 `Accept-Language: zh-CN` / `Accept-Language: en`，并返回 `Content-Language`。未支持的语言回退至简体中文。账号信息、提示词、生成内容、日志原文和未知的上游错误保持原样；生成结果及 SSE 流不会经过翻译。

API messages and errors honor `Accept-Language: zh-CN` or `Accept-Language: en` and return `Content-Language`. Unsupported languages fall back to Simplified Chinese. Account data, prompts, generated content, raw logs, and unknown upstream diagnostics remain unchanged. Generation results and SSE streams are passed through unchanged.

翻译词典位于 `static/locales/`：`common.json` 为共享文案，`login.json` / `manage.json` / `test.json` 对应三个页面，`backend.json` 对应 API 提示。每个词典包含 `zh-CN` 和 `en`，使用原文作为消息键；新增译文时需保留 `{0}` 等占位符。

Translation catalogs live in `static/locales/`. Each catalog contains `zh-CN` and `en` entries keyed by the source message. Keep interpolation placeholders such as `{0}` unchanged when editing translations.

扩展的词典位于 `extension/locales.json`。The extension catalog is `extension/locales.json`.

## 📋 支持的模型

### 图片生成

当前 `/v1/models` 与 Gemini `/models` 公开 Nano Banana 2.1 和 Nano Banana 2 Lite 两个图片系列，共 30 个比例与分辨率变体。已有 `gemini-3.1-flash-image-*` 名称继续保留，调用新版 Nano Banana 2.1（`BELUGA`）。旧 Pro 名称 `gemini-3.0-pro-image-*` 继续调用 Nano Banana Pro（`GEM_PIX_2`），但不出现在模型列表中。旧 Imagen 模型已停止支持；模型不存在时不会回退到旧上游字段。

| 当前公开名称 | 分辨率 | 比例 |
|---|---:|---:|
| `gemini-3.1-flash-image-landscape` | 默认 | 横屏 |
| `gemini-3.1-flash-image-portrait` | 默认 | 竖屏 |
| `gemini-3.1-flash-image-square` | 默认 | 方图 |
| `gemini-3.1-flash-image-four-three` | 默认 | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four` | 默认 | 竖屏 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | 2K | 横屏 |
| `gemini-3.1-flash-image-portrait-2k` | 2K | 竖屏 |
| `gemini-3.1-flash-image-square-2k` | 2K | 方图 |
| `gemini-3.1-flash-image-four-three-2k` | 2K | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | 2K | 竖屏 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | 4K | 横屏 |
| `gemini-3.1-flash-image-portrait-4k` | 4K | 竖屏 |
| `gemini-3.1-flash-image-square-4k` | 4K | 方图 |
| `gemini-3.1-flash-image-four-three-4k` | 4K | 横屏 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | 4K | 竖屏 3:4 |

Nano Banana 2 Lite 使用 `gemini-3.1-flash-lite-image-*`，比例及分辨率后缀与上表一致，例如 `gemini-3.1-flash-lite-image-landscape`、`gemini-3.1-flash-lite-image-portrait-2k`、`gemini-3.1-flash-lite-image-square-4k`。也支持简化名称 `gemini-3.1-flash-lite-image` 搭配 `aspectRatio` / `imageSize` 参数。

2K/4K 变体通过生成后的高清放大实现，不代表 Lite 原生输出这些分辨率。主程序通过图片 RPC 明确提交模型、比例和参考图；浏览器打码与第三方打码均只提供验证码。`browser` / `personal` 模式不需要第三方打码密钥，图片、上传、放大及视频请求按所需 action 获取验证码。Lite 不会自动回退到其他图片模型。

### 视频生成

当前 `/v1/models` 与 Gemini `/models` 只公开 Flow 现行模型族。旧下划线模型名继续兼容，但不再出现在模型列表中。

| 当前公开名称 | 上游模型族 | 能力 |
|---|---|---|
| `veo-3.1-lite` / `veo-3.1-fast` / `veo-3.1-quality` / `omni-1.2-flash` | 当前默认模型 | 8 秒横屏文生视频 |
| `veo-3.1-lite-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Lite | 文生视频 |
| `veo-3.1-fast-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Fast | 文生视频 |
| `veo-3.1-quality-{4,6,8}s-{landscape,portrait}` | Veo 3.1 - Quality | 文生视频 |
| `omni-1.2-flash-{3..10}s-{landscape,portrait}`、`omni-1.2-flash-{11..15}s-portrait` | Omni 1.2 Flash (`macaron_cookie`) | 文生/参考图视频 |
| `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}` | Omni 1.1 Flash (`abra`，上游 2026-12-05 下线) | 文生/参考图视频 |
| `veo-3.1-{lite,fast,quality}-i2v-{4,6}s-{landscape,portrait}` | Veo 3.1 | 首帧/首尾帧视频 |
| `veo-3.1-{lite,fast}-r2v-8s-{landscape,portrait}` | Veo 3.1 | 多参考图视频 |
| `veo-3.1-{lite,fast,quality}-extend-8s-{landscape,portrait}` | Veo 3.1 | 视频续写 |

表格中的花括号表示任选其中一个值。例如 `veo-3.1-fast-{4,6,8}s-{landscape,portrait}` 包含 4/6/8 秒与横屏/竖屏组合。

#### Omni 1.2 Flash

`Omni 1.2 Flash` 是 Flow 当前的默认视频模型（2026-11-08 实测抓取），同时支持文生视频和参考图视频。调用方使用同一组模型名，服务会根据请求中是否包含图片自动选择生成模式：

- `omni-1.2-flash` 是 `omni-1.2-flash-8s-landscape` 的默认别名。
- 完整模型名为 `omni-1.2-flash-{3..10}s-{landscape,portrait}`，可选择 3–10 秒以及横屏或竖屏。
- 11–15 秒仅提供竖屏：`omni-1.2-flash-{11..15}s-portrait`（上游目录不提供长时长的横屏档）。
- 仅传入文本时使用文生视频模式；传入 1–3 张 `image_url` 图片时使用参考图视频模式。
- 时长和画面方向由模型名指定，无需额外传入同名参数。
- 上一代 `omni-1.1-flash`（`abra`）上游已标记 2026-12-05 下线，下线前仍可继续调用。

参考图视频请求示例：

```json
{
  "model": "omni-1.2-flash-8s-landscape",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "让画面中的人物自然转身并看向镜头" },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,<参考图base64>"
          }
        }
      ]
    }
  ],
  "stream": true
}
```

#### 兼容旧名：文生视频 (T2V - Text to Video)
⚠️ **不支持上传图片**

| 模型名称 | 说明| 尺寸 |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_landscape` | 文生视频 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_ultra` | 文生视频 | 横屏 |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_fast_ultra_relaxed` | 文生视频 | 横屏 |
| `veo_3_1_t2v_portrait` | 文生视频 | 竖屏 |
| `veo_3_1_t2v_landscape` | 文生视频 | 横屏 |
| `veo_3_1_t2v_landscape_4s` | 文生视频 4秒 | 横屏 |
| `veo_3_1_t2v_portrait_4s` | 文生视频 4秒 | 竖屏 |
| `veo_3_1_t2v_landscape_6s` | 文生视频 6秒 | 横屏 |
| `veo_3_1_t2v_portrait_6s` | 文生视频 6秒 | 竖屏 |
| `veo_3_1_t2v_fast_landscape_4s` | 文生视频 Fast 4秒 | 横屏 |
| `veo_3_1_t2v_fast_portrait_4s` | 文生视频 Fast 4秒 | 竖屏 |
| `veo_3_1_t2v_fast_landscape_6s` | 文生视频 Fast 6秒 | 横屏 |
| `veo_3_1_t2v_fast_portrait_6s` | 文生视频 Fast 6秒 | 竖屏 |
| `veo_3_1_t2v_lite_portrait` | 文生视频 Lite | 竖屏 |
| `veo_3_1_t2v_lite_landscape` | 文生视频 Lite | 横屏 |
| `veo_3_1_t2v_lite_4s_portrait` | 文生视频 Lite 4秒 | 竖屏 |
| `veo_3_1_t2v_lite_4s_landscape` | 文生视频 Lite 4秒 | 横屏 |
| `veo_3_1_t2v_lite_6s_portrait` | 文生视频 Lite 6秒 | 竖屏 |
| `veo_3_1_t2v_lite_6s_landscape` | 文生视频 Lite 6秒 | 横屏 |

#### 兼容旧名：首尾帧模型 (I2V - Image to Video)
📸 **支持1-2张图片：1张作为首帧，2张作为首尾帧**

> 💡 **自动适配**：系统会根据图片数量自动选择对应的 model_key
> - **单帧模式**（1张图）：使用首帧生成视频
> - **双帧模式**（2张图）：使用首帧+尾帧生成过渡视频
> - `veo_3_1_i2v_lite_*` 仅支持 **1 张** 首帧图片
> - `veo_3_1_interpolation_lite_*` 仅支持 **2 张** 首尾帧图片

| 模型名称 | 说明| 尺寸 |
|---------|---------|--------|
| `veo_3_1_i2v_s_fast_portrait_fl` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_fl` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_fl` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_portrait` | 图生视频 | 竖屏 |
| `veo_3_1_i2v_s_landscape` | 图生视频 | 横屏 |
| `veo_3_1_i2v_s_landscape_4s` | 图生视频 4秒 | 横屏 |
| `veo_3_1_i2v_s_portrait_4s` | 图生视频 4秒 | 竖屏 |
| `veo_3_1_i2v_s_landscape_6s` | 图生视频 6秒 | 横屏 |
| `veo_3_1_i2v_s_portrait_6s` | 图生视频 6秒 | 竖屏 |
| `veo_3_1_i2v_s_fast_landscape_4s_fl` | 图生视频 Fast 4秒 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_4s_fl` | 图生视频 Fast 4秒 | 竖屏 |
| `veo_3_1_i2v_s_fast_landscape_6s_fl` | 图生视频 Fast 6秒 | 横屏 |
| `veo_3_1_i2v_s_fast_portrait_6s_fl` | 图生视频 Fast 6秒 | 竖屏 |
| `veo_3_1_i2v_lite_portrait` | 图生视频 Lite（仅首帧） | 竖屏 |
| `veo_3_1_i2v_lite_landscape` | 图生视频 Lite（仅首帧） | 横屏 |
| `veo_3_1_i2v_lite_4s_portrait` | 图生视频 Lite 4秒（仅首帧） | 竖屏 |
| `veo_3_1_i2v_lite_4s_landscape` | 图生视频 Lite 4秒（仅首帧） | 横屏 |
| `veo_3_1_i2v_lite_6s_portrait` | 图生视频 Lite 6秒（仅首帧） | 竖屏 |
| `veo_3_1_i2v_lite_6s_landscape` | 图生视频 Lite 6秒（仅首帧） | 横屏 |
| `veo_3_1_interpolation_lite_portrait` | 图生视频 Lite（首尾帧过渡） | 竖屏 |
| `veo_3_1_interpolation_lite_landscape` | 图生视频 Lite（首尾帧过渡） | 横屏 |
| `veo_3_1_interpolation_lite_4s_portrait` | 图生视频 Lite 4秒（首尾帧过渡） | 竖屏 |
| `veo_3_1_interpolation_lite_4s_landscape` | 图生视频 Lite 4秒（首尾帧过渡） | 横屏 |
| `veo_3_1_interpolation_lite_6s_portrait` | 图生视频 Lite 6秒（首尾帧过渡） | 竖屏 |
| `veo_3_1_interpolation_lite_6s_landscape` | 图生视频 Lite 6秒（首尾帧过渡） | 横屏 |

#### 兼容旧名：多图生成 (R2V - Reference Images to Video)
🖼️ **支持多张图片**

> **2026-03-06 更新**
>
> - 已同步上游新版 `R2V` 视频请求体
> - `textInput` 已切换为 `structuredPrompt.parts`
> - 顶层新增 `mediaGenerationContext.batchId`
> - 顶层新增 `useV2ModelConfig: true`
> - 横屏 / 竖屏 `R2V` 模型共用同一套新版请求体
> - 横屏 `R2V` 的上游 `videoModelKey` 已切换为 `*_landscape` 形式
> - 根据当前上游协议，`referenceImages` 当前最多传 **3 张**

| 模型名称 | 说明| 尺寸 |
|---------|---------|--------|
| `veo_3_1_r2v_fast_portrait` | 图生视频 | 竖屏 |
| `veo_3_1_r2v_fast_landscape` | 图生视频 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra` | 图生视频 | 竖屏 |
| `veo_3_1_r2v_fast_landscape_ultra` | 图生视频 | 横屏 |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | 图生视频 | 竖屏 |
| `veo_3_1_r2v_fast_landscape_ultra_relaxed` | 图生视频 | 横屏 |

#### 兼容旧名：视频放大模型 (Upsample)

这些模型不是直接调用上游 upsampler key，而是先用对应的 Veo 3.1 普通模型生成视频，再提交 1080P/4K 放大请求。

| 模型名称 | 说明 | 输出 |
|---------|---------|--------|
| `veo_3_1_t2v_landscape_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_portrait_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_landscape_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_portrait_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_landscape_4s_4k` | 文生视频 4秒放大 | 4K |
| `veo_3_1_t2v_portrait_4s_4k` | 文生视频 4秒放大 | 4K |
| `veo_3_1_t2v_landscape_4s_1080p` | 文生视频 4秒放大 | 1080P |
| `veo_3_1_t2v_portrait_4s_1080p` | 文生视频 4秒放大 | 1080P |
| `veo_3_1_t2v_landscape_6s_4k` | 文生视频 6秒放大 | 4K |
| `veo_3_1_t2v_portrait_6s_4k` | 文生视频 6秒放大 | 4K |
| `veo_3_1_t2v_landscape_6s_1080p` | 文生视频 6秒放大 | 1080P |
| `veo_3_1_t2v_portrait_6s_1080p` | 文生视频 6秒放大 | 1080P |
| `veo_3_1_t2v_fast_portrait_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | 文生视频放大 | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_t2v_fast_ultra_1080p` | 文生视频放大 | 1080P |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_i2v_s_landscape_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_portrait_4k` | 图生视频放大 | 4K |
| `veo_3_1_i2v_s_landscape_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_i2v_s_portrait_1080p` | 图生视频放大 | 1080P |
| `veo_3_1_i2v_s_landscape_4s_4k` | 图生视频 4秒放大 | 4K |
| `veo_3_1_i2v_s_portrait_4s_4k` | 图生视频 4秒放大 | 4K |
| `veo_3_1_i2v_s_landscape_4s_1080p` | 图生视频 4秒放大 | 1080P |
| `veo_3_1_i2v_s_portrait_4s_1080p` | 图生视频 4秒放大 | 1080P |
| `veo_3_1_i2v_s_landscape_6s_4k` | 图生视频 6秒放大 | 4K |
| `veo_3_1_i2v_s_portrait_6s_4k` | 图生视频 6秒放大 | 4K |
| `veo_3_1_i2v_s_landscape_6s_1080p` | 图生视频 6秒放大 | 1080P |
| `veo_3_1_i2v_s_portrait_6s_1080p` | 图生视频 6秒放大 | 1080P |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | 多图视频放大 | 4K |
| `veo_3_1_r2v_fast_landscape_ultra_4k` | 多图视频放大 | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | 多图视频放大 | 1080P |
| `veo_3_1_r2v_fast_landscape_ultra_1080p` | 多图视频放大 | 1080P |

## 📡 API 使用示例（需要使用流式）

> 除了下方 `OpenAI-compatible` 示例，服务也支持 Gemini 官方格式：
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Gemini 官方格式支持以下认证方式：
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Gemini 官方图片请求体已兼容：
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gemini 官方 generateContent（文生图）

> 已使用真实 Token 实测通过。
> 如需流式返回，可将路径替换为 `:streamGenerateContent?alt=sse`。

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "一颗放在木桌上的红苹果，棚拍光线，极简背景"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### 文生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只可爱的猫咪在花园里玩耍"
      }
    ],
    "stream": true
  }'
```

### 图生图

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "将这张图片变成水彩画风格"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### 文生视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-8s-landscape",
    "messages": [
      {
        "role": "user",
        "content": "一只小猫在草地上追逐蝴蝶"
      }
    ],
    "stream": true
  }'
```

### 首尾帧生成视频

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-i2v-4s-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "从第一张图过渡到第二张图"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<首帧base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<尾帧base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### 多图生成视频

> `R2V` 会由服务端自动组装新版视频请求体，调用方仍然使用 OpenAI 兼容输入即可。
> 服务端会将横屏 `R2V` 自动映射到最新的 `*_landscape` 上游模型键。
> 当前最多传 **3 张参考图**。

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-r2v-8s-portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "以三张参考图的人物和场景为基础，生成一段镜头平滑推进的竖屏视频"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图1base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图2base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<参考图3base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- [PearNoDec](https://github.com/PearNoDec) 提供的YesCaptcha打码方案
- [raomaiping](https://github.com/raomaiping) 提供的无头打码方案
感谢所有贡献者和使用者的支持！

---

## 📞 联系方式

- 提交 Issue：[GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ 如果这个项目对你有帮助，请给个 Star！**

## Star History

[![Star History Chart](https://star-history.dera.page/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://star-history.dera.page/#TheSmallHanCat/flow2api&type=date&legend=top-left)
