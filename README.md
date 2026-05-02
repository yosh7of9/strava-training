# Strava Training Dashboard

Stravaと連携し、日々のトレーニング負荷を自動で計算・管理するダッシュボードアプリです。
パフォーマンス管理チャート（PMC: Performance Management Chart）を用いて、現在のフィットネス（CTL）、疲労（ATL）、そして調子（TSB/Form）を可視化します。

## ✨ 主な機能

- **Strava OAuth認証**: Stravaアカウントを利用したセキュアなログイン。
- **アクセス制限**: 指定されたStrava Athlete IDのユーザーのみがアクセス可能（安全なプライベート運用）。
- **TSS自動計算**: 
  - パワーデータがある場合は正規のTSSを計算。
  - パワーがない場合はSuffer Score、もしくは平均心拍数から推定TSS（hrTSS相当）を自動フォールバック計算。
- **PMCグラフ描画**: Chart.jsを用いたインタラクティブでレスポンシブなグラフ表示。
- **Webhook自動連携**: Stravaでアクティビティを保存した瞬間にWebhookを受け取り、GCP Pub/Sub経由でバックグラウンド処理を行い、DB（Firestore）を自動更新。

## 🛠 技術スタック

- **Backend**: Python 3.11+, FastAPI
- **Frontend**: HTML/Jinja2, Tailwind CSS, Chart.js
- **Database**: Google Cloud Firestore
- **Messaging**: Google Cloud Pub/Sub
- **Deploy Target**: Google Cloud Run (予定)

## 🚀 ローカル環境での動かし方

### 1. リポジトリのクローンと環境準備

```bash
git clone https://github.com/yosh7of9/strava-training.git
cd strava-training
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、Strava APIの情報を入力します。

```bash
cp .env.example .env
# .env を開いて STRAVA_CLIENT_ID などを追記してください
```

### 3. アクセス権限の設定

他人がアクセスできないようにするため、許可するユーザーのStrava Athlete IDを登録します。
`allowed_users.example.txt` をコピーして `allowed_users.txt` を作成し、ご自身のIDを追記してください。

```bash
cp allowed_users.example.txt allowed_users.txt
# allowed_users.txt を開いて自分のIDを追記
```

### 4. アプリの起動

```bash
uvicorn main:app --reload
```

ブラウザで `http://localhost:8000` にアクセスし、「Login with Strava」ボタンからログインしてください。
初回ログイン時は、ダッシュボード画面右上の「Sync Past Activities」をクリックして過去のデータを取得・グラフ化してください。

## 📜 フォルダ構成

- `/core`: 設定（Config）やデータベース（Firestore）接続などの基盤コード
- `/routers`: FastAPIのルーティング（Auth, Sync, Webhook, Processor 等）
- `/templates`: HTMLテンプレートファイル（Jinja2）
- `main.py`: アプリケーションのエントリーポイント
