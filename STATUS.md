# Surge Radar — 本番状態レポート
最終更新: 2026-06-27 (クラウド移行完了)

---

## 完成基準チェックリスト

| 基準 | 状態 |
|------|------|
| PCをつけなくてもサイトが見られる | 🔲 Renderデプロイ後 |
| スマホから外出先でも使える | 🔲 Renderデプロイ後 |
| クラウドDBにデータが保存される | ✅ Neon PostgreSQL 移行完了 |
| 毎営業日クラウド上で自動実行 | ✅ GitHub Actions 設定済み(07:40 UTC) |
| 材料が複数ソースから取得・分析 | ✅ TDnet+EDINET+Kabutan+Yahoo |
| 材料不足・失敗が画面で分かる | ✅ |
| 全対象銘柄で予測ランキング | ✅ 3,572銘柄 |
| live予測保存・成否判定・再学習 | ✅ |
| スマホでランキング・チャート確認 | 🔲 Renderデプロイ後 |
| Pipeline失敗時に通知 | ✅ GitHub Actionsメール |
| PWAホーム画面追加 | ✅ (Renderデプロイ後有効) |
| Push通知 (リアルタイム) | 🔲 VAPID鍵設定済、Renderデプロイ後 |

---

## クラウド本番構成

```
[スマホ/外部] → HTTPS → [Render.com 無料 Web]
                                    ↓
                      [Neon PostgreSQL 無料 (surge_radar DB)]
                                    ↑
           [GitHub Actions 毎営業日 07:40 UTC = 16:40 JST 自動実行]
```

| コンポーネント | サービス | 費用 | 状態 |
|--------------|---------|------|------|
| Web サーバー | Render free | 無料(15分スリープ) | 🔲 未デプロイ |
| PostgreSQL | Neon free (surge_radar DB) | 無料(512MB) | ✅ 稼働中 |
| 日次Cron | GitHub Actions | 無料(public repo) | ✅ 設定済み |
| モデル保存 | PostgreSQL BYTEA | 無料 | ✅ 3モデル移行済み |
| PWA Push通知 | Web Push (VAPID) | 無料 | ✅ 鍵設定済み |
| ドメイン | .onrender.com | 無料 | 🔲 デプロイ後判明 |

---

## GitHubリポジトリ・Secrets 状態

- **GitHub Repo**: https://github.com/roromukuro-afk/jp-surge-radar (public)
- **GitHub Secrets設定済み**:
  - `DATABASE_URL` ✅ (Neon PostgreSQL surge_radar)
  - `VAPID_PRIVATE_KEY` ✅
  - `VAPID_PUBLIC_KEY` ✅
  - `VAPID_ADMIN_EMAIL` ✅

---

## データベース移行状況 (SQLite → Neon PostgreSQL)

| テーブル | SQLite件数 | PostgreSQL件数 | 状態 |
|---------|-----------|---------------|------|
| securities | 3,572 | 3,572 | ✅ |
| materials | 1,801 | 1,801 | ✅ |
| teacher_samples | 36,665 | 36,665 | ✅ |
| model_meta | 3 | 3 | ✅ (BYTEA含む) |
| predictions | 650 | 650 | ✅ |
| prediction_outcomes | 50 | 50 | ✅ |
| theme_regime | 12 | 12 | ✅ |
| job_logs | 20 | 20 | ✅ |
| push_subscriptions | 0 | 0 | ✅ |
| **prices** | **1,740,991** | **0** | ⚡ 初回GitHub Actions実行時に自動取得 |

---

## Renderデプロイ手順 (残作業)

### STEP 1: Renderアカウント作成
1. https://render.com → 「Get Started for Free」
2. **GitHub アカウントでサインイン** (roromukuro-afk)
3. ダッシュボードが開く

