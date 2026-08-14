"""
機械学習モデル + 過去急騰パターン類似度。

- 分類器: 教師データ(急騰=1/非急騰=0)で急騰確率を学習 (GradientBoosting)。
- 類似度: 過去急騰のT0特徴ベクトル群への近さ(コサイン)を 0..1 で返す。
- 未学習でも全体が動くよう、load 失敗時は None / 0 に縮退(ルールベースで予測継続)。
- データが貯まるほど再学習で精度改善できる構造。
- クラウドモード(DATABASE_URL): モデルを model_meta.model_data に保存/取得。
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime

import numpy as np
from joblib import dump, load

from . import db
from .config import CURRENT_MODEL_VERSION_FILE, MODEL_DIR
from .features import FEATURE_KEYS, to_vector

DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# 類似度NN(pos/neg_danger)専用の特徴量サブセット。
# teacher.build_historical() は material/theme_tailwind/market_score を渡さないため、
# これらに依存する特徴量は historical_pos/neg (教師データの大半) で常に0固定。
# ライブサンプルが少数でも混入すると、StandardScalerがこれらの列の標準偏差を
# 極小値として計算し(例: market_score scale_=0.0067)、本番クエリの実値
# (例: market_score=-0.284) を標準化した際に数十標準偏差級の異常値へ爆発、
# コサイン類似度計算全体を支配して全銘柄が「類似度0.98超」に張り付く不具合の原因
# だった(2026-08-10)。類似度NNは常に全サンプルで意味のある値を持つ
# チャート/価格系特徴量のみに限定することで、この訓練/クエリ分布の不一致を
# 構造的に回避する。分類器(GradientBoostingClassifier)は引き続き全特徴量を使う。
_SIMILARITY_EXCLUDE_KEYS = {
    "material_raw", "pos_impact", "neg_impact", "has_fresh_material",
    "dilution_flag", "going_concern_flag", "n_materials",
    "theme_tailwind", "market_score",
}
SIMILARITY_FEATURE_INDICES = np.array(
    [i for i, k in enumerate(FEATURE_KEYS) if k not in _SIMILARITY_EXCLUDE_KEYS])


def _bundle_path(version: str):
    return MODEL_DIR / f"model_{version}.joblib"


def current_version() -> str | None:
    if CURRENT_MODEL_VERSION_FILE.exists():
        v = CURRENT_MODEL_VERSION_FILE.read_text().strip()
        if v:
            return v
    # クラウドモード: ファイルがなければ DB から最新バージョンを取得
    try:
        with db.cursor() as conn:
            r = conn.execute(
                "SELECT version FROM model_meta ORDER BY trained_at DESC LIMIT 1"
            ).fetchone()
        return r["version"] if r else None
    except Exception:
        return None


def _save_model_to_db(version: str, bundle_path) -> None:
    """モデルファイルを DB の model_data 列に保存 (クラウドモード用)。"""
    try:
        with open(bundle_path, "rb") as f:
            data = f.read()
        with db.cursor() as conn:
            conn.execute(
                "UPDATE model_meta SET model_data=%s WHERE version=%s",
                (data, version),
            )
    except Exception as e:
        print(f"    [model] DB保存スキップ: {e}")


def _load_model_from_db(version: str):
    """DB の model_data 列からモデルを読み込む。"""
    try:
        with db.cursor() as conn:
            r = conn.execute(
                "SELECT model_data FROM model_meta WHERE version=%s", (version,)
            ).fetchone()
        if r and r.get("model_data"):
            data = r["model_data"]
            if isinstance(data, memoryview):
                data = bytes(data)
            return load(io.BytesIO(data))
    except Exception as e:
        print(f"    [model] DB読み込みスキップ: {e}")
    return None


def prune_old_model_blobs(keep: int = 4) -> dict:
    """古いモデルバージョンの model_data(BYTEA、重い本体)だけを削除する。

    Predictor は常に最新バージョンしか読み込まないため、古いバンドル本体は
    実運用で一切参照されない。ただし version/trained_at/metrics/feature_importance
    等の軽量メタデータは全件残すため、過去のモデル品質推移(AUC等)は引き続き
    閲覧できる。Neon の Storage クォータ(累積型・月次リセットなし)を圧迫する
    主因だったため、再学習のたびに自動実行して肥大化を防ぐ。
    """
    with db.cursor() as conn:
        versions = conn.execute(
            "SELECT version FROM model_meta WHERE model_data IS NOT NULL "
            "ORDER BY trained_at DESC"
        ).fetchall()
        to_clear = [v["version"] for v in versions[keep:]]
        if to_clear:
            ph = ",".join(["%s"] * len(to_clear))
            conn.execute(f"UPDATE model_meta SET model_data=NULL WHERE version IN ({ph})",
                        tuple(to_clear))
    return {"kept": min(len(versions), keep), "cleared": len(to_clear)}


MATURE_BARS = 18  # 20営業日窓のうちこれ以上追跡できていれば"満期"とみなす


def load_samples() -> tuple[np.ndarray, np.ndarray, list[str]]:
    X, y, src, _danger, _bars, _codes = load_samples_full()
    return X, y, src


def load_samples_full() -> tuple[np.ndarray, np.ndarray, list[str], np.ndarray, np.ndarray, list[str]]:
    """教師データを1クエリで取得し、X, y, source, danger_flag, bars_tracked, code を返す。

    danger_flag: label==0 のうち "危険失敗"(danger_fail) だったか。
    historical は tags.result_class、live は tags.result に danger_fail が入る。
    bars_tracked: live サンプルの実追跡日数 (prediction_outcomes へ prediction_id で
    LEFT JOIN)。live_fail は必ず20日待って確定するため常に成熟(=20)。live_success は
    +20%到達した時点で即確定するため未成熟(早期)なものが大半 — sample_weights の
    早期成功割引に使う。historical/該当なしは None 扱い (NaN)。
    code: 同一銘柄が短期間に何度もlive_successになった際、類似度NN(pos)側で
    ほぼ同一の特徴ベクトルが重複登録されるのを防ぐための重複排除に使う。
    X/y/src と行対応が崩れないよう、必ず同一クエリ結果から同時に組み立てる。
    """
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT t.code,t.features,t.label,t.source,t.tags,o.bars_tracked
               FROM teacher_samples t
               LEFT JOIN prediction_outcomes o ON o.prediction_id=t.prediction_id"""
        ).fetchall()
    X, y, src, danger, bars, codes = [], [], [], [], [], []
    for r in rows:
        feats = db.loadj(r["features"], {})
        if not feats:
            continue
        label = int(r["label"])
        X.append(to_vector(feats))
        y.append(label)
        src.append(r["source"])
        bars.append(r["bars_tracked"] if r["bars_tracked"] is not None else np.nan)
        codes.append(r["code"] or "")
        if label == 0:
            tags = db.loadj(r["tags"], {})
            rc = tags.get("result_class") or tags.get("result")
            danger.append(rc == "danger_fail")
        else:
            danger.append(False)
    if not X:
        return (np.empty((0, len(FEATURE_KEYS))), np.empty((0,)), [],
                np.empty((0,), dtype=bool), np.empty((0,)), [])
    return (np.array(X, dtype=float), np.array(y, dtype=int), src,
            np.array(danger, dtype=bool), np.array(bars, dtype=float), codes)


