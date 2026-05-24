# Discord側設定とサーバー常時稼働手順

## 1. Discord Developer Portal

Discord Developer PortalでBotアプリを開きます。

### Botページ

`Privileged Gateway Intents` で次をONにします。

- Server Members Intent
- Message Content Intent

この2つは、Botコードから自動でONにできません。Discordの管理画面で手動設定が必要です。

### OAuth2 URL Generator

Scopes:

- `bot`
- `applications.commands`

Bot Permissions:

- View Channels
- Send Messages
- Manage Messages
- Embed Links
- Attach Files
- Read Message History
- Manage Channels
- Manage Roles
- Moderate Members
- Kick Members
- Ban Members

招待URLの形:

```text
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1101927672854&integration_type=0&scope=bot+applications.commands
```

`YOUR_CLIENT_ID` はDeveloper PortalのApplication IDに置き換えてください。

## 2. Discordサーバー内セットアップ

Botを招待して起動したら、サーバー管理権限を持つユーザーで次を実行します。

```text
/cache_setup
```

これで次が自動作成されます。

- `Cacheスタッフ` ロール
- `cache-management` カテゴリ
- `cache-logs`
- `moderation-logs`
- `reports`

その後、Cacheを使う運営メンバーへ `Cacheスタッフ` ロールを付けてください。

## 3. Cacheコマンド

```text
-setup
-help
-ping
-warn @user 理由
-warnings @user
-unwarn 12 誤警告のため
-clearwarns @user
-mute @user 10m 理由
-kick @user 理由
-ban @user 理由
-purge 50
-report @user 理由
-cancelreport 3 誤通報のため
```

スラッシュコマンドでも同じように使えます。

```text
/warn
/warnings
/unwarn
/clearwarns
/timeout
/kick
/ban
/purge
/report
/report_cancel
/automod_toggle
/log_search
/log_export
/cache_health
```

`cache-logs` には通常メッセージ、画像・添付ファイル、編集・削除、リアクション、VCログが流れます。運営メンバー以外に見えないようにしてください。

## 4. Renderで常時稼働

RenderではWebサービスではなく、Background Workerとして動かします。

1. GitHubにこのフォルダの内容をアップロードします。
2. RenderでNew Blueprintを選び、このリポジトリを選びます。
3. `render.yaml` を使って `Cache` workerを作成します。
4. 環境変数 `DISCORD_TOKEN` にBotトークンを設定します。
5. 必要なら `SYNC_GUILD_ID` にDiscordサーバーIDを設定します。複数サーバーで使う場合は `123,456` のようにカンマ区切りで入れます。
6. Deployします。

この構成では `/data` に永続ディスクを付け、SQLiteログDBを `/data/bot.sqlite3` に保存します。

### APIキーでRender設定を自動反映する場合

Render DashboardのAccount SettingsでAPIキーを作成し、プロジェクト直下に `.render_api_key` という名前で保存します。このファイルはGitHubへ上がらない設定です。

その後、次を実行するとCacheサービスを探し、必要な環境変数を設定して再デプロイします。

```powershell
.\.venv\Scripts\python.exe .\scripts\render_apply.py --sync-guild-id "サーバーID" --owner-ids "自分のDiscordユーザーID"
```

複数サーバーの場合:

```powershell
.\.venv\Scripts\python.exe .\scripts\render_apply.py --sync-guild-id "サーバーID1,サーバーID2" --owner-ids "自分のDiscordユーザーID"
```

既存の `DISCORD_TOKEN` は上書きしません。未設定の場合だけ、結果に `missing_required_env` として表示されます。

## 5. 注意

- Botのロールは、Kick/Ban/Timeoutしたい対象メンバーより上に置いてください。
- `Manage Roles` は、Botより下のロールだけ操作できます。
- ログには個人情報が含まれる可能性があります。サークル内で、保存目的・閲覧できる人・保存期間を共有してから運用してください。
