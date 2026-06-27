"""
銘柄ユニバース取得。

優先順位:
  1. J-Quants listed/info (トークンがあれば最も正確・全市場)
  2. JPX 公開 Excel data_j.xls (東証全銘柄, 無料・登録不要)
  3. 内蔵シードリスト (オフライン/取得失敗時のフォールバック。主要な低位~中位株)
"""
from __future__ import annotations

import io

import pandas as pd
import requests

from . import db
from .config import CACHE_DIR, TARGET_MARKETS
from .sources import jquants

JPX_XLS_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"

# フォールバック用シード (証券コード, 名称, 市場, 33業種)。3000円以下を中心に幅広いテーマで構成。
SEED = [
    ("3350", "メタプラネット", "スタンダード", "サービス業"),
    ("5803", "フジクラ", "プライム", "非鉄金属"),
    ("6526", "ソシオネクスト", "プライム", "電気機器"),
    ("3778", "さくらインターネット", "プライム", "情報・通信業"),
    ("4490", "ビザスク", "グロース", "サービス業"),
    ("7011", "三菱重工業", "プライム", "機械"),
    ("7012", "川崎重工業", "プライム", "輸送用機器"),
    ("6315", "TOWA", "プライム", "機械"),
    ("3479", "TKP", "グロース", "不動産業"),
    ("4934", "プレミアアンチエイジング", "グロース", "化学"),
    ("6areer", "X", "グロース", "サービス業"),
    ("2158", "FRONTEO", "グロース", "情報・通信業"),
    ("3656", "KLab", "プライム", "情報・通信業"),
    ("3692", "FFRIセキュリティ", "グロース", "情報・通信業"),
    ("4011", "ヘッドウォータース", "グロース", "情報・通信業"),
    ("5246", "ELEMENTS", "グロース", "情報・通信業"),
    ("7374", "コンフィデンス", "グロース", "サービス業"),
    ("6areerlink", "X", "グロース", "サービス業"),
    ("9succ", "X", "グロース", "サービス業"),
    ("2160", "GNI", "グロース", "医薬品"),
    ("4575", "キャンバス", "グロース", "医薬品"),
    ("4596", "窪田製薬HD", "グロース", "医薬品"),
    ("4592", "サンバイオ", "グロース", "医薬品"),
    ("7777", "スリー・ディー・マトリックス", "グロース", "精密機器"),
    ("6areer2", "X", "グロース", "サービス業"),
    ("3856", "Abalance", "スタンダード", "電気機器"),
    ("9211", "エフ・コード", "グロース", "情報・通信業"),
    ("5script", "X", "グロース", "情報・通信業"),
    ("2588", "プレミアムウォーターHD", "プライム", "食料品"),
    ("3097", "物語コーポレーション", "プライム", "小売業"),
    ("3697", "SHIFT", "プライム", "情報・通信業"),
    ("4385", "メルカリ", "プライム", "情報・通信業"),
    ("4477", "BASE", "グロース", "情報・通信業"),
    ("4485", "JTOWER", "グロース", "情報・通信業"),
    ("4488", "AIインサイド", "グロース", "情報・通信業"),
    ("5588", "ファーストアカウンティング", "グロース", "情報・通信業"),
    ("6areer3", "X", "グロース", "サービス業"),
    ("2480", "システム・ロケーション", "スタンダード", "情報・通信業"),
    ("3905", "データセクション", "グロース", "情報・通信業"),
    ("6178", "日本郵政", "プライム", "サービス業"),
    ("8up", "X", "グロース", "サービス業"),
    ("3672", "オルトプラス", "グロース", "情報・通信業"),
    ("3760", "ケイブ", "スタンダード", "情報・通信業"),
    ("3990", "UUUM", "グロース", "情報・通信業"),
    ("4king", "X", "グロース", "情報・通信業"),
]


def _clean_seed() -> list[tuple]:
    out = []
    for code, name, market, sec in SEED:
        if not code.isdigit():
            continue  # プレースホルダ除去
        out.append((code, name, market, sec))
    return out


