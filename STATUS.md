# Surge Radar — 本番状態レポート
最終更新: 2026-06-27 (クラウド本番公開完了)

> 機密情報 (DATABASE_URL / VAPID_PRIVATE_KEY 等) はこのファイルに記載しない。
> 値は `.env` / GitHub Secrets / Vercel Environment Variables のみで管理。

---

## 公開URL (本番・稼働中)

**https://jpsurgeradar.vercel.app**

- スマホ・PC・外出先のモバイル回線からHTTPSでアクセス可能
- PCを閉じていても稼働 (Vercel サーバーレス)
- `/healthz` でNeon PostgreSQLの統計をリアルタイム返却 (動作確認済み)

---

## 完成基準チェックリスト

| 基準 | 状態 |
|------|------|
| Webアプリがクラウドにデプロイ | ✅ Vercel 本番稼働 |
| 公開URLでスマホから外出先でもアクセス | ✅ https://jpsurgeradar.vercel.app |
| PCを閉じても動く | ✅ Vercel サーバーレス |
| クラウドDBにデータが永続保存 | ✅ Neon PostgreSQL |
| 毎営業日クラウド自動実行 | ✅ GitHub Actions daily.yml (16:40 JST) |
| 全対象銘柄(3000円以下)を分析 | ⏳ 価格bootstrap実行中 (universe 3,572) |
| 材料・チャート・出来高・テーマ・AI類似度統合ランキング | ✅ |
| live予測保存 | ✅ predictions 693件 |
| 5/10/20営業日 成否判定 | ✅ track.py |
| 失敗予測の教師データ化 | ✅ live_fail=40 |
| 必要時に再学習 | ✅ train.retrain (CIで実行) |
| スマホでランキング・詳細・材料・チャート確認 | ✅ 全ページHTTP 200 |
| PWAホーム画面追加 | ✅ manifest.json / sw.js / icons 配信確認 |
| Push通知 | ✅ VAPID configured (購読UI稼働、購読者0) |
| STATUS.mdに現実の状態を記録 | ✅ 本ファイル |

---

## クラウド本番構成

```
[スマホ/外部] → HTTPS → [Vercel サーバーレス (FastAPI 軽量版 app_vercel)]
                                    ↓ 読み取り
                      [Neon PostgreSQL 無料 (surge_radar DB, Singapore)]
                                    ↑ 書き込み
   [GitHub Actions daily.yml : 毎営業日 07:40 UTC = 16:40 JST に全処理]
```

| コンポーネント | サービス | 費用 | 状態 |
|--------------|---------|------|------|
| Web (公開URL) | Vercel | 無料 (Hobby) | ✅ 稼働中 |
| PostgreSQL | Neon free (surge_radar) | 無料 (512MB) | ✅ 稼働中 |
| 日次パイプライン | GitHub Actions | 無料 (public repo) | ✅ 稼働中 |
| モデル保存 | PostgreSQL BYTEA | 無料 | ✅ 3モデル |
| Push通知 | Web Push (VAPID) | 無料 | ✅ 鍵設定済 |
| 予備ホスト | Render (render.yaml) | 無料 | ⬜ 任意 (Vercelで充足) |

Web層 (Vercel) は numpy/pandas/sklearn を含まない軽量依存 (`requirements.txt`)。
重いML処理はGitHub Actions側 (`requirements-pipeline.txt`) で実行し、
両者はNeon PostgreSQLを介して連携する。

---

## GitHub Actions ワークフロー (3分割)

| ワークフロー | トリガ | 役割 | 直近実績 |
|------------|--------|------|---------|
| `validate.yml` | master push | DB接続+schema+5銘柄スモーク | ✅ 2分46秒 success |
| `bootstrap.yml` | 手動 | 全銘柄の初回価格取得 (最大6h) | ⏳ 実行中 |
| `daily.yml` | cron 16:40 JST / 手動 | 本番日次 (差分株価→材料→track→retrain→predict→push) | 設定済 (55分上限) |

