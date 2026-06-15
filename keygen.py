"""
keygen.py - 生成 Platform 使用的 RSA 密钥对

运行：python keygen.py
输出：platform_private.pem（签名 JWT 用）、platform_jwks.json（Tool 验签用）

Tool 侧本 demo 不需要独立密钥（AGS 的 OAuth2 token 验证已简化）。
生产环境中 Tool 也应有自己的密钥对，用于 Client Credentials 认证。
"""

import json, base64
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def int_to_base64url(n: int) -> str:
    """将大整数编码为 Base64URL 字符串（JWK 格式要求）。"""
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def generate_platform_keypair():
    """生成 2048-bit RSA 密钥对，保存 PEM 私钥和 JWKS 公钥文件。"""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # 保存私钥（PEM，无密码保护，仅用于 demo）
    with open("platform_private.pem", "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    # 构造 JWK 公钥
    pub_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "platform-key-1",
                "n": int_to_base64url(pub_numbers.n),
                "e": int_to_base64url(pub_numbers.e),
            }
        ]
    }

    with open("platform_jwks.json", "w") as f:
        json.dump(jwks, f, indent=2)

    print("生成完毕：")
    print("  platform_private.pem  <- Platform 用来签 JWT")
    print("  platform_jwks.json    <- Tool 用来验证 JWT 签名")


if __name__ == "__main__":
    generate_platform_keypair()
