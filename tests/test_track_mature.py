"""早期 judged (+20%到達) の予測が20営業日満了時に教師データ化されることを SQLite で検証する。"""
import pandas as pd

from surge_radar import db, themes, track
from surge_radar.config import JUDGE_WINDOW

DATES = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-08-03", periods=JUDGE_WINDOW + 1)]
T0 = DATES[0]


def _bar(code, i):
    """i=0 が T0 (終値100)。1111/3333 は2本目で高値+25% (S判定)、2222 は横ばい。"""
    if i == 0:
        return (code, DATES[0], 100, 101, 99, 100, 1e6, 1e8)
    if code in ("1111", "3333") and i == 2:
        return (code, DATES[i], 105, 125, 104, 118, 1e6, 1e8)
    return (code, DATES[i], 100, 103, 97, 100, 1e6, 1e8)


def _add_bars(upto):
    with db.cursor() as conn:
        conn.executemany(
            "INSERT INTO prices(code,date,open,high,low,close,volume,turnover)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code,date) DO NOTHING",
            [_bar(c, i) for c in ("1111", "2222", "3333") for i in range(upto + 1)])


def _snapshot():
    with db.cursor() as conn:
        status = {r["code"]: r["status"] for r in conn.execute("SELECT code,status FROM predictions")}
        outcomes = {r["code"]: dict(r) for r in conn.execute(
            "SELECT p.code,o.judged_date,o.bars_tracked,o.result_class FROM prediction_outcomes o"
            " JOIN predictions p ON p.id=o.prediction_id")}
        teacher = [(r["source"], r["code"], r["label"]) for r in conn.execute(
            "SELECT source,code,label FROM teacher_samples ORDER BY code")]
    return status, outcomes, teacher


def test_early_success_gets_teacher_row_at_full_window(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", None)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(themes, "market_regime", lambda asof: {"score": 0.0})
    monkeypatch.setattr(track, "WRITE_CHUNK", 1)  # チャンク単位の確定経路を通す
    db.init_db()
    with db.cursor() as conn:
        for code in ("1111", "2222", "3333"):
            conn.execute("INSERT INTO predictions(run_date,code,features) VALUES(%s,%s,%s)",
                         (T0, code, db.j({"f": 1.0})))
        # 3333 は seed-teacher の historical と (code, t0_date) で衝突する
        conn.execute("INSERT INTO teacher_samples(source,code,t0_date,label,features)"
                     " VALUES('historical_neg','3333',%s,0,'{}')", (T0,))

    # 3営業日目: 1111/3333 が+20%到達で早期 judged。教師データはまだ作らない
    _add_bars(3)
    r = track.track_all(DATES[3])
    status, outcomes, teacher = _snapshot()
    assert status == {"1111": "judged", "2222": "open", "3333": "judged"}
    assert outcomes["1111"] == {"code": "1111", "judged_date": DATES[3],
                                "bars_tracked": 3, "result_class": "S"}
    assert teacher == [("historical_neg", "3333", 0)]
    assert (r["judged"], r["live_success"], r["matured"]) == (2, 2, 0)

    # 19営業日目: まだ満期前。早期 judged の結果行は触らない
    _add_bars(JUDGE_WINDOW - 1)
    r = track.track_all(DATES[JUDGE_WINDOW - 1])
    status, outcomes, teacher = _snapshot()
    assert outcomes["1111"]["bars_tracked"] == 3
    assert teacher == [("historical_neg", "3333", 0)]
    assert (r["judged"], r["matured"]) == (0, 0)

    # 20営業日目: 早期成功も失敗と同じ成熟条件で教師データ化される
    _add_bars(JUDGE_WINDOW)
    r = track.track_all(DATES[JUDGE_WINDOW])
    status, outcomes, teacher = _snapshot()
    assert set(status.values()) == {"judged"}
    # judged_date は早期判定日のまま、bars_tracked は満期分に更新 (model.py の成熟判定用)
    assert outcomes["1111"] == {"code": "1111", "judged_date": DATES[3],
                                "bars_tracked": JUDGE_WINDOW, "result_class": "S"}
    assert outcomes["3333"]["bars_tracked"] == JUDGE_WINDOW
    assert teacher == [("live_success", "1111", 1), ("live_fail", "2222", 0),
                       ("historical_neg", "3333", 0)]  # 衝突は既存を残す
    assert (r["judged"], r["live_fail"], r["live_success"]) == (1, 1, 0)
    assert (r["matured"], r["matured_success"]) == (2, 2)

    # 再実行: 衝突で教師データが入らなかった 3333 も含め、満期待ち一覧から外れている
    r = track.track_all(DATES[JUDGE_WINDOW])
    assert _snapshot()[2] == teacher
    assert (r["judged"], r["matured"]) == (0, 0)
