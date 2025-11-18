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
- `src/main.py`: FastAPI ベースの WebSocket プロキシ (stdout に詳細ログを出力)
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
               openAiModelName="gpt-realtime-preview" \
               openAiModelVersion="2024-08-06"
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

## ローカル実行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

ローカルの `ws://localhost:8080/ws` に対して `tests/websocket_client.py` を実行すれば、接続ハンドリングを確認できます。

## 環境変数テンプレート

ローカル実行時に必要な値は `.env.example` にまとめています。以下を参考に `.env` を作成し、Git 追跡から除外されたまま利用してください。

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt4o-realtime
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_API_VERSION=2024-08-06
PORT=8080
CONTAINER_APP_REVISION=local
```

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
  "ws://<application-gateway-ip>/ws" \
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