def sample_weights(src: list[str], bars_tracked: np.ndarray | None = None) -> np.ndarray:
    """live_success/live_fail に重みを乗せる (historicalに埋もれないようにする)。

    live件数が少ないうちは控えめ、増えるほど自動的に重みが上がる自己スケール式
    (learning.live_sample_weight_fraction)。手動チューニング不要。

    重要: live_success は +20%到達した瞬間に即確定するため、大半(実測88%)が
    bars_tracked<18の"早期・未成熟"な勝ちで、danger_fail等で本当に失敗する
    候補と同じ特徴量傾向を持つケースも含まれる (生存者バイアス)。live_fail は
    20日満期を待たないと確定しないため常に成熟している。この非対称性を無視して
    両方に同じ重みを掛けると、モデルが「早期モメンタム特徴=成功」と過学習し
    predict_proba が全体的に高騰する (実際に2026-07-27に発生・確認済み)。
    → live_success は bars_tracked>=MATURE_BARS の"満期"のものだけ重み付け対象。
      未成熟の live_success は historical と同じ重み1.0 (満期になれば自動的に
      重み付け対象になる)。live_fail は常に対象 (常に成熟しているため)。
    """
    from . import learning
    n = len(src)
    src_arr = np.array(src)
    if bars_tracked is None:
        bars_tracked = np.full(n, np.nan)
    mature = bars_tracked >= MATURE_BARS
    is_live_fail = src_arr == "live_fail"
    is_live_success_mature = (src_arr == "live_success") & mature
    is_live_boosted = is_live_fail | is_live_success_mature

    n_live = int(is_live_boosted.sum())
    n_hist = n - n_live
    w = np.ones(n, dtype=float)
    if n_live == 0 or n_hist == 0:
        return w
    target_frac = learning.live_sample_weight_fraction(n_live, n_hist)
    # target_frac = (n_live * w_live) / (n_live*w_live + n_hist*w_hist) を w_live について解く (w_hist=1)
    w_live = (target_frac * n_hist) / ((1 - target_frac) * n_live)
    w[is_live_boosted] = w_live
    return w


