# Surge Radar — 本番状態レポート
最終更新: 2026-06-28 (bootstrap完了・材料分析アップグレード・全銘柄predict)

> 機密情報 (DATABASE_URL / VAPID_PRIVATE_KEY / EDINET_API_KEY 等) はこのファイルに書かない。
> 値は `.env` / GitHub Secrets / Vercel Environment Variables のみで管理。

---

## 公開URL (本番・稼働中)

**https://jpsurgeradar.vercel.app** — スマホ・PC・外出先のモバイル回線からHTTPS。PCを閉じても稼働。

---

## クラウド構成

```
[スマホ/外部] → HTTPS → [Vercel サーバーレス (FastAPI 軽量版)]
                                ↓ 読取
                  [Neon PostgreSQL (surge_radar, Singapore)]
                                ↑ 書込
   [GitHub Actions daily.yml : 毎営業日 07:40 UTC = 16:40 JST]
```

| コンポーネント | サービス | 状態 |
|--------------|---------|------|
| Web (公開URL) | Vercel Hobby | ✅ 稼働 |
| DB | Neon free (512MB) | ✅ 稼働 |
| 日次パイプライン | GitHub Actions (public) | ✅ 稼働 |
| 予備ホスト | Render (render.yaml) | ⬜ 任意 |

Web層は numpy/pandas/sklearn を含まない軽量依存。重いML処理はGitHub Actions側。

---

## bootstrap 結果 (価格データ全件取得)

| 項目 | 値 |
|------|---|
| workflow | ✅ success |
| 実行時間 | 約3時間42分 (price fetch ~2h45m + 初回predict ~56m) |
| 価格取得 | 成功 3,522 / 失敗 **0** |
| **priced_codes** | **3,572 / 3,572 (全銘柄)** |
| **prices 行数** | **1,740,520** (約2年分OHLCV) |
| job_logs | ✅ daily_pipeline ok 記録 |
| rate limit | なし (fail=0) |

---

## DB データ状況 (Neon PostgreSQL)

| テーブル | 件数 |
|---------|------|
| securities | 3,572 |
| prices | 1,740,520 行 / 3,572 codes |
| materials | 1,803 |
| teacher_samples | 36,666 (live_fail 40 / live_success 10) |
| model_meta | 3 (v20260626_105519, CV AUC 0.7558) |
| predictions | 直近runで再生成 (3000円以下・60本以上の全対象) |

---

## 材料分析アップグレード (今回の中核) — 件数でなく「質」

材料を単なる見出し保存から、未織り込み・接続度・反応・リスク・AIコメントを持つ分析データへ。

### 材料品質フィールド (全1,803件に付与)
| フィールド | 内容 | 充足 |
|-----------|------|------|
| material_type | 種別(上方修正/大型受注/提携/増資/薬事承認/月次…) | 202 (公式開示分) |
| unpriced | 未織り込み感(サプライズ性) | **1,803** |
| connection (connect) | 銘柄接続度(公式開示=1.0/kabutan=0.8) | **1,803** |
| chart_reaction | 材料後の株価反応(prices事後計算) | **1,610** |
| volume_reaction | 材料後の出来高反応 | **777** |
| risk | 出尽くし+希薄化リスク | **366** |
| ai_comment | 上記統合の日本語コメント(ルールベース) | **1,803** |
| material_quality | 接続度×未織込×持続×反応×(1-リスク) | 全件算出 (平均 0.241) |

material_quality 上位例: M&A・買収 (q0.67, 反応1.0)、TOB、上方修正(ストップ高+出来高1.0)、過去最高益。
→ 材料が銘柄に接続し、株価・出来高が反応した「本物」を上位に出せている。

モデル特徴量キー(material_raw 等)は不変に保ち、学習済みモデルとの互換を維持。
material_quality と top材料情報は予測の flags に保存し、ランキング診断・詳細画面で表示。

### 材料ソース別 (正直な状況)
| ソース | 件数 | 状態 |
|--------|------|------|
| Kabutan | 1,682 | ✅ 銘柄ニュース。**見出しのみ(本文未取得)**。分析で個別スコア化済 |
| TDnet | 121 | ✅ 公式開示。material_type分類が効く |
| **EDINET** | **0** | ⚠️ **APIキー必須**(下記)。コードはキー対応済 |
| 外部ニュース(Yahoo/Reuters) | 0 | ❌ **daily未実装**(取得コードはあるが日次に組込まず) |
| 企業IR本文 | 0 | ❌ **未実装** |
| body(本文) | 0 | ❌ **未実装**(見出しのみ保存) |

### EDINET が 0 件の理由 (重要)
EDINET API v2 は2024年以降 **サブスクリプションキー必須**。キー無しの呼び出しは
`401 invalid subscription key` を返すため0件。コードは `EDINET_API_KEY` 環境変数に
対応済み(未設定時は理由をログ出力してスキップ)。
**ユーザーが https://api.edinet-fsa.go.jp で無料キーを取得し、GitHub Secrets に
`EDINET_API_KEY` を設定すれば EDINET 材料が自動で入る。**

---

## パイプライン性能 (タイムアウト対策)

| ステップ | 旧 | 新 | 対策 |
|---------|---|---|------|
| track | ~15分 | **24秒** | バルクプリロード+batch upsert |
| predict 特徴量 | 500本/銘柄 | 260本に限定 | 52週高値=250本のみ必要。値は不変を検証 |
| DB接続 | 毎回新規(0.8s) | スレッドプール再利用(0.22s) | 接続プーリング |
| 銘柄ごと往復 | 数千 | バルク化 | load_history_bulk 等 |

