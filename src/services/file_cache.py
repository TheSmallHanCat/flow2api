"""File caching service"""
import os
import asyncio
import hashlib
import time
import mimetypes
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.config import config
from ..core.logger import debug_logger


class FileCache:
    """File caching service for videos"""

    def __init__(
        self,
        cache_dir: str = "tmp",
        default_timeout: int = 7200,
        proxy_manager=None,
        flow_client=None,
    ):
        """
        Initialize file cache

        Args:
            cache_dir: Cache directory path
            default_timeout: Default cache timeout in seconds (default: 2 hours)
            proxy_manager: ProxyManager instance for downloading files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_timeout = max(0, int(default_timeout))
        self.proxy_manager = proxy_manager
        self.flow_client = flow_client
        self._cleanup_task = None
        self._download_locks: Dict[str, asyncio.Lock] = {}

    def _is_cleanup_disabled(self) -> bool:
        return self.default_timeout <= 0

    def _get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """读取当前请求链路里绑定的浏览器指纹。"""
        if not self.flow_client or not hasattr(self.flow_client, "get_request_fingerprint"):
            return None

        try:
            fingerprint = self.flow_client.get_request_fingerprint()
            if isinstance(fingerprint, dict) and fingerprint:
                return fingerprint
        except Exception as e:
            debug_logger.log_warning(f"Get request fingerprint failed: {str(e)}")

        return None

    async def _resolve_download_proxy(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """根据媒体类型解析下载代理地址。"""
        if isinstance(fingerprint, dict):
            fingerprint_proxy = str(fingerprint.get("proxy_url") or "").strip()
            if fingerprint_proxy:
                return fingerprint_proxy

        if not self.proxy_manager:
            return None

        try:
            # 媒体下载（图片/视频）优先使用独立的上传/下载代理
            if media_type in ("image", "video") and hasattr(self.proxy_manager, "get_media_proxy_url"):
                return await self.proxy_manager.get_media_proxy_url()

            # 其他下载走请求代理
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                return await self.proxy_manager.get_request_proxy_url()

            # 向后兼容旧实现
            if hasattr(self.proxy_manager, "get_proxy_url"):
                return await self.proxy_manager.get_proxy_url()
        except Exception as e:
            debug_logger.log_warning(f"Resolve download proxy failed: {str(e)}")

        return None

    def _guess_extension(self, url: str, media_type: str) -> str:
        """尽量保留原始扩展名，未知时回退到默认值。"""
        path = urlparse(url).path or ""
        guessed, _ = mimetypes.guess_type(path)
        suffix = Path(path).suffix.lower()

        if media_type == "video":
            if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
                return suffix
            if guessed == "video/webm":
                return ".webm"
            if guessed == "video/quicktime":
                return ".mov"
            return ".mp4"

        if media_type == "image":
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
                return suffix
            if guessed == "image/png":
                return ".png"
            if guessed == "image/webp":
                return ".webp"
            if guessed == "image/gif":
                return ".gif"
            if guessed == "image/avif":
                return ".avif"
            if guessed == "image/bmp":
                return ".bmp"
            return ".jpg"

        return suffix

    def _build_download_headers(
        self,
        media_type: str,
        fingerprint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """构建媒体下载请求头，优先复用当前打码浏览器指纹。"""
        headers = {
            "Accept": (
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                if media_type == "image"
                else "*/*"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Referer": "https://labs.google/",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        }

        if media_type == "image":
            headers["Sec-Fetch-Dest"] = "image"
        else:
            headers["Sec-Fetch-Dest"] = "video"

        if isinstance(fingerprint, dict):
            if fingerprint.get("user_agent"):
                headers["User-Agent"] = str(fingerprint["user_agent"])
            if fingerprint.get("accept_language"):
                headers["Accept-Language"] = str(fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = str(fingerprint["sec_ch_ua"])
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = str(fingerprint["sec_ch_ua_mobile"])
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = str(fingerprint["sec_ch_ua_platform"])

        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        return headers

    def _write_cached_content(self, file_path: Path, content: bytes):
        """先写临时文件，再原子替换，避免并发读到半截文件。"""
        temp_path = file_path.with_suffix(f"{file_path.suffix}.part")
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
            temp_path.replace(file_path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    async def start_cleanup_task(self):
        """Start background cleanup task"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup_task(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self):
        """Background task to clean up expired files"""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                await self._cleanup_expired_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_logger.log_error(
                    error_message=f"Cleanup task error: {str(e)}",
                    status_code=0,
                    response_text=""
                )

    async def _cleanup_expired_files(self):
        """Remove expired cache files"""
        try:
            if self._is_cleanup_disabled():
                return
            current_time = time.time()
            removed_count = 0

            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    # Check file age
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > self.default_timeout:
                        try:
                            file_path.unlink()
                            removed_count += 1
                        except Exception:
                            pass

            if removed_count > 0:
                debug_logger.log_info(f"Cleanup: removed {removed_count} expired cache files")

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cleanup expired files: {str(e)}",
                status_code=0,
                response_text=""
            )

    def _generate_cache_filename(self, url: str, media_type: str) -> str:
        """Generate unique filename for cached file"""
        # Use URL hash as filename
        url_hash = hashlib.md5(url.encode()).hexdigest()
        ext = self._guess_extension(url, media_type)

        return f"{url_hash}{ext}"

    def _normalize_cache_error(self, error: Exception) -> str:
        """整理缓存错误，避免将底层命令异常直接暴露给用户。"""
        if isinstance(error, FileNotFoundError):
            missing_name = Path(getattr(error, "filename", "") or "curl").name or "curl"
            return f"本机未安装 {missing_name}"

        message = str(error or "").strip()
        if not message:
            return "未知错误"

        if message.startswith("Failed to cache file:"):
            message = message.split(":", 1)[1].strip() or "未知错误"

        return message

    async def download_and_cache(self, url: str, media_type: str) -> str:
        """
        Download file from URL and cache it locally

        Args:
            url: File URL to download
            media_type: 'image' or 'video'

        Returns:
            Local cache filename
        """
        filename = self._generate_cache_filename(url, media_type)
        file_path = self.cache_dir / filename
        download_lock = self._download_locks.setdefault(filename, asyncio.Lock())

        async with download_lock:
            # Check if already cached and not expired
            if file_path.exists():
                if self._is_cleanup_disabled():
                    return filename
                file_age = time.time() - file_path.stat().st_mtime
                if file_age < self.default_timeout:
                    debug_logger.log_info(f"Cache hit: {filename}")
                    return filename
                try:
                    file_path.unlink()
                except Exception:
                    pass

            # Download file
            debug_logger.log_info(f"Downloading file from: {url}")

            fingerprint = self._get_request_fingerprint()
            proxy_url = await self._resolve_download_proxy(media_type, fingerprint=fingerprint)
            headers = self._build_download_headers(media_type, fingerprint=fingerprint)

            # Try method 1: curl_cffi with browser impersonation
            try:
                async with AsyncSession() as session:
                    response = await session.get(
                        url,
                        timeout=60,
                        proxy=proxy_url,
                        headers=headers,
                        impersonate="chrome120",
                        verify=False
                    )

                    if response.status_code == 200 and response.content:
                        self._write_cached_content(file_path, response.content)
                        debug_logger.log_info(
                            f"File cached (curl_cffi): {filename} ({len(response.content)} bytes)"
                        )
                        return filename
                    debug_logger.log_warning(
                        f"curl_cffi failed with HTTP {response.status_code}, trying wget..."
                    )

            except Exception as e:
                debug_logger.log_warning(f"curl_cffi failed: {str(e)}, trying wget...")

            # Try method 2: wget command
            try:
                import subprocess

                wget_cmd = [
                    "wget",
                    "-q",
                    "-O", str(file_path),
                    "--timeout=60",
                    "--tries=3",
                    f"--user-agent={headers.get('User-Agent', '')}",
                    f"--header=Accept: {headers.get('Accept', '*/*')}",
                    f"--header=Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                    f"--header=Connection: {headers.get('Connection', 'keep-alive')}",
                    f"--header=Referer: {headers.get('Referer', 'https://labs.google/')}",
                ]

                if "sec-ch-ua" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua: {headers['sec-ch-ua']}")
                if "sec-ch-ua-mobile" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}")
                if "sec-ch-ua-platform" in headers:
                    wget_cmd.append(f"--header=sec-ch-ua-platform: {headers['sec-ch-ua-platform']}")

                if proxy_url:
                    env = os.environ.copy()
                    env["http_proxy"] = proxy_url
                    env["https_proxy"] = proxy_url
                else:
                    env = None

                wget_cmd.append(url)
                result = subprocess.run(wget_cmd, capture_output=True, timeout=90, env=env)

                if result.returncode == 0 and file_path.exists():
                    file_size = file_path.stat().st_size
                    if file_size > 0:
                        debug_logger.log_info(f"File cached (wget): {filename} ({file_size} bytes)")
                        return filename
                    raise Exception("Downloaded file is empty")

                error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                debug_logger.log_warning(f"wget failed: {error_msg}, trying curl...")

            except FileNotFoundError:
                debug_logger.log_warning("wget not found, trying curl...")
            except Exception as e:
                debug_logger.log_warning(f"wget failed: {str(e)}, trying curl...")

            # Try method 3: system curl command
            try:
                import subprocess

                curl_cmd = [
                    "curl",
                    "-L",
                    "-s",
                    "-o", str(file_path),
                    "--max-time", "60",
                    "-H", f"Accept: {headers.get('Accept', '*/*')}",
                    "-H", f"Accept-Language: {headers.get('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')}",
                    "-H", f"Connection: {headers.get('Connection', 'keep-alive')}",
                    "-H", f"Referer: {headers.get('Referer', 'https://labs.google/')}",
                    "-A", headers.get("User-Agent", ""),
                ]

                if "sec-ch-ua" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua: {headers['sec-ch-ua']}"])
                if "sec-ch-ua-mobile" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua-mobile: {headers['sec-ch-ua-mobile']}"])
                if "sec-ch-ua-platform" in headers:
                    curl_cmd.extend(["-H", f"sec-ch-ua-platform: {headers['sec-ch-ua-platform']}"])
                if proxy_url:
                    curl_cmd.extend(["-x", proxy_url])

                curl_cmd.append(url)
                result = subprocess.run(curl_cmd, capture_output=True, timeout=90)

                if result.returncode == 0 and file_path.exists():
                    file_size = file_path.stat().st_size
                    if file_size > 0:
                        debug_logger.log_info(f"File cached (curl): {filename} ({file_size} bytes)")
                        return filename
                    raise Exception("Downloaded file is empty")

                error_msg = result.stderr.decode("utf-8", errors="ignore") if result.stderr else "Unknown error"
                raise Exception(f"curl command failed: {error_msg}")

            except FileNotFoundError as e:
                normalized_error = self._normalize_cache_error(e)
                debug_logger.log_error(
                    error_message=f"Failed to download file: {str(e)}",
                    status_code=0,
                    response_text=str(e)
                )
                raise Exception(normalized_error) from e
            except Exception as e:
                normalized_error = self._normalize_cache_error(e)
                debug_logger.log_error(
                    error_message=f"Failed to download file: {str(e)}",
                    status_code=0,
                    response_text=str(e)
                )
                raise Exception(normalized_error) from e

    async def cache_base64_image(self, base64_data: str, resolution: str = "") -> str:
        """
        Cache base64 encoded image data to local file

        Args:
            base64_data: Base64 encoded image data (without data:image/... prefix)
            resolution: Resolution info for filename (e.g., "4K", "2K")

        Returns:
            Local cache filename
        """
        import base64
        import uuid

        # Generate unique filename
        unique_id = hashlib.md5(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()
        suffix = f"_{resolution}" if resolution else ""
        filename = f"{unique_id}{suffix}.jpg"
        file_path = self.cache_dir / filename

        try:
            # Decode base64 and save to file
            image_data = base64.b64decode(base64_data)
            with open(file_path, 'wb') as f:
                f.write(image_data)
            debug_logger.log_info(f"Base64 image cached: {filename} ({len(image_data)} bytes)")
            return filename
        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to cache base64 image: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise Exception(f"Failed to cache base64 image: {str(e)}")

    def get_cache_path(self, filename: str) -> Path:
        """Get full path to cached file"""
        return self.cache_dir / filename

    def set_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        self.default_timeout = max(0, int(timeout))
        debug_logger.log_info(f"Cache timeout updated to {timeout} seconds")

    def get_timeout(self) -> int:
        """Get current cache timeout"""
        return self.default_timeout

    async def clear_all(self):
        """Clear all cached files"""
        try:
            removed_count = 0
            for file_path in self.cache_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        removed_count += 1
                    except Exception:
                        pass

            debug_logger.log_info(f"Cache cleared: removed {removed_count} files")
            return removed_count

        except Exception as e:
            debug_logger.log_error(
                error_message=f"Failed to clear cache: {str(e)}",
                status_code=0,
                response_text=""
            )
            raise
