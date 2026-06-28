"""
Web Push 通知送信ヘルパ。

必要な環境変数 (オプション、未設定時は通知スキップ):
  VAPID_PRIVATE_KEY  - PEM形式の VAPID 秘密鍵
  VAPID_PUBLIC_KEY   - base64url エンコードの VAPID 公開鍵 (ブラウザ購読用)
  VAPID_ADMIN_EMAIL  - 管理者メール (VAPID claims)

初回セットアップ:
  1. python scripts/gen_vapid_keys.py で鍵生成
  2. 環境変数に設定 (Render / GitHub Secrets)
  3. ブラウザでサイトを開き「通知を許可」
"""
from __future__ import annotations

import json
import os

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_ADMIN_EMAIL = os.environ.get("VAPID_ADMIN_EMAIL", "noreply@example.com")


def is_configured() -> bool:
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


_vapid_obj = None


def _vapid_key():
    """VAPID秘密鍵を pywebpush が扱える形にする。

    PEM文字列をそのまま渡すと pywebpush は raw鍵として base64デコードを試みて失敗する
    (ASN.1 parsing error)。PEMなら py_vapid.Vapid オブジェクトに変換して渡す。
    PEM以外(base64 raw)はそのまま文字列を返す。結果はキャッシュ。
    """
    global _vapid_obj
    if _vapid_obj is not None:
        return _vapid_obj
    key = VAPID_PRIVATE_KEY
    if "BEGIN" in key:  # PEM
        try:
            try:
                from py_vapid import Vapid02 as _Vapid
            except ImportError:
                from py_vapid import Vapid as _Vapid
            _vapid_obj = _Vapid.from_pem(key.encode())
            return _vapid_obj
        except Exception:
            pass
    _vapid_obj = key  # raw base64 などはそのまま
    return _vapid_obj


def send_all(title: str, body: str, url: str = "/", tag: str = "pipeline") -> dict:
    """全購読者に Web Push 通知を送信。返り値は {sent, failed, reason}。"""
    if not is_configured():
        return {"sent": 0, "failed": 0, "reason": "VAPID keys not configured"}

    try:
        from pywebpush import webpush, WebPushException  # noqa: F401
    except ImportError:
        return {"sent": 0, "failed": 0, "reason": "pywebpush not installed"}

    from . import db

    vapid_key = _vapid_key()

    with db.cursor() as conn:
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()

    if not subs:
        return {"sent": 0, "failed": 0, "reason": "no subscribers"}

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag},
                         ensure_ascii=False)
    sent = failed = 0
    dead: list[str] = []

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=vapid_key,
                vapid_claims={"sub": f"mailto:{VAPID_ADMIN_EMAIL}"},
            )
            sent += 1
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status in (404, 410):  # 購読が失効
                dead.append(sub["endpoint"])
            failed += 1

    if dead:
        with db.cursor() as conn:
            for ep in dead:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (ep,))

    return {"sent": sent, "failed": failed, "expired_removed": len(dead)}


def notify_pipeline_result(summary: dict, asof: str) -> list[dict]:
    """daily 成功時の粒度別通知。購読者0なら send_all 側で no-op。

    トリガ: A候補発生 / B候補数 / 高material_quality / live S/A/B成功 / danger_fail。
    複数該当時は重要度の高いものから個別に送る。
    """
    results = []
    if not is_configured():
        return [{"sent": 0, "reason": "VAPID not configured"}]
    pred = summary.get("predict") or {}
    cats = pred.get("categories") or {}
    a = cats.get("A", 0) or 0
    b = cats.get("B", 0) or 0
    c = cats.get("C", 0) or 0
    track = summary.get("track") or {}
    danger = track.get("danger_fail", 0) or 0
    sab = track.get("success_sab") or {}
    succ = (sab.get("S", 0) + sab.get("A", 0) + sab.get("B", 0)) if sab else 0

    # 1) A候補は最重要
    if a > 0:
        results.append(send_all(
            title=f"🚀 A候補 {a}件 発生 ({asof})",
            body=f"今すぐ買い検討型 {a}件 / B {b}件 / C {c}件。ランキング確認。",
            url="/?category=A", tag="alert-A"))
    # 2) danger_fail 発生
    if danger > 0:
        results.append(send_all(
            title=f"⚠️ danger_fail {danger}件",
            body=f"{asof}: 危険失敗を検出。失敗教師データに追加。",
            url="/failures", tag="alert-danger"))
    # 3) live 成功(S/A/B)
    if succ > 0:
        results.append(send_all(
            title=f"✅ live予測 成功 {succ}件 ({asof})",
            body=f"S{sab.get('S',0)}/A{sab.get('A',0)}/B{sab.get('B',0)} が+20%到達。",
            url="/history", tag="alert-success"))
    # 4) 通常更新サマリ (A候補が無い場合のみ単独で送る)
    if a == 0:
        results.append(send_all(
            title=f"急騰レーダー {asof} 更新完了",
            body=f"B候補 {b}件 / C候補 {c}件。ランキングを確認。",
            url="/", tag="daily"))
    return results
