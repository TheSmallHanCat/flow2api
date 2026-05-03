"""Prometheus monitoring helpers for Flow2API."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ModuleNotFoundError:
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

        def labels(self, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *args: Any, **kwargs: Any) -> None:
            return None

        def set(self, *args: Any, **kwargs: Any) -> None:
            return None

        def observe(self, *args: Any, **kwargs: Any) -> None:
            return None

        def clear(self) -> None:
            return None

    class CollectorRegistry:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

    Counter = Gauge = Histogram = _NoopMetric

    def generate_latest(registry: Any = None) -> bytes:
        return b"# prometheus_client is not installed; metrics are disabled\n"

from .config import config

_PROCESS_START_TIME = time.time()


def _to_utc_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    return None


def _to_timestamp(value: Any) -> float:
    dt = _to_utc_datetime(value)
    if dt is None:
        return 0.0
    return float(dt.timestamp())


async def _probe_remote_browser_health(base_url: str, timeout_seconds: float = 3.0) -> tuple[bool, float]:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return False, 0.0

    url = f"{normalized}/api/v1/health"
    started_at = time.perf_counter()

    def do_request() -> tuple[int, str]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=max(0.5, float(timeout_seconds))) as response:
            status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return status_code, body.decode(charset, errors="replace")

    try:
        status_code, body_text = await asyncio.to_thread(do_request)
        ok = 200 <= status_code < 300
        if ok and body_text:
            try:
                payload = json.loads(body_text)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("ok") is False:
                ok = False
        latency = time.perf_counter() - started_at
        return ok, latency
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        latency = time.perf_counter() - started_at
        return False, latency
    except Exception:
        latency = time.perf_counter() - started_at
        return False, latency


MAIN_REGISTRY = CollectorRegistry(auto_describe=True)

MAIN_UP = Gauge(
    "flow2api_up",
    "Whether the Flow2API service process is running.",
    registry=MAIN_REGISTRY,
)
MAIN_PROCESS_START_TIME = Gauge(
    "flow2api_process_start_time_seconds",
    "Flow2API process start time since unix epoch in seconds.",
    registry=MAIN_REGISTRY,
)
GENERATION_REQUESTS_TOTAL = Counter(
    "flow2api_generation_requests_total",
    "Logical generation request outcomes handled by Flow2API.",
    ["generation_type", "result"],
    registry=MAIN_REGISTRY,
)
GENERATION_DURATION_SECONDS = Histogram(
    "flow2api_generation_duration_seconds",
    "Generation request duration in seconds.",
    ["generation_type", "result"],
    buckets=(0.1, 0.3, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
    registry=MAIN_REGISTRY,
)
TOKEN_REFRESH_TOTAL = Counter(
    "flow2api_token_refresh_total",
    "Token refresh attempts grouped by kind and result.",
    ["kind", "result"],
    registry=MAIN_REGISTRY,
)
TOKENS_TOTAL = Gauge(
    "flow2api_tokens_total",
    "Total number of configured tokens.",
    registry=MAIN_REGISTRY,
)
TOKENS_ACTIVE = Gauge(
    "flow2api_tokens_active",
    "Number of active tokens.",
    registry=MAIN_REGISTRY,
)
TOKENS_INACTIVE = Gauge(
    "flow2api_tokens_inactive",
    "Number of inactive tokens.",
    registry=MAIN_REGISTRY,
)
TOKENS_MISSING_AT = Gauge(
    "flow2api_tokens_missing_at",
    "Number of tokens without an access token.",
    registry=MAIN_REGISTRY,
)
TOKENS_EXPIRED = Gauge(
    "flow2api_tokens_expired",
    "Number of tokens whose access token is already expired.",
    registry=MAIN_REGISTRY,
)
TOKENS_EXPIRING_SOON = Gauge(
    "flow2api_tokens_expiring_within_hour",
    "Number of tokens whose access token will expire within the next hour.",
    registry=MAIN_REGISTRY,
)
TOKENS_BANNED_429 = Gauge(
    "flow2api_tokens_banned_429",
    "Number of tokens currently disabled because of 429 rate limit bans.",
    registry=MAIN_REGISTRY,
)
TOKENS_CREDITS_TOTAL = Gauge(
    "flow2api_token_credits_total",
    "Sum of credits across all tokens.",
    registry=MAIN_REGISTRY,
)
ACTIVE_TOKENS_CREDITS_TOTAL = Gauge(
    "flow2api_active_token_credits_total",
    "Sum of credits across active tokens.",
    registry=MAIN_REGISTRY,
)
TOKENS_ERROR_TOTAL = Gauge(
    "flow2api_token_error_total",
    "Sum of historical token errors across all tokens.",
    registry=MAIN_REGISTRY,
)
TOKENS_TODAY_ERROR_TOTAL = Gauge(
    "flow2api_token_today_error_total",
    "Sum of today's token errors across all tokens.",
    registry=MAIN_REGISTRY,
)
IMAGE_INFLIGHT_TOTAL = Gauge(
    "flow2api_image_inflight_total",
    "Total in-flight image requests tracked by the concurrency manager.",
    registry=MAIN_REGISTRY,
)
VIDEO_INFLIGHT_TOTAL = Gauge(
    "flow2api_video_inflight_total",
    "Total in-flight video requests tracked by the concurrency manager.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TOTAL_IMAGES = Gauge(
    "flow2api_dashboard_total_images",
    "Dashboard total image count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TOTAL_VIDEOS = Gauge(
    "flow2api_dashboard_total_videos",
    "Dashboard total video count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TOTAL_ERRORS = Gauge(
    "flow2api_dashboard_total_errors",
    "Dashboard total error count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TODAY_IMAGES = Gauge(
    "flow2api_dashboard_today_images",
    "Dashboard today image count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TODAY_VIDEOS = Gauge(
    "flow2api_dashboard_today_videos",
    "Dashboard today video count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
DASHBOARD_TODAY_ERRORS = Gauge(
    "flow2api_dashboard_today_errors",
    "Dashboard today error count from persisted token statistics.",
    registry=MAIN_REGISTRY,
)
REMOTE_BROWSER_CONFIGURED = Gauge(
    "flow2api_remote_browser_configured",
    "Whether remote_browser mode has a target base URL configured.",
    registry=MAIN_REGISTRY,
)
REMOTE_BROWSER_TARGET_UP = Gauge(
    "flow2api_remote_browser_target_up",
    "Whether the configured remote_browser target responded successfully.",
    registry=MAIN_REGISTRY,
)
REMOTE_BROWSER_TARGET_LATENCY_SECONDS = Gauge(
    "flow2api_remote_browser_target_latency_seconds",
    "Probe latency of the configured remote_browser target in seconds.",
    registry=MAIN_REGISTRY,
)
TOKEN_ACTIVE = Gauge(
    "flow2api_token_active",
    "Whether a token is active.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_AT_EXPIRES_TIMESTAMP = Gauge(
    "flow2api_token_at_expires_timestamp_seconds",
    "AT expiration time for a token since unix epoch in seconds.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_EXPIRED = Gauge(
    "flow2api_token_expired",
    "Whether a token access token is expired.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_EXPIRING_SOON = Gauge(
    "flow2api_token_expiring_within_hour",
    "Whether a token access token will expire within the next hour.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_MISSING_AT = Gauge(
    "flow2api_token_missing_at",
    "Whether a token is missing an access token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_BANNED = Gauge(
    "flow2api_token_banned",
    "Whether a token is banned.",
    ["token_id", "reason"],
    registry=MAIN_REGISTRY,
)
TOKEN_CREDITS = Gauge(
    "flow2api_token_credits",
    "Current credits for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_ERROR_TOTAL = Gauge(
    "flow2api_token_error_count",
    "Historical total error count for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_TODAY_ERROR_TOTAL = Gauge(
    "flow2api_token_today_error_count",
    "Today's error count for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_CONSECUTIVE_ERROR_TOTAL = Gauge(
    "flow2api_token_consecutive_error_count",
    "Current consecutive error count for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_LAST_USED_TIMESTAMP = Gauge(
    "flow2api_token_last_used_timestamp_seconds",
    "Last-used timestamp for a token since unix epoch in seconds.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_LAST_ERROR_TIMESTAMP = Gauge(
    "flow2api_token_last_error_timestamp_seconds",
    "Last-error timestamp for a token since unix epoch in seconds.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_IMAGE_INFLIGHT = Gauge(
    "flow2api_token_image_inflight",
    "Current in-flight image requests for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)
TOKEN_VIDEO_INFLIGHT = Gauge(
    "flow2api_token_video_inflight",
    "Current in-flight video requests for a token.",
    ["token_id"],
    registry=MAIN_REGISTRY,
)

MAIN_UP.set(1.0)
MAIN_PROCESS_START_TIME.set(_PROCESS_START_TIME)


def record_generation_result(generation_type: str, result: str, duration_seconds: Optional[float]) -> None:
    normalized_type = generation_type if generation_type in {"image", "video"} else "unknown"
    normalized_result = result if result in {"success", "failed", "cancelled", "no_token", "invalid"} else "unknown"
    GENERATION_REQUESTS_TOTAL.labels(
        generation_type=normalized_type,
        result=normalized_result,
    ).inc()
    if duration_seconds is not None and duration_seconds >= 0:
        GENERATION_DURATION_SECONDS.labels(
            generation_type=normalized_type,
            result=normalized_result,
        ).observe(float(duration_seconds))


def record_token_refresh(kind: str, result: str) -> None:
    normalized_kind = kind if kind in {"at", "st"} else "unknown"
    normalized_result = result if result in {"success", "failure"} else "unknown"
    TOKEN_REFRESH_TOTAL.labels(kind=normalized_kind, result=normalized_result).inc()


async def update_main_runtime_metrics(db: Any, concurrency_manager: Optional[Any] = None) -> None:
    rows = await db.get_all_tokens_with_stats()
    now = datetime.now(timezone.utc)

    TOKEN_ACTIVE.clear()
    TOKEN_AT_EXPIRES_TIMESTAMP.clear()
    TOKEN_EXPIRED.clear()
    TOKEN_EXPIRING_SOON.clear()
    TOKEN_MISSING_AT.clear()
    TOKEN_BANNED.clear()
    TOKEN_CREDITS.clear()
    TOKEN_ERROR_TOTAL.clear()
    TOKEN_TODAY_ERROR_TOTAL.clear()
    TOKEN_CONSECUTIVE_ERROR_TOTAL.clear()
    TOKEN_LAST_USED_TIMESTAMP.clear()
    TOKEN_LAST_ERROR_TIMESTAMP.clear()
    TOKEN_IMAGE_INFLIGHT.clear()
    TOKEN_VIDEO_INFLIGHT.clear()

    total_tokens = len(rows)
    active_tokens = 0
    inactive_tokens = 0
    missing_at_tokens = 0
    expired_tokens = 0
    expiring_soon_tokens = 0
    banned_429_tokens = 0
    total_credits = 0
    active_total_credits = 0
    total_errors = 0
    total_today_errors = 0
    total_image_inflight = 0
    total_video_inflight = 0

    for row in rows:
        token_id = str(row.get("id") or "")
        if not token_id:
            continue

        is_active = bool(row.get("is_active"))
        at_value = str(row.get("at") or "").strip()
        at_expires = _to_utc_datetime(row.get("at_expires"))
        ban_reason = str(row.get("ban_reason") or "").strip() or "none"
        credits = int(row.get("credits") or 0)
        error_count = int(row.get("error_count") or 0)
        today_error_count = int(row.get("today_error_count") or 0)
        consecutive_error_count = int(row.get("consecutive_error_count") or 0)
        last_used_at = _to_timestamp(row.get("last_used_at"))
        last_error_at = _to_timestamp(row.get("last_error_at"))

        expired = False
        expiring_soon = False
        if at_expires is not None:
            expired = at_expires <= now
            expiring_soon = not expired and (at_expires - now).total_seconds() < 3600

        if is_active:
            active_tokens += 1
            active_total_credits += credits
        else:
            inactive_tokens += 1

        if not at_value:
            missing_at_tokens += 1
        if expired:
            expired_tokens += 1
        if expiring_soon:
            expiring_soon_tokens += 1
        if (not is_active) and ban_reason == "429_rate_limit":
            banned_429_tokens += 1

        total_credits += credits
        total_errors += error_count
        total_today_errors += today_error_count

        image_inflight = 0
        video_inflight = 0
        if concurrency_manager is not None:
            image_inflight = int(await concurrency_manager.get_image_inflight(int(token_id)))
            video_inflight = int(await concurrency_manager.get_video_inflight(int(token_id)))
        total_image_inflight += image_inflight
        total_video_inflight += video_inflight

        TOKEN_ACTIVE.labels(token_id=token_id).set(1.0 if is_active else 0.0)
        TOKEN_AT_EXPIRES_TIMESTAMP.labels(token_id=token_id).set(_to_timestamp(at_expires))
        TOKEN_EXPIRED.labels(token_id=token_id).set(1.0 if expired else 0.0)
        TOKEN_EXPIRING_SOON.labels(token_id=token_id).set(1.0 if expiring_soon else 0.0)
        TOKEN_MISSING_AT.labels(token_id=token_id).set(1.0 if not at_value else 0.0)
        TOKEN_BANNED.labels(token_id=token_id, reason=ban_reason).set(
            1.0 if (not is_active and ban_reason != "none") else 0.0
        )
        TOKEN_CREDITS.labels(token_id=token_id).set(float(credits))
        TOKEN_ERROR_TOTAL.labels(token_id=token_id).set(float(error_count))
        TOKEN_TODAY_ERROR_TOTAL.labels(token_id=token_id).set(float(today_error_count))
        TOKEN_CONSECUTIVE_ERROR_TOTAL.labels(token_id=token_id).set(float(consecutive_error_count))
        TOKEN_LAST_USED_TIMESTAMP.labels(token_id=token_id).set(last_used_at)
        TOKEN_LAST_ERROR_TIMESTAMP.labels(token_id=token_id).set(last_error_at)
        TOKEN_IMAGE_INFLIGHT.labels(token_id=token_id).set(float(image_inflight))
        TOKEN_VIDEO_INFLIGHT.labels(token_id=token_id).set(float(video_inflight))

    TOKENS_TOTAL.set(float(total_tokens))
    TOKENS_ACTIVE.set(float(active_tokens))
    TOKENS_INACTIVE.set(float(inactive_tokens))
    TOKENS_MISSING_AT.set(float(missing_at_tokens))
    TOKENS_EXPIRED.set(float(expired_tokens))
    TOKENS_EXPIRING_SOON.set(float(expiring_soon_tokens))
    TOKENS_BANNED_429.set(float(banned_429_tokens))
    TOKENS_CREDITS_TOTAL.set(float(total_credits))
    ACTIVE_TOKENS_CREDITS_TOTAL.set(float(active_total_credits))
    TOKENS_ERROR_TOTAL.set(float(total_errors))
    TOKENS_TODAY_ERROR_TOTAL.set(float(total_today_errors))
    IMAGE_INFLIGHT_TOTAL.set(float(total_image_inflight))
    VIDEO_INFLIGHT_TOTAL.set(float(total_video_inflight))

    dashboard_stats = await db.get_dashboard_stats()
    DASHBOARD_TOTAL_IMAGES.set(float(dashboard_stats.get("total_images") or 0))
    DASHBOARD_TOTAL_VIDEOS.set(float(dashboard_stats.get("total_videos") or 0))
    DASHBOARD_TOTAL_ERRORS.set(float(dashboard_stats.get("total_errors") or 0))
    DASHBOARD_TODAY_IMAGES.set(float(dashboard_stats.get("today_images") or 0))
    DASHBOARD_TODAY_VIDEOS.set(float(dashboard_stats.get("today_videos") or 0))
    DASHBOARD_TODAY_ERRORS.set(float(dashboard_stats.get("today_errors") or 0))

    remote_browser_base_url = (config.remote_browser_base_url or "").strip()
    remote_browser_configured = config.captcha_method == "remote_browser" and bool(remote_browser_base_url)
    REMOTE_BROWSER_CONFIGURED.set(1.0 if remote_browser_configured else 0.0)

    if remote_browser_configured:
        remote_browser_up, remote_browser_latency = await _probe_remote_browser_health(remote_browser_base_url)
        REMOTE_BROWSER_TARGET_UP.set(1.0 if remote_browser_up else 0.0)
        REMOTE_BROWSER_TARGET_LATENCY_SECONDS.set(float(remote_browser_latency))
    else:
        REMOTE_BROWSER_TARGET_UP.set(0.0)
        REMOTE_BROWSER_TARGET_LATENCY_SECONDS.set(0.0)


async def render_main_metrics(db: Any, concurrency_manager: Optional[Any] = None) -> bytes:
    await update_main_runtime_metrics(db, concurrency_manager=concurrency_manager)
    return generate_latest(MAIN_REGISTRY)


async def build_public_health_snapshot(db: Any) -> dict[str, Any]:
    rows = await db.get_all_tokens_with_stats()
    now = datetime.now(timezone.utc)

    active_tokens = 0
    missing_at_tokens = 0
    expired_tokens = 0
    expiring_soon_tokens = 0
    banned_429_tokens = 0

    for row in rows:
        if bool(row.get("is_active")):
            active_tokens += 1
        if not str(row.get("at") or "").strip():
            missing_at_tokens += 1

        at_expires = _to_utc_datetime(row.get("at_expires"))
        if at_expires is not None:
            if at_expires <= now:
                expired_tokens += 1
            elif (at_expires - now).total_seconds() < 3600:
                expiring_soon_tokens += 1

        if (not bool(row.get("is_active"))) and str(row.get("ban_reason") or "").strip() == "429_rate_limit":
            banned_429_tokens += 1

    return {
        "backend_running": True,
        "has_active_tokens": active_tokens > 0,
        "total_tokens": len(rows),
        "active_tokens": active_tokens,
        "tokens_missing_at": missing_at_tokens,
        "tokens_expired": expired_tokens,
        "tokens_expiring_within_1h": expiring_soon_tokens,
        "banned_429_tokens": banned_429_tokens,
        "captcha_method": config.captcha_method,
        "remote_browser_configured": (
            config.captcha_method == "remote_browser"
            and bool((config.remote_browser_base_url or "").strip())
        ),
    }
