# 日本株 急騰レーダー (Surge Radar) 🇯🇵📈

日本株3000円以下の全銘柄を対象に、**AIが毎日自動で短期急騰の火種を発掘**し、
**予測 → 追跡 → 成否判定 → 失敗教師データ化 → 再学習 → 的中率改善** のループを回す
スクリーニング・研究支援ツールです。

> ⚠️ 本ツールは投資助言ではありません。将来の株価上昇を保証せず、AI予測は外れる可能性があり、
> 損失リスクがあります。最終判断はご自身で行ってください。

---

## 何をするか

- 東証全銘柄(プライム/スタンダード/グロース ≒ 3,500銘柄)を JPX 公開データから取得
- **3000円以下**に絞り、材料・チャート・出来高・テーマ地合いを多面的に分析
- 過去の **+20%急騰前パターン**を教師データ化し、ML(GradientBoosting)+類似度で照合
- 「平均点ランキング」ではなく、**短期急騰の火種ランキング**を A〜E に分類して出力
- 予測後 5/10/20営業日の **+20%到達**を追跡し、外れた予測を**失敗教師データ**として再学習
- 毎日放置で動く(Windows タスクスケジューラ)

### 成功判定
予測時点価格を基準に、20営業日以内に**高値ベース +20%** で成功。
S(5日内) / A(10日内) / B(20日内) / 惜しい(+10〜20%) / 失敗 / 危険失敗。

### 候補分類
A:今すぐ買い検討型 / B:ブレイク確認買い型 / C:押し目・再点火待ち型(乱用しない) / D:監視 / E:見送り・除外

---

## アーキテクチャ

```
surge_radar/
├─ config.py        定数(価格上限3000・成功閾値20%・窓5/10/20・テーマETF 等)
├─ db.py            SQLite スキーマ(予測/成否/教師/モデルメタ/ジョブログ)
├─ sources/         データ取得アダプタ
│   ├─ yahoo.py     Yahoo chart API 直叩き(主データ源)
│   └─ jquants.py   J-Quants(トークンがあれば併用)
├─ universe.py      銘柄ユニバース(J-Quants → JPX xls → 内蔵シード)
├─ ingest.py        OHLCV 取り込み
├─ indicators.py    テクニカル/チャート/出来高の特徴量プリミティブ
├─ materials.py     材料分析(ルール+NLP、TDnet取得、LLM深掘りフック)
├─ themes.py        テーマ地合い/市場レジーム(ETF/指数で客観確認)
├─ features.py      特徴量アセンブリ(1銘柄1時点 → ベクトル)
├─ labeling.py      成否判定・失敗タグ付け
├─ scoring.py       総合スコア・除外ゲート・A〜E分類・理由・失敗条件
├─ model.py         ML分類器 + 過去急騰パターン類似度
├─ teacher.py       過去データからの教師生成
├─ track.py         予測追跡・成否判定・教師データ化(学習ループ本丸)
├─ train.py         再学習オーケストレーション
├─ predict.py       日次ランキング生成(asof で過去バックフィル可)
├─ pipeline.py      日次フルパイプライン(ジョブログ付き)
├─ queries.py       Web表示用クエリ
├─ cli.py           CLI
└─ web/             FastAPI + Jinja2 + Tailwind(レスポンシブ)
```

データ・モデル・バッチ・AI分析・Webを分離。SQLite はゼロ運用で、後で Postgres へ移行しやすい構成。

---

## セットアップ

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 使い方(CLI)

```bash
# 初回: ユニバース取得 → 株価取得 → 教師生成 → 学習 → 予測
python -m surge_radar.cli universe
python -m surge_radar.cli ingest          # 全対象。テストは --limit 300
python -m surge_radar.cli materials       # TDnet開示収集
python -m surge_radar.cli seed-teacher    # 過去急騰から教師データ生成
python -m surge_radar.cli train           # モデル学習
python -m surge_radar.cli predict         # ランキング生成

# 日次フル(放置運用の本体)
python -m surge_radar.cli daily

# 過去バックフィル(成否追跡の検証用)
python -m surge_radar.cli predict --asof 2026-05-20
python -m surge_radar.cli track

# Web 表示
python -m surge_radar.cli serve           # http://127.0.0.1:8000
```

## 自動運用(Windows)

```powershell
# 平日16:30に日次バッチを登録
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

`scripts\serve.bat` で Web を起動。ログは `data\logs\` と `/logs` 画面で確認。

---

## データ取得元

| 種別 | ソース | 備考 |
|---|---|---|
| 銘柄一覧 | JPX 公開 `data_j.xls` | 無料・登録不要 |
| 株価 OHLCV | Yahoo Finance chart API | 無料。`yfinance` ラッパに依存しない直接実装 |
| 開示(材料) | TDnet (yanoshin WebAPI) | 無料 JSON |
| テーマ/地合い | ETF/指数(Yahoo) | TOPIX・グロース250・半導体・SOX 等 |
| 高品質データ | J-Quants(任意) | 環境変数でトークン設定時に併用 |

### 任意設定(環境変数)
- `JQUANTS_REFRESH_TOKEN`(または `JQUANTS_MAIL`/`JQUANTS_PASSWORD`): J-Quants 併用
- `ANTHROPIC_API_KEY` + `SURGE_ENABLE_LLM=1`: 上位候補のみ材料 LLM 深掘り(コスト発生のため既定オフ)

---

## 学習ループの考え方

初期教師は過去の +20% 急騰銘柄の T-20〜T0 パターン(正例)+ 非急騰(負例)。
**本命の失敗教師は、運用開始後に AI 自身が予測して外した銘柄**(`live_fail`)。
near_miss / quick_fail / material_fail / chart_fail / volume_fail / trend_fail /
theme_fail / market_fail / trap_fail / dilution_fail / liquidity_fail に分類して再学習に反映します。
