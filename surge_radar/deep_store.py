"""
深掘り分析の保存口。毎日のスケジュールタスクがここを呼ぶ。

分析そのもの(材料の主体検証・織り込み判定・Reachable Zone の根拠づけ)は
LLM が Web/DB を見て行うが、学習に回すには構造化された値が必要なので、
保存できる形を1か所に固定しておく。散文は rationale に入れる。
"""
from __future__ import annotations

from datetime import datetime

from . import db

ENTRY_TYPES = ("A", "B", "C")

FIELDS = (
    "entry_type", "driver_score", "risk_score", "driver_kind", "catalyst_type",
    "catalyst_date", "unpriced", "target20_price", "reachable_low", "reachable_high",
    "reachable_ok", "failure_line", "failure_distance", "rationale", "sources",
)


def save(run_date: str, candidates: list[dict], analyst: str = "") -> dict:
    """1日分の深掘り結果を保存する。同じ (run_date, code) は上書き。

    candidates の各要素に必要なキー:
      code, name, base_price, base_date, entry_type(A/B/C), driver_score(0-100),
      risk_score(0-30), driver_kind, catalyst_type, catalyst_date, unpriced(0-1),
      target20_price, reachable_low, reachable_high, reachable_ok(0/1),
      failure_line, failure_distance, rationale, sources(list)

    reachable_ok は「+20%閾値まで届く経路を根拠づけて説明できたか」。
    2026-09-17時点ではこれが成績と関係するかは未検証なので、判定を落とす
    ゲートとしては使わず、後で検証するための記録として残す。
    """
    if not candidates:
        return {"run_date": run_date, "saved": 0}
    analyst = analyst or f"claude/{datetime.now().strftime('%Y-%m')}"
    saved = 0
    with db.cursor() as conn:
        for rank, c in enumerate(candidates, 1):
            code = str(c.get("code", "")).strip()
            if not code:
                continue
            et = c.get("entry_type")
            if et not in ENTRY_TYPES:
                et = None
            conn.execute(
                """INSERT INTO deep_analysis
                   (run_date,code,name,base_price,base_date,rank,entry_type,
                    driver_score,risk_score,driver_kind,catalyst_type,catalyst_date,
                    unpriced,target20_price,reachable_low,reachable_high,reachable_ok,
                    failure_line,failure_distance,rationale,sources,analyst,status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
                   ON CONFLICT(run_date,code) DO UPDATE SET
                     name=excluded.name,base_price=excluded.base_price,
                     base_date=excluded.base_date,rank=excluded.rank,
                     entry_type=excluded.entry_type,driver_score=excluded.driver_score,
                     risk_score=excluded.risk_score,driver_kind=excluded.driver_kind,
                     catalyst_type=excluded.catalyst_type,catalyst_date=excluded.catalyst_date,
                     unpriced=excluded.unpriced,target20_price=excluded.target20_price,
                     reachable_low=excluded.reachable_low,reachable_high=excluded.reachable_high,
                     reachable_ok=excluded.reachable_ok,failure_line=excluded.failure_line,
                     failure_distance=excluded.failure_distance,rationale=excluded.rationale,
                     sources=excluded.sources,analyst=excluded.analyst""",
                (run_date, code, c.get("name", ""), c.get("base_price"),
                 c.get("base_date") or run_date, rank, et,
                 c.get("driver_score"), c.get("risk_score"), c.get("driver_kind", ""),
                 c.get("catalyst_type", ""), c.get("catalyst_date", ""),
                 c.get("unpriced"), c.get("target20_price"), c.get("reachable_low"),
                 c.get("reachable_high"), int(bool(c.get("reachable_ok"))),
                 c.get("failure_line"), c.get("failure_distance"),
                 c.get("rationale", ""), db.j(c.get("sources", [])), analyst))
            saved += 1
    return {"run_date": run_date, "saved": saved}


def engine_candidates(run_date: str, limit: int = 30) -> list[dict]:
    """その日のエンジン側 A/B/C 候補を、深掘りの出発点として返す。

    深掘りは「エンジンが挙げた候補を検証する」立場。エンジンが挙げていない
    銘柄を独自に拾うことも妨げないが、比較のためには同じ母集団から出発する
    ほうが解釈しやすい。
    """
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT code,name,base_price,category,score,probability,
                      reasons,flags,top_material
               FROM predictions
               WHERE run_date=%s AND category IN ('A','B','C')
               ORDER BY score DESC LIMIT %s""", (run_date, limit)).fetchall()
    return [dict(r) for r in rows]


def latest_run_date() -> str | None:
    with db.cursor() as conn:
        r = conn.execute("SELECT MAX(run_date) d FROM predictions").fetchone()
    return r["d"] if r else None
