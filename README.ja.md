# GPT Realtime WebSocket via Application Gateway (日本語)

English version is available in [README.md](README.md)。

## 概要

本リポジトリは、Azure Application Gateway 経由で Azure Container Apps (FastAPI) を公開し、Azure OpenAI `gpt-realtime` と WebSocket 連携する最小構成です。以下の 4 つの観点で Blue/Green デプロイ時の挙動を検証できます。

- **cooldownPeriod**: スケールダウン対象リビジョンが新規トラフィックを受けないか
- **セッションアフィニティ**: 長時間 WebSocket 接続がリビジョン切り替えで強制終了されないか
- **Graceful termination**: Container Apps の `terminationGracePeriodSeconds=30` が SIGTERM→SIGKILL に反映されるか
- **マルチリビジョン運用**: 重み付けトラフィック分割で、新旧リビジョンの接続を安全に切り替えられるか

## アーキテクチャ

```
クライアント WebSocket --> Application Gateway (HTTP) --> Azure Container Apps (FastAPI)
                                                        \--> Azure OpenAI gpt-realtime (WebSocket)
Azure Container Registry に FastAPI イメージを保存し、ACA のリビジョン切り替えで利用します。
```

主なファイル:

- `infra/main.bicep`: ACR, Azure OpenAI, Container Apps, Application Gateway を一括デプロイ
- `src/main.py`: FastAPI ベースの WebSocket プロキシ (テキストのみ / stdout ログ)
- `tests/websocket_client.py`: 5 分間接続を維持する WebSocket クライアント
- `scripts/test-blue-green.sh`: 画像ビルド/プッシュ・新リビジョンデプロイ・トラフィック切替を自動化

## 前提条件

- Azure CLI `>= 2.63`
- Bicep CLI `>= 0.27`
- Docker 24 以降
- Python 3.11
- Azure OpenAI (gpt-realtime) へのアクセス権

利用する主な環境変数:

- `RESOURCE_GROUP`
- `LOCATION`
- `PREFIX` (3 文字以上)
- `AZURE_OPENAI_DEPLOYMENT`

## インフラデプロイ手順

```bash
# リソースグループ作成
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"

# Bicep で一括デプロイ
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters prefix="$PREFIX" \
               openAiDeploymentName="$AZURE_OPENAI_DEPLOYMENT" \
               openAiDeploymentSku="GlobalStandard" \
               openAiModelName="gpt-realtime" \
               openAiModelVersion="2025-08-28"
```

コマンド完了後、`containerRegistryLoginServer`, `containerAppName`, `applicationGatewayPublicIp` などの出力を取得できます。

> **補足**: `openAiDeploymentSku` の既定値は `GlobalStandard` です。`gpt-realtime` に必須の SKU なので、Microsoft 側で新しいリアルタイム SKU が提供されるまではこの値を維持してください。

## FastAPI イメージのビルドとプッシュ

```bash
ACR_LOGIN_SERVER=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name <deployment-name> \
  --query "properties.outputs.containerRegistryLoginServer.value" -o tsv)

IMAGE_TAG=v0.1.0
IMAGE_NAME="$ACR_LOGIN_SERVER/$PREFIX-ws:$IMAGE_TAG"

az acr login --name "$ACR_LOGIN_SERVER"
docker build -t "$IMAGE_NAME" .
docker push "$IMAGE_NAME"
```

Container Apps へ適用:

```bash
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$IMAGE_NAME" \
  --revision-suffix "rev$(date +%H%M%S)"
```

Azure OpenAI の API キーを後から登録する場合:

```bash
az containerapp secret set \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --secrets azure-openai-api-key="$AZURE_OPENAI_API_KEY"
```

続いて、同じシークレットを `AZURE_OPENAI_API_KEY` 環境変数に紐づけます。

```bash
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --set-env-vars AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key
```

> `infra/main.bicep` に `openAiApiKey` パラメーターを渡した場合は、デプロイ時にシークレット作成と環境変数設定が両方行われます。Bicep にキーを渡していない場合のみ、上記 CLI で後付けしてください。

## ローカル実行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

ローカルの `ws://localhost:8080/chat` に対して `tests/websocket_client.py` を実行すれば、接続ハンドリングを確認できます。

