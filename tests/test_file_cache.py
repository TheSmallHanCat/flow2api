import asyncio
import os
import time

from src.services.file_cache import FileCache


def test_cleanup_keeps_files_when_timeout_is_zero(tmp_path):
    cache = FileCache(cache_dir=str(tmp_path), default_timeout=0)
    cached_file = tmp_path / "expired.jpg"
    cached_file.write_bytes(b"cached")

    expired_at = time.time() - 3600
    os.utime(cached_file, (expired_at, expired_at))

    asyncio.run(cache._cleanup_expired_files())

    assert cached_file.exists()


def test_cleanup_removes_files_when_timeout_is_positive(tmp_path):
    cache = FileCache(cache_dir=str(tmp_path), default_timeout=1)
    cached_file = tmp_path / "expired.jpg"
    cached_file.write_bytes(b"cached")

    expired_at = time.time() - 3600
    os.utime(cached_file, (expired_at, expired_at))

    asyncio.run(cache._cleanup_expired_files())

    assert not cached_file.exists()
