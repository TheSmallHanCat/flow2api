import tempfile
import unittest

from src.core.database import Database
from src.core.models import Token


class DailyStatsResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(
            Token(
                st="st-test",
                at="at-test",
                email="tester@example.com",
                name="tester",
            )
        )

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    async def test_dashboard_stats_ignore_stale_previous_day_counts(self):
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE token_stats
                SET today_image_count = 9,
                    today_video_count = 4,
                    today_error_count = 2,
                    today_date = '2000-01-01'
                WHERE token_id = ?
                """,
                (self.token_id,),
            )
            await conn.commit()

        stats = await self.db.get_dashboard_stats()
        token_rows = await self.db.get_all_tokens_with_stats()

        self.assertEqual(stats["today_images"], 0)
        self.assertEqual(stats["today_videos"], 0)
        self.assertEqual(stats["today_errors"], 0)
        self.assertEqual(token_rows[0]["today_image_count"], 0)
        self.assertEqual(token_rows[0]["today_video_count"], 0)
        self.assertEqual(token_rows[0]["today_error_count"], 0)

    async def test_cross_day_video_increment_resets_other_daily_counters(self):
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                UPDATE token_stats
                SET image_count = 12,
                    video_count = 3,
                    error_count = 5,
                    today_image_count = 7,
                    today_video_count = 2,
                    today_error_count = 1,
                    today_date = '2000-01-01'
                WHERE token_id = ?
                """,
                (self.token_id,),
            )
            await conn.commit()

        await self.db.increment_video_count(self.token_id)

        stats = await self.db.get_dashboard_stats()
        token_rows = await self.db.get_all_tokens_with_stats()
        token_row = token_rows[0]

        self.assertEqual(stats["today_images"], 0)
        self.assertEqual(stats["today_videos"], 1)
        self.assertEqual(stats["today_errors"], 0)
        self.assertEqual(token_row["image_count"], 12)
        self.assertEqual(token_row["video_count"], 4)
        self.assertEqual(token_row["today_image_count"], 0)
        self.assertEqual(token_row["today_video_count"], 1)
        self.assertEqual(token_row["today_error_count"], 0)


if __name__ == "__main__":
    unittest.main()
