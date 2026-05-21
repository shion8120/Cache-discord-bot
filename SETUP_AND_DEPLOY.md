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
/auttaja_setup
```

これで次が自動作成されます。

- `Bot管理スタッフ` ロール
- `bot-management` カテゴリ
- `bot-logs`
- `mod-logs`
- `reports`

その後、Botを使う運営メンバーへ `Bot管理スタッフ` ロールを付けてください。

## 3. Auttaja風コマンド

```text
-setup
-help
-ping
-warn @user 理由
-warnings @user
-clearwarns @user
-mute @user 10m 理由
-kick @user 理由
-ban @user 理由
-purge 50
-report @user 理由
```

スラッシュコマンドでも同じように使えます。

```text
/warn
/warnings
/clearwarns
/timeout
/kick
/ban
/purge
/report
/automod_toggle
/log_search
/log_export
```

## 4. Renderで常時稼働

RenderではWebサービスではなく、Background Workerとして動かします。

1. GitHubにこのフォルダの内容をアップロードします。
2. RenderでNew Blueprintを選び、このリポジトリを選びます。
3. `render.yaml` を使って `circle-discord-bot` workerを作成します。
4. 環境変数 `DISCORD_TOKEN` にBotトークンを設定します。
5. 必要なら `SYNC_GUILD_ID` に自分のDiscordサーバーIDを設定します。
6. Deployします。

この構成では `/data` に永続ディスクを付け、SQLiteログDBを `/data/bot.sqlite3` に保存します。

## 5. 注意

- Botのロールは、Kick/Ban/Timeoutしたい対象メンバーより上に置いてください。
- `Manage Roles` は、Botより下のロールだけ操作できます。
- ログには個人情報が含まれる可能性があります。サークル内で、保存目的・閲覧できる人・保存期間を共有してから運用してください。
