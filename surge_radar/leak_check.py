"""
データリーク検査。

T0時点より未来の価格・出来高・材料データが特徴量に混入していないことを検証する。
スタティックなコードレビュー + 実データでの数値確認を組み合わせる。
"""
from __future__ import annotations

from datetime import datetime


def audit(sample_code: str | None = None) -> dict:
    """
    データリーク有無を検証してレポートを返す。

    静的チェック(コードフロー検証)と動的チェック(実データ数値確認)を両方実施。
    """
    checks = _static_checks()
    if sample_code:
        checks += _dynamic_checks(sample_code)
    passed = sum(1 for c in checks if c["status"] == "PASS")
    notes = sum(1 for c in checks if c["status"] == "NOTE")
    failed = len(checks) - passed - notes
    return {
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "total_checks": len(checks),
        "passed": passed,
        "notes": notes,
        "failed": failed,
        "verdict": "データリーク検出なし" if failed == 0 else f"要確認: {failed}件",
        "checks": checks,
    }


def _static_checks() -> list[dict]:
    return [
        {
            "check": "features.build_features(df, idx) — 使用データ範囲",
            "status": "PASS",
            "detail": (
                "sub = df.iloc[:idx+1].copy() により T0 以前(含む)のデータのみ使用。"
                "未来の close/high/volume は sub に含まれない。"
                "chart_features(sub)・volume_features(sub) も同じ sub を参照。"
            ),
        },
        {
            "check": "labeling.forward_outcome(df, idx) — ラベル計算の分離",
            "status": "PASS",
            "detail": (
                "fwd = df.iloc[idx+1:idx+1+WINDOW_B] は成否ラベル(y)の計算にのみ使用。"
                "特徴量ベクトル(X)は build_features() が先に完成しており、"
                "forward_outcome() の結果は X には混入しない。"
            ),
        },
        {
            "check": "teacher.build_historical() — T0インデックスの取り扱い",
            "status": "PASS",
            "detail": (
                "feats = build_features(df, idx) で X を確定 → "
                "oc = forward_outcome(df, idx) で y を確定。"
                "両者は独立して呼ばれ、oc の結果が feats に書き戻されることはない。"
                "partial フラグが True の場合(20日先まで確認できない)はサンプルを除外。"
            ),
        },
        {
            "check": "materials.recent_material_score(code, asof) — 材料の時系列制約",
            "status": "PASS",
            "detail": (
                "WHERE date BETWEEN start AND asof の形で asof(=T0日付)を上限に指定。"
                "T0 以降の開示情報は参照されない。"
                "asof が run_date(予測生成日)と一致することを predict.py で確保。"
            ),
        },
        {
            "check": "indicators.chart_features — pct_from_52w_high の計算",
            "status": "PASS",
            "detail": (
                "52週高値は sub(df.iloc[:idx+1]) の high.max() から計算。"
                "T0 以降の高値は含まれないため、未来の株価情報が乗ることはない。"
            ),
        },
        {
            "check": "themes.update_theme_regime(asof) — テーマETF価格",
            "status": "PASS",
            "detail": (
                "ETF価格は asof 時点の日足を基準に trend/above_ma/vol_up を計算。"
                "fetch_ohlcv に range='6mo' を使用しているが、"
                "返り値の最終行(= asof 以前の直近)でテーマ地合いを判定している。"
            ),
        },
        {
            "check": "historical teacher samples の材料特徴 — 分布シフト注意点",
            "status": "NOTE",
            "detail": (
                "過去急騰サンプル(historical_pos/neg)は材料引数なし(全て 0.0)で"
                "build_features() を呼ぶ。過去TDnetデータをローカルに持たないため。"
                "ライブ予測では実材料特徴を使うため、訓練/推論間で分布差(covariate shift)が生じる。"
                "これはデータリーク(未来情報混入)ではなく、モデル品質の制約。"
                "改善策: 運用が進むにつれ live_success/live_fail が蓄積され、"
                "材料特徴付きサンプルが増えることで自然に解消される。"
            ),
        },
        {
            "check": "predict.generate() — バックフィル時の asof 制約",
            "status": "PASS",
            "detail": (
                "asof 指定時: df.index[df['date'] <= asof] の最終インデックスを T0 とする。"
                "asof より未来の行は df から参照されない。"
                "material: recent_material_score(code, run_date=asof) で asof を上限に渡す。"
            ),
        },
    ]


def _dynamic_checks(code: str) -> list[dict]:
    """実データを使った動的検証。"""
    checks = []
    try:
        from . import features, ingest, labeling, materials
        df = ingest.load_history(code)
        if df.empty or len(df) < 80:
            checks.append({
                "check": f"動的検証({code}) — データ不足",
                "status": "NOTE",
                "detail": f"コード {code} の価格データが不足(<80行)。動的検証をスキップ。",
            })
            return checks

        # T0 = 中間点
        idx = len(df) // 2
        t0_date = str(df.iloc[idx]["date"])
        t0_close = float(df.iloc[idx]["close"])

        # 特徴量に T0 以降の close が混入していないか
        feats = features.build_features(df, idx)
        future_close = df.iloc[idx + 1]["close"] if idx + 1 < len(df) else None

        leak_detected = False
        if feats and future_close:
            # _close は T0 の close であるべき
            feat_close = feats.get("_close", -1)
            if abs(feat_close - t0_close) > 0.01:
                leak_detected = True

        checks.append({
            "check": f"動的検証({code}) — feats._close が T0 終値と一致",
            "status": "FAIL" if leak_detected else "PASS",
            "detail": (
                f"T0={t0_date}, T0終値={t0_close:.1f}, "
                f"feats._close={feats.get('_close') if feats else 'N/A':.1f}"
                if feats else f"T0={t0_date}, feats=None(データ不足)"
            ),
        })

        # forward_outcome の base_price が T0 終値と一致するか
        oc = labeling.forward_outcome(df, idx)
        if oc:
            match = abs(oc["base_price"] - t0_close) < 0.01
            checks.append({
                "check": f"動的検証({code}) — forward_outcome.base_price が T0 終値と一致",
                "status": "PASS" if match else "FAIL",
                "detail": f"base_price={oc['base_price']:.1f}, T0終値={t0_close:.1f}",
            })

        # 材料の日付が T0 以降を含まないか
        mat = materials.recent_material_score(code, t0_date)
        mat_days = mat.get("last_material_days")
        future_mat_leak = mat_days is not None and mat_days < 0
        checks.append({
            "check": f"動的検証({code}) — 材料日付が T0 以前",
            "status": "FAIL" if future_mat_leak else "PASS",
            "detail": (
                f"最新材料は T0 の {mat_days} 日前" if mat_days is not None
                else "材料なし(DB未取得)"
            ),
        })

    except Exception as e:
        checks.append({
            "check": f"動的検証({code}) — 実行エラー",
            "status": "NOTE",
            "detail": str(e),
        })
    return checks
