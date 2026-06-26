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


def load_samples() -> tuple[np.ndarray, np.ndarray, list[str]]:
    with db.cursor() as conn:
        rows = conn.execute("SELECT features,label,source FROM teacher_samples").fetchall()
    X, y, src = [], [], []
    for r in rows:
        feats = db.loadj(r["features"], {})
        if not feats:
            continue
        X.append(to_vector(feats))
        y.append(int(r["label"]))
        src.append(r["source"])
    if not X:
        return np.empty((0, len(FEATURE_KEYS))), np.empty((0,)), []
    return np.array(X, dtype=float), np.array(y, dtype=int), src


def train(notes: str = "") -> dict:
    """教師データから再学習して保存。返り値はメトリクス。"""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.neighbors import NearestNeighbors

    X, y, _ = load_samples()
    n_pos = int((y == 1).sum()) if len(y) else 0
    n_neg = int((y == 0).sum()) if len(y) else 0
    if len(X) < 30 or n_pos < 8 or n_neg < 8:
        return {"trained": False, "reason": "教師データ不足",
                "n_samples": len(X), "n_pos": n_pos, "n_neg": n_neg}

    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)

    clf = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                     subsample=0.85, random_state=42)
    try:
        auc = float(np.mean(cross_val_score(clf, Xs, y, cv=4, scoring="roc_auc")))
    except Exception:
        auc = float("nan")
    clf.fit(Xs, y)
    train_auc = float(roc_auc_score(y, clf.predict_proba(Xs)[:, 1]))

    pos = Xs[y == 1]
    nn = NearestNeighbors(n_neighbors=min(5, len(pos)), metric="cosine").fit(pos) if len(pos) else None

    importance = dict(sorted(
        zip(FEATURE_KEYS, [float(v) for v in clf.feature_importances_]),
        key=lambda kv: kv[1], reverse=True))

    version = datetime.now().strftime("v%Y%m%d_%H%M%S")
    bundle = {"clf": clf, "scaler": scaler, "nn": nn, "pos": pos,
              "feature_keys": FEATURE_KEYS}

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
    if DATABASE_URL:
        _save_model_to_db(version, bundle_path)

    return {"trained": True, "version": version, "n_samples": len(X),
            "n_pos": n_pos, "n_neg": n_neg, **metrics,
            "top_features": list(importance.items())[:10]}


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
        xs = self.bundle["scaler"].transform(x)
        dist, _ = self.bundle["nn"].kneighbors(xs, n_neighbors=min(5, len(self.bundle["pos"])))
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
