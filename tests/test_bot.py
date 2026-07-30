from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot as cache_module


def fake_message(message_id: int) -> SimpleNamespace:
    author = SimpleNamespace(id=1000 + message_id, bot=False)
    guild = SimpleNamespace(id=42)
    channel = SimpleNamespace(id=500)
    return SimpleNamespace(
        guild=guild,
        channel=channel,
        id=message_id,
        author=author,
        content=f"message {message_id}",
        attachments=[],
        embeds=[],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class PrefixCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_prefix_text_is_not_treated_as_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cache_module, "DATABASE_PATH", Path(directory) / "bot.sqlite3"):
                cache = cache_module.CacheBot()

            message = SimpleNamespace(
                guild=SimpleNamespace(id=42),
                content="-this-is-ordinary-text",
                author=SimpleNamespace(bot=False),
            )

            handled = await cache.handle_prefix_command(message)

        self.assertFalse(handled)


class MessagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def create_database(self, directory: str) -> cache_module.LogDatabase:
        database = cache_module.LogDatabase(Path(directory) / "bot.sqlite3")
        await database.connect()
        return database

    async def test_concurrent_message_writes_are_all_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = await self.create_database(directory)
            try:
                await asyncio.gather(
                    *(database.save_message(fake_message(message_id)) for message_id in range(200))
                )
                cursor = await database.db.execute(
                    "SELECT COUNT(*) AS count FROM messages WHERE guild_id = ?",
                    (42,),
                )
                message_count = (await cursor.fetchone())["count"]
                await cursor.close()
                cursor = await database.db.execute(
                    "SELECT COUNT(*) AS count FROM message_events WHERE guild_id = ?",
                    (42,),
                )
                event_count = (await cursor.fetchone())["count"]
                await cursor.close()
            finally:
                await database.close()

        self.assertEqual(message_count, 200)
        self.assertEqual(event_count, 200)

    async def test_transient_commit_error_is_retried_without_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = await self.create_database(directory)
            original_commit = database.db.commit
            commit_calls = 0

            async def flaky_commit() -> None:
                nonlocal commit_calls
                commit_calls += 1
                if commit_calls == 1:
                    raise sqlite3.OperationalError("database is locked")
                await original_commit()

            database.db.commit = flaky_commit
            try:
                with patch.object(cache_module, "MESSAGE_WRITE_RETRY_BASE_SECONDS", 0):
                    await database.save_message(fake_message(1))
                cursor = await database.db.execute(
                    "SELECT COUNT(*) AS count FROM messages WHERE message_id = 1"
                )
                message_count = (await cursor.fetchone())["count"]
                await cursor.close()
                cursor = await database.db.execute(
                    "SELECT COUNT(*) AS count FROM message_events WHERE message_id = 1"
                )
                event_count = (await cursor.fetchone())["count"]
                await cursor.close()
            finally:
                await database.close()

        self.assertEqual(commit_calls, 2)
        self.assertEqual(message_count, 1)
        self.assertEqual(event_count, 1)


if __name__ == "__main__":
    unittest.main()
