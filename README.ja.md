# Browser Chat Bridge

[English](README.md) | 日本語

Browser Chat Bridge は、ブラウザ上の Gemini 互換チャット UI を、run 単位で安全に操作するためのローカルブリッジです。呼び出し側は新しいプロンプトだけを送信し、会話履歴はクラウド側の同一会話に保持されます。

## 構成

Browser Host、Driver、Bridge を独立したローカルプロセスとして動かします。既定では loopback のみを利用します。

```powershell
$env:CHAT_DRIVER_CDP_ENDPOINT = "http://127.0.0.1:51881"
$env:CHAT_DRIVER_BACKEND = "chromium"
python -m browser_chat_bridge.driver_server
python -m browser_chat_bridge.bridge_server
```

設定例は `.env.example` を参照してください。実際の認証情報やPC固有パスはリポジトリへコミットしないでください。

## Windows の運用

付属のスクリプトで Browser Host / Driver / Bridge をまとめて管理できます。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
powershell -ExecutionPolicy Bypass -File scripts/status.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

Microsoft Edge は実際の browser-chat turn が受理されるまで遅延起動されます。専用プロファイルを変更する場合は `CHAT_BROWSER_PROFILE` を使用してください。

## 同時実行

Bridge が新規に受理する turn は最大2件です。3件目は Driver へ送信する前に `BUSY` を返すため、遅延した重複送信を避けながら上位層でフォールバックできます。

## セキュリティ境界

- CDP とローカルサービスは loopback を前提とします。
- ブラウザプロファイル、実行ログ、`.env`、鍵・証明書類は Git 管理外です。
- 公開リポジトリには実PC固有パスや資格情報を置かず、必要な差異は環境変数で与えます。

## 関連資料

- `docs/DESIGN.md`
- `docs/GEMINI_DOM_CONTRACT_20260904.md`
- `.env.example`

このリポジトリは開発中です。ブラウザ側 DOM やサービス仕様の変更に伴い、実装契約が変わる場合があります。

## ライセンス

このプロジェクトは MIT License の下で公開されています。詳細は `LICENSE` を参照してください。
