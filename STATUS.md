# Surge Radar — 本番移行状態レポート
最終更新: 2026-06-26 (セッション4 — クラウド本番実装完了)

---

## 完成基準チェックリスト

| 基準 | ローカル | クラウド |
|------|---------|---------|
| PCをつけなくてもサイトが見られる | ❌ | 🔲 要デプロイ |
| スマホから外出先でも使える | ❌ | 🔲 要デプロイ |
| クラウドDBにデータが保存される | ❌ | 🔲 要移行 |
| 毎営業日クラウド上で自動実行 | ❌ | 🔲 要GitHub設定 |
| 材料が複数ソースから取得・分析 | ✅ TDnet+EDINET+Kabutan | ✅ 同左 |
| 材料不足・失敗が画面で分かる | ✅ | ✅ |
| 全対象銘柄で予測ランキング | ✅ | 🔲 要デプロイ後確認 |
| live予測保存・成否判定・再学習 | ✅ | 🔲 要デプロイ後確認 |
| スマホでランキング・チャート確認 | LAN内のみ | 🔲 要デプロイ |
| Pipeline失敗時に通知 | ✅ログページ | ✅ GitHub Actionsメール |
| PWAホーム画面追加 | ✅ | ✅ |
| Push通知 (リアルタイム) | ❌ | 🔲 VAPID鍵設定後 |

---

## クラウド本番構成

```
[スマホ/外部] → HTTPS → [Render.com 無料 Web]
                                    ↓
                      [Supabase PostgreSQL 無料 500MB]
                                    ↑
           [GitHub Actions 毎営業日 16:40 JST 自動実行]
```

| コンポーネント | サービス | 費用 | 状態 |
|--------------|---------|------|------|
| Web サーバー | Render free | 無料(15分スリープ) | 🔲 未デプロイ |
| PostgreSQL | Supabase free | 無料(500MB) | 🔲 未作成 |
| 日次Cron | GitHub Actions | 無料(public repo) | 🔲 未プッシュ |
| モデル保存 | PostgreSQL BYTEA | 無料 | 🔲 移行待ち |
| PWA Push通知 | Web Push (VAPID) | 無料 | 🔲 鍵未生成 |
| ドメイン | .onrender.com | 無料 | 🔲 デプロイ後判明 |

**公開URL**: 未デプロイ (デプロイ後ここに記入)

---

## クラウド移行手順

### STEP 1: Supabase PostgreSQL セットアップ (5分)

```
1. https://supabase.com → 無料アカウント作成
2. New Project → 名前: surge-radar / リージョン: Northeast Asia (Tokyo)
3. Settings → Database → Connection string (URI) をコピー
   形式: postgresql://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
4. この文字列を DATABASE_URL として保存 (後で使う)
```

### STEP 2: スキーマ初期化 + データ移行

```powershell
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@db.XXXX.supabase.co:5432/postgres"

cd C:\Users\rorom\jp_surge_radar

# PG スキーマ作成
.venv\Scripts\python.exe -m surge_radar.cli init-db

# SQLite → PostgreSQL データ移行 (約233MB, 数分)
.venv\Scripts\python.exe scripts/migrate_to_pg.py
```

### STEP 3: GitHub リポジトリ + Secrets

```powershell
cd C:\Users\rorom\jp_surge_radar
git init
git add .
git commit -m "Cloud-ready surge radar"
git remote add origin https://github.com/YOUR_USERNAME/jp-surge-radar.git
git push -u origin main
```

GitHub → Settings → Secrets → Actions → New secret:

| Secret名 | 値 |
|----------|---|
| `DATABASE_URL` | Supabase 接続文字列 |
| `VAPID_PRIVATE_KEY` | (下記 STEP 4 で生成) |
| `VAPID_PUBLIC_KEY` | (下記 STEP 4 で生成) |
| `VAPID_ADMIN_EMAIL` | あなたのメールアドレス |

### STEP 4: VAPID 鍵生成 (Push通知用)

```powershell
.venv\Scripts\python.exe scripts/gen_vapid_keys.py
# 出力された2つの値を GitHub Secrets と Render 環境変数に設定
```