def train(notes: str = "", dry_run: bool = False) -> dict:
    """教師データから再学習して保存。返り値はメトリクス。

    - live_success/live_fail は historical に埋もれないよう自己スケール式で重み付け
      (sample_weights) して最終fitに反映する。
    - 成功類似度(nn/pos)と対になる「危険失敗パターン類似度」(danger_nn/neg_danger)も
      同時に構築し、predict時の危険パターン検知に使う。
    - dry_run=True: 学習・評価は行うが DB/ファイルへの書き込みは一切しない (検証用)。
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.neighbors import NearestNeighbors

    X, y, src, danger_flags, bars_tracked, codes = load_samples_full()
    n_pos = int((y == 1).sum()) if len(y) else 0
    n_neg = int((y == 0).sum()) if len(y) else 0
    if len(X) < 30 or n_pos < 8 or n_neg < 8:
        return {"trained": False, "reason": "教師データ不足",
                "n_samples": len(X), "n_pos": n_pos, "n_neg": n_neg}

    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    weights = sample_weights(src, bars_tracked)

    clf = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                     subsample=0.85, random_state=42)
    try:
        auc = float(np.mean(cross_val_score(clf, Xs, y, cv=4, scoring="roc_auc")))
    except Exception:
        auc = float("nan")
    clf.fit(Xs, y, sample_weight=weights)
    train_auc = float(roc_auc_score(y, clf.predict_proba(Xs)[:, 1]))

    # 類似度NN用の正例プール: 同一銘柄が短期間に何度もlive_successになると
    # ほぼ同一の特徴ベクトルが重複登録され、小さいk(=5)のNNではその銘柄の
    # 重複が近傍を独占して similarity が異常に高騰する (B_very_strong_ai の暴走)。
    # live_success は銘柄ごとに1件のみ残して重複を除く。classifier学習用の
    # weights/X/yはそのまま(重複を減らさない)ので学習信号自体は変えない。
    pos_mask = y == 1
    pos_all = Xs[pos_mask]
    pos_src = np.array(src)[pos_mask]
    pos_codes = np.array(codes)[pos_mask]
    keep = np.ones(len(pos_all), dtype=bool)
    seen_live_codes: set[str] = set()
    for i in range(len(pos_all)):
        if pos_src[i] == "live_success":
            c = pos_codes[i]
            if c and c in seen_live_codes:
                keep[i] = False
            elif c:
                seen_live_codes.add(c)
    pos = pos_all[keep][:, SIMILARITY_FEATURE_INDICES]
    nn = NearestNeighbors(n_neighbors=min(5, len(pos)), metric="cosine").fit(pos) if len(pos) else None

    # 類似度しきい値の自己較正: ライブサンプル(実際の市場コンテキスト付き)が
    # 増減するたびに pos プールの分布が変わり、固定の magic number(0.68/0.78等)
    # だと選別力を失う/暴走する不具合を2026-08-10・08-14に繰り返した。
    # 再学習のたびに「典型的な銘柄(負例からランダム抽出)が pos プールに対して
    # 実際どの程度似ているか」を測り直し、その分布の上位パーセンタイルを
    # "strong"/"very_strong" の基準にする — 分布が動いても閾値が自動追従する。
    sim_thresholds = {"strong": 0.68, "very_strong": 0.78}  # nn未構築時のフォールバック
    if nn is not None and len(pos) >= 5:
        neg_all = Xs[y == 0][:, SIMILARITY_FEATURE_INDICES]
        if len(neg_all):
            rng = np.random.default_rng(42)
            sample_n = min(1000, len(neg_all))
            sample_idx = rng.choice(len(neg_all), size=sample_n, replace=False)
            sample = neg_all[sample_idx]
            dist, _ = nn.kneighbors(sample, n_neighbors=min(5, len(pos)))
            calib_sims = np.clip(1 - dist.mean(axis=1), 0.0, 1.0)
            sim_thresholds = {
                "strong": round(float(np.percentile(calib_sims, 85)), 4),
                "very_strong": round(float(np.percentile(calib_sims, 95)), 4),
            }

    # 危険失敗パターン類似度用NN (件数が少なすぎる場合はNone=無効)
    neg_danger = (Xs[danger_flags][:, SIMILARITY_FEATURE_INDICES] if danger_flags.any()
                 else np.empty((0, len(SIMILARITY_FEATURE_INDICES))))
    danger_nn = (NearestNeighbors(n_neighbors=min(5, len(neg_danger)), metric="cosine").fit(neg_danger)
                if len(neg_danger) >= 5 else None)

    importance = dict(sorted(
        zip(FEATURE_KEYS, [float(v) for v in clf.feature_importances_]),
        key=lambda kv: kv[1], reverse=True))

    src_arr = np.array(src)
    is_live_fail = src_arr == "live_fail"
    is_live_success = src_arr == "live_success"
    mature = bars_tracked >= MATURE_BARS
    n_live_fail = int(is_live_fail.sum())
    n_live_success_mature = int((is_live_success & mature).sum())
    n_live_success_immature = int((is_live_success & ~mature).sum())
    boosted = is_live_fail | (is_live_success & mature)
    live_weight = float(weights[boosted][0]) if boosted.any() else 1.0

    result_extra = {
        "n_live_fail": n_live_fail,
        "n_live_success_mature": n_live_success_mature,
        "n_live_success_immature_discounted": n_live_success_immature,
        "n_danger_samples": int(len(neg_danger)),
        "live_sample_weight": round(live_weight, 3),
        "sim_thresholds": sim_thresholds,
    }

    if dry_run:
        metrics = {"cv_auc": round(auc, 4) if auc == auc else None,
                   "train_auc": round(train_auc, 4)}
        return {"trained": True, "dry_run": True, "n_samples": len(X),
                "n_pos": n_pos, "n_neg": n_neg, **metrics,
                "top_features": list(importance.items())[:10], **result_extra}

    version = datetime.now().strftime("v%Y%m%d_%H%M%S")
    bundle = {"clf": clf, "scaler": scaler, "nn": nn, "pos": pos,
              "danger_nn": danger_nn, "neg_danger": neg_danger,
              "feature_keys": FEATURE_KEYS, "sim_thresholds": sim_thresholds}

    # ローカルファイルに保存 (クラウドでも一時的に必要)
    bundle_path = _bundle_path(version)
    dump(bundle, bundle_path)
    CURRENT_MODEL_VERSION_FILE.write_text(version)

    metrics = {"cv_auc": round(auc, 4) if auc == auc else None,
               "train_auc": round(train_auc, 4)}

    # model_meta に記録 (PostgreSQL: UPSERT / SQLite: INSERT OR REPLACE)
    with db.cursor() as conn:
        if DATABASE_URL:
            conn.execute(
                """INSERT INTO model_meta(version,trained_at,n_samples,n_pos,n_neg,metrics,feature_importance,notes)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (version) DO UPDATE SET
                     trained_at=EXCLUDED.trained_at,
                     n_samples=EXCLUDED.n_samples,
                     n_pos=EXCLUDED.n_pos,
                     n_neg=EXCLUDED.n_neg,
                     metrics=EXCLUDED.metrics,
                     feature_importance=EXCLUDED.feature_importance,
                     notes=EXCLUDED.notes""",
                (version, datetime.now().isoformat(timespec="seconds"), len(X), n_pos, n_neg,
                 json.dumps(metrics), json.dumps(importance, ensure_ascii=False), notes),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO model_meta"
                "(version,trained_at,n_samples,n_pos,n_neg,metrics,feature_importance,notes)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (version, datetime.now().isoformat(timespec="seconds"), len(X), n_pos, n_neg,
                 json.dumps(metrics), json.dumps(importance, ensure_ascii=False), notes),
            )

    # クラウドモード: モデルデータを DB に保存
    prune_result = {}
    if DATABASE_URL:
        _save_model_to_db(version, bundle_path)
        # 古いバージョンの重いBYTEA本体を自動整理 (Storageクォータの肥大化防止)
        try:
            prune_result = prune_old_model_blobs()
        except Exception as e:
            print(f"    [model] prune skip: {e}")

    return {"trained": True, "version": version, "n_samples": len(X),
            "n_pos": n_pos, "n_neg": n_neg, **metrics,
            "top_features": list(importance.items())[:10], **result_extra,
            "pruned_old_blobs": prune_result}


class Predictor:
    """学習済みモデルのラッパ。未学習時は確率/類似度を None/0 に。"""

    def __init__(self):
        self.bundle = None
        v = current_version()
        if v:
            # クラウドモード: DB から優先ロード
            if DATABASE_URL:
                self.bundle = _load_model_from_db(v)
            # ローカルモード or DB ロード失敗時: ファイルから
            if self.bundle is None:
                path = _bundle_path(v)
                if path.exists():
                    try:
                        self.bundle = load(path)
                    except Exception:
                        self.bundle = None
        self.version = v if self.bundle else None

    @property
    def ready(self) -> bool:
        return self.bundle is not None

    @property
    def sim_thresholds(self) -> dict:
        """再学習ごとに自己較正された similarity の strong/very_strong 基準値。
        旧バンドル(未保存)や未学習時は安全なデフォルトにフォールバック。"""
        if self.ready and self.bundle.get("sim_thresholds"):
            return self.bundle["sim_thresholds"]
        return {"strong": 0.68, "very_strong": 0.78}

    def predict_proba(self, feats: dict) -> float | None:
        if not self.ready:
            return None
        x = np.array([to_vector(feats)], dtype=float)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        xs = self.bundle["scaler"].transform(x)
        return float(self.bundle["clf"].predict_proba(xs)[0, 1])

    def similarity(self, feats: dict) -> float:
        if not self.ready or self.bundle.get("nn") is None:
            return 0.0
        x = np.array([to_vector(feats)], dtype=float)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        xs = self.bundle["scaler"].transform(x)[:, SIMILARITY_FEATURE_INDICES]
        dist, _ = self.bundle["nn"].kneighbors(xs, n_neighbors=min(5, len(self.bundle["pos"])))
        sim = 1 - float(np.mean(dist))
        return max(0.0, min(1.0, sim))

    def danger_similarity(self, feats: dict) -> float:
        """過去の"危険失敗"(danger_fail)パターンへの類似度 (0..1)。

        成功類似度(similarity)と対になる失敗側のシグナル。旧バンドル(danger_nn未構築)
        では常に0.0を返す (再学習で自動的に有効になる、破壊的変更ではない)。
        """
        if not self.ready or self.bundle.get("danger_nn") is None:
            return 0.0
        x = np.array([to_vector(feats)], dtype=float)
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
        xs = self.bundle["scaler"].transform(x)[:, SIMILARITY_FEATURE_INDICES]
        neg = self.bundle["neg_danger"]
        dist, _ = self.bundle["danger_nn"].kneighbors(xs, n_neighbors=min(5, len(neg)))
        sim = 1 - float(np.mean(dist))
        return max(0.0, min(1.0, sim))


def latest_meta() -> dict | None:
    with db.cursor() as conn:
        r = conn.execute(
            "SELECT version,trained_at,n_samples,n_pos,n_neg,metrics,feature_importance,notes "
            "FROM model_meta ORDER BY trained_at DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["metrics"] = db.loadj(d.get("metrics"), {})
    d["feature_importance"] = db.loadj(d.get("feature_importance"), {})
    return d
