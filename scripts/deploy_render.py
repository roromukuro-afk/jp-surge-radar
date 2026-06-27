"""
Render.com Web Service を Render API 経由で自動作成・デプロイするスクリプト。

使い方:
  1. https://dashboard.render.com → Account Settings → API Keys → Create API Key
  2. $env:RENDER_API_KEY=rnd_xxxxxxxxxxxxxxx  (PowerShell)
  3. python scripts/deploy_render.py

DATABASE_URL / VAPID 鍵は .env から自動読み込み (表示しない)。
"""
import os
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

RENDER_API = "https://api.render.com/v1"
REPO = "https://github.com/roromukuro-afk/jp-surge-radar"
_ROOT = Path(__file__).parent.parent


def _load_env():
    """Load .env without exposing values."""
    env_file = _ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


def _load_multiline_env():
    """Parse multi-line VAPID_PRIVATE_KEY from .env properly."""
    env_file = _ROOT / ".env"
    if not env_file.exists():
        return
    text = env_file.read_text(encoding="utf-8")
    in_block = False
    key_name = None
    block_lines: list[str] = []
    result: dict[str, str] = {}
    for line in text.splitlines():
        if in_block:
            block_lines.append(line)
            if "-----END" in line:
                result[key_name] = "\n".join(block_lines)
                in_block = False
                block_lines = []
                key_name = None
        elif "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if "-----BEGIN" in v:
                key_name = k
                block_lines = [v]
                in_block = True
            else:
                result[k] = v
    for k, v in result.items():
        if k not in os.environ:
            os.environ[k] = v


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
        body_text = e.read().decode()
        print(f"HTTP {e.code}: {body_text}")
        raise


def main():
    _load_multiline_env()

    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key or not api_key.startswith("rnd_"):
        print("ERROR: RENDER_API_KEY が設定されていません。")
        print()
        print("手順:")
        print("  1. https://dashboard.render.com → Account Settings → API Keys")
        print("  2. 'Create API Key' をクリック → rnd_xxx... をコピー")
        print("  3. PowerShell: $env:RENDER_API_KEY='rnd_xxx...'")
        print("  4. python scripts/deploy_render.py を再実行")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "").strip()
    vapid_priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    vapid_pub = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    vapid_email = os.environ.get("VAPID_ADMIN_EMAIL", "roromukuro@gmail.com").strip()

    if not db_url:
        print("ERROR: DATABASE_URL が見つかりません (.env を確認)。")
        sys.exit(1)
    if not vapid_priv:
        print("ERROR: VAPID_PRIVATE_KEY が見つかりません (.env を確認)。")
        sys.exit(1)

    print("=== Render 既存サービス確認 ===")
    try:
        services = _api("GET", "/services?limit=20", key=api_key)
        if isinstance(services, list):
            existing = [s for s in services if s.get("service", {}).get("name") == "surge-radar"]
        else:
            existing = []
        if existing:
            svc = existing[0]["service"]
            svc_id = svc["id"]
            url_val = svc.get("serviceDetails", {}).get("url", "")
            print(f"既存サービス発見 → 再デプロイを実行")
            resp = _api("POST", f"/services/{svc_id}/deploys", {"clearCache": "do_not_clear"}, key=api_key)
            deploy_id = resp.get("id", "?")
            print(f"Deploy ID: {deploy_id}")
            print(f"ダッシュボード: https://dashboard.render.com/web/{svc_id}")
            if url_val:
                print(f"URL: {url_val}")
            print("ビルド完了まで約3〜5分かかります。")
            return
    except Exception as e:
        print(f"既存サービス確認中にエラー: {e}")
        print("新規作成を試みます...")

    print("=== オーナーID 取得 ===")
    try:
        owners = _api("GET", "/owners?limit=1", key=api_key)
        owner_id = owners[0]["owner"]["id"]
        print(f"Owner ID: {owner_id[:8]}...")
    except Exception as e:
        print(f"ERROR: オーナー情報を取得できませんでした: {e}")
        print("APIキーを確認してください。")
        sys.exit(1)

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

    try:
        resp = _api("POST", "/services", payload, key=api_key)
    except Exception as e:
        print(f"ERROR: サービス作成失敗: {e}")
        print()
        print("Render ダッシュボードで手動作成してください:")
        print("  https://dashboard.render.com → New+ → Web Service")
        print(f"  Repo: {REPO}")
        sys.exit(1)

    svc = resp.get("service", resp)
    svc_id = svc.get("id", "?")
    svc_url = svc.get("serviceDetails", {}).get("url", "")
    print(f"サービス作成完了!")
    print(f"ダッシュボード: https://dashboard.render.com/web/{svc_id}")
    if svc_url:
        print(f"URL: {svc_url}")
    else:
        print("URL: ビルド完了後に https://dashboard.render.com で確認してください")
    print()
    print("ビルドが完了するまで約3〜5分かかります。")
    print("完了確認: <URL>/healthz")


if __name__ == "__main__":
    main()
