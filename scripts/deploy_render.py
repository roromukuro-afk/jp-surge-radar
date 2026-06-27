"""
Render.com Web Service を Render API 経由で自動作成・デプロイするスクリプト。

使い方:
  1. https://dashboard.render.com → Account Settings → API Keys → Create API Key
  2. set RENDER_API_KEY=rnd_xxxxxxxxxxxxxxx
  3. python scripts/deploy_render.py

必要な環境変数 (Render Env Vars として設定):
  DATABASE_URL        (Neon PostgreSQL - .env 参照)
  VAPID_PRIVATE_KEY   (.env 参照)
  VAPID_PUBLIC_KEY    (BGPbIu5i_... - .env 参照)
  VAPID_ADMIN_EMAIL   (roromukuro@gmail.com)
"""
import os
import sys
import json
import urllib.request
import urllib.error

RENDER_API = "https://api.render.com/v1"
REPO = "https://github.com/roromukuro-afk/jp-surge-radar"


def _api(method: str, path: str, body=None, *, key: str):
    url = RENDER_API + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        raise


def main():
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key or not api_key.startswith("rnd_"):
        print("ERROR: RENDER_API_KEY が設定されていません。")
        print("  https://dashboard.render.com → Account Settings → API Keys で取得し、")
        print("  $env:RENDER_API_KEY='rnd_xxx...' を実行してください。")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "").strip()
    vapid_priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    vapid_pub = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    vapid_email = os.environ.get("VAPID_ADMIN_EMAIL", "roromukuro@gmail.com").strip()

    if not db_url:
        print("ERROR: DATABASE_URL が設定されていません。.env を確認してください。")
        sys.exit(1)
    if not vapid_priv:
        print("ERROR: VAPID_PRIVATE_KEY が設定されていません。.env を確認してください。")
        sys.exit(1)

    print("=== Render 既存サービス確認 ===")
    try:
        services = _api("GET", "/services?limit=20", key=api_key)
        existing = [s for s in services if s.get("service", {}).get("name") == "surge-radar"]
        if existing:
            svc = existing[0]["service"]
            svc_id = svc["id"]
            url_val = svc.get("serviceDetails", {}).get("url", "")
            print(f"既存サービス発見: {svc_id} → {url_val}")
            print("=== 再デプロイを実行 ===")
            resp = _api("POST", f"/services/{svc_id}/deploys", {"clearCache": "do_not_clear"}, key=api_key)
            deploy_id = resp.get("id", "?")
            print(f"Deploy started: {deploy_id}")
            print(f"ダッシュボード: https://dashboard.render.com/web/{svc_id}")
            print(f"URL: {url_val}")
            return
    except Exception as e:
        print(f"既存サービス確認エラー (無視して新規作成): {e}")

    print("=== オーナーID 取得 ===")
    owners = _api("GET", "/owners?limit=1", key=api_key)
    if not owners:
        print("ERROR: オーナー情報を取得できませんでした。APIキーを確認してください。")
        sys.exit(1)
    owner_id = owners[0]["owner"]["id"]
    print(f"Owner ID: {owner_id}")

    print("=== GitHub リポジトリ接続確認 ===")
    print(f"リポジトリ: {REPO}")
    print("NOTE: Render ダッシュボードで GitHub 連携が必要です。")
    print("  https://dashboard.render.com → Profile → GitHub連携")

    print("=== Web Service 作成 ===")
    payload = {
        "type": "web_service",
        "name": "surge-radar",
        "ownerId": owner_id,
        "repo": REPO,
        "branch": "master",
        "region": "singapore",
        "plan": "free",
        "runtime": "python",
        "buildCommand": "pip install -r requirements.txt",
        "startCommand": "python -m surge_radar.cli serve --host 0.0.0.0 --port $PORT",
        "healthCheckPath": "/healthz",
        "autoDeploy": "yes",
        "envVars": [
            {"key": "DATABASE_URL", "value": db_url},
            {"key": "VAPID_PRIVATE_KEY", "value": vapid_priv},
            {"key": "VAPID_PUBLIC_KEY", "value": vapid_pub},
            {"key": "VAPID_ADMIN_EMAIL", "value": vapid_email},
            {"key": "SURGE_ENABLE_LLM", "value": "0"},
        ]
    }

    resp = _api("POST", "/services", payload, key=api_key)
    svc = resp.get("service", resp)
    svc_id = svc.get("id", "?")
    svc_url = svc.get("serviceDetails", {}).get("url", "")
    print(f"サービス作成完了: {svc_id}")
    print(f"URL: {svc_url or '(デプロイ完了後に確定)'}")
    print(f"ダッシュボード: https://dashboard.render.com/web/{svc_id}")
    print()
    print("ビルドが完了するまで約3〜5分かかります。")
    print(f"完了後: https://surge-radar-xxxx.onrender.com/healthz で確認してください。")


if __name__ == "__main__":
    main()
