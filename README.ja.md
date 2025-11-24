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
  --secrets azure-openai-api-key="$AZURE_OPENAI_KEY"
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
AZURE_OPENAI_API_VERSION=2025-04-01-preview
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

## ログと SIGTERM の観測

Container Apps の stdout/stderr は Log Analytics に送られます。リアルタイムで確認する場合:

```bash
az containerapp logs show \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --follow
```

`signal.received`, `client.connected`, `bridge.completed` などのログを追跡すると、Graceful termination の挙動を把握できます。

## Blue/Green ロールアウト支援スクリプト

```bash
export RESOURCE_GROUP=<rg>
export CONTAINERAPP_NAME=$PREFIX-ws
export ACR_NAME=<acr-name>
export IMAGE_TAG=v0.1.1

./scripts/test-blue-green.sh
```

スクリプトの処理内容:

1. `docker build` + `docker push`
2. 新リビジョンの `az containerapp update`
3. 50/50 → 100% の重み付けトラフィック変更
4. 旧リビジョンの `az containerapp revision deactivate`
5. `az containerapp logs show --follow` コマンドを表示 (ログストリーム追跡用)

## 長時間接続テスト

```bash
python tests/websocket_client.py \
  "ws://<application-gateway-ip>/chat" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

5 分間接続を維持しながらトラフィック分割を切り替えることで、WebSocket が切断されないかを確認できます。

## GitHub Actions でのデプロイ

`.github/workflows/deploy.yml` を使うと、Docker イメージのビルド/プッシュと Container Apps 更新を自動化できます。すでに [Configure Azure Settings](https://github.com/marketplace/configure-azure-settings) アプリをこのリポジトリにインストールしている前提なので、`azure/login` アクションは不要です。

1. Configure Azure Settings アプリで対象サブスクリプション/リソースグループへのアクセスを許可。
2. リポジトリの **Variables** (Settings → Secrets and Variables → Actions → Variables) に以下を登録:
  - `AZURE_CONTAINER_REGISTRY` (例: `gptrtacr`)
  - `RESOURCE_GROUP` (例: `rg-gptrealtimewebsocket-demo-swedencentral-001`)
  - `CONTAINER_APP_NAME` (例: `gptrt-ws`)
3. `main` ブランチへ push すると、ワークフローが自動で ACR へのログイン / `docker build` / `docker push` / `az containerapp update` を実行し、最後に公開 FQDN を表示します。手動実行したい場合は Actions 画面で `Run workflow` を押してください。

## 次のステップ

- Application Gateway に TLS 証明書を適用し HTTPS 化
- Application Insights にメトリクスを送信
- FastAPI 側ログを JSON 形式にし Azure Monitor KQL で分析
