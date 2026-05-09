"""Dataset 加密 / 解密 — 防止本機 jsonl 被偷檔時內容外流。

設計：
- 對稱加密 Fernet (AES-128-CBC + HMAC-SHA256)
- key 存 ~/.line_bot_encryption.key（mode 600，**不在專案內，不在 git 範圍**）
- 加密 .jsonl → .jsonl.enc，原 .jsonl 刪
- 訓練前解密成 tmp file，訓練完 shred

CLI：
    python finetune/dataset_crypt.py --encrypt finetune/data/line_export_raw.jsonl
    python finetune/dataset_crypt.py --decrypt finetune/data/line_export_raw.jsonl.enc -o /tmp/decrypted.jsonl
    python finetune/dataset_crypt.py --keygen   # 第一次用要先建 key
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

KEY_PATH = Path.home() / ".line_bot_encryption.key"


def _load_or_generate_key() -> bytes:
    """讀 key；不存在就建（mode 600）。"""
    from cryptography.fernet import Fernet

    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    KEY_PATH.chmod(0o600)
    print(f"[INFO] 新建 key 於 {KEY_PATH}（mode 600）", file=sys.stderr)
    return key


def encrypt_file(src: Path, *, delete_src: bool = True) -> Path:
    """src → src.enc。可選刪除 src（加密完直接砍明文）。"""
    from cryptography.fernet import Fernet

    key = _load_or_generate_key()
    cipher = Fernet(key)
    plaintext = src.read_bytes()
    encrypted = cipher.encrypt(plaintext)
    out = src.with_suffix(src.suffix + ".enc")
    out.write_bytes(encrypted)
    out.chmod(0o600)
    print(
        f"[OK] 加密 {src} ({len(plaintext)} bytes) → {out} ({len(encrypted)} bytes)",
        file=sys.stderr,
    )
    if delete_src:
        src.unlink()
        print(f"[OK] 刪除明文 {src}", file=sys.stderr)
    return out


def decrypt_file(enc: Path, out: Path | None = None) -> Path:
    """enc → out（預設寫到 /tmp/<name>，避免明文長存專案內）。"""
    from cryptography.fernet import Fernet

    key = _load_or_generate_key()
    cipher = Fernet(key)
    encrypted = enc.read_bytes()
    plaintext = cipher.decrypt(encrypted)
    if out is None:
        # 去 .enc 後綴，寫 /tmp
        name = enc.name
        if name.endswith(".enc"):
            name = name[:-4]
        out = Path("/tmp") / name
    out.write_bytes(plaintext)
    out.chmod(0o600)
    print(
        f"[OK] 解密 {enc} ({len(encrypted)} bytes) → {out} ({len(plaintext)} bytes)",
        file=sys.stderr,
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encrypt", type=Path, help="加密這個檔（.jsonl）")
    parser.add_argument("--decrypt", type=Path, help="解密這個檔（.jsonl.enc）")
    parser.add_argument("-o", "--out", type=Path, help="解密輸出位置（預設 /tmp）")
    parser.add_argument("--keep-src", action="store_true", help="加密後不刪明文")
    parser.add_argument("--keygen", action="store_true", help="只建 key 不操作檔")
    args = parser.parse_args()

    if args.keygen:
        _load_or_generate_key()
        return 0

    if args.encrypt:
        if not args.encrypt.exists():
            print(f"[ERR] 找不到 {args.encrypt}", file=sys.stderr)
            return 1
        encrypt_file(args.encrypt, delete_src=not args.keep_src)
        return 0

    if args.decrypt:
        if not args.decrypt.exists():
            print(f"[ERR] 找不到 {args.decrypt}", file=sys.stderr)
            return 1
        decrypt_file(args.decrypt, args.out)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