## WebSocket の挙動

FastAPI プロキシは `/chat` を公開し、`text` フィールドを含むシンプルな JSON メッセージを受け付けます。
ユーザーのテキストを Azure OpenAI Realtime API に中継し、以下をストリーミングで返します:

- テキストレスポンス（`response.text.delta`）
- 音声レスポンス（`response.audio.delta`）
- 音声トランスクリプト（`response.audio_transcript.delta`）
- ステータスメッセージ

ルートパス（`/`）の Web インターフェースは、ブラウザベースのクライアントを提供し、
WebSocket エンドポイントに接続してテキストと音声レスポンスの両方を表示・再生します。
`tests/websocket_client.py` を `ws://localhost:8080/chat` や Application Gateway
経由のエンドポイントに向けて実行し、Blue/Green 切り替え時の挙動を観察できます。

## 環境変数テンプレート

ローカル実行時に必要な値は `.env.example` にまとめています。以下を参考に `.env` を作成し、Git 追跡から除外されたまま利用してください。

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-realtime
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_API_VERSION=2025-08-28
PORT=8080
CONTAINER_APP_REVISION=local
```

これらの値をセットすると、プロキシは `wss://<endpoint>/openai/v1?api-version=<version>&model=<deployment>` という最新クイックスタート準拠の形式で Azure に接続します。エンドポイントが `*.cognitiveservices.azure.com` ドメインの場合は、従来の `.../openai/realtime?deployment=` 形式へ自動フォールバックし、GlobalStandard 時代のデプロイでもそのまま利用できます。

## Application Gateway 経由の WebSocket 接続

FastAPI アプリケーションは、デフォルトで現在のホスト（ブラウザの `location.host`）に基づいて WebSocket エンドポイントを動的に解決します。つまり:

- Container Apps に直接アクセスした場合: `ws://<container-app-fqdn>/chat`
- Application Gateway 経由でアクセスした場合: `ws://<agw-public-ip>/chat`

### 環境変数による明示的な設定（オプション）

Application Gateway のエンドポイントを明示的に設定する必要がある場合は、`APPLICATION_GATEWAY_HOST` 環境変数を設定します:

```bash
# Application Gateway のパブリック IP を取得
AGW_IP=$(az network public-ip show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$PREFIX-pip" \
  --query ipAddress -o tsv)

# Container App に Application Gateway のホストを設定
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --set-env-vars APPLICATION_GATEWAY_HOST="$AGW_IP"
```

`APPLICATION_GATEWAY_HOST` が設定されている場合、アプリケーションは以下のエンドポイントに接続する HTML を提供します:
- `ws://<APPLICATION_GATEWAY_HOST>/chat` (HTTP の場合)
- `wss://<APPLICATION_GATEWAY_HOST>/chat` (HTTPS の場合)

### WebSocket 接続のテスト

```bash
# Application Gateway の IP を取得
AGW_IP=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name <deployment-name> \
  --query "properties.outputs.applicationGatewayPublicIp.value" -o tsv)

# WebSocket クライアントでテスト
python tests/websocket_client.py "ws://$AGW_IP/chat" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

または、ブラウザで `http://$AGW_IP/` を開いて Web インターフェースをテストできます。

### Application Gateway の設定について

Application Gateway は WebSocket 接続をサポートするよう設定されています:

- **バックエンドプロトコル**: HTTP (ポート 80) - TLS 終端は Application Gateway で実施
- **リクエストタイムアウト**: 120 秒（より長い WebSocket セッションが必要な場合は調整可能）
- **バックエンドプール**: Container Apps の ingress FQDN を指定
- **ヘルスプローブ**: `/healthz` エンドポイントを 30 秒ごとに確認

本番環境では以下を検討してください:
- Application Gateway フロントエンドに TLS/SSL 証明書を追加
- 長時間の WebSocket 接続のため `requestTimeout` を増加
- Graceful シャットダウンのため `connectionDraining` を設定

## GitHub Actions でのデプロイ