### パイプライン失敗扱い (本番仕様)
- **CRITICAL (失敗で全体中断)**: DB接続 / universe / ingest / predict / predictions保存 / job_logs保存
- **WARNING (記録して継続)**: TDnet / Kabutan / EDINET / themes / track / retrain / Push通知

---

## 重大な性能修正 (タイムアウト解消)

旧構成は45分でタイムアウトしていた。原因と対策:

1. **接続プーリング欠如** — `db.cursor()` が毎回 Neon(Singapore) へ新規接続 (~0.8秒)。
   track_all / predict が数千銘柄でループし無音ハング → スレッドローカル接続プール導入 (806ms→223ms)。
2. **銘柄ごとDB往復** — predict が全universe(~3500)を1銘柄ずつクエリ。
   → priced事前フィルタ + バルクプリロード (load_history_bulk / 材料 / 業種) で9000往復→約12クエリ。
3. **出力バッファリング** — `PYTHONUNBUFFERED=1` をCIに設定。
4. **TDnet バックオフ暴走** — リトライ5/指数バックオフ → リトライ2/2分上限。

結果: フルパイプライン(5銘柄) 15分+クラッシュ → **77秒** で完走。validate CI **2分46秒**。

---

## データベース状況 (Neon PostgreSQL)

| テーブル | 件数 | 備考 |
|---------|------|------|
| securities | 3,572 | universe |
| prices | bootstrap実行中 | 既存50銘柄→全対象へ拡大中 |
| materials | 1,801 | kabutan:1,362 / tdnet:119 |
| teacher_samples | 36,665 | historical 36,615 / live 50 (live_fail=40) |
| model_meta | 3 | BYTEA含む。CIで実モデルロード確認 |
| predictions | 693 | 最新 run_date 2026-06-27 |
| prediction_outcomes | 52 | |

---

## 材料ソース状況

| ソース | 方式 | APIキー | 状況 |
|--------|------|---------|------|
| TDnet (yanoshin) | 範囲一括 (リトライ2/2分上限) | 不要 | ✅ |
| EDINET (金融庁 v2) | 日付別一覧 | 不要 | ✅ |
| Kabutan | HTMLスクレイピング | 不要 | ✅ |
| Yahoo Finance News | REST | 不要 | ✅ |

UIにソース別件数・材料あり銘柄数・最終取得日時を表示 (overview)。

---

## 動作確認済みエンドポイント (本番URL)

| パス | 結果 |
|------|------|
| `/healthz` | ✅ Neon統計JSON返却 |
| `/` ランキング | ✅ HTTP 200 |
| `/pred/{id}` 詳細(材料/チャート/MA/出来高/失敗条件) | ✅ HTTP 200 |
| `/accuracy` `/model` `/history` | ✅ HTTP 200 |
| `/api/ranking` `/api/accuracy` | ✅ JSON |
| `/sw.js` `/static/manifest.json` `/static/icon-192.png` | ✅ PWA資産 |
| `/push/public-key` | ✅ configured:true |

---

## デプロイ運用メモ

- Vercelプロジェクト: `roromukuro-5711s-projects/jp_surge_radar` (GitHub連携済み、master pushで自動デプロイ)
- 環境変数 (DATABASE_URL / VAPID_* / SURGE_ENABLE_LLM) は Vercel Production に設定済み (暗号化)
- 手動再デプロイ: `vercel deploy --prod --yes --scope roromukuro-5711s-projects`
- 環境変数更新: `python scripts/set_vercel_env.py --scope roromukuro-5711s-projects` (.envから、値は非表示)

---

## 残課題 / 次に自動実行される予定

- [ ] bootstrap.yml 完了 → prices 全対象銘柄取得
- [ ] 完了後 daily.yml をフル実行 (limit無し) し全銘柄ランキング生成
- [ ] daily.yml が55分以内で安定完走することを実データで確認
- [ ] 実機スマホでPWAホーム画面追加・Push購読テスト (購読者登録後にPush送信確認)
- 次回自動実行: 毎営業日 16:40 JST (cron) に daily.yml