### STEP 5: Render デプロイ

```
1. https://render.com → GitHub でサインアップ
2. New → Web Service → リポジトリ選択
3. Build: pip install -r requirements.txt
   Start: python -m surge_radar.cli serve --host 0.0.0.0 --port $PORT
   Plan: Free
4. Environment Variables:
   DATABASE_URL / VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY / VAPID_ADMIN_EMAIL
5. Deploy → 2〜3分後に https://surge-radar-xxxx.onrender.com が起動
```

### STEP 6: 動作確認

```
1. ブラウザで https://...onrender.com → ランキング表示 (初回30秒待ち)
2. スマホ → 同URL → ホーム画面に追加 (PWA)
3. GitHub Actions → Daily Pipeline → Run workflow (手動実行テスト)
4. 平日16:40 JST に自動実行されることを翌日確認
```

---

## 実装済みコード (全て完了)

| 機能 | ファイル | 状態 |
|------|---------|------|
| DB デュアルモード SQLite/PostgreSQL | db.py | ✅ |
| 全SQL %s プレースホルダ統一 | 全pyファイル | ✅ |
| INSERT OR IGNORE → ON CONFLICT 自動変換 | db.py _adapt_pg() | ✅ |
| モデル DB保存 BYTEA | model.py | ✅ |
| render.yaml | render.yaml | ✅ |
| GitHub Actions daily 16:40 JST | .github/workflows/daily.yml | ✅ |
| PWA manifest.json | static/manifest.json | ✅ |
| PWA アイコン | static/icon-192.png, icon-512.png | ✅ |
| Service Worker (scope /) | static/sw.js + /sw.js ルート | ✅ |
| Push通知テーブル push_subscriptions | db.py | ✅ |
| Push購読エンドポイント | web/app.py | ✅ |
| Push送信ヘルパ | push_notify.py | ✅ |
| Pipeline完了・失敗 Push通知 | pipeline.py | ✅ |
| EDINET APIキー不要化 | pipeline.py | ✅ |
| 材料ソース別件数 (overview) | queries.py | ✅ |
| 材料ステータスパネル UI | templates/ranking.html | ✅ |
| モバイルカード改善 (チャート/出来高/classify_path) | templates/ranking.html | ✅ |
| SQLite→PostgreSQL 移行スクリプト | scripts/migrate_to_pg.py | ✅ |
| VAPID 鍵生成スクリプト | scripts/gen_vapid_keys.py | ✅ |

---

## 現在のローカルデータ状況 (2026-06-26)

```
証券マスタ:   3,572 銘柄
株価データ:   3,572 銘柄(100%)
評価対象:     2,827 銘柄
材料DB:       1,801 件 (TDnet + EDINET + Kabutan)
教師データ:   36,665件 (historical_pos:12,215 / historical_neg:24,450 / live_fail:40)
モデル:       v20260626_105519 (CV AUC: 0.7558)
DBサイズ:     233.4 MB (Supabase 500MB 内に収まる)
ローカルURL:  http://localhost:8012
クラウドURL:  (デプロイ後記入)
```

## 材料ソース状況

| ソース | 方式 | APIキー | 状況 |
|--------|------|---------|------|
| TDnet (yanoshin) | 範囲一括取得 | 不要 | ✅ 稼働 |
| EDINET (金融庁) | 日付別一覧 v2 | 不要 | ✅ 稼働 |
| Kabutan | HTML スクレイピング | 不要 | ✅ 上位銘柄補完 |
| Yahoo Finance News | REST API | 不要 | ✅ 英語補完 |

## 料金

| サービス | 無料枠 | 注意点 |
|---------|------|-------|
| Supabase | 500MB/2プロジェクト | DB が 500MB 超えたら有料 |
| Render | 750h/月 | 月750hを超えると停止 (1インスタンスなら余裕) |
| GitHub Actions | public repo: 無制限 | private: 2000min/月 |
| Render スリープ | 15分不使用でスリープ | 初回アクセス30秒待ち |