### STEP 2: Web Service 作成
1. 「New +」→「Web Service」
2. リポジトリ選択: `roromukuro-afk/jp-surge-radar`
3. 設定確認 (render.yaml が自動読み込みされる):
   - Name: `surge-radar`
   - Region: Singapore
   - Branch: `master`
   - Build: `pip install -r requirements.txt`
   - Start: `python -m surge_radar.cli serve --host 0.0.0.0 --port $PORT`
   - Plan: **Free**

### STEP 3: 環境変数設定
「Environment」タブで以下を設定:

| 変数名 | 値 |
|--------|---|
| `DATABASE_URL` | (Neon接続文字列 - .env参照) |
| `VAPID_PRIVATE_KEY` | (scripts/gen_vapid_keys.py 実行済みの値 - .env参照) |
| `VAPID_PUBLIC_KEY` | `BGPbIu5i_U-VwtetW8VyH-5ezvNEcFK1dZqv6RE84xB1_WNJEu3FtEicVv-23fZkCkPBzitILNaZz3Y7zMKRyqE` |
| `VAPID_ADMIN_EMAIL` | `roromukuro@gmail.com` |
| `SURGE_ENABLE_LLM` | `0` |

### STEP 4: デプロイ
「Create Web Service」→ 約3分でビルド完了 →
`https://surge-radar-xxxx.onrender.com` が起動

---

## 現在のデータ状況 (2026-06-27)

```
DB:             Neon PostgreSQL (surge_radar)
証券マスタ:     3,572 銘柄
株価データ:     0件 (初回GitHub Actions実行で取得予定)
評価対象:       3,572 銘柄
材料DB:         1,801件 (TDnet:119 / Kabutan:1,362)
教師データ:     36,665件 (live_fail=40)
モデル:         v20260626_105519 (CV AUC: 0.7558)
本日予測:       B=30, C=42, D=228
ローカルURL:    http://localhost:8012
クラウドURL:    (Renderデプロイ後記入)
```

---

## 材料ソース状況

| ソース | 方式 | APIキー | 状況 |
|--------|------|---------|------|
| TDnet (yanoshin) | 範囲一括取得 | 不要 | ✅ 稼働 |
| EDINET (金融庁) | 日付別一覧 v2 | 不要 | ✅ 稼働 |
| Kabutan | HTML スクレイピング | 不要 | ✅ 稼働 |
| Yahoo Finance News | REST API | 不要 | ✅ 稼働 |

---

## 料金

| サービス | 無料枠 | 注意点 |
|---------|------|-------|
| Neon | 512MB / 1プロジェクト | 500MB超えたら有料。prices除いて移行 |
| Render | 750h/月 | 月750hを超えると停止 (1インスタンスなら余裕) |
| GitHub Actions | public repo: 無制限 | private: 2000min/月 |
| Render スリープ | 15分不使用でスリープ | 初回アクセス30秒待ち |

---

## 残課題

- [ ] Renderデプロイ (ユーザーがRenderアカウント作成・接続)
- [ ] スマホ外部アクセス確認
- [ ] PWAホーム画面追加テスト
- [ ] Push通知テスト
- [ ] GitHub Actions 手動実行確認 (workflow_dispatch認識待ち)
- [ ] 初回GitHub Actions実行: prices再取得・予測再生成

---

## 実装済みコード (全て完了)

| 機能 | ファイル | 状態 |
|------|---------|------|
| DB デュアルモード SQLite/PostgreSQL | db.py | ✅ |
| 全SQL %s プレースホルダ統一 | 全pyファイル | ✅ |
| INSERT OR IGNORE → ON CONFLICT 自動変換 | db.py _adapt_pg() | ✅ |
| モデル DB保存 BYTEA | model.py | ✅ |
| render.yaml | render.yaml | ✅ |
| GitHub Actions 毎営業日 16:40 JST | .github/workflows/daily.yml | ✅ |
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
| モバイルカード改善 | templates/ranking.html | ✅ |
| SQLite→PostgreSQL 移行スクリプト | scripts/migrate_to_pg.py | ✅ |
| VAPID 鍵生成スクリプト | scripts/gen_vapid_keys.py | ✅ |
