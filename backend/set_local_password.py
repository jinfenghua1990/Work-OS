#!/usr/bin/env python3
"""Generate a local Work OS password hash without printing the password."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
from local_auth import password_hash


def main() -> None:
    first = getpass.getpass("请输入本地 Work OS 管理密码（至少 12 位）：")
    again = getpass.getpass("请再次输入：")
    if first != again:
        raise SystemExit("两次密码不一致，未生成配置。")
    try:
        result = password_hash(first)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print("\n将下面一行添加到项目根目录 .env（不要提交到 GitHub）：")
    print("WORKOS_AUTH_PASSWORD_HASH=" + result)
    print("重启本地 API 后将启用登录。未配置时仍只允许 127.0.0.1 本地访问。")


if __name__ == "__main__":
    main()