def fetch_jpx_xls() -> pd.DataFrame:
    """JPX 公開 data_j.xls をDataFrame化。失敗時は空DF。"""
    try:
        print("  [universe] downloading JPX xls...", flush=True)
        r = requests.get(JPX_XLS_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        (CACHE_DIR / "data_j.xls").write_bytes(r.content)
        print(f"  [universe] JPX xls downloaded ({len(r.content)//1024}KB), parsing...", flush=True)
        df = pd.read_excel(io.BytesIO(r.content))
        print(f"  [universe] JPX xls parsed: {len(df)} rows", flush=True)
        return df
    except Exception as e:
        print(f"  [universe] JPX xls failed: {e}", flush=True)
        p = CACHE_DIR / "data_j.xls"
        if p.exists():
            try:
                print("  [universe] using cached JPX xls", flush=True)
                return pd.read_excel(p)
            except Exception:
                return pd.DataFrame()
        return pd.DataFrame()


def _normalize_jpx(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    cols = {c: str(c) for c in df.columns}
    df = df.rename(columns=cols)
    # 列名(JPX): コード, 銘柄名, 市場・商品区分, 33業種区分, 17業種区分
    def col(*names):
        for n in names:
            for c in df.columns:
                if n in str(c):
                    return c
        return None
    c_code = col("コード")
    c_name = col("銘柄名")
    c_mkt = col("市場・商品区分", "市場")
    c_s33 = col("33業種区分")
    c_s17 = col("17業種区分")
    out = []
    for _, row in df.iterrows():
        code = str(row[c_code]).strip().split(".")[0] if c_code else None
        if not code or not code.isdigit():
            continue
        market = str(row[c_mkt]) if c_mkt else ""
        # ETF/REIT/出資証券などは除外し、内国株式中心
        if any(x in market for x in ["ETF", "ETN", "REIT", "出資証券", "PRO", "外国"]):
            continue
        out.append({
            "code": code,
            "name": str(row[c_name]) if c_name else "",
            "market": _market_label(market),
            "sector33": str(row[c_s33]) if c_s33 else "",
            "sector17": str(row[c_s17]) if c_s17 else "",
        })
    return out


def _market_label(m: str) -> str:
    if "プライム" in m:
        return "プライム"
    if "スタンダード" in m:
        return "スタンダード"
    if "グロース" in m:
        return "グロース"
    return m


def load_universe(use_remote: bool = True) -> list[dict]:
    """ユニバースを取得して返す (DBには保存しない)。"""
    # 1) J-Quants
    print("  [universe] checking J-Quants...", flush=True)
    if use_remote and jquants.is_available():
        print("  [universe] fetching from J-Quants...", flush=True)
        dfj = jquants.fetch_listed()
        if not dfj.empty:
            rows = []
            for _, r in dfj.iterrows():
                code = str(r.get("Code", "")).strip()[:4]
                if not code.isdigit():
                    continue
                rows.append({
                    "code": code,
                    "name": r.get("CompanyName", ""),
                    "market": _market_label(str(r.get("MarketCodeName", ""))),
                    "sector33": r.get("Sector33CodeName", ""),
                    "sector17": r.get("Sector17CodeName", ""),
                })
            if rows:
                return rows
    # 2) JPX xls
    if use_remote:
        rows = _normalize_jpx(fetch_jpx_xls())
        if rows:
            return rows
    # 3) seed
    return [{"code": c, "name": n, "market": m, "sector33": s, "sector17": ""}
            for c, n, m, s in _clean_seed()]


def save_universe(rows: list[dict]) -> int:
    db.init_db()
    with db.cursor() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO securities(code,name,market,sector33,sector17)
                   VALUES(%s,%s,%s,%s,%s)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, market=excluded.market,
                     sector33=excluded.sector33, sector17=excluded.sector17,
                     updated_at=CURRENT_TIMESTAMP""",
                (r["code"], r.get("name"), r.get("market"),
                 r.get("sector33"), r.get("sector17")),
            )
    return len(rows)


def get_target_codes(price_cap_only: bool = False) -> list[str]:
    """対象市場の銘柄コード一覧。"""
    with db.cursor() as conn:
        q = "SELECT code, market FROM securities"
        rows = conn.execute(q).fetchall()
    codes = [r["code"] for r in rows
             if (not TARGET_MARKETS) or any(t in (r["market"] or "") for t in TARGET_MARKETS)]
    return codes


if __name__ == "__main__":
    u = load_universe()
    n = save_universe(u)
    print(f"universe loaded: {n} (sample: {u[:3]})")