### daily workflow 実測 (2026-06-28 フル実行, conclusion=success)
| ステップ | 時間 |
|---------|------|
| universe | 1s |
| ingest (差分) | 50s |
| materials (TDnet/EDINET) | 50s |
| themes | 7s |
| track (5/10/20日判定) | **22s** (旧15分から改善) |
| teacher_status / train | 8s |
| **predict (全2,841銘柄評価)** | **35.8分** |
| enrich + push + job_logs | ~2分 |
| **合計** | **約41分** |

timeout は安全余裕で75分に設定(CIランナー個体差対策)。実測41分で完走。
※ 一度ランナー個体差で predict が55分超となりキャンセルされたため、75分余裕を確保。

### daily workflow ステップ (run_daily)
差分price更新 → 材料(TDnet/EDINET) → themes → track(5/10/20日判定・live_fail/success・danger_fail) →
teacher_status → 必要時retrain → predict → top候補kabutan enrich → push通知 → job_logs保存。
失敗扱い: CRITICAL(DB/universe/ingest/predict/保存) は中断、WARNING(材料/track/retrain/push) は継続。

---

## full predict 結果 (2026-06-28, 全銘柄)

| 項目 | 値 |
|------|---|
| 評価銘柄 | 2,841 (3000円以下・60本以上) / skip 731 |
| 保存予測 | 300 (上位) |
| **A/B/C/D/E** | A:0 / **B:31** / **C:36** / D:233 / E:0(保存上位) |
| material_quality>0 | 134 |
| material_quality>0.3 | 62 |
| **B/C 材料あり** | **43** |
| B/C 材料なし(AI類似/チャートのみ) | 24 (うちAI類似度のみ 23) |
| 材料+チャート+出来高 揃い | 32 |
| model | v20260626_105519 (実モデル) |
| market_score | 0.744 |

### 材料込み/材料なしの明確化 (UI + 診断)
ランキング画面に材料カバレッジパネルを表示(材料あり/材料なし/材料+チャート+出来高/材料スコア>0.3)。
材料なしで B/C に上がる候補(AI類似度のみ 23件)は classify_path=B_very_strong_ai 等で記録し、
各候補の reasons に「AI類似: 過去急騰前パターンと高類似」と根拠を明示。

### 上位候補例 (材料分析込み)
- #1 7875 竹田iP [B] matQ0.46 出来高伴い株価反応あり (cr1.0/vr1.0)
- #3 6335 東京機械 [B] B_material_volume matQ0.45 (cr1.0/vr0.86)
- #4 6298 ワイエイシイ [B] type=決算 「株価反応は限定的」
- #6 6999 KOA [B] matQ0 (材料なし・AI類似/チャートのみ)
- #8 6620 宮越HD [B] B_material_volume matQ0.46 (cr1.0/vr1.0)

---

## Push 通知

| 項目 | 状態 |
|------|------|
| VAPID | ✅ configured (public key配信, configured:true) |
| 購読UI(通知ベル ON/OFF) | ✅ 配信済 |
| Service Worker (push/notificationclick/showNotification) | ✅ |
| /push/subscribe 保存 | ✅ (検証済) |
| 粒度別トリガ | ✅ A候補/danger_fail/live S・A・B成功/日次サマリ/pipeline失敗 |
| **購読者** | **0人** |
| **実送信(delivery)** | ⚠️ **未確認**(購読者0のため。実機での購読が必要) |
| iOS制約 | PWAをホーム画面追加後でないとPush不可(iOS 16.4+)。要実機 |
| 代替案 | iOSで困難なら LINE Notify / メール / GitHub Actions通知を検討(要キー) |

実機手順: スマホでURLを開く→ホーム画面追加→アプリ起動→通知ベルON→許可→push_subscriptions保存→
次のdaily(または手動dispatch)で通知配信。

---

## スマホ / PWA

| 確認項目 | 状態 |
|---------|------|
| モバイル回線でアクセス | ✅ |
| viewport / レスポンシブ | ✅ |
| ホーム画面追加(manifest standalone+maskable+shortcuts) | ✅ |
| ランキング/詳細/材料/チャート(MA5/25/75)/出来高 | ✅ HTTP 200 |
| 材料ステータス(ソース別件数) | ✅ |
| 材料の material_type/未織込/接続/チャート反応/出来高反応/リスク/AIコメント表示 | ✅ (detail) |
| 材料URLリンク | ✅ |
| Push購読UI | ✅ |

---

## 残課題 (正直版)

- [ ] **EDINET_API_KEY 未設定** → EDINET材料0件。ユーザーが無料キー取得で解消
- [ ] 外部ニュース(Yahoo/Reuters)を daily に組込み(コードはあり)
- [ ] 企業IR本文 / Kabutan本文の取得(現状は見出しのみ、body=0)
- [ ] Push購読者0 → 実機でホーム画面追加+購読+通知到達テスト
- [ ] 材料反応は価格更新ごとに再計算(daily/週次でbackfill自動化を検討)
- [ ] daily の B/C で材料なし候補(AI類似のみ)の比率を継続監視

## 次に自動実行される予定
- 毎営業日 16:40 JST (cron) に daily.yml(差分price→材料→track→retrain→predict→push→job_logs)
- master push 時に validate.yml(非破壊スモーク, predict保存しない)
