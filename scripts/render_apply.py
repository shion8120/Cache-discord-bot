from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://api.render.com/v1"
REPO_URL = "https://github.com/shion8120/Cache-discord-bot"
DEFAULT_SERVICE_NAMES = {"Cache", "Cache-discord-bot", "cache-discord-bot"}


def read_api_key() -> str:
    value = os.getenv("RENDER_API_KEY", "").strip()
    if value:
        return value
    key_file = Path(".render_api_key")
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise SystemExit(
        "Render API key is missing. Set RENDER_API_KEY or create .render_api_key."
    )


class RenderClient:
    def __init__(self, api_key: str):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cache-render-apply",
        }

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}{path}",
            data=data,
            method=method,
            headers=self.headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(f"Render API error {exc.code}: {body}") from exc

    def list_services(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        cursor = None
        services: list[dict[str, Any]] = []
        while True:
            query = {"limit": "100"}
            if names:
                query["name"] = ",".join(names)
            if cursor:
                query["cursor"] = cursor
            path = "/services?" + urllib.parse.urlencode(query)
            page = self.request("GET", path)
            if not page:
                return services
            for item in page:
                services.append(item["service"])
                cursor = item.get("cursor")
            if not cursor or len(page) < 100:
                return services

    def find_service(self, explicit_id: str | None = None) -> dict[str, Any]:
        if explicit_id:
            return self.request("GET", f"/services/{explicit_id}")
        services = self.list_services(sorted(DEFAULT_SERVICE_NAMES))
        for service in services:
            if service.get("name") in DEFAULT_SERVICE_NAMES:
                return service
        services = self.list_services()
        for service in services:
            repo = (service.get("repo") or "").rstrip("/")
            if repo == REPO_URL:
                return service
        names = ", ".join(sorted(service.get("name", "?") for service in services))
        raise SystemExit(f"Cache service was not found. Visible services: {names}")

    def put_env(self, service_id: str, key: str, value: str) -> None:
        encoded = urllib.parse.quote(key, safe="")
        self.request("PUT", f"/services/{service_id}/env-vars/{encoded}", {"value": value})

    def list_env_keys(self, service_id: str) -> set[str]:
        values = self.request("GET", f"/services/{service_id}/env-vars")
        return {item["envVar"]["key"] for item in values}

    def trigger_deploy(self, service_id: str) -> Any:
        return self.request(
            "POST",
            f"/services/{service_id}/deploys",
            {"clearCache": "do_not_clear"},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Cache Render settings.")
    parser.add_argument("--service-id", default=os.getenv("RENDER_SERVICE_ID"))
    parser.add_argument("--sync-guild-id", default=os.getenv("SYNC_GUILD_ID", ""))
    parser.add_argument("--owner-ids", default=os.getenv("OWNER_IDS", ""))
    parser.add_argument("--retention-days", default=os.getenv("RETENTION_DAYS", "180"))
    parser.add_argument("--command-prefix", default=os.getenv("COMMAND_PREFIX", "-"))
    parser.add_argument(
        "--server-log-channel-name",
        default=os.getenv("SERVER_LOG_CHANNEL_NAME", "server-log"),
    )
    parser.add_argument("--no-deploy", action="store_true")
    args = parser.parse_args()

    client = RenderClient(read_api_key())
    service = client.find_service(args.service_id)
    service_id = service["id"]

    env_updates = {
        "DATABASE_PATH": "/data/bot.sqlite3",
        "RETENTION_DAYS": str(args.retention_days),
        "COMMAND_PREFIX": args.command_prefix,
        "SERVER_LOG_CHANNEL_NAME": args.server_log_channel_name,
    }
    if args.sync_guild_id:
        env_updates["SYNC_GUILD_ID"] = args.sync_guild_id
    if args.owner_ids:
        env_updates["OWNER_IDS"] = args.owner_ids

    for key, value in env_updates.items():
        client.put_env(service_id, key, value)

    env_keys = client.list_env_keys(service_id)
    missing = [key for key in ["DISCORD_TOKEN"] if key not in env_keys]

    deploy = None
    if not args.no_deploy:
        deploy = client.trigger_deploy(service_id)

    print(
        json.dumps(
            {
                "service": service.get("name"),
                "service_id": service_id,
                "updated_env": sorted(env_updates),
                "missing_required_env": missing,
                "deploy_id": deploy.get("id") if isinstance(deploy, dict) else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if missing:
        sys.exit(2)


if __name__ == "__main__":
    main()
