"""
VAPID 鍵ペアを生成するワンタイムスクリプト。
実行後、出力された値を環境変数として設定してください。

Usage: python scripts/gen_vapid_keys.py

必要: pip install pywebpush  (requirements.txt に含まれています)
"""
import base64
import sys

try:
    from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption
    )
except ImportError:
    print("cryptography ライブラリが見つかりません。")
    print("pip install pywebpush  を実行してください。")
    sys.exit(1)

# EC P-256 鍵ペア生成
private_key = generate_private_key(SECP256R1())

# 秘密鍵: PEM 形式 (pywebpush が期待する形式)
private_pem = private_key.private_bytes(
    Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
).decode("ascii").strip()

# 公開鍵: Base64URL エンコード (非圧縮点形式, 65バイト)
pub_raw = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
public_b64 = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode("ascii")

SEP = "=" * 65
print(f"\n{SEP}")
print("VAPID キー生成完了！以下を環境変数として設定してください")
print(SEP)
print()
print("【Render の場合】")
print("  Dashboard → Web Service → Environment → Add Environment Variable")
print()
print("【GitHub Actions の場合】")
print("  Settings → Secrets and variables → Actions → New repository secret")
print()
print(f"VAPID_PRIVATE_KEY=")
for line in private_pem.splitlines():
    print(f"  {line}")
print()
print(f"VAPID_PUBLIC_KEY={public_b64}")
print()
print(f"VAPID_ADMIN_EMAIL=your-email@example.com")
print()
print(SEP)
print("注意: この鍵を再生成するとブラウザの購読がリセットされます。")
print("      一度生成したら大切に保管してください。")
print(SEP)
