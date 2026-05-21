# Discord Circle Logger Bot

大学サークルのDiscordサーバー向けに、VC入退室、メッセージ編集・削除、必要に応じたメッセージ本文のログを保存するBotです。

標準では、会話内容を公開ログチャンネルに丸ごと流すのではなく、SQLiteに保存して、管理権限のある人だけがスラッシュコマンドで検索・CSV出力できる方式にしています。編集・削除・VC入退室などの重要イベントは、設定したログチャンネルへ通知できます。

## 主な機能

- VC入室、退室、移動の保存と通知
- メッセージ作成、編集、削除の保存
- 編集ログのBefore / After確認
- 削除メッセージの確認
- Auttaja風の `-setup` / `-warn` / `-mute` / `-kick` / `-ban` / `-purge`
- スラッシュコマンド版の警告、タイムアウト、Kick、Ban、通報
- 自動モデレーション
  - 連投対策
  - Discord招待リンク対策
  - 大量メンション対策
  - Zalgo/装飾過多テキスト対策
  - 任意で通常リンク対策
  - 任意でレイド検知
- 管理者向けのログ検索
- CSVエクスポート
- Discord上からログ機能のON/OFF切り替え
- Bot状態確認
- 古いログの自動整理

## セットアップ

このフォルダでは、Bot専用のPython 3.12環境を `.venv` に作成済みです。通常は次のファイルを実行すれば起動できます。

```powershell
.\start_bot.bat
```

PowerShellのスクリプト実行が許可されている環境では、こちらでも起動できます。

```powershell
.\start_bot.ps1
```

別のPCで最初から準備する場合は、以下の手順でセットアップします。

1. Python 3.11以上を用意します。
2. 依存ライブラリを入れます。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. `.env.example` を `.env` にコピーして、`DISCORD_TOKEN` を設定します。

```powershell
Copy-Item .env.example .env
```

4. Discord Developer PortalでBotを作成し、以下のPrivileged Gateway Intentsを有効にします。

- Message Content Intent
- Server Members Intent

5. Bot招待URLには、最低限以下の権限を付与してください。

- Send Messages
- Use Slash Commands
- Embed Links
- Attach Files
- Read Message History
- View Channels
- Manage Messages
- Manage Channels
- Manage Roles
- Moderate Members
- Kick Members
- Ban Members

6. Botを起動します。

```powershell
.\start_bot.bat
```

## 最初に使うコマンド

サーバー内で、管理権限を持つユーザーが実行します。

```text
/auttaja_setup
/log_status
```

`/auttaja_setup` は次の設定を自動作成します。

- `Bot管理スタッフ` ロール
- `bot-management` カテゴリ
- `bot-logs` チャンネル
- `mod-logs` チャンネル
- `reports` チャンネル
- ログ保存、VCログ、編集/削除ログ、自動モデレーションの初期設定

スタッフには、作成された `Bot管理スタッフ` ロールを手動で付けてください。

## 管理コマンド

### Auttaja風テキストコマンド

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

### スラッシュコマンド

```text
/log_setup
```

ログ通知先のチャンネルを設定します。

```text
/log_status
```

現在の設定と保存件数を確認します。

```text
/log_toggle
```

メッセージログ、VCログ、通知投稿、管理コマンドログをON/OFFできます。

```text
/log_search
```

保存済みログを検索します。対象はメッセージ作成、編集、削除、メッセージ全体、VCから選べます。

```text
/log_export
```

直近1〜90日分のログをCSVで出力します。

```text
/bot_health
```

Botの状態を確認します。

```text
/automod_toggle
```

自動モデレーションの各機能をON/OFFできます。

```text
/warn
/warnings
/clearwarns
/timeout
/kick
/ban
/purge
/report
```

警告、処罰、削除、通報をDiscord内から実行できます。

## 保存期間

`.env` の `RETENTION_DAYS` でログ保存日数を変更できます。初期値は180日です。`0` にすると自動削除しません。

## 運用方針

会話ログは個人情報を含む可能性があります。サークル内で運用する場合は、ログ取得の目的、閲覧できる人、保存期間を明示してから導入するのがおすすめです。

特定のテキストチャンネルへ全会話を転送する方式も可能ですが、閲覧権限の事故が起きやすいため、このBotでは「DB保存＋管理者コマンド検索」を標準にしています。

## サーバーで動かす場合

常時起動するBotなので、Webサイト用の無料枠よりも、常駐プロセスを動かせる環境が向いています。

- 小規模ならRenderのBackground Worker
- 安定運用ならVPS
- 自宅PC運用も可能ですが、PC停止中はBotも止まります

このリポジトリには `Dockerfile` と `render.yaml` を入れてあるため、RenderのBlueprintからBackground Workerとしてデプロイできます。

Renderで動かす場合は、環境変数に最低限これを設定します。

```text
DISCORD_TOKEN=Botトークン
DATABASE_PATH=/data/bot.sqlite3
RETENTION_DAYS=180
COMMAND_PREFIX=-
```

`render.yaml` では `/data` に永続ディスクを付けています。SQLiteのログDBを消さないため、このディスク設定は外さないでください。

Discord側の権限設定とRenderの詳しい手順は [SETUP_AND_DEPLOY.md](SETUP_AND_DEPLOY.md) にまとめています。

## 注意

メッセージ編集・削除のBeforeを安定して残すため、Botは通常メッセージもDBへ保存します。不要な場合は `/log_toggle` でメッセージログをOFFにしてください。
