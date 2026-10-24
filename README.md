# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**A full-featured OpenAI-compatible API service that provides a unified interface for Google Flow.**

</div>

## Sponsors

Thanks to [useapi.net](https://useapi.net/docs/api-google-flow-v1?utm_source=flow2api&utm_medium=referral&utm_campaign=flow2api-sponsor) for sponsoring this project. Its hosted APIs cover Google Flow (Veo 3.1, Omni 1.1 Flash, and Nano Banana Pro), Google Vids, Flow Music, Gemini Notebook, Meta Muse, and other services under one subscription. See the sponsor's website for current features and pricing.

Thanks to [ProxyLane](https://proxylane.dev/?utm_source=flow2api&utm_medium=partnership&utm_campaign=flow2api_sponsor_202610&utm_content=github_readme) for sponsoring this project. ProxyLane offers residential proxies in 195 countries, HTTP/SOCKS5 support, and username/password authentication. See its website for current terms and offers.

## Key features

- Text-to-image and image-to-image generation.
- Text-to-video, image-to-video, first/last-frame video, reference-image video, and video extension.
- Automatic Google/Flow cookie refresh and session validation.
- VideoFX Credits balance lookup.
- Token rotation, load balancing, and concurrency controls.
- HTTP and SOCKS5 proxy support.
- Web-based administration for tokens and configuration.
- Multi-turn image-generation conversations.
- Gemini API request compatibility, including `generateContent`, `streamGenerateContent`, `systemInstruction`, and text/image/file parts.

## Getting started

### Requirements

- Docker and Docker Compose (recommended), or Python 3.8+.
- A supported captcha-solving provider may be required because Flow can request additional reCAPTCHA challenges. Available integrations are configured in the admin console and example TOML file.

For automatic cookie updates, see [Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater).

### Option 1: Docker (recommended)

Clone the repository and start the service:

```bash
git clone https://github.com/kattmio/flow2api.git
cd flow2api
docker compose up -d
docker compose logs -f
```

The default Compose configuration mounts `./tmp:/app/tmp`. A cache timeout of `0` disables automatic cache expiry. Keep the `tmp` mount if cache files must survive container recreation.

#### WARP proxy mode

```bash
docker compose -f docker-compose.proxy.yml up -d
docker compose -f docker-compose.proxy.yml logs -f
```

#### Headed browser captcha mode

Use this mode when captcha solving needs a graphical browser inside the container. It starts Xvfb and Fluxbox, enables `ALLOW_DOCKER_HEADED_CAPTCHA=true`, and exposes only the application port—not a remote desktop port.

```bash
docker compose -f docker-compose.headed.yml up -d --build
docker compose -f docker-compose.headed.yml logs -f
```

The API listens on port `8000`. In the admin console, choose `browser` or `personal` as the captcha method. The built-in personal browser runs in headed mode by default; set `PERSONAL_BROWSER_HEADLESS=true` to temporarily use headless mode.

### Option 2: Local Python installation

```bash
git clone https://github.com/kattmio/flow2api.git
cd flow2api
python -m venv venv
```

Activate the environment and install dependencies:

```bash
# Windows
venv\\Scripts\\activate

# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
python main.py
```

Open **http://localhost:8000** to access the admin console. The default initial credentials are username `admin` and password `admin`. **Change the password immediately after the first sign-in.**

## Monitoring endpoints

- `GET /health` — public health summary, including service status, active tokens, tokens expiring soon, expired tokens, and tokens disabled after HTTP 429 responses.
- `GET /metrics` — Prometheus metrics.
- `GET /api/tokens` — token status fields such as `at_expires`, `at_expired`, `at_expiring_within_1h`, `ban_reason`, and `consecutive_error_count`.

Prometheus can scrape `/metrics` directly. For Kubernetes deployments, prefer scraping from inside the cluster and restrict external access to this endpoint at the Ingress or Gateway layer.

## Model testing page

Open **http://localhost:8000/test** to use the built-in model tester. It supports browsing models by category, streaming generation progress, uploading images for image-to-image and image-to-video requests, and previewing generated images or videos.

## Supported models

The model names below describe the public naming scheme. Exact availability depends on the current upstream Flow service and the configuration of your instance.

### Image generation

The `/v1/models` and Gemini `/models` endpoints expose Nano Banana 2.1 and Nano Banana 2 Lite, with landscape, portrait, square, 4:3, and 3:4 aspect ratios in default, 2K, and 4K variants. Existing `gemini-3.1-flash-image-*` names are retained and routed to Nano Banana 2.1 (`BELUGA`). Legacy `gemini-3.0-pro-image-*` names still route to Nano Banana Pro (`GEM_PIX_2`) but are not listed. Legacy Imagen models are no longer supported, and unknown model names do not fall back to legacy upstream fields.

Nano Banana 2 Lite uses the `gemini-3.1-flash-lite-image-*` prefix with the same aspect-ratio and resolution suffixes. The simplified name `gemini-3.1-flash-lite-image` can also be used with `aspectRatio` and `imageSize` parameters.

The 2K/4K variants upscale the generated image; they do not represent native Lite output at those resolutions. The main program submits the model, aspect ratio, and reference images through the image RPC. Browser-based and third-party captcha integrations provide captcha solutions only. The `browser` and `personal` modes do not require a third-party captcha API key, and the Lite model does not automatically fall back to another image model.

### Video generation

The `/v1/models` and Gemini `/models` endpoints expose current Flow model families. Legacy underscore-style names remain compatible but are not listed.

| Public model pattern | Capability |
| --- | --- |
| `veo-3.1-lite`, `veo-3.1-fast`, `veo-3.1-quality`, `omni-1.1-flash` | Default 8-second landscape text-to-video aliases |
| `veo-3.1-{lite,fast,quality}-{4,6,8}s-{landscape,portrait}` | Text-to-video |
| `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}` | Text-to-video or reference-image video |
| `veo-3.1-{lite,fast,quality}-i2v-{4,6}s-{landscape,portrait}` | First-frame or first/last-frame video |
| `veo-3.1-{lite,fast}-r2v-8s-{landscape,portrait}` | Video generation from multiple reference images |
| `veo-3.1-{lite,fast,quality}-extend-8s-{landscape,portrait}` | Video extension |

Braces indicate alternatives. For example, `veo-3.1-fast-{4,6,8}s-{landscape,portrait}` represents combinations of 4, 6, or 8 seconds and landscape or portrait orientation.

#### Omni 1.1 Flash

Omni 1.1 Flash supports both text-to-video and reference-image video. The service selects the mode based on whether the request contains images:

- `omni-1.1-flash` is an alias for `omni-1.1-flash-8s-landscape`.
- Full names follow `omni-1.1-flash-{4,6,8,10}s-{landscape,portrait}`.
- Text-only requests use text-to-video mode. Requests containing 1–3 `image_url` images use reference-image mode.
- Duration and orientation are encoded in the model name; no matching parameters are required.

#### Legacy model aliases

Legacy aliases include text-to-video (`T2V`), image-to-video (`I2V`), reference-images-to-video (`R2V`), and video upscaling. For I2V, one image supplies the first frame and two images supply the first and last frames. Lite I2V aliases may be limited to first-frame-only or interpolation-only behavior. R2V currently accepts up to three reference images.

Video upscaling aliases first generate a video with the corresponding standard Veo 3.1 model and then submit a 1080p or 4K upscaling request.

## API examples

The service supports OpenAI-compatible chat completions and the official Gemini request format. Gemini endpoints include:

- `POST /v1beta/models/{model}:generateContent`
- `POST /models/{model}:generateContent`
- `POST /v1beta/models/{model}:streamGenerateContent`
- `POST /models/{model}:streamGenerateContent`

Gemini authentication can use `Authorization: Bearer <api_key>`, `x-goog-api-key: <api_key>`, or `?key=<api_key>`. Supported image request fields include `systemInstruction`, `contents[].parts[].text`, `inlineData`, `fileData.fileUri`, `generationConfig.responseModalities`, and `generationConfig.imageConfig.aspectRatio/imageSize`.

### Gemini text-to-image

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {"parts": [{"text": "Return an image only."}]},
    "contents": [{"role": "user", "parts": [{"text": "A red apple on a wooden table, studio lighting, minimalist background"}]}],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"}
    }
  }'
```

For streaming, use `:streamGenerateContent?alt=sse` instead of `:generateContent`.

### OpenAI-compatible text-to-image

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [{"role": "user", "content": "A cute cat playing in a garden"}],
    "stream": true
  }'
```

### Image-to-image

Use a text part and an `image_url` part in the user's message. For example, the text prompt can be: `Turn this image into a watercolor painting`. The image can be provided as a data URL such as `data:image/jpeg;base64,<base64_encoded_image>`.

### Text-to-video

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast-8s-landscape",
    "messages": [{"role": "user", "content": "A kitten chasing butterflies in a meadow"}],
    "stream": true
  }'
```

### First/last-frame video

Use the `veo-3.1-fast-i2v-4s-landscape` model and include two `image_url` parts. The text prompt can be `Transition from the first image to the second image`.

### Reference-image video

R2V request bodies are assembled by the server. Callers can use the OpenAI-compatible input format, and landscape requests are mapped to the latest upstream `*_landscape` model key. Up to three reference images are currently accepted.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Acknowledgements

- [PearNoDec](https://github.com/PearNoDec) — upstream project and implementation contributions.
