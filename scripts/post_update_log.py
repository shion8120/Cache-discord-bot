from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


RENDER_API_BASE = "https://api.render.com/v1"
DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_MESSAGE = (
    "Cache update complete. Future update notices will use ASCII text "
    "to avoid garbled characters."
)


def read_render_api_key() -> str:
    value = os.getenv("RENDER_API_KEY", "").strip()
    if value:
        return value
    key_file = Path(".render_api_key")
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise SystemExit("Render API key is missing.")


def request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP error {exc.code}: {body}") from exc


def render_env(service_id: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {read_render_api_key()}",
        "Accept": "application/json",
        "User-Agent": "cache-update-log",
    }
    values = request(
        "GET",
        f"{RENDER_API_BASE}/services/{service_id}/env-vars",
        headers,
    )
    return {item["envVar"]["key"]: item["envVar"].get("value", "") for item in values}


def discord_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "cache-update-log",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Post a Cache update notice to server-log.")
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    args = parser.parse_args()

    env = render_env(args.service_id)
    token = env.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing on Render.")

    headers = discord_headers(token)
    guilds = request("GET", f"{DISCORD_API_BASE}/users/@me/guilds", headers)
    results: list[dict[str, str]] = []

    for guild in guilds:
        guild_id = guild["id"]
        channels = request(
            "GET",
            f"{DISCORD_API_BASE}/guilds/{guild_id}/channels",
            headers,
        )
        server_log = next(
            (
                channel
                for channel in channels
                if channel.get("type") == 0 and channel.get("name") == "server-log"
            ),
            None,
        )
        if not server_log:
            results.append(
                {
                    "guild_id": guild_id,
                    "guild_name": guild.get("name", ""),
                    "status": "server-log not found",
                }
            )
            continue

        message = request(
            "POST",
            f"{DISCORD_API_BASE}/channels/{server_log['id']}/messages",
            headers,
            {
                "content": args.message,
                "allowed_mentions": {"parse": []},
                "flags": 4096,
            },
        )
        results.append(
            {
                "guild_id": guild_id,
                "guild_name": guild.get("name", ""),
                "channel_id": server_log["id"],
                "message_id": message.get("id", ""),
                "status": "sent",
            }
        )

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
