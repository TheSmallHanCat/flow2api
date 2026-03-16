# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**A fully-featured OpenAI-compatible API service providing a unified interface for Flow**

</div>

## ✨ Core Features

- 🎨 **Text-to-Image** / **Image-to-Image**
- 🎬 **Text-to-Video** / **Image-to-Video**
- 🎞️ **First/Last Frame Video**
- 🔄 **AT/ST Auto-Refresh** - AT auto-refreshes on expiration; ST auto-refreshes via browser when expired (personal mode)
- 📊 **Credits Display** - Real-time query and display of VideoFX Credits
- 🚀 **Load Balancing** - Multi-token round-robin and concurrency control
- 🌐 **Proxy Support** - HTTP/SOCKS5 proxy support
- 📱 **Web Management Interface** - Intuitive token and configuration management
- 🎨 **Image Generation Continuous Conversation**
- 🧩 **Gemini Official Request Body Compatible** - Supports `generateContent` / `streamGenerateContent`, `systemInstruction`, `contents.parts.text/inlineData/fileData`
- ✅ **Gemini Official Format Verified** - Tested with real tokens; `/models/{model}:generateContent` correctly returns official `candidates[].content.parts[].inlineData`

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose (recommended)
- Or Python 3.8+

- Since Flow has added additional captcha verification, you can choose to use browser captcha or third-party captcha services:
Register at [YesCaptcha](https://yescaptcha.com/i/13Xd8K) and get an API key, then enter it in the system configuration page under `YesCaptcha API Key`.
- The default `docker-compose.yml` is recommended for use with third-party captcha services (yescaptcha/capmonster/ezcaptcha/capsolver).
For headed browser captcha within Docker (browser/personal), use the `docker-compose.headed.yml` below.

- Browser extension for automatic ST updates: [Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)

### Method 1: Docker Deployment (Recommended)

#### Standard Mode (Without Proxy)

```bash
# Clone the project
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Start the service
docker-compose up -d

# View logs
docker-compose logs -f
```

#### WARP Mode (With Proxy)

```bash
# Start with WARP proxy
docker-compose -f docker-compose.warp.yml up -d

# View logs
docker-compose -f docker-compose.warp.yml logs -f
```

#### Docker Headed Captcha Mode (browser / personal)

> For scenarios where you need a virtual desktop and want to enable headed browser captcha inside a container.
> This mode starts `Xvfb + Fluxbox` for container-internal visualization and sets `ALLOW_DOCKER_HEADED_CAPTCHA=true`.
> Only the application port is exposed; no remote desktop connection ports are provided.

```bash
# Start headed mode (use --build for first time)
docker compose -f docker-compose.headed.yml up -d --build

# View logs
docker compose -f docker-compose.headed.yml logs -f
```

- API port: `8000`
- After entering the admin panel, set the captcha method to `browser` or `personal`

### Method 2: Local Deployment

```bash
# Clone the project
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the service
python main.py
```

### First Access

After starting the service, visit the admin panel at: **http://localhost:8000**. Change the password immediately after first login!

- **Username**: `admin`
- **Password**: `admin`

## 📋 Supported Models

### Image Generation

| Model Name | Description | Aspect Ratio |
|---------|--------|--------|
| `gemini-2.5-flash-image-landscape` | Text/Image-to-Image | Landscape |
| `gemini-2.5-flash-image-portrait` | Text/Image-to-Image | Portrait |
| `gemini-3.0-pro-image-landscape` | Text/Image-to-Image | Landscape |
| `gemini-3.0-pro-image-portrait` | Text/Image-to-Image | Portrait |
| `gemini-3.0-pro-image-square` | Text/Image-to-Image | Square |
| `gemini-3.0-pro-image-four-three` | Text/Image-to-Image | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four` | Text/Image-to-Image | Portrait 3:4 |
| `gemini-3.0-pro-image-landscape-2k` | Text/Image-to-Image (2K) | Landscape |
| `gemini-3.0-pro-image-portrait-2k` | Text/Image-to-Image (2K) | Portrait |
| `gemini-3.0-pro-image-square-2k` | Text/Image-to-Image (2K) | Square |
| `gemini-3.0-pro-image-four-three-2k` | Text/Image-to-Image (2K) | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four-2k` | Text/Image-to-Image (2K) | Portrait 3:4 |
| `gemini-3.0-pro-image-landscape-4k` | Text/Image-to-Image (4K) | Landscape |
| `gemini-3.0-pro-image-portrait-4k` | Text/Image-to-Image (4K) | Portrait |
| `gemini-3.0-pro-image-square-4k` | Text/Image-to-Image (4K) | Square |
| `gemini-3.0-pro-image-four-three-4k` | Text/Image-to-Image (4K) | Landscape 4:3 |
| `gemini-3.0-pro-image-three-four-4k` | Text/Image-to-Image (4K) | Portrait 3:4 |
| `imagen-4.0-generate-preview-landscape` | Text/Image-to-Image | Landscape |
| `imagen-4.0-generate-preview-portrait` | Text/Image-to-Image | Portrait |
| `gemini-3.1-flash-image-landscape` | Text/Image-to-Image | Landscape |
| `gemini-3.1-flash-image-portrait` | Text/Image-to-Image | Portrait |
| `gemini-3.1-flash-image-square` | Text/Image-to-Image | Square |
| `gemini-3.1-flash-image-four-three` | Text/Image-to-Image | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four` | Text/Image-to-Image | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | Text/Image-to-Image (2K) | Landscape |
| `gemini-3.1-flash-image-portrait-2k` | Text/Image-to-Image (2K) | Portrait |
| `gemini-3.1-flash-image-square-2k` | Text/Image-to-Image (2K) | Square |
| `gemini-3.1-flash-image-four-three-2k` | Text/Image-to-Image (2K) | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | Text/Image-to-Image (2K) | Portrait 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | Text/Image-to-Image (4K) | Landscape |
| `gemini-3.1-flash-image-portrait-4k` | Text/Image-to-Image (4K) | Portrait |
| `gemini-3.1-flash-image-square-4k` | Text/Image-to-Image (4K) | Square |
| `gemini-3.1-flash-image-four-three-4k` | Text/Image-to-Image (4K) | Landscape 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | Text/Image-to-Image (4K) | Portrait 3:4 |

### Video Generation

#### Text-to-Video (T2V)
⚠️ **Does not support image upload**

| Model Name | Description | Aspect Ratio |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait` | Text-to-Video | Portrait |
| `veo_3_1_t2v_fast_landscape` | Text-to-Video | Landscape |
| `veo_2_1_fast_d_15_t2v_portrait` | Text-to-Video | Portrait |
| `veo_2_1_fast_d_15_t2v_landscape` | Text-to-Video | Landscape |
| `veo_2_0_t2v_portrait` | Text-to-Video | Portrait |
| `veo_2_0_t2v_landscape` | Text-to-Video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra` | Text-to-Video | Portrait |
| `veo_3_1_t2v_fast_ultra` | Text-to-Video | Landscape |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | Text-to-Video | Portrait |
| `veo_3_1_t2v_fast_ultra_relaxed` | Text-to-Video | Landscape |
| `veo_3_1_t2v_portrait` | Text-to-Video | Portrait |
| `veo_3_1_t2v_landscape` | Text-to-Video | Landscape |

#### First/Last Frame Model (I2V - Image to Video)
📸 **Supports 1-2 images: 1 image as first frame, 2 images as first and last frames**

> 💡 **Auto-adaptation**: The system automatically selects the corresponding model_key based on image count
> - **Single frame mode** (1 image): Uses first frame to generate video
> - **Dual frame mode** (2 images): Uses first + last frame to generate transition video

| Model Name | Description | Aspect Ratio |
|---------|---------|--------|
| `veo_3_1_i2v_s_fast_portrait_fl` | Image-to-Video | Portrait |
| `veo_3_1_i2v_s_fast_fl` | Image-to-Video | Landscape |
| `veo_2_1_fast_d_15_i2v_portrait` | Image-to-Video | Portrait |
| `veo_2_1_fast_d_15_i2v_landscape` | Image-to-Video | Landscape |
| `veo_2_0_i2v_portrait` | Image-to-Video | Portrait |
| `veo_2_0_i2v_landscape` | Image-to-Video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | Image-to-Video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_fl` | Image-to-Video | Landscape |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | Image-to-Video | Portrait |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | Image-to-Video | Landscape |
| `veo_3_1_i2v_s_portrait` | Image-to-Video | Portrait |
| `veo_3_1_i2v_s_landscape` | Image-to-Video | Landscape |

#### Multi-Image Generation (R2V - Reference Images to Video)
🖼️ **Supports multiple images**

> **2026-03-06 Update**
>
> - Synchronized with upstream new `R2V` video request body
> - `textInput` switched to `structuredPrompt.parts`
> - Added top-level `mediaGenerationContext.batchId`
> - Added top-level `useV2ModelConfig: true`
> - Landscape / Portrait `R2V` models share the same new request body
> - Landscape `R2V` upstream `videoModelKey` switched to `*_landscape` format
> - Per current upstream protocol, `referenceImages` supports up to **3 images**

| Model Name | Description | Aspect Ratio |
|---------|---------|--------|
| `veo_3_1_r2v_fast_portrait` | Image-to-Video | Portrait |
| `veo_3_1_r2v_fast` | Image-to-Video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra` | Image-to-Video | Portrait |
| `veo_3_1_r2v_fast_ultra` | Image-to-Video | Landscape |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | Image-to-Video | Portrait |
| `veo_3_1_r2v_fast_ultra_relaxed` | Image-to-Video | Landscape |

#### Video Upsample Models

| Model Name | Description | Output |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait_4k` | T2V Upsample | 4K |
| `veo_3_1_t2v_fast_4k` | T2V Upsample | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | T2V Upsample | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | T2V Upsample | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | T2V Upsample | 1080P |
| `veo_3_1_t2v_fast_1080p` | T2V Upsample | 1080P |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | T2V Upsample | 1080P |
| `veo_3_1_t2v_fast_ultra_1080p` | T2V Upsample | 1080P |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | I2V Upsample | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | I2V Upsample | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | I2V Upsample | 1080P |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | I2V Upsample | 1080P |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | R2V Upsample | 4K |
| `veo_3_1_r2v_fast_ultra_4k` | R2V Upsample | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | R2V Upsample | 1080P |
| `veo_3_1_r2v_fast_ultra_1080p` | R2V Upsample | 1080P |

## 📡 API Usage Examples (Streaming Required)

> In addition to the `OpenAI-compatible` examples below, the service also supports Gemini official format:
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Gemini official format supports the following authentication methods:
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Gemini official image request body is compatible with:
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gemini Official generateContent (Text-to-Image)

> Tested with real tokens.
> For streaming response, replace the path with `:streamGenerateContent?alt=sse`.

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
            "text": "A red apple on a wooden table, studio lighting, minimal background"
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

### Text-to-Image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "A cute cat playing in a garden"
      }
    ],
    "stream": true
  }'
```

### Image-to-Image

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
            "text": "Transform this image into watercolor painting style"
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

### Text-to-Video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "A kitten chasing butterflies on a grass field"
      }
    ],
    "stream": true
  }'
```

### First/Last Frame Video Generation

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Transition from the first image to the second image"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<first_frame_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<last_frame_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Multi-Image Video Generation

> `R2V` request body is automatically assembled by the server; callers still use OpenAI-compatible input.
> The server automatically maps landscape `R2V` to the latest `*_landscape` upstream model key.
> Currently supports up to **3 reference images**.

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Based on the characters and scenes in the three reference images, generate a smooth camera push-in portrait video"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_1_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_2_base64>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<reference_image_3_base64>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgements

- [PearNoDec](https://github.com/PearNoDec) for the YesCaptcha captcha solution
- [raomaiping](https://github.com/raomaiping) for the headless captcha solution
Thanks to all contributors and users for their support!

---

## 📞 Contact

- Submit an Issue: [GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ If this project is helpful to you, please give it a Star!**

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://www.star-history.com/#TheSmallHanCat/flow2api&type=date&legend=top-left)
