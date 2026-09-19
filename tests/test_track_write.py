"""track の確定書き込み (結果・教師データ・judged) を SQLite で検証する。"""
from surge_radar import db, track


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", None)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    with db.cursor() as conn:
        for code in ("1111", "2222", "3333"):
            conn.execute("INSERT INTO predictions(run_date,code,features,origin) VALUES(%s,%s,%s,%s)",
                         ("2025-10-01", code, db.j({"f": 1.0}), "backfill"))
        # 2222 は seed-teacher の historical と (code, t0_date) で衝突する
        conn.execute("INSERT INTO teacher_samples(source,code,t0_date,label,features)"
                     " VALUES('historical_neg','2222','2025-10-01',0,'{}')")
        # 3333 は前回の途中停止で教師データだけ入っている
        conn.execute("INSERT INTO teacher_samples(source,code,t0_date,label,features,prediction_id)"
                     " VALUES('live_fail','3333','2025-09-30',0,'{}',3)")
        return [dict(r) for r in conn.execute("SELECT * FROM predictions ORDER BY id").fetchall()]


def _outcome(pid):
    return (pid, "2026-09-18", 20, 0.01, 0.02, 0.03, None, -0.05, 0, 1, 0, 0,
            "quick_fail", db.j(["quick_fail"]), "", "x")


def test_commit_finalized_batches_and_skips_duplicates(tmp_path, monkeypatch):
    preds = _setup(tmp_path, monkeypatch)
    teacher = [track._teacher_row(p, {"f": 1.0}, 0, ["quick_fail"], "quick_fail") for p in preds]
    track._commit_finalized([_outcome(p["id"]) for p in preds], teacher)
    # 再実行しても落ちず、増えない
    track._commit_finalized([_outcome(p["id"]) for p in preds], teacher)

    with db.cursor() as conn:
        status = {r["code"]: r["status"] for r in conn.execute("SELECT code,status FROM predictions")}
        rows = [dict(r) for r in conn.execute(
            "SELECT source,code,t0_date,prediction_id FROM teacher_samples ORDER BY code,t0_date")]
        n_out = conn.execute("SELECT COUNT(*) AS n FROM prediction_outcomes").fetchone()["n"]
    assert set(status.values()) == {"judged"}
    assert n_out == 3
    assert rows == [
        {"source": "live_fail", "code": "1111", "t0_date": "2025-10-01", "prediction_id": 1},
        {"source": "historical_neg", "code": "2222", "t0_date": "2025-10-01", "prediction_id": None},
        {"source": "live_fail", "code": "3333", "t0_date": "2025-09-30", "prediction_id": 3},
    ]


def test_teacher_row_skips_empty_features():
    assert track._teacher_row({"id": 1, "code": "1111", "run_date": "2025-10-01"},
                              {}, 1, [], "S") is None
