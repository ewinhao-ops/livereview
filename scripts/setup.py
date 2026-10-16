"""Create local configuration without overwriting an existing installation."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure LiveReview locally")
    parser.add_argument("--generate-password", action="store_true",
                        help="Generate a random password, saved only in backend/.env")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = root / "backend" / ".env"
    if target.exists():
        print("已存在 backend/.env，保留现有配置。")
        return
    if args.generate_password:
        password = secrets.token_urlsafe(24)
    else:
        password = getpass.getpass("设置管理员密码（至少 12 位）：")
        if len(password) < 12 or password != getpass.getpass("再次输入密码："):
            raise SystemExit("密码太短或两次输入不同；未创建配置。")
        if any(c in password for c in "\r\n\"'\\#$") or password.strip() != password:
            raise SystemExit("密码请不要包含引号、反斜线、#、$ 或首尾空格。")
    content = (root / ".env.example").read_text(encoding="utf-8")
    content = content.replace("ACCESS_PASSWORD=\n", f'ACCESS_PASSWORD="{password}"\n')
    content = content.replace("SECRET_KEY=\n", f"SECRET_KEY={secrets.token_hex(32)}\n")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    (root / "backend" / "data").mkdir(exist_ok=True)
    print("配置已保存到 backend/.env。密码和签名密钥仅保存在本机，不会写入 Git。")
    if args.generate_password:
        print("查看 backend/.env 的 ACCESS_PASSWORD 获取本机管理员密码。")


if __name__ == "__main__":
    main()
