from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cache")

JST = timezone(timedelta(hours=9))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SYNC_GUILD_ID = os.getenv("SYNC_GUILD_ID")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "180") or "0")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "-")
SERVER_LOG_CHANNEL_NAME = os.getenv("SERVER_LOG_CHANNEL_NAME", "server-log")
LEGACY_LOG_CHANNEL_NAMES = {"cache-logs", "bot-logs"}
AUTO_SYNC_ALL_GUILDS = os.getenv("AUTO_SYNC_ALL_GUILDS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
OWNER_IDS = {
    int(value.strip())
    for value in os.getenv("OWNER_IDS", "").split(",")
    if value.strip().isdigit()
}

INVITE_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)
LINK_RE = re.compile(r"https?://", re.IGNORECASE)
ZALGO_RE = re.compile(r"[\u0300-\u036f]{4,}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_guild_ids() -> list[int]:
    raw = ",".join(
        value
        for value in [os.getenv("SYNC_GUILD_IDS"), os.getenv("SYNC_GUILD_ID")]
        if value
    )
    ids: list[int] = []
    for value in raw.split(","):
        value = value.strip()
        if value.isdigit():
            ids.append(int(value))
    return list(dict.fromkeys(ids))


def to_jst_text(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def shorten(text: str | None, limit: int = 900) -> str:
    if not text:
        return "(no content)"
    cleaned = text.replace("`", "'")
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def code_block(text: str | None, limit: int = 1500) -> str:
    body = text if text else "(no content)"
    body = body.replace("```", "`\u200b``")
    if len(body) > limit:
        body = body[: limit - 3] + "..."
    return f"```\n{body}\n```"


def allowed_text(value: bool) -> str:
    return "Allowed" if value else "Denied"


def channel_name(channel: discord.abc.GuildChannel | None) -> str:
    return channel.name if channel else "Unknown"


def actor_label(user: discord.abc.User | None) -> str:
    return user_label(user) if user else "Unknown"


def channel_label(channel: discord.abc.GuildChannel | None) -> str:
    if isinstance(channel, discord.TextChannel):
        return f"#{channel.name}"
    return channel.name if channel else "Unknown"


def permission_display_name(name: str) -> str:
    aliases = {
        "manage_emojis_and_stickers": "manage_emojis",
        "use_external_emojis": "external_emojis",
        "use_external_stickers": "external_stickers",
        "use_application_commands": "use_slash_commands",
        "use_embedded_activities": "start_embedded_activities",
    }
    return aliases.get(name, name)


def role_permission_lines(
    permissions: discord.Permissions,
    names: list[str] | None = None,
) -> list[str]:
    values = dict(iter(permissions))
    selected = names or [name for name, value in permissions if value]
    return [
        f"{permission_display_name(name)}: {allowed_text(bool(values.get(name)))}"
        for name in selected
    ]


def attachment_urls(message: discord.Message) -> list[str]:
    return [attachment.url for attachment in message.attachments]


def image_attachment_url(message: discord.Message) -> str | None:
    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/"):
            return attachment.url
        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return attachment.url
    return None


def attachment_summary(message: discord.Message, limit: int = 700) -> str:
    if not message.attachments:
        return "-"
    lines = [f"[{attachment.filename}]({attachment.url})" for attachment in message.attachments]
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def user_label(user: discord.abc.User) -> str:
    return f"{user} ({user.id})"


def parse_duration(text: str) -> int | None:
    matches = re.findall(r"(\d+)\s*([smhd])", text.lower())
    if not matches:
        return None
    total = 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for amount, unit in matches:
        total += int(amount) * multipliers[unit]
    return total if total > 0 else None


def split_after_mention(content: str) -> str:
    parts = content.split(maxsplit=2)
    return parts[2].strip() if len(parts) >= 3 else "理由なし"


async def is_log_manager(interaction: discord.Interaction) -> bool:
    if interaction.user.id in OWNER_IDS:
        return True
    if not isinstance(interaction.user, discord.Member):
        return False
    permissions = interaction.user.guild_permissions
    return (
        permissions.administrator
        or permissions.manage_guild
        or any(role.name in {"Cacheスタッフ", "Bot管理スタッフ"} for role in interaction.user.roles)
    )


def manager_only() -> app_commands.Check:
    return app_commands.check(is_log_manager)


class LogDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER,
                mod_log_channel_id INTEGER,
                report_channel_id INTEGER,
                staff_role_id INTEGER,
                voice_logging_enabled INTEGER NOT NULL DEFAULT 1,
                message_logging_enabled INTEGER NOT NULL DEFAULT 1,
                message_edit_logging_enabled INTEGER NOT NULL DEFAULT 1,
                message_delete_logging_enabled INTEGER NOT NULL DEFAULT 1,
                reaction_logging_enabled INTEGER NOT NULL DEFAULT 1,
                voice_join_logging_enabled INTEGER NOT NULL DEFAULT 1,
                voice_leave_logging_enabled INTEGER NOT NULL DEFAULT 1,
                voice_move_logging_enabled INTEGER NOT NULL DEFAULT 1,
                member_join_logging_enabled INTEGER NOT NULL DEFAULT 1,
                member_leave_logging_enabled INTEGER NOT NULL DEFAULT 1,
                role_create_logging_enabled INTEGER NOT NULL DEFAULT 1,
                role_update_logging_enabled INTEGER NOT NULL DEFAULT 1,
                channel_update_logging_enabled INTEGER NOT NULL DEFAULT 1,
                moderation_logging_enabled INTEGER NOT NULL DEFAULT 1,
                report_logging_enabled INTEGER NOT NULL DEFAULT 1,
                command_logging_enabled INTEGER NOT NULL DEFAULT 1,
                notify_events_enabled INTEGER NOT NULL DEFAULT 1,
                automod_enabled INTEGER NOT NULL DEFAULT 0,
                anti_spam_enabled INTEGER NOT NULL DEFAULT 0,
                anti_invite_enabled INTEGER NOT NULL DEFAULT 0,
                anti_link_enabled INTEGER NOT NULL DEFAULT 0,
                anti_mention_enabled INTEGER NOT NULL DEFAULT 0,
                anti_zalgo_enabled INTEGER NOT NULL DEFAULT 0,
                raid_guard_enabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT,
                attachment_urls TEXT NOT NULL DEFAULT '[]',
                embeds_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT,
                PRIMARY KEY (guild_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS message_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                author_id INTEGER,
                author_name TEXT,
                event_type TEXT NOT NULL,
                before_content TEXT,
                after_content TEXT,
                attachment_urls TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS voice_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                before_channel_id INTEGER,
                before_channel_name TEXT,
                after_channel_id INTEGER,
                after_channel_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reaction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                user_name TEXT,
                emoji TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                actor_id INTEGER,
                actor_name TEXT,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mod_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                moderator_id INTEGER,
                moderator_name TEXT,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                duration_seconds INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                reporter_name TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_by_id INTEGER,
                resolved_by_name TEXT,
                resolved_at TEXT,
                resolution_note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_message_events_guild_created
                ON message_events (guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_message_events_author
                ON message_events (guild_id, author_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_voice_events_guild_created
                ON voice_events (guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_voice_events_user
                ON voice_events (guild_id, user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_reaction_events_guild_created
                ON reaction_events (guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_reaction_events_message
                ON reaction_events (guild_id, message_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_bot_events_guild_created
                ON bot_events (guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_mod_cases_guild_target
                ON mod_cases (guild_id, target_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_reports_guild_created
                ON reports (guild_id, created_at);
            """
        )
        await self.ensure_settings_columns()
        await self.ensure_report_columns()
        await self.db.commit()

    async def ensure_settings_columns(self) -> None:
        cursor = await self.db.execute("PRAGMA table_info(guild_settings)")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        migrations = {
            "mod_log_channel_id": "INTEGER",
            "report_channel_id": "INTEGER",
            "staff_role_id": "INTEGER",
            "message_edit_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "message_delete_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "reaction_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "voice_join_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "voice_leave_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "voice_move_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "member_join_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "member_leave_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "role_create_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "role_update_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "channel_update_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "moderation_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "report_logging_enabled": "INTEGER NOT NULL DEFAULT 1",
            "automod_enabled": "INTEGER NOT NULL DEFAULT 0",
            "anti_spam_enabled": "INTEGER NOT NULL DEFAULT 0",
            "anti_invite_enabled": "INTEGER NOT NULL DEFAULT 0",
            "anti_link_enabled": "INTEGER NOT NULL DEFAULT 0",
            "anti_mention_enabled": "INTEGER NOT NULL DEFAULT 0",
            "anti_zalgo_enabled": "INTEGER NOT NULL DEFAULT 0",
            "raid_guard_enabled": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await self.db.execute(
                    f"ALTER TABLE guild_settings ADD COLUMN {column} {definition}"
                )

    async def ensure_report_columns(self) -> None:
        cursor = await self.db.execute("PRAGMA table_info(reports)")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        migrations = {
            "status": "TEXT NOT NULL DEFAULT 'open'",
            "resolved_by_id": "INTEGER",
            "resolved_by_name": "TEXT",
            "resolved_at": "TEXT",
            "resolution_note": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await self.db.execute(
                    f"ALTER TABLE reports ADD COLUMN {column} {definition}"
                )

    async def close(self) -> None:
        await self.db.close()

    async def ensure_guild(self, guild_id: int) -> None:
        timestamp = now_iso()
        await self.db.execute(
            """
            INSERT OR IGNORE INTO guild_settings (guild_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (guild_id, timestamp, timestamp),
        )
        await self.db.commit()

    async def settings(self, guild_id: int) -> aiosqlite.Row:
        await self.ensure_guild(guild_id)
        cursor = await self.db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            """
            UPDATE guild_settings
            SET log_channel_id = ?, updated_at = ?
            WHERE guild_id = ?
            """,
            (channel_id, now_iso(), guild_id),
        )
        await self.db.commit()

    async def set_setup_channels(
        self,
        guild_id: int,
        log_channel_id: int,
        mod_log_channel_id: int,
        report_channel_id: int,
        staff_role_id: int,
    ) -> None:
        await self.ensure_guild(guild_id)
        await self.db.execute(
            """
            UPDATE guild_settings
            SET log_channel_id = ?,
                mod_log_channel_id = ?,
                report_channel_id = ?,
                staff_role_id = ?,
                voice_logging_enabled = 1,
                message_logging_enabled = 1,
                message_edit_logging_enabled = 1,
                message_delete_logging_enabled = 1,
                reaction_logging_enabled = 1,
                voice_join_logging_enabled = 1,
                voice_leave_logging_enabled = 1,
                voice_move_logging_enabled = 1,
                member_join_logging_enabled = 1,
                member_leave_logging_enabled = 1,
                role_create_logging_enabled = 1,
                role_update_logging_enabled = 1,
                channel_update_logging_enabled = 1,
                moderation_logging_enabled = 1,
                report_logging_enabled = 1,
                command_logging_enabled = 1,
                notify_events_enabled = 1,
                automod_enabled = 1,
                anti_spam_enabled = 1,
                anti_invite_enabled = 1,
                anti_mention_enabled = 1,
                anti_zalgo_enabled = 1,
                updated_at = ?
            WHERE guild_id = ?
            """,
            (
                log_channel_id,
                mod_log_channel_id,
                report_channel_id,
                staff_role_id,
                now_iso(),
                guild_id,
            ),
        )
        await self.db.commit()

    async def set_toggle(self, guild_id: int, column: str, enabled: bool) -> None:
        if column not in {
            "voice_logging_enabled",
            "message_logging_enabled",
            "message_edit_logging_enabled",
            "message_delete_logging_enabled",
            "reaction_logging_enabled",
            "voice_join_logging_enabled",
            "voice_leave_logging_enabled",
            "voice_move_logging_enabled",
            "member_join_logging_enabled",
            "member_leave_logging_enabled",
            "role_create_logging_enabled",
            "role_update_logging_enabled",
            "channel_update_logging_enabled",
            "moderation_logging_enabled",
            "report_logging_enabled",
            "command_logging_enabled",
            "notify_events_enabled",
            "automod_enabled",
            "anti_spam_enabled",
            "anti_invite_enabled",
            "anti_link_enabled",
            "anti_mention_enabled",
            "anti_zalgo_enabled",
            "raid_guard_enabled",
        }:
            raise ValueError(f"Invalid toggle column: {column}")
        await self.ensure_guild(guild_id)
        await self.db.execute(
            f"UPDATE guild_settings SET {column} = ?, updated_at = ? WHERE guild_id = ?",
            (1 if enabled else 0, now_iso(), guild_id),
        )
        await self.db.commit()

    async def save_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        await self.db.execute(
            """
            INSERT INTO messages (
                guild_id, channel_id, message_id, author_id, author_name,
                content, attachment_urls, embeds_count, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, message_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                author_id = excluded.author_id,
                author_name = excluded.author_name,
                content = excluded.content,
                attachment_urls = excluded.attachment_urls,
                embeds_count = excluded.embeds_count,
                updated_at = excluded.updated_at,
                deleted_at = NULL
            """,
            (
                message.guild.id,
                message.channel.id,
                message.id,
                message.author.id,
                str(message.author),
                message.content,
                json.dumps(attachment_urls(message), ensure_ascii=False),
                len(message.embeds),
                message.created_at.astimezone(timezone.utc).isoformat(),
                now_iso(),
            ),
        )
        await self.record_message_event(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=message.author.id,
            author_name=str(message.author),
            event_type="create",
            before_content=None,
            after_content=message.content,
            attachments=attachment_urls(message),
        )

    async def update_message_from_raw_edit(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int | None,
        author_name: str | None,
        before_content: str | None,
        after_content: str | None,
        attachments: list[str],
        embeds_count: int,
        record_event: bool = True,
    ) -> None:
        timestamp = now_iso()
        if author_id is None:
            await self.db.execute(
                """
                UPDATE messages
                SET channel_id = ?, content = ?, attachment_urls = ?,
                    embeds_count = ?, updated_at = ?, deleted_at = NULL
                WHERE guild_id = ? AND message_id = ?
                """,
                (
                    channel_id,
                    after_content,
                    json.dumps(attachments, ensure_ascii=False),
                    embeds_count,
                    timestamp,
                    guild_id,
                    message_id,
                ),
            )
        else:
            await self.db.execute(
                """
                INSERT INTO messages (
                    guild_id, channel_id, message_id, author_id, author_name,
                    content, attachment_urls, embeds_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, message_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    author_id = excluded.author_id,
                    author_name = excluded.author_name,
                    content = excluded.content,
                    attachment_urls = excluded.attachment_urls,
                    embeds_count = excluded.embeds_count,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    author_id,
                    author_name or str(author_id),
                    after_content,
                    json.dumps(attachments, ensure_ascii=False),
                    embeds_count,
                    timestamp,
                    timestamp,
                ),
            )
        if record_event:
            await self.record_message_event(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                author_id=author_id,
                author_name=author_name,
                event_type="edit",
                before_content=before_content,
                after_content=after_content,
                attachments=attachments,
            )
        else:
            await self.db.commit()

    async def get_message(self, guild_id: int, message_id: int) -> Optional[aiosqlite.Row]:
        cursor = await self.db.execute(
            "SELECT * FROM messages WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def mark_message_deleted(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        cached_message: discord.Message | None = None,
        record_event: bool = True,
    ) -> Optional[aiosqlite.Row]:
        stored = await self.get_message(guild_id, message_id)
        timestamp = now_iso()
        await self.db.execute(
            """
            UPDATE messages
            SET deleted_at = ?, updated_at = ?
            WHERE guild_id = ? AND message_id = ?
            """,
            (timestamp, timestamp, guild_id, message_id),
        )

        author_id: int | None = None
        author_name: str | None = None
        before_content: str | None = None
        attachments: list[str] = []

        if cached_message:
            author_id = cached_message.author.id
            author_name = str(cached_message.author)
            before_content = cached_message.content
            attachments = attachment_urls(cached_message)
        elif stored:
            author_id = stored["author_id"]
            author_name = stored["author_name"]
            before_content = stored["content"]
            attachments = json.loads(stored["attachment_urls"] or "[]")

        if record_event:
            await self.record_message_event(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message_id,
                author_id=author_id,
                author_name=author_name,
                event_type="delete",
                before_content=before_content,
                after_content=None,
                attachments=attachments,
            )
        else:
            await self.db.commit()
        return stored

    async def record_message_event(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int | None,
        author_name: str | None,
        event_type: str,
        before_content: str | None,
        after_content: str | None,
        attachments: list[str],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO message_events (
                guild_id, channel_id, message_id, author_id, author_name,
                event_type, before_content, after_content, attachment_urls, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                author_id,
                author_name,
                event_type,
                before_content,
                after_content,
                json.dumps(attachments, ensure_ascii=False),
                now_iso(),
            ),
        )
        await self.db.commit()

    async def record_voice_event(
        self,
        guild_id: int,
        member: discord.Member,
        event_type: str,
        before_channel: discord.VoiceChannel | discord.StageChannel | None,
        after_channel: discord.VoiceChannel | discord.StageChannel | None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO voice_events (
                guild_id, user_id, user_name, event_type,
                before_channel_id, before_channel_name,
                after_channel_id, after_channel_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                member.id,
                str(member),
                event_type,
                before_channel.id if before_channel else None,
                before_channel.name if before_channel else None,
                after_channel.id if after_channel else None,
                after_channel.name if after_channel else None,
                now_iso(),
            ),
        )
        await self.db.commit()

    async def record_reaction_event(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user: discord.abc.User | None,
        emoji: str,
        event_type: str,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO reaction_events (
                guild_id, channel_id, message_id, user_id, user_name,
                emoji, event_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                user.id if user else None,
                str(user) if user else None,
                emoji,
                event_type,
                now_iso(),
            ),
        )
        await self.db.commit()

    async def record_bot_event(
        self,
        guild_id: int | None,
        actor: discord.abc.User | None,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO bot_events (
                guild_id, actor_id, actor_name, event_type, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor.id if actor else None,
                str(actor) if actor else None,
                event_type,
                json.dumps(details or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
        await self.db.commit()

    async def add_mod_case(
        self,
        guild_id: int,
        target: discord.abc.User,
        moderator: discord.abc.User | None,
        action: str,
        reason: str,
        duration_seconds: int | None = None,
    ) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO mod_cases (
                guild_id, target_id, target_name, moderator_id, moderator_name,
                action, reason, duration_seconds, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                target.id,
                str(target),
                moderator.id if moderator else None,
                str(moderator) if moderator else None,
                action,
                reason,
                duration_seconds,
                now_iso(),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def add_report(
        self,
        guild_id: int,
        reporter: discord.abc.User,
        target: discord.abc.User,
        reason: str,
    ) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO reports (
                guild_id, reporter_id, reporter_name, target_id, target_name,
                reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                reporter.id,
                str(reporter),
                target.id,
                str(target),
                reason,
                now_iso(),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def get_mod_cases(
        self,
        guild_id: int,
        target_id: int,
        limit: int = 10,
    ) -> list[aiosqlite.Row]:
        cursor = await self.db.execute(
            """
            SELECT * FROM mod_cases
            WHERE guild_id = ? AND target_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (guild_id, target_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def get_mod_case(self, guild_id: int, case_id: int) -> aiosqlite.Row | None:
        cursor = await self.db.execute(
            "SELECT * FROM mod_cases WHERE guild_id = ? AND id = ?",
            (guild_id, case_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def remove_warning_case(self, guild_id: int, case_id: int) -> aiosqlite.Row | None:
        row = await self.get_mod_case(guild_id, case_id)
        if not row or row["action"] != "warn":
            return None
        await self.db.execute(
            "DELETE FROM mod_cases WHERE guild_id = ? AND id = ? AND action = 'warn'",
            (guild_id, case_id),
        )
        await self.db.commit()
        return row

    async def clear_warnings(self, guild_id: int, target_id: int) -> int:
        cursor = await self.db.execute(
            """
            DELETE FROM mod_cases
            WHERE guild_id = ? AND target_id = ? AND action = 'warn'
            """,
            (guild_id, target_id),
        )
        count = max(cursor.rowcount, 0)
        await cursor.close()
        await self.db.commit()
        return count

    async def get_report(self, guild_id: int, report_id: int) -> aiosqlite.Row | None:
        cursor = await self.db.execute(
            "SELECT * FROM reports WHERE guild_id = ? AND id = ?",
            (guild_id, report_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def update_report_status(
        self,
        guild_id: int,
        report_id: int,
        status: str,
        actor: discord.abc.User,
        note: str,
    ) -> aiosqlite.Row | None:
        row = await self.get_report(guild_id, report_id)
        if not row:
            return None
        await self.db.execute(
            """
            UPDATE reports
            SET status = ?,
                resolved_by_id = ?,
                resolved_by_name = ?,
                resolved_at = ?,
                resolution_note = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                status,
                actor.id,
                str(actor),
                now_iso(),
                note,
                guild_id,
                report_id,
            ),
        )
        await self.db.commit()
        return row

    async def search_message_events(
        self,
        guild_id: int,
        event_types: list[str],
        user_id: int | None,
        channel_id: int | None,
        keyword: str | None,
        limit: int,
    ) -> list[aiosqlite.Row]:
        clauses = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(event_types)
        if user_id:
            clauses.append("author_id = ?")
            params.append(user_id)
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if keyword:
            clauses.append("(before_content LIKE ? OR after_content LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        params.append(limit)
        cursor = await self.db.execute(
            f"""
            SELECT * FROM message_events
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def search_voice_events(
        self,
        guild_id: int,
        user_id: int | None,
        keyword: str | None,
        limit: int,
    ) -> list[aiosqlite.Row]:
        clauses = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if keyword:
            clauses.append(
                "(before_channel_name LIKE ? OR after_channel_name LIKE ? OR user_name LIKE ?)"
            )
            like = f"%{keyword}%"
            params.extend([like, like, like])
        params.append(limit)
        cursor = await self.db.execute(
            f"""
            SELECT * FROM voice_events
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def search_reaction_events(
        self,
        guild_id: int,
        user_id: int | None,
        channel_id: int | None,
        keyword: str | None,
        limit: int,
    ) -> list[aiosqlite.Row]:
        clauses = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if channel_id:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if keyword:
            clauses.append("(emoji LIKE ? OR user_name LIKE ? OR event_type LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        params.append(limit)
        cursor = await self.db.execute(
            f"""
            SELECT * FROM reaction_events
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def stats(self, guild_id: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for name, table in {
            "messages": "messages",
            "message_events": "message_events",
            "voice_events": "voice_events",
            "reaction_events": "reaction_events",
            "bot_events": "bot_events",
            "mod_cases": "mod_cases",
            "reports": "reports",
        }.items():
            cursor = await self.db.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            result[name] = int(row["count"])
        return result

    async def cleanup_old_logs(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        deleted = 0
        for table, column in {
            "message_events": "created_at",
            "voice_events": "created_at",
            "reaction_events": "created_at",
            "bot_events": "created_at",
            "mod_cases": "created_at",
            "reports": "created_at",
            "messages": "updated_at",
        }.items():
            cursor = await self.db.execute(
                f"DELETE FROM {table} WHERE {column} < ?",
                (cutoff,),
            )
            deleted += max(cursor.rowcount, 0)
            await cursor.close()
        await self.db.commit()
        return deleted

    async def export_events(
        self,
        guild_id: int,
        kind: str,
        days: int,
        limit: int = 5000,
    ) -> tuple[list[str], list[aiosqlite.Row]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        if kind == "voice":
            headers = [
                "created_at",
                "user_id",
                "user_name",
                "event_type",
                "before_channel_name",
                "after_channel_name",
            ]
            query = """
                SELECT created_at, user_id, user_name, event_type,
                       before_channel_name, after_channel_name
                FROM voice_events
                WHERE guild_id = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """
        elif kind == "reactions":
            headers = [
                "created_at",
                "event_type",
                "channel_id",
                "message_id",
                "user_id",
                "user_name",
                "emoji",
            ]
            query = """
                SELECT created_at, event_type, channel_id, message_id,
                       user_id, user_name, emoji
                FROM reaction_events
                WHERE guild_id = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """
        else:
            headers = [
                "created_at",
                "event_type",
                "channel_id",
                "message_id",
                "author_id",
                "author_name",
                "before_content",
                "after_content",
                "attachment_urls",
            ]
            query = """
                SELECT created_at, event_type, channel_id, message_id,
                       author_id, author_name, before_content, after_content,
                       attachment_urls
                FROM message_events
                WHERE guild_id = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
            """
        cursor = await self.db.execute(query, (guild_id, since, limit))
        rows = await cursor.fetchall()
        await cursor.close()
        return headers, rows


class CacheBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        intents.reactions = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = LogDatabase(DATABASE_PATH)
        self.cleanup_task: asyncio.Task[None] | None = None
        self.recent_messages: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(
            lambda: deque(maxlen=10)
        )
        self.recent_joins: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=30))
        self.synced_guild_ids: set[int] = set()

    async def setup_hook(self) -> None:
        await self.db.connect()
        setup_commands(self)
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())
        guild_ids = sync_guild_ids()
        if guild_ids:
            for guild_id in guild_ids:
                await self.sync_commands_for_guild_id(guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global commands", len(synced))

    async def sync_commands_for_guild_id(self, guild_id: int) -> None:
        if guild_id in self.synced_guild_ids:
            return
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException:
            logger.exception("Failed to sync commands to guild %s", guild_id)
            return
        self.synced_guild_ids.add(guild_id)
        logger.info("Synced %s commands to guild %s", len(synced), guild_id)

    async def close(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
        await self.db.close()
        await super().close()

    async def cleanup_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                deleted = await self.db.cleanup_old_logs(RETENTION_DAYS)
                if deleted:
                    logger.info("Cleaned up %s old log rows", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to clean up old logs")
            await asyncio.sleep(60 * 60 * 24)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)
        for guild in self.guilds:
            await self.db.ensure_guild(guild.id)
            if AUTO_SYNC_ALL_GUILDS:
                await self.sync_commands_for_guild_id(guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.ensure_guild(guild.id)
        await self.db.record_bot_event(guild.id, self.user, "guild_join", {"name": guild.name})
        if AUTO_SYNC_ALL_GUILDS:
            await self.sync_commands_for_guild_id(guild.id)

    async def on_member_join(self, member: discord.Member) -> None:
        settings = await self.db.settings(member.guild.id)
        now = time.monotonic()
        joins = self.recent_joins[member.guild.id]
        joins.append(now)
        recent_joins = [ts for ts in joins if now - ts <= 60]

        if settings["member_join_logging_enabled"]:
            await self.send_server_log(
                member.guild,
                (
                    f"{user_label(member)} has joined the server! "
                    f"There are {member.guild.member_count or len(member.guild.members)} members."
                ),
            )

        if settings["raid_guard_enabled"] and len(recent_joins) >= 8:
            reason = "60秒以内に8人以上が参加したため"
            try:
                await member.timeout(
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                    reason=f"Raid Guard: {reason}",
                )
                await self.add_case_and_notify(
                    member.guild,
                    member,
                    self.user,
                    "raid_guard_timeout",
                    reason,
                    600,
                )
            except discord.Forbidden:
                logger.warning("Cannot timeout raid guard member %s", member.id)
            except discord.HTTPException:
                logger.exception("Failed to apply raid guard timeout")

    async def on_member_remove(self, member: discord.Member) -> None:
        settings = await self.db.settings(member.guild.id)
        if not settings["member_leave_logging_enabled"]:
            return
        roles = ", ".join(role.name for role in member.roles) or "@everyone"
        await self.send_server_log(
            member.guild,
            (
                f"{user_label(member)} has left the server! "
                f"There are {member.guild.member_count or len(member.guild.members)} members. "
                f"Roles: {roles}"
            ),
        )

    async def log_channel_for(self, guild: discord.Guild) -> discord.TextChannel | None:
        settings = await self.db.settings(guild.id)
        server_log = discord.utils.get(guild.text_channels, name=SERVER_LOG_CHANNEL_NAME)
        if server_log:
            return server_log
        channel_id = settings["log_channel_id"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            if channel.name == SERVER_LOG_CHANNEL_NAME:
                return channel
            if channel.name in LEGACY_LOG_CHANNEL_NAMES:
                try:
                    await channel.edit(
                        name=SERVER_LOG_CHANNEL_NAME,
                        reason="Cache server-log migration",
                    )
                    return channel
                except discord.Forbidden:
                    logger.warning("Cannot rename legacy log channel %s", channel.id)
                except discord.HTTPException:
                    logger.exception("Failed to rename legacy log channel %s", channel.id)
        for legacy_name in LEGACY_LOG_CHANNEL_NAMES:
            legacy_channel = discord.utils.get(guild.text_channels, name=legacy_name)
            if legacy_channel:
                try:
                    await legacy_channel.edit(
                        name=SERVER_LOG_CHANNEL_NAME,
                        reason="Cache server-log migration",
                    )
                    return legacy_channel
                except discord.Forbidden:
                    logger.warning("Cannot rename legacy log channel %s", legacy_channel.id)
                except discord.HTTPException:
                    logger.exception(
                        "Failed to rename legacy log channel %s",
                        legacy_channel.id,
                    )
        return None

    async def send_server_log(self, guild: discord.Guild, content: str) -> None:
        settings = await self.db.settings(guild.id)
        if not settings["notify_events_enabled"]:
            return
        channel = await self.log_channel_for(guild)
        if not channel:
            return
        try:
            for start in range(0, len(content), 1900):
                await channel.send(content[start : start + 1900])
        except discord.Forbidden:
            logger.warning("Cannot send logs to channel %s in guild %s", channel.id, guild.id)
        except discord.HTTPException:
            logger.exception("Failed to send server log")

    async def notify(self, guild: discord.Guild, embed: discord.Embed) -> None:
        settings = await self.db.settings(guild.id)
        if not settings["notify_events_enabled"]:
            return
        channel = await self.log_channel_for(guild)
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send logs to channel %s in guild %s", channel.id, guild.id)
        except discord.HTTPException:
            logger.exception("Failed to send log notification")

    async def record_admin_action(
        self,
        guild_id: int,
        actor: discord.abc.User,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        settings = await self.db.settings(guild_id)
        if settings["command_logging_enabled"]:
            await self.db.record_bot_event(guild_id, actor, event_type, details)

    async def staff_role_for(self, guild: discord.Guild) -> discord.Role | None:
        settings = await self.db.settings(guild.id)
        role_id = settings["staff_role_id"]
        return guild.get_role(role_id) if role_id else None

    async def is_staff_member(self, member: discord.Member) -> bool:
        if member.id in OWNER_IDS:
            return True
        permissions = member.guild_permissions
        if permissions.administrator or permissions.manage_guild:
            return True
        staff_role = await self.staff_role_for(member.guild)
        return bool(staff_role and staff_role in member.roles)

    async def mod_channel_for(self, guild: discord.Guild) -> discord.TextChannel | None:
        return await self.log_channel_for(guild)

    async def report_channel_for(self, guild: discord.Guild) -> discord.TextChannel | None:
        return await self.log_channel_for(guild)

    async def notify_mod(self, guild: discord.Guild, embed: discord.Embed) -> None:
        settings = await self.db.settings(guild.id)
        if not settings["moderation_logging_enabled"]:
            return
        lines = [embed.title or "Moderation event"]
        for field in embed.fields:
            lines.append(f"{field.name}: {field.value}")
        await self.send_server_log(guild, "\n".join(lines))

    async def notify_reaction_event(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
        user: discord.abc.User | None,
        emoji: str,
        event_type: str,
    ) -> None:
        channel = guild.get_channel(channel_id)
        if event_type == "add":
            action = "reacted with"
        elif event_type == "remove":
            action = "removed reaction"
        elif event_type == "clear":
            action = "cleared all reactions from"
        else:
            action = "cleared reaction"
        await self.send_server_log(
            guild,
            (
                f"{actor_label(user)} {action} {emoji} on Message {message_id} "
                f"in #{channel_name(channel)}."
            ),
        )

    async def notify_report_status(
        self,
        guild: discord.Guild,
        report: aiosqlite.Row,
        actor: discord.abc.User,
        status: str,
        note: str,
    ) -> None:
        settings = await self.db.settings(guild.id)
        if not settings["report_logging_enabled"]:
            return
        await self.send_server_log(
            guild,
            "\n".join(
                [
                    f"Report #{report['id']} {status} by {user_label(actor)}.",
                    f"Reporter: {report['reporter_name']} ({report['reporter_id']})",
                    f"Target: {report['target_name']} ({report['target_id']})",
                    f"Reason: {shorten(report['reason'], 600)}",
                    f"Note: {shorten(note, 600)}",
                ]
            ),
        )

    def case_embed(
        self,
        case_id: int,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User | None,
        reason: str,
        duration_seconds: int | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Case #{case_id} | {action}",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="対象", value=user_label(target), inline=False)
        embed.add_field(
            name="実行者",
            value=user_label(moderator) if moderator else "Bot",
            inline=False,
        )
        embed.add_field(name="理由", value=shorten(reason, 600), inline=False)
        if duration_seconds:
            embed.add_field(name="時間", value=f"{duration_seconds // 60}分", inline=True)
        return embed

    async def user_from_id(self, user_id: int) -> discord.User | discord.Member | discord.Object:
        for guild in self.guilds:
            member = guild.get_member(user_id)
            if member:
                return member
        user = self.get_user(user_id)
        if user:
            return user
        try:
            return await self.fetch_user(user_id)
        except discord.HTTPException:
            return discord.Object(id=user_id)

    async def run_cache_setup(
        self,
        guild: discord.Guild,
        actor: discord.abc.User,
    ) -> dict[str, discord.abc.Snowflake]:
        me = guild.me or guild.get_member(self.user.id)
        if not me:
            raise RuntimeError("Botメンバー情報を取得できませんでした。")
        missing = []
        if not me.guild_permissions.manage_channels:
            missing.append("Manage Channels")
        if not me.guild_permissions.manage_roles:
            missing.append("Manage Roles")
        if missing:
            raise RuntimeError("Botに必要な権限がありません: " + ", ".join(missing))

        staff_role = discord.utils.get(guild.roles, name="Cacheスタッフ")
        if staff_role is None:
            staff_role = discord.utils.get(guild.roles, name="Bot管理スタッフ")
            if staff_role is not None:
                await staff_role.edit(name="Cacheスタッフ", reason=f"Cache setup by {actor}")
        if staff_role is None:
            staff_role = await guild.create_role(
                name="Cacheスタッフ",
                reason=f"Initial setup by {actor}",
            )

        category = discord.utils.get(guild.categories, name="cache-management")
        if category is None:
            category = discord.utils.get(guild.categories, name="bot-management")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            ),
            staff_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                embed_links=True,
                attach_files=True,
            ),
        }
        if category is None:
            category = await guild.create_category(
                "cache-management",
                overwrites=overwrites,
                reason=f"Initial setup by {actor}",
            )
        elif category.name != "cache-management":
            await category.edit(name="cache-management", reason=f"Cache setup by {actor}")

        async def ensure_channel(
            name: str,
            legacy_name: str | None = None,
            legacy_names: tuple[str, ...] = (),
        ) -> discord.TextChannel:
            channel = discord.utils.get(guild.text_channels, name=name)
            if channel is None:
                for candidate in tuple(value for value in (legacy_name, *legacy_names) if value):
                    channel = discord.utils.get(guild.text_channels, name=candidate)
                    if channel is not None:
                        break
            if channel is None:
                channel = await guild.create_text_channel(
                    name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Initial setup by {actor}",
                )
            else:
                edits: dict[str, Any] = {}
                if channel.name != name:
                    edits["name"] = name
                if channel.category_id != category.id:
                    edits["category"] = category
                if edits:
                    await channel.edit(**edits, reason=f"Cache setup by {actor}")
            return channel

        server_log = await ensure_channel(
            SERVER_LOG_CHANNEL_NAME,
            "cache-logs",
            ("bot-logs",),
        )
        await self.db.set_setup_channels(
            guild.id,
            server_log.id,
            server_log.id,
            server_log.id,
            staff_role.id,
        )
        await self.db.record_bot_event(
            guild.id,
            actor,
            "cache_setup",
            {
                "server_log": server_log.id,
                "staff_role": staff_role.id,
            },
        )
        await server_log.send(
            "\n".join(
                [
                    "Cache setup complete.",
                    f"Logs will be posted in {server_log.mention}.",
                    f"Staff role: {staff_role.mention}",
                ]
            )
        )
        return {
            "server_log": server_log,
            "bot_logs": server_log,
            "mod_logs": server_log,
            "reports": server_log,
            "staff_role": staff_role,
        }

    async def add_case_and_notify(
        self,
        guild: discord.Guild,
        target: discord.abc.User,
        moderator: discord.abc.User | None,
        action: str,
        reason: str,
        duration_seconds: int | None = None,
    ) -> int:
        case_id = await self.db.add_mod_case(
            guild.id,
            target,
            moderator,
            action,
            reason,
            duration_seconds,
        )
        lines = [
            f"Case #{case_id} {action} has been recorded.",
            f"Target: {user_label(target)}",
            f"Moderator: {user_label(moderator) if moderator else 'Bot'}",
            f"Reason: {shorten(reason, 600)}",
        ]
        if duration_seconds:
            lines.append(f"Duration: {duration_seconds // 60} minutes")
        settings = await self.db.settings(guild.id)
        if settings["moderation_logging_enabled"]:
            await self.send_server_log(guild, "\n".join(lines))
        return case_id

    async def handle_automod(self, message: discord.Message) -> None:
        if not message.guild or not isinstance(message.author, discord.Member):
            return
        settings = await self.db.settings(message.guild.id)
        if not settings["automod_enabled"] or await self.is_staff_member(message.author):
            return

        reasons: list[str] = []
        content = message.content or ""
        if settings["anti_invite_enabled"] and INVITE_RE.search(content):
            reasons.append("Discord招待リンク")
        if settings["anti_link_enabled"] and LINK_RE.search(content):
            reasons.append("リンク投稿")
        mention_count = len(message.mentions) + len(message.role_mentions)
        if settings["anti_mention_enabled"] and mention_count >= 5:
            reasons.append("大量メンション")
        if settings["anti_zalgo_enabled"] and ZALGO_RE.search(content):
            reasons.append("Zalgo/装飾過多テキスト")

        if settings["anti_spam_enabled"]:
            key = (message.guild.id, message.author.id)
            now = time.monotonic()
            bucket = self.recent_messages[key]
            bucket.append((now, content[:200]))
            recent = [(ts, body) for ts, body in bucket if now - ts <= 7]
            repeats = [body for ts, body in bucket if now - ts <= 20 and body == content[:200]]
            if len(recent) >= 5 or len(repeats) >= 3:
                reasons.append("短時間の連投")

        if not reasons:
            return

        reason = " / ".join(reasons)
        try:
            await message.delete(reason=f"Automod: {reason}")
        except discord.Forbidden:
            logger.warning("Cannot delete automod message in guild %s", message.guild.id)
        except discord.HTTPException:
            logger.exception("Failed to delete automod message")

        await self.add_case_and_notify(
            message.guild,
            message.author,
            self.user,
            "automod_delete",
            reason,
        )

        if any(item in reason for item in ["大量メンション", "短時間の連投"]):
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=5)
                await message.author.timeout(until, reason=f"Automod: {reason}")
                await self.add_case_and_notify(
                    message.guild,
                    message.author,
                    self.user,
                    "automod_timeout",
                    reason,
                    300,
                )
            except discord.Forbidden:
                logger.warning("Cannot timeout automod member %s", message.author.id)
            except discord.HTTPException:
                logger.exception("Failed to timeout automod member")

    async def handle_prefix_command(self, message: discord.Message) -> bool:
        if not message.guild or not message.content.startswith(COMMAND_PREFIX):
            return False
        raw = message.content[len(COMMAND_PREFIX) :].strip()
        if not raw:
            return False
        command, _, args = raw.partition(" ")
        command = command.lower()

        if command in {"help", "commands"}:
            embed = discord.Embed(
                title="Cache コマンド",
                description=(
                    "`-setup`, `-ping`, `-warn @user 理由`, `-warnings @user`, "
                    "`-unwarn case_id 理由`, `-clearwarns @user`, "
                    "`-mute @user 10m 理由`, `-kick @user 理由`, `-ban @user 理由`, "
                    "`-purge 50`, `-report @user 理由`, `-cancelreport report_id 理由`"
                ),
                color=discord.Color.blurple(),
            )
            await message.reply(embed=embed, mention_author=False)
            return True

        if command == "ping":
            await message.reply(f"Pong! {round(self.latency * 1000)}ms", mention_author=False)
            return True

        if command == "report":
            if not message.mentions:
                await message.reply("通報対象をメンションしてください。", mention_author=False)
                return True
            target = message.mentions[0]
            reason = split_after_mention(message.content)
            report_id = await self.db.add_report(message.guild.id, message.author, target, reason)
            settings = await self.db.settings(message.guild.id)
            if settings["report_logging_enabled"]:
                await self.send_server_log(
                    message.guild,
                    "\n".join(
                        [
                            f"Report #{report_id} has been created.",
                            f"Reporter: {user_label(message.author)}",
                            f"Target: {user_label(target)}",
                            f"Reason: {shorten(reason)}",
                        ]
                    ),
                )
            await message.reply(f"通報を受け付けました。Report #{report_id}", mention_author=False)
            return True

        if command in {"cancelreport", "closereport"}:
            parts = args.split(maxsplit=1)
            if not parts or not parts[0].isdigit():
                await message.reply("例: `-cancelreport 3 誤通報のため`", mention_author=False)
                return True
            report_id = int(parts[0])
            note = parts[1] if len(parts) >= 2 else "理由なし"
            report = await self.db.get_report(message.guild.id, report_id)
            if not report:
                await message.reply("その通報IDは見つかりません。", mention_author=False)
                return True
            is_staff = isinstance(message.author, discord.Member) and await self.is_staff_member(message.author)
            if not is_staff and report["reporter_id"] != message.author.id:
                await message.reply("自分の通報、またはスタッフ権限のある通報だけ取り消せます。", mention_author=False)
                return True
            status = "closed" if command == "closereport" else "cancelled"
            await self.db.update_report_status(message.guild.id, report_id, status, message.author, note)
            await self.notify_report_status(message.guild, report, message.author, status, note)
            await message.reply(f"Report #{report_id} を `{status}` にしました。", mention_author=False)
            return True

        if not isinstance(message.author, discord.Member) or not await self.is_staff_member(message.author):
            await message.reply("このコマンドを使う権限がありません。", mention_author=False)
            return True

        if command == "setup":
            try:
                result = await self.run_cache_setup(message.guild, message.author)
            except Exception as exc:
                await message.reply(f"セットアップに失敗しました: {exc}", mention_author=False)
                return True
            await message.reply(
                f"セットアップ完了: {result['server_log'].mention}",
                mention_author=False,
            )
            return True

        if command in {"unwarn", "removewarn"}:
            parts = args.split(maxsplit=1)
            if not parts or not parts[0].isdigit():
                await message.reply("例: `-unwarn 12 誤警告のため`", mention_author=False)
                return True
            case_id = int(parts[0])
            note = parts[1] if len(parts) >= 2 else "理由なし"
            removed = await self.db.remove_warning_case(message.guild.id, case_id)
            if not removed:
                await message.reply("その警告Case IDは見つかりません。", mention_author=False)
                return True
            target = await self.user_from_id(removed["target_id"])
            new_case_id = await self.add_case_and_notify(
                message.guild,
                target,
                message.author,
                "remove_warn",
                f"Case #{case_id} を取り消し: {note}",
            )
            await message.reply(
                f"警告 Case #{case_id} を取り消しました。記録 Case #{new_case_id}",
                mention_author=False,
            )
            return True

        if command in {"warn", "warnings", "clearwarns", "mute", "timeout", "kick", "ban"}:
            if not message.mentions:
                await message.reply("対象ユーザーをメンションしてください。", mention_author=False)
                return True
            target = message.mentions[0]
            member = target if isinstance(target, discord.Member) else message.guild.get_member(target.id)

            if command == "warnings":
                cases = await self.db.get_mod_cases(message.guild.id, target.id, 10)
                warnings = [case for case in cases if case["action"] == "warn"]
                if not warnings:
                    await message.reply("警告履歴はありません。", mention_author=False)
                    return True
                lines = [
                    f"#{case['id']} `{to_jst_text(case['created_at'])}` {case['reason']}"
                    for case in warnings
                ]
                await message.reply("\n".join(lines), mention_author=False)
                return True

            if command == "clearwarns":
                count = await self.db.clear_warnings(message.guild.id, target.id)
                await self.add_case_and_notify(
                    message.guild,
                    target,
                    message.author,
                    "clear_warns",
                    f"{count}件の警告を削除",
                )
                await message.reply(f"{count}件の警告を削除しました。", mention_author=False)
                return True

            reason = split_after_mention(message.content)
            if command == "warn":
                case_id = await self.add_case_and_notify(
                    message.guild,
                    target,
                    message.author,
                    "warn",
                    reason,
                )
                warnings = [
                    case for case in await self.db.get_mod_cases(message.guild.id, target.id, 20)
                    if case["action"] == "warn"
                ]
                if len(warnings) >= 3 and member:
                    try:
                        await member.timeout(
                            datetime.now(timezone.utc) + timedelta(hours=1),
                            reason="警告が3件に達したため自動タイムアウト",
                        )
                        await self.add_case_and_notify(
                            message.guild,
                            member,
                            self.user,
                            "auto_timeout",
                            "警告が3件に達したため",
                            3600,
                        )
                    except discord.HTTPException:
                        logger.exception("Failed to auto-timeout warned member")
                await message.reply(f"警告を記録しました。Case #{case_id}", mention_author=False)
                return True

            if command in {"mute", "timeout"}:
                parts = args.split(maxsplit=2)
                duration = parse_duration(parts[1]) if len(parts) >= 2 else None
                if not member or not duration:
                    await message.reply("例: `-mute @user 10m 理由`", mention_author=False)
                    return True
                reason = parts[2] if len(parts) >= 3 else "理由なし"
                await member.timeout(
                    datetime.now(timezone.utc) + timedelta(seconds=duration),
                    reason=reason,
                )
                case_id = await self.add_case_and_notify(
                    message.guild,
                    member,
                    message.author,
                    "timeout",
                    reason,
                    duration,
                )
                await message.reply(f"タイムアウトしました。Case #{case_id}", mention_author=False)
                return True

            if command == "kick":
                if not member:
                    await message.reply("このサーバー内のメンバーを指定してください。", mention_author=False)
                    return True
                await member.kick(reason=reason)
                case_id = await self.add_case_and_notify(
                    message.guild, member, message.author, "kick", reason
                )
                await message.reply(f"Kickしました。Case #{case_id}", mention_author=False)
                return True

            if command == "ban":
                await message.guild.ban(target, reason=reason, delete_message_days=0)
                case_id = await self.add_case_and_notify(
                    message.guild, target, message.author, "ban", reason
                )
                await message.reply(f"Banしました。Case #{case_id}", mention_author=False)
                return True

        if command == "purge":
            if not isinstance(message.channel, discord.TextChannel):
                return True
            parts = args.split()
            if not parts or not parts[0].isdigit():
                await message.reply("例: `-purge 50` または `-purge 50 @user`", mention_author=False)
                return True
            amount = min(int(parts[0]), 100)
            target_id = message.mentions[0].id if message.mentions else None

            def should_delete(target_message: discord.Message) -> bool:
                return target_id is None or target_message.author.id == target_id

            deleted = await message.channel.purge(limit=amount + 1, check=should_delete)
            await self.add_case_and_notify(
                message.guild,
                message.author,
                message.author,
                "purge",
                f"{len(deleted)}件削除",
            )
            notice = await message.channel.send(f"{len(deleted)}件削除しました。")
            await asyncio.sleep(5)
            await notice.delete()
            return True

        return False

    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        settings = await self.db.settings(message.guild.id)
        if settings["message_logging_enabled"]:
            await self.db.save_message(message)
        if await self.handle_prefix_command(message):
            return
        await self.handle_automod(message)

    async def user_for_reaction(self, guild: discord.Guild, user_id: int) -> discord.User | discord.Member | None:
        member = guild.get_member(user_id)
        if member:
            return member
        user = self.get_user(user_id)
        if user:
            return user
        try:
            return await self.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        user = payload.member or await self.user_for_reaction(guild, payload.user_id)
        if user and user.bot:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"] or not settings["reaction_logging_enabled"]:
            return
        emoji = str(payload.emoji)
        await self.db.record_reaction_event(
            guild.id,
            payload.channel_id,
            payload.message_id,
            user,
            emoji,
            "add",
        )
        await self.notify_reaction_event(
            guild,
            payload.channel_id,
            payload.message_id,
            user,
            emoji,
            "add",
        )

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not payload.guild_id:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        user = await self.user_for_reaction(guild, payload.user_id)
        if user and user.bot:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"] or not settings["reaction_logging_enabled"]:
            return
        emoji = str(payload.emoji)
        await self.db.record_reaction_event(
            guild.id,
            payload.channel_id,
            payload.message_id,
            user,
            emoji,
            "remove",
        )
        await self.notify_reaction_event(
            guild,
            payload.channel_id,
            payload.message_id,
            user,
            emoji,
            "remove",
        )

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        if not payload.guild_id:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"] or not settings["reaction_logging_enabled"]:
            return
        await self.db.record_reaction_event(
            guild.id,
            payload.channel_id,
            payload.message_id,
            None,
            "*",
            "clear",
        )
        await self.notify_reaction_event(
            guild,
            payload.channel_id,
            payload.message_id,
            None,
            "*",
            "clear",
        )

    async def on_raw_reaction_clear_emoji(
        self,
        payload: discord.RawReactionClearEmojiEvent,
    ) -> None:
        if not payload.guild_id:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"] or not settings["reaction_logging_enabled"]:
            return
        emoji = str(payload.emoji)
        await self.db.record_reaction_event(
            guild.id,
            payload.channel_id,
            payload.message_id,
            None,
            emoji,
            "clear_emoji",
        )
        await self.notify_reaction_event(
            guild,
            payload.channel_id,
            payload.message_id,
            None,
            emoji,
            "clear_emoji",
        )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not payload.guild_id:
            return
        if "content" not in payload.data and "attachments" not in payload.data:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"]:
            return
        should_log_edit = bool(settings["message_edit_logging_enabled"])

        stored = await self.db.get_message(guild.id, payload.message_id)
        cached = payload.cached_message
        if cached and cached.author.bot:
            return

        author_data = payload.data.get("author") or {}
        if author_data.get("bot"):
            return
        author_id = int(author_data["id"]) if str(author_data.get("id", "")).isdigit() else None
        author_name = author_data.get("username")
        if stored:
            author_id = stored["author_id"]
            author_name = stored["author_name"]
            before_content = stored["content"]
            before_attachments = json.loads(stored["attachment_urls"] or "[]")
        elif cached:
            author_id = cached.author.id
            author_name = str(cached.author)
            before_content = cached.content
            before_attachments = attachment_urls(cached)
        else:
            before_content = None
            before_attachments = []

        after_content = payload.data.get("content", before_content)
        if "attachments" in payload.data:
            after_attachments = [
                attachment.get("url")
                for attachment in payload.data.get("attachments", [])
                if attachment.get("url")
            ]
        else:
            after_attachments = before_attachments
        if "embeds" in payload.data:
            embeds_count = len(payload.data.get("embeds", []))
        elif stored:
            embeds_count = stored["embeds_count"]
        elif cached:
            embeds_count = len(cached.embeds)
        else:
            embeds_count = 0

        if before_content == after_content and before_attachments == after_attachments:
            return
        await self.db.update_message_from_raw_edit(
            guild_id=guild.id,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            author_id=author_id,
            author_name=author_name,
            before_content=before_content,
            after_content=after_content,
            attachments=after_attachments,
            embeds_count=embeds_count,
            record_event=should_log_edit,
        )
        if not should_log_edit:
            return

        channel = guild.get_channel(payload.channel_id)
        await self.send_server_log(
            guild,
            "\n".join(
                [
                    (
                        f"ℹ Message edited from {author_name or 'Unknown'} "
                        f"({author_id or 'Unknown'}) in {channel_label(channel)}"
                    ),
                    "Before:",
                    code_block(before_content),
                    "After:",
                    code_block(after_content),
                ]
            ),
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if not payload.guild_id:
            return
        guild = self.get_guild(payload.guild_id)
        if not guild:
            return
        settings = await self.db.settings(guild.id)
        if not settings["message_logging_enabled"]:
            return
        should_log_delete = bool(settings["message_delete_logging_enabled"])
        if payload.cached_message and payload.cached_message.author.bot:
            return
        stored = await self.db.mark_message_deleted(
            guild_id=guild.id,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            cached_message=payload.cached_message,
            record_event=should_log_delete,
        )
        if not should_log_delete:
            return
        author = "Unknown"
        content = None
        if payload.cached_message:
            author = user_label(payload.cached_message.author)
            content = payload.cached_message.content
        elif stored:
            author = f"{stored['author_name']} ({stored['author_id']})"
            content = stored["content"]

        channel = guild.get_channel(payload.channel_id)
        await self.send_server_log(
            guild,
            "\n".join(
                [
                    (
                        f"Message {payload.message_id} deleted from {author} "
                        f"in {channel_label(channel)}:"
                    ),
                    code_block(content),
                ]
            ),
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if before.channel == after.channel:
            return
        settings = await self.db.settings(member.guild.id)
        if not settings["voice_logging_enabled"]:
            return

        if before.channel is None and after.channel is not None:
            event_type = "join"
            should_log_voice = bool(settings["voice_join_logging_enabled"])
        elif before.channel is not None and after.channel is None:
            event_type = "leave"
            should_log_voice = bool(settings["voice_leave_logging_enabled"])
        else:
            event_type = "move"
            should_log_voice = bool(settings["voice_move_logging_enabled"])
        if not should_log_voice:
            return

        await self.db.record_voice_event(
            member.guild.id,
            member,
            event_type,
            before.channel,
            after.channel,
        )
        if event_type == "join":
            await self.send_server_log(
                member.guild,
                f"{user_label(member)} has joined voice in {after.channel.name}",
            )
        elif event_type == "leave":
            await self.send_server_log(
                member.guild,
                f"{user_label(member)} has left voice channel {before.channel.name}",
            )
        else:
            await self.send_server_log(
                member.guild,
                "\n".join(
                    [
                        f"{user_label(member)} has left voice channel {before.channel.name}",
                        f"{user_label(member)} has joined voice in {after.channel.name}",
                    ]
                ),
            )

    async def audit_actor_for(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
    ) -> discord.abc.User | None:
        me = guild.me or (self.user and guild.get_member(self.user.id))
        if not me or not me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1)
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                entry_target_id = getattr(entry.target, "id", None)
                if entry_target_id != target_id:
                    continue
                created_at = entry.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - created_at
                if age.total_seconds() <= 30:
                    return entry.user
        except discord.Forbidden:
            logger.warning("Cannot read audit logs in guild %s", guild.id)
        except discord.HTTPException:
            logger.exception("Failed to read audit logs in guild %s", guild.id)
        return None

    def role_update_text(
        self,
        before: discord.Role,
        after: discord.Role,
        actor: discord.abc.User | None,
    ) -> str | None:
        before_permissions = dict(iter(before.permissions))
        after_permissions = dict(iter(after.permissions))
        changed_permissions = [
            name
            for name, before_value in before_permissions.items()
            if before_value != after_permissions.get(name)
        ]

        before_lines: list[str] = []
        after_lines: list[str] = []
        newly_allowed = [
            name
            for name in changed_permissions
            if after_permissions.get(name) and not before_permissions.get(name)
        ]

        if changed_permissions:
            before_lines = role_permission_lines(before.permissions, changed_permissions)
            if len(newly_allowed) >= 10 and len(newly_allowed) == len(changed_permissions):
                after_lines = [f"{len(newly_allowed)} permissions allowed."]
            else:
                after_lines = role_permission_lines(after.permissions, changed_permissions)
        else:
            watched_fields = [
                ("name", before.name, after.name),
                ("color", str(before.color), str(after.color)),
                ("hoist", str(before.hoist), str(after.hoist)),
                ("mentionable", str(before.mentionable), str(after.mentionable)),
            ]
            for field_name, before_value, after_value in watched_fields:
                if before_value != after_value:
                    before_lines.append(f"{field_name}: {before_value}")
                    after_lines.append(f"{field_name}: {after_value}")

        if not before_lines and not after_lines:
            return None

        marker = "❗" if len(newly_allowed) >= 10 else "⚠"
        return "\n".join(
            [
                f"{marker} Role {after.name} has been updated by {actor_label(actor)}.",
                "Before:",
                code_block("\n".join(before_lines)),
                "After:",
                code_block("\n".join(after_lines)),
            ]
        )

    async def on_guild_role_create(self, role: discord.Role) -> None:
        settings = await self.db.settings(role.guild.id)
        if not settings["role_create_logging_enabled"]:
            return
        actor = await self.audit_actor_for(
            role.guild,
            discord.AuditLogAction.role_create,
            role.id,
        )
        await self.send_server_log(
            role.guild,
            f"Role {role.name} has been created by {actor_label(actor)}.",
        )

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        settings = await self.db.settings(after.guild.id)
        if not settings["role_update_logging_enabled"]:
            return
        actor = await self.audit_actor_for(
            after.guild,
            discord.AuditLogAction.role_update,
            after.id,
        )
        message = self.role_update_text(before, after, actor)
        if message:
            await self.send_server_log(after.guild, message)

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if not isinstance(before, discord.TextChannel) or not isinstance(after, discord.TextChannel):
            return
        settings = await self.db.settings(after.guild.id)
        if not settings["channel_update_logging_enabled"]:
            return
        if before.name == after.name and before.category_id == after.category_id:
            return

        lines = [
            "Text Channel Update:",
            f"Name: {after.name} ({after.id})",
        ]
        if before.name != after.name:
            lines.extend(
                [
                    f"Name (Before): {before.name}",
                    f"Name (After): {after.name}",
                ]
            )
        if before.category_id != after.category_id:
            lines.extend(
                [
                    f"Category (Before): {channel_name(before.category)}",
                    f"Category (After): {channel_name(after.category)}",
                ]
            )
        await self.send_server_log(after.guild, "\n".join(lines))


def setup_commands(bot: CacheBot) -> None:
    category_choices = [
        app_commands.Choice(name="全通知投稿", value="notify_events_enabled"),
        app_commands.Choice(name="メッセージ保存", value="message_logging_enabled"),
        app_commands.Choice(name="メッセージ編集ログ", value="message_edit_logging_enabled"),
        app_commands.Choice(name="メッセージ削除ログ", value="message_delete_logging_enabled"),
        app_commands.Choice(name="リアクションログ", value="reaction_logging_enabled"),
        app_commands.Choice(name="VCログ全体", value="voice_logging_enabled"),
        app_commands.Choice(name="VC入室ログ", value="voice_join_logging_enabled"),
        app_commands.Choice(name="VC退室ログ", value="voice_leave_logging_enabled"),
        app_commands.Choice(name="VC移動ログ", value="voice_move_logging_enabled"),
        app_commands.Choice(name="メンバー参加ログ", value="member_join_logging_enabled"),
        app_commands.Choice(name="メンバー退出ログ", value="member_leave_logging_enabled"),
        app_commands.Choice(name="ロール作成ログ", value="role_create_logging_enabled"),
        app_commands.Choice(name="ロール更新ログ", value="role_update_logging_enabled"),
        app_commands.Choice(name="チャンネル更新ログ", value="channel_update_logging_enabled"),
        app_commands.Choice(name="処罰ログ", value="moderation_logging_enabled"),
        app_commands.Choice(name="通報ログ", value="report_logging_enabled"),
        app_commands.Choice(name="管理コマンドログ", value="command_logging_enabled"),
    ]
    search_choices = [
        app_commands.Choice(name="メッセージ作成", value="create"),
        app_commands.Choice(name="メッセージ編集", value="edit"),
        app_commands.Choice(name="メッセージ削除", value="delete"),
        app_commands.Choice(name="メッセージ全体", value="messages"),
        app_commands.Choice(name="VC", value="voice"),
        app_commands.Choice(name="リアクション", value="reactions"),
    ]
    export_choices = [
        app_commands.Choice(name="メッセージ", value="messages"),
        app_commands.Choice(name="VC", value="voice"),
        app_commands.Choice(name="リアクション", value="reactions"),
    ]
    automod_choices = [
        app_commands.Choice(name="自動モデレーション全体", value="automod_enabled"),
        app_commands.Choice(name="連投対策", value="anti_spam_enabled"),
        app_commands.Choice(name="Discord招待リンク対策", value="anti_invite_enabled"),
        app_commands.Choice(name="通常リンク対策", value="anti_link_enabled"),
        app_commands.Choice(name="大量メンション対策", value="anti_mention_enabled"),
        app_commands.Choice(name="Zalgo/装飾過多対策", value="anti_zalgo_enabled"),
        app_commands.Choice(name="レイド検知", value="raid_guard_enabled"),
    ]

    @bot.tree.command(name="cache_setup", description="Cacheの管理チャンネルとロールを自動作成します")
    @app_commands.guild_only()
    @manager_only()
    async def cache_setup(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.run_cache_setup(interaction.guild, interaction.user)
        except Exception as exc:
            await interaction.followup.send(f"セットアップに失敗しました: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            (
                "セットアップ完了です。\n"
                f"ログ: {result['server_log'].mention}\n"
                f"スタッフロール: {result['staff_role'].mention}"
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="automod_toggle", description="自動モデレーション機能のON/OFFを切り替えます")
    @app_commands.guild_only()
    @app_commands.choices(feature=automod_choices)
    @manager_only()
    async def automod_toggle(
        interaction: discord.Interaction,
        feature: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        await bot.db.set_toggle(interaction.guild_id, feature.value, enabled)
        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "automod_toggle",
            {"feature": feature.value, "enabled": enabled},
        )
        await interaction.response.send_message(
            f"{feature.name} を {'ON' if enabled else 'OFF'} にしました。",
            ephemeral=True,
        )

    @bot.tree.command(name="warn", description="ユーザーに警告を記録します")
    @app_commands.guild_only()
    @manager_only()
    async def warn(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str = "理由なし",
    ) -> None:
        case_id = await bot.add_case_and_notify(
            interaction.guild,
            user,
            interaction.user,
            "warn",
            reason,
        )
        warnings = [
            case for case in await bot.db.get_mod_cases(interaction.guild_id, user.id, 20)
            if case["action"] == "warn"
        ]
        if len(warnings) >= 3:
            try:
                await user.timeout(
                    datetime.now(timezone.utc) + timedelta(hours=1),
                    reason="警告が3件に達したため自動タイムアウト",
                )
                await bot.add_case_and_notify(
                    interaction.guild,
                    user,
                    bot.user,
                    "auto_timeout",
                    "警告が3件に達したため",
                    3600,
                )
            except discord.HTTPException:
                logger.exception("Failed to auto-timeout warned member")
        await interaction.response.send_message(
            f"{user.mention} に警告を記録しました。Case #{case_id}",
            ephemeral=True,
        )

    @bot.tree.command(name="warnings", description="ユーザーの警告履歴を確認します")
    @app_commands.guild_only()
    @manager_only()
    async def warnings(interaction: discord.Interaction, user: discord.User) -> None:
        rows = await bot.db.get_mod_cases(interaction.guild_id, user.id, 20)
        warn_rows = [row for row in rows if row["action"] == "warn"]
        if not warn_rows:
            await interaction.response.send_message("警告履歴はありません。", ephemeral=True)
            return
        lines = [
            f"#{row['id']} `{to_jst_text(row['created_at'])}` {row['reason']}"
            for row in warn_rows[:10]
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="unwarn", description="Case IDを指定して警告を1件取り消します")
    @app_commands.guild_only()
    @manager_only()
    async def unwarn(
        interaction: discord.Interaction,
        case_id: int,
        reason: str = "理由なし",
    ) -> None:
        removed = await bot.db.remove_warning_case(interaction.guild_id, case_id)
        if not removed:
            await interaction.response.send_message(
                "その警告Case IDは見つかりません。",
                ephemeral=True,
            )
            return
        target = await bot.user_from_id(removed["target_id"])
        new_case_id = await bot.add_case_and_notify(
            interaction.guild,
            target,
            interaction.user,
            "remove_warn",
            f"Case #{case_id} を取り消し: {reason}",
        )
        await interaction.response.send_message(
            f"警告 Case #{case_id} を取り消しました。記録 Case #{new_case_id}",
            ephemeral=True,
        )

    @bot.tree.command(name="clearwarns", description="ユーザーの警告履歴を削除します")
    @app_commands.guild_only()
    @manager_only()
    async def clearwarns(interaction: discord.Interaction, user: discord.User) -> None:
        count = await bot.db.clear_warnings(interaction.guild_id, user.id)
        await bot.add_case_and_notify(
            interaction.guild,
            user,
            interaction.user,
            "clear_warns",
            f"{count}件の警告を削除",
        )
        await interaction.response.send_message(f"{count}件の警告を削除しました。", ephemeral=True)

    @bot.tree.command(name="timeout", description="ユーザーを指定時間タイムアウトします")
    @app_commands.guild_only()
    @manager_only()
    async def timeout_user(
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str = "理由なし",
    ) -> None:
        seconds = parse_duration(duration)
        if not seconds:
            await interaction.response.send_message("時間は `10m`, `1h`, `2d` のように指定してください。", ephemeral=True)
            return
        await user.timeout(datetime.now(timezone.utc) + timedelta(seconds=seconds), reason=reason)
        case_id = await bot.add_case_and_notify(
            interaction.guild,
            user,
            interaction.user,
            "timeout",
            reason,
            seconds,
        )
        await interaction.response.send_message(
            f"{user.mention} をタイムアウトしました。Case #{case_id}",
            ephemeral=True,
        )

    @bot.tree.command(name="kick", description="ユーザーをKickします")
    @app_commands.guild_only()
    @manager_only()
    async def kick_user(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str = "理由なし",
    ) -> None:
        await user.kick(reason=reason)
        case_id = await bot.add_case_and_notify(
            interaction.guild,
            user,
            interaction.user,
            "kick",
            reason,
        )
        await interaction.response.send_message(f"{user} をKickしました。Case #{case_id}", ephemeral=True)

    @bot.tree.command(name="ban", description="ユーザーをBanします")
    @app_commands.guild_only()
    @manager_only()
    async def ban_user(
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "理由なし",
    ) -> None:
        await interaction.guild.ban(user, reason=reason, delete_message_days=0)
        case_id = await bot.add_case_and_notify(
            interaction.guild,
            user,
            interaction.user,
            "ban",
            reason,
        )
        await interaction.response.send_message(f"{user} をBanしました。Case #{case_id}", ephemeral=True)

    @bot.tree.command(name="purge", description="指定数のメッセージを削除します")
    @app_commands.guild_only()
    @manager_only()
    async def purge(
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        user: discord.User | None = None,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        def should_delete(target_message: discord.Message) -> bool:
            return user is None or target_message.author.id == user.id

        deleted = await interaction.channel.purge(limit=amount, check=should_delete)
        await bot.add_case_and_notify(
            interaction.guild,
            interaction.user,
            interaction.user,
            "purge",
            f"{len(deleted)}件削除",
        )
        await interaction.followup.send(f"{len(deleted)}件削除しました。", ephemeral=True)

    @bot.tree.command(name="report", description="スタッフへユーザーを通報します")
    @app_commands.guild_only()
    async def report(
        interaction: discord.Interaction,
        user: discord.User,
        reason: str,
    ) -> None:
        report_id = await bot.db.add_report(interaction.guild_id, interaction.user, user, reason)
        settings = await bot.db.settings(interaction.guild_id)
        if settings["report_logging_enabled"]:
            await bot.send_server_log(
                interaction.guild,
                "\n".join(
                    [
                        f"Report #{report_id} has been created.",
                        f"Reporter: {user_label(interaction.user)}",
                        f"Target: {user_label(user)}",
                        f"Reason: {shorten(reason)}",
                    ]
                ),
            )
        await interaction.response.send_message(
            f"通報を受け付けました。Report #{report_id}",
            ephemeral=True,
        )

    @bot.tree.command(name="report_cancel", description="通報IDを指定して通報を取り消します")
    @app_commands.guild_only()
    async def report_cancel(
        interaction: discord.Interaction,
        report_id: int,
        reason: str = "理由なし",
    ) -> None:
        report_row = await bot.db.get_report(interaction.guild_id, report_id)
        if not report_row:
            await interaction.response.send_message("その通報IDは見つかりません。", ephemeral=True)
            return
        is_staff = (
            isinstance(interaction.user, discord.Member)
            and await bot.is_staff_member(interaction.user)
        )
        if not is_staff and report_row["reporter_id"] != interaction.user.id:
            await interaction.response.send_message(
                "自分の通報、またはスタッフ権限のある通報だけ取り消せます。",
                ephemeral=True,
            )
            return
        await bot.db.update_report_status(
            interaction.guild_id,
            report_id,
            "cancelled",
            interaction.user,
            reason,
        )
        await bot.notify_report_status(
            interaction.guild,
            report_row,
            interaction.user,
            "cancelled",
            reason,
        )
        await interaction.response.send_message(
            f"Report #{report_id} を取り消しました。",
            ephemeral=True,
        )

    @bot.tree.command(name="log_setup", description="ログ通知先チャンネルを設定します")
    @app_commands.guild_only()
    @manager_only()
    async def log_setup(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if channel.name != SERVER_LOG_CHANNEL_NAME:
            await interaction.response.send_message(
                f"ログ送信先は #{SERVER_LOG_CHANNEL_NAME} のみです。/cache_setup で作成してから指定してください。",
                ephemeral=True,
            )
            return
        await bot.db.set_log_channel(interaction.guild_id, channel.id)
        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "log_setup",
            {"channel_id": channel.id},
        )
        await interaction.response.send_message(
            f"ログ通知先を {channel.mention} に設定しました。",
            ephemeral=True,
        )

    @bot.tree.command(name="log_status", description="ログ設定と保存件数を確認します")
    @app_commands.guild_only()
    @manager_only()
    async def log_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        settings = await bot.db.settings(interaction.guild_id)
        stats = await bot.db.stats(interaction.guild_id)
        log_channel = (
            f"<#{settings['log_channel_id']}>" if settings["log_channel_id"] else "未設定"
        )
        mod_channel = (
            f"<#{settings['mod_log_channel_id']}>" if settings["mod_log_channel_id"] else "未設定"
        )
        report_channel = (
            f"<#{settings['report_channel_id']}>" if settings["report_channel_id"] else "未設定"
        )
        embed = discord.Embed(
            title="ログ設定",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="通知先", value=log_channel, inline=False)
        embed.add_field(name="処罰ログ", value=mod_channel, inline=True)
        embed.add_field(name="通報先", value=report_channel, inline=True)
        toggle_labels = [
            ("全通知投稿", "notify_events_enabled"),
            ("メッセージ保存", "message_logging_enabled"),
            ("編集", "message_edit_logging_enabled"),
            ("削除", "message_delete_logging_enabled"),
            ("リアクション", "reaction_logging_enabled"),
            ("VC全体", "voice_logging_enabled"),
            ("VC入室", "voice_join_logging_enabled"),
            ("VC退室", "voice_leave_logging_enabled"),
            ("VC移動", "voice_move_logging_enabled"),
            ("参加", "member_join_logging_enabled"),
            ("退出", "member_leave_logging_enabled"),
            ("ロール作成", "role_create_logging_enabled"),
            ("ロール更新", "role_update_logging_enabled"),
            ("チャンネル更新", "channel_update_logging_enabled"),
            ("処罰", "moderation_logging_enabled"),
            ("通報", "report_logging_enabled"),
            ("管理コマンド", "command_logging_enabled"),
            ("自動モデレーション", "automod_enabled"),
        ]
        status_lines = [
            f"{label}: {'ON' if settings[column] else 'OFF'}"
            for label, column in toggle_labels
        ]
        embed.add_field(name="ログON/OFF", value=code_block("\n".join(status_lines), 1000), inline=False)
        embed.add_field(name="保存メッセージ", value=str(stats["messages"]), inline=True)
        embed.add_field(name="メッセージイベント", value=str(stats["message_events"]), inline=True)
        embed.add_field(name="VCイベント", value=str(stats["voice_events"]), inline=True)
        embed.add_field(name="リアクション", value=str(stats["reaction_events"]), inline=True)
        embed.add_field(name="処罰ケース", value=str(stats["mod_cases"]), inline=True)
        embed.add_field(name="通報", value=str(stats["reports"]), inline=True)
        embed.set_footer(text=f"DB: {DATABASE_PATH}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="log_toggle", description="ログ機能のON/OFFを切り替えます")
    @app_commands.guild_only()
    @app_commands.choices(category=category_choices)
    @manager_only()
    async def log_toggle(
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        await bot.db.set_toggle(interaction.guild_id, category.value, enabled)
        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "log_toggle",
            {"category": category.value, "enabled": enabled},
        )
        await interaction.response.send_message(
            f"{category.name} を {'ON' if enabled else 'OFF'} にしました。",
            ephemeral=True,
        )

    @bot.tree.command(name="log_search", description="保存済みログを検索します")
    @app_commands.guild_only()
    @app_commands.choices(kind=search_choices)
    @manager_only()
    async def log_search(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        user: discord.User | None = None,
        channel: discord.TextChannel | None = None,
        keyword: str | None = None,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if kind.value == "voice":
            rows = await bot.db.search_voice_events(
                interaction.guild_id,
                user.id if user else None,
                keyword,
                limit,
            )
            lines = [
                (
                    f"`{to_jst_text(row['created_at'])}` {row['event_type']} "
                    f"{row['user_name']} | {row['before_channel_name'] or '-'} -> "
                    f"{row['after_channel_name'] or '-'}"
                )
                for row in rows
            ]
        elif kind.value == "reactions":
            rows = await bot.db.search_reaction_events(
                interaction.guild_id,
                user.id if user else None,
                channel.id if channel else None,
                keyword,
                limit,
            )
            lines = [
                (
                    f"`{to_jst_text(row['created_at'])}` {row['event_type']} "
                    f"{row['emoji']} <#{row['channel_id']}> "
                    f"{row['user_name'] or '不明'} | message:{row['message_id']}"
                )
                for row in rows
            ]
        else:
            event_types = [] if kind.value == "messages" else [kind.value]
            rows = await bot.db.search_message_events(
                interaction.guild_id,
                event_types,
                user.id if user else None,
                channel.id if channel else None,
                keyword,
                limit,
            )
            lines = []
            for row in rows:
                body = row["after_content"] if row["event_type"] != "delete" else row["before_content"]
                lines.append(
                    f"`{to_jst_text(row['created_at'])}` {row['event_type']} "
                    f"<#{row['channel_id']}> {row['author_name'] or '不明'}\n"
                    f"{shorten(body, 180)}"
                )

        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "log_search",
            {
                "kind": kind.value,
                "user_id": user.id if user else None,
                "channel_id": channel.id if channel else None,
                "keyword": keyword,
                "limit": limit,
            },
        )
        if not lines:
            await interaction.followup.send("該当するログはありませんでした。", ephemeral=True)
            return
        text = "\n\n".join(lines)
        if len(text) <= 1900:
            await interaction.followup.send(text, ephemeral=True)
            return
        file = discord.File(
            io.BytesIO(text.encode("utf-8-sig")),
            filename=f"log_search_{kind.value}.txt",
        )
        await interaction.followup.send("結果が長いためファイルで出力します。", file=file, ephemeral=True)

    @bot.tree.command(name="log_export", description="ログをCSVで出力します")
    @app_commands.guild_only()
    @app_commands.choices(kind=export_choices)
    @manager_only()
    async def log_export(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        headers, rows = await bot.db.export_events(interaction.guild_id, kind.value, days)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[header] for header in headers])
        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "log_export",
            {"kind": kind.value, "days": days, "rows": len(rows)},
        )
        file = discord.File(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            filename=f"{kind.value}_logs_{days}days.csv",
        )
        await interaction.followup.send(
            f"直近{days}日分のログをCSVで出力しました。",
            file=file,
            ephemeral=True,
        )

    @bot.tree.command(name="cache_health", description="Cacheの状態を確認します")
    @app_commands.guild_only()
    @manager_only()
    async def cache_health(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = await bot.db.stats(interaction.guild_id)
        embed = discord.Embed(
            title="Cache状態",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="ログイン", value=str(bot.user), inline=False)
        embed.add_field(name="応答速度", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="サーバー数", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="保存イベント", value=str(sum(stats.values())), inline=True)
        await bot.record_admin_action(
            interaction.guild_id,
            interaction.user,
            "cache_health",
            {},
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "このコマンドを使う権限がありません。サーバー管理権限が必要です。"
        else:
            logger.exception("App command failed", exc_info=error)
            message = "コマンド実行中にエラーが発生しました。Botのログを確認してください。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "replace_me":
        raise RuntimeError(".env に DISCORD_TOKEN を設定してください。")
    bot = CacheBot()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
