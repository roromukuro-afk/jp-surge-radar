"""
グローバル設定 / 定数。

このアプリの目的: 日本株3000円以下の全銘柄から「短期急騰の火種」をAIが毎日発掘し、
予測 → 追跡 → 成否判定 → 失敗教師データ化 → 再学習 のループを回す。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ---- クラウド DB (Supabase / Neon 等) ----
# DATABASE_URL が設定されている場合は PostgreSQL を使用。ローカルは SQLite。
DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# ---- パス ----
# 読み取り専用FS (Vercel等のサーバーレス) では ROOT/data に書けないため、
# 書込不可なら /tmp 配下にフォールバックする。クラウドDB利用時はローカル
# データディレクトリは実質使われないが、import 時クラッシュを防ぐ。
ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    candidate = ROOT / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # 実際に書込めるか確認
        probe = candidate / ".write_probe"
        probe.touch()
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "surge_radar_data"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = Path(os.environ.get("SURGE_DB_PATH", DATA_DIR / "surge_radar.db"))
MODEL_DIR = DATA_DIR / "models"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"

for _d in (MODEL_DIR, CACHE_DIR, LOG_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# ---- 対象市場・価格条件 ----
PRICE_CAP = float(os.environ.get("SURGE_PRICE_CAP", 3000))  # 1株3000円以下
TARGET_MARKETS = ["プライム", "スタンダード", "グロース"]  # 東証中心。名証等は取得可能なら追加

# ---- 成功判定パラメータ ----
SUCCESS_THRESHOLD = 0.20      # +20%到達で成功
NEAR_MISS_THRESHOLD = 0.10    # +10%以上+20%未満で「惜しい」
WINDOW_S = 5                  # S成功: 5営業日以内
WINDOW_A = 10                 # A成功: 10営業日以内
WINDOW_B = 20                 # B成功: 20営業日以内（最終判定窓）
JUDGE_WINDOW = 20             # 追跡する最大営業日数
DANGER_DRAWDOWN = -0.15       # 危険失敗とみなす大幅下落の目安

# ---- 結果クラス ----
RESULT_CLASSES = ["S", "A", "B", "near", "fail", "danger_fail"]

# ---- 候補分類 (A:今すぐ / B:ブレイク確認 / C:押し目 / D:監視 / E:見送り) ----
CATEGORIES = ["A", "B", "C", "D", "E"]

# ---- 失敗理由タグ ----
FAILURE_TAGS = [
    "near_miss",      # +10〜19.9%までは上がった
    "quick_fail",     # 予測後すぐ下落
    "material_fail",  # 材料が一過性だった
    "chart_fail",     # チャート転換に見えたが騙し
    "volume_fail",    # 出来高急増が初動でなく天井
    "trend_fail",     # 右肩下がり継続
    "theme_fail",     # テーマ波及が来なかった
    "market_fail",    # 地合い悪化
    "trap_fail",      # 高値圏下落トラップ
    "dilution_fail",  # 希薄化・資金調達リスク
    "liquidity_fail", # 流動性不足
]

# ---- テーマ地合い観測用 ETF / 指数 (yfinanceティッカー) ----
# 客観的なテーマ資金流入の確認に使用。リーダー株の強さだけで関連株を加点しない。
THEME_TICKERS = {
    "半導体": "1625.T",        # NEXT FUNDS 電気機器(代理)
    "TOPIX": "1306.T",         # TOPIX連動ETF（地合い）
    "グロース250": "2516.T",   # 東証グロース市場250指数ETF
    "日経平均": "1321.T",      # 日経225ETF
    "SOX": "SOXX",             # 米半導体（テーマ先行指標）
    "NASDAQ": "^IXIC",
}
# 市場全体の地合い判定に使う基準
MARKET_REGIME_TICKER = "1306.T"

# ---- 流動性ゲート ----
MIN_AVG_TURNOVER = 30_000_000      # 直近平均売買代金の最低ライン(円)。これ未満は流動性不足
MIN_AVG_VOLUME = 20_000            # 直近平均出来高の最低ライン(株)

# ---- 特徴量ウィンドウ ----
LOOKBACK = 20      # T-20..T0
HISTORY_MIN_BARS = 120  # 特徴量計算に必要な最低足数(200日線は無くても計算は続行)

# ---- ランキング出力 ----
TOP_N_DEFAULT = 300   # 全評価結果を十分保存する(全銘柄分類要件)

# ---- モデル ----
CURRENT_MODEL_VERSION_FILE = MODEL_DIR / "CURRENT_VERSION"
