import asyncio
import time

from src.core.database import Database


def test_uploaded_image_cache_crud_roundtrip(tmp_path):
    db = Database(db_path=str(tmp_path / "flow.db"))
    asyncio.run(db.init_db())

    asyncio.run(
        db.upsert_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
            media_id="media-1",
        )
    )

    entry = asyncio.run(
        db.get_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    assert entry is not None
    assert entry["media_id"] == "media-1"

    asyncio.run(
        db.upsert_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
            media_id="media-2",
        )
    )

    updated_entry = asyncio.run(
        db.get_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    assert updated_entry is not None
    assert updated_entry["media_id"] == "media-2"

    asyncio.run(
        db.delete_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )

    deleted_entry = asyncio.run(
        db.get_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    assert deleted_entry is None


def test_uploaded_image_cache_touch_updates_last_used_at(tmp_path):
    db = Database(db_path=str(tmp_path / "flow.db"))
    asyncio.run(db.init_db())

    asyncio.run(
        db.upsert_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
            media_id="media-1",
        )
    )
    before_touch = asyncio.run(
        db.get_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )

    time.sleep(1.1)

    asyncio.run(
        db.touch_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )
    after_touch = asyncio.run(
        db.get_uploaded_image_cache(
            email="user@example.com",
            project_id="project-1",
            image_hash="hash-1",
            aspect_ratio="IMAGE_ASPECT_RATIO_SQUARE",
        )
    )

    assert before_touch is not None
    assert after_touch is not None
    assert after_touch["last_used_at"] >= before_touch["last_used_at"]
