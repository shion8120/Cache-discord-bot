# Cache

大学サークルのDiscordサーバー向けに、ログ管理、通報、警告、処罰、自動モデレーションをまとめて扱う管理サービスです。

標準では、通常メッセージは編集・削除ログのBefore確認用にSQLiteへ保存し、編集・削除、リアクション、VC入退室、メンバー参加/退出、ロール更新、テキストチャンネル更新を `server-log` にチャット形式で通知します。管理権限のある人はスラッシュコマンドで検索・CSV出力できます。

## 主な機能

- VC入室、退室、移動の保存と通知
- メッセージ作成、編集、削除の保存
- 画像・添付ファイルの保存と通知
- リアクション追加・削除・一括削除の保存と通知
- 編集ログのBefore / After確認
- 削除メッセージの確認
- メンバー参加/退出、ロール作成/更新、テキストチャンネル更新の通知
- `-setup` / `-warn` / `-mute` / `-kick` / `-ban` / `-purge` などのテキストコマンド
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
- Cache状態確認
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
- View Audit Log
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
/cache_setup
/log_status
```

`/cache_setup` は次の設定を自動作成します。

- `Cacheスタッフ` ロール
- `cache-management` カテゴリ
- `server-log` チャンネル
- ログ保存、VCログ、編集/削除ログ、自動モデレーションの初期設定

スタッフには、作成された `Cacheスタッフ` ロールを手動で付けてください。

## 管理コマンド

### テキストコマンド

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
/log-setting
```

ログ設定パネルを開きます。ONにするログを選択して「確定」を押すと、そのサーバーだけの設定として保存されます。
```text
/log_toggle
```

サーバー管理者はログ種別ごとにON/OFFできます。対象は、全通知投稿、メッセージ保存、メッセージ編集、メッセージ削除、リアクション、VC入室/退室/移動、メンバー参加/退出、ロール作成/更新、チャンネル更新、処罰、通報、管理コマンドログです。

```text
/log_search
```

保存済みログを検索します。対象はメッセージ作成、編集、削除、メッセージ全体、VC、リアクションから選べます。

```text
/log_export
```

直近1〜90日分のログをCSVで出力します。

```text
/cache_health
```

Cacheの状態を確認します。

```text
/automod_toggle
```

自動モデレーションの各機能をON/OFFできます。

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
```

警告、処罰、削除、通報をDiscord内から実行できます。

警告を1件だけ取り消す場合は、`/warnings` でCase IDを確認してから `/unwarn case_id:番号` を使います。全警告を消す場合は `/clearwarns` です。

通報を取り消す場合は、通報時に表示されるReport IDを使って `/report_cancel report_id:番号` を実行します。通報は削除せず、取り消し済みとして記録を残します。

## 保存期間

`.env` の `RETENTION_DAYS` でログ保存日数を変更できます。初期値は180日です。`0` にすると自動削除しません。

## 運用方針

会話ログは個人情報を含む可能性があります。サークル内で運用する場合は、ログ取得の目的、閲覧できる人、保存期間を明示してから導入するのがおすすめです。

`server-log` には編集・削除された会話内容や画像URLも流れます。閲覧権限の事故が起きやすいため、`server-log` は運営だけが見られる権限にしてください。ログ投稿は通知抑制付きで送信します。チャンネル投稿そのものを止めたい場合は `/log_toggle` で通知投稿をOFFにできます。

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
SERVER_LOG_CHANNEL_NAME=server-log
AUTO_SYNC_ALL_GUILDS=1
```

`render.yaml` では `/data` に永続ディスクを付けています。SQLiteのログDBを消さないため、このディスク設定は外さないでください。

Discord側の権限設定とRenderの詳しい手順は [SETUP_AND_DEPLOY.md](SETUP_AND_DEPLOY.md) にまとめています。

Render APIキーを使える場合は、`scripts/render_apply.py` で環境変数設定と再デプロイをまとめて実行できます。APIキーは `.render_api_key` に保存しても、`.gitignore` によりGitHubへは上がりません。

## 別サーバーへ導入する場合

同じCacheを別のDiscordサーバーでも使えます。ログや設定はサーバーIDごとに分かれて保存されます。

1. 既存の招待URLで新しいサーバーへCacheを招待します。
2. そのサーバーでCacheのロールを一般メンバーより上に移動します。
3. 新しいサーバーで `/cache_setup` を実行します。
4. 運営メンバーへ `Cacheスタッフ` ロールを付けます。

`AUTO_SYNC_ALL_GUILDS=1` の場合、Cacheは参加済み/新規参加サーバーのIDと名前を自動取得し、そのサーバーへスラッシュコマンドを同期します。`/cache_setup` がすぐ見えない場合だけ、Renderを再デプロイするか `SYNC_GUILD_ID` にサーバーIDを追加してください。

スラッシュコマンドが見えない場合でも、`-setup` は使えます。`SYNC_GUILD_ID` を追加して再デプロイすると、スラッシュコマンドの反映が速くなります。

## 注意

メッセージ編集・削除のBeforeを安定して残すため、Botは通常メッセージもDBへ保存します。不要な場合は `/log_toggle` でメッセージログをOFFにしてください。
