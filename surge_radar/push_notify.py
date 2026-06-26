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


def send_all(title: str, body: str, url: str = "/", tag: str = "pipeline") -> dict:
    """全購読者に Web Push 通知を送信。返り値は {sent, failed, reason}。"""
    if not is_configured():
        return {"sent": 0, "failed": 0, "reason": "VAPID keys not configured"}

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return {"sent": 0, "failed": 0, "reason": "pywebpush not installed"}

    from . import db

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
                vapid_private_key=VAPID_PRIVATE_KEY,
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