このリポジトリには `.github/workflows/deploy.yml` が含まれており、コンテナイメージのビルド → ACR へのプッシュ → Container Apps の更新を自動化できます。認証は [Configure Azure Settings](https://github.com/marketplace/configure-azure-settings) GitHub App が担うため、`azure/login` ステップは不要です。

1. Configure Azure Settings アプリをこのリポジトリにインストールし、サブスクリプション/リソースグループに紐付けます。
2. リポジトリの **Variables** (Settings → Secrets and Variables → Actions → Variables) に以下を登録します:
  - `AZURE_CONTAINER_REGISTRY` (例: `gptrtacr`)
  - `RESOURCE_GROUP` (例: `rg-gptrealtimewebsocket-demo-swedencentral-001`)
  - `CONTAINER_APP_NAME` (例: `gptrt-ws`)
3. `main` に push（または Actions から *Run workflow* で手動実行）すると、パイプラインが以下を実行します:
  - `az acr login`
  - `docker build` と `docker push`
  - 新しい revision suffix を付けた `az containerapp update`
  - 検証用に Container App の ingress FQDN を出力

## ログの観測（SIGTERM / SIGKILL）

Container Apps の stdout/stderr は Log Analytics に送られます。リアルタイムで確認する場合:

```bash
az containerapp logs show \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --follow
```

`signal.received`, `client.connected`, `bridge.completed`, `connection.closed` などのログを追跡すると、Graceful termination の挙動（SIGTERM→SIGKILL のタイミング等）を把握できます。

## Blue/Green ロールアウト支援スクリプト

`scripts/test-blue-green.sh` はシンプルなロールアウトを自動化します:

```bash
export RESOURCE_GROUP=<rg>
export CONTAINERAPP_NAME=$PREFIX-ws
export ACR_NAME=<acr-name>
export IMAGE_TAG=v0.1.1

./scripts/test-blue-green.sh
```

スクリプトの処理内容:

1. `docker build` + `docker push`
2. `az containerapp update`（新しい revision suffix）
3. 重み付けトラフィック分割（50/50 → 新リビジョンへ 100%）
4. 旧リビジョンの無効化（deactivate）
5. SIGTERM のタイミング観測用に `az containerapp logs show --follow` コマンドを出力

## 長時間接続テスト

5 接続を 5 分維持し、ロールアウト中のルーティング挙動を観察します:

```bash
python tests/websocket_client.py \
  "ws://<application-gateway-ip>/chat" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

## 次のステップ

- Application Gateway に TLS 証明書を追加
- Application Insights を統合してより深いテレメトリを取得
- FastAPI プロキシを拡張し、構造化ログを Azure Monitor テーブルへ送信

## Sideband アーキテクチャ デモ（WebRTC + WebSocket のセッション分離）

このリポジトリには、OpenAI の [sideband server controls](https://platform.openai.com/docs/guides/realtime-server-controls) の考え方（ユーザー↔OpenAI の接続と、サーバー↔OpenAI の制御チャネルを分離する）をデモする実装が含まれています。

**Azure OpenAI と OpenAI 直 API の両方に対応しています。**

### アーキテクチャ

```
┌─────────────┐                      ┌─────────────────────┐
│    User     │◄─────WebRTC─────────►│  Azure OpenAI /     │
│  (Browser)  │   (audio/video)      │  OpenAI Realtime    │
└─────────────┘                      └─────────────────────┘
                                              ▲
                                              │
                                         WebSocket
                                        (call_id)
                                              │
                                        ┌─────┴─────┐
                                        │   Server  │
                                        │ (Control) │
                                        └───────────┘
```

### メリット

- **低遅延**: 音声は WebRTC でユーザーと OpenAI 間を直接流れ、サーバーをバイパス
- **スケーラビリティ**: サーバーが音声データを扱わないため帯域/CPU を節約
- **責務分離**: サーバーは業務ロジック・ツール・セッション管理に集中し、メディアは独立
- **コスト効率**: 音声処理のためのサーバーリソースを削減

### 仕組み

1. **セッション作成**: サーバーが追跡用のセッション ID を発行
2. **WebRTC 接続**: ユーザーが OpenAI へ WebRTC 接続し、`call_id` を取得
3. **Sideband 接続**: サーバーが `call_id` を使って同一セッションへ WebSocket 接続
4. **並行動作**: 音声はユーザー↔OpenAI、制御/監視はサーバー↔OpenAI で同時に動作

### エンドポイント

| エンドポイント | メソッド | 説明 |
|----------|--------|-------------|
| `/sideband` | GET | Sideband デモ用 Web UI |
| `/sideband/config` | GET | 現在のプロバイダー設定を取得 |
| `/sideband/session` | POST | Sideband セッションを作成 |
| `/sideband/ephemeral-key` | POST | WebRTC 用の ephemeral key を取得 |
| `/sideband/offer` | POST | WebRTC SDP offer を交換 |
| `/sideband/control/{session_id}` | WS | サーバー側 Sideband 制御 WebSocket |
| `/sideband/sessions` | GET | アクティブなセッション一覧 |
| `/sideband/session/{session_id}` | GET | セッション詳細 |

### Azure OpenAI でのテスト

1. **環境変数の設定**:

```bash
# Azure OpenAI 用（必須）
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
export AZURE_OPENAI_API_KEY=your-api-key
export AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o-realtime-preview

# 対応モデル: gpt-4o-realtime-preview, gpt-4o-mini-realtime-preview, gpt-realtime, gpt-realtime-mini
# 対応リージョン: East US 2, Sweden Central
```

2. **サーバー起動**:

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

3. **デモへアクセス**: ブラウザで `http://localhost:8080/sideband`

4. **画面の手順に従う**:
   - "1. Create Session" でセッション初期化
   - "2. Connect WebRTC" で Azure OpenAI へ直接音声接続（`call_id` 取得）
   - "3. Connect Server Sideband" でサーバー制御チャネルを接続
   - "Start Microphone" で音声ストリーミング開始
   - "Update Instructions" / "Send Server Message" でサーバー側制御をデモ

### OpenAI 直 API でのテスト

Azure OpenAI がない場合は、OpenAI 直 API を利用できます:

```bash
# OpenAI 直 API 用（AZURE_OPENAI_ENDPOINT を設定しない）
export OPENAI_API_KEY=sk-your-api-key
export OPENAI_REALTIME_MODEL=gpt-4o-realtime-preview-2024-12-17
```

### ログの見どころ

デモ実行中は、サーバーログで「セッション分離」が確認できます:

```
============================================================
[SIDEBAND SESSION LOG] 2025-11-30T10:30:15.123456
  Provider: AZURE
  Session ID: sideband_abc123def456
  Call ID: rtc_u1_9c6574da8b8a41a18da9308f4ad974ce
  Event: Server WebSocket Connected
  Details: Now BOTH user (WebRTC) and server (WebSocket) are connected to the SAME Azure OpenAI session!
  WebRTC Connected: True
  WebSocket (Server) Connected: True
  Events from OpenAI: 5
  Events to OpenAI: 2
============================================================
```

主に以下のログが重要です:
- `[NEW SIDEBAND SESSION CREATED]` - セッション初期化
- `[WEBRTC CONNECTION ESTABLISHED]` - WebRTC 接続確立（`call_id` 取得）
- `[SERVER SIDEBAND CONNECTION STARTING]` - サーバーが同一セッションへ接続開始
- `[SIDEBAND SESSION LOG]` - WebRTC と WebSocket の両方が接続されている状態のスナップショット

### 実装の要点ファイル

- [src/sideband.py](src/sideband.py) - Sideband モジュール（Azure OpenAI / OpenAI 直 API 対応）
- [src/main.py](src/main.py) - Sideband エンドポイントを統合した FastAPI アプリ

### Azure OpenAI 固有のメモ

- **API エンドポイント**:
  - Ephemeral key: `https://{resource}.openai.azure.com/openai/v1/realtime/client_secrets`
  - WebRTC 呼び出し: `https://{resource}.openai.azure.com/openai/v1/realtime/calls`
  - WebSocket sideband: `wss://{resource}.openai.azure.com/openai/v1/realtime?call_id={call_id}`
- **認証**: API key（`api-key` ヘッダー）または Azure AD Bearer token
- **対応リージョン**: East US 2, Sweden Central（GlobalStandard デプロイ）
