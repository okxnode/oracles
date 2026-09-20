#!/usr/bin/env python3
"""从旧的「账号清单 + 共用私钥」结构迁移到本项目格式。

老结构（很多号池脚本都是这样）::

    oracle/
    ├── accounts.json      [{"index":1,"region":"...","user":"...",
    │                        "fingerprint":"...","tenancy":"..."}, ...]
    └── si.pem             所有账号共用的 API 私钥

本项目新结构::

    $ORACLES_HOME/
    ├── accounts.json      （多了 alias / key_file 等可选字段）
    ├── oracles.env
    └── keys/
        └── oci_api_key.pem

用法::

    python scripts/import_legacy_accounts.py \
        --source ~/Desktop/oracle \
        --dest   ~/.oracles

    # 只看会做什么，不落盘
    python scripts/import_legacy_accounts.py --source ~/Desktop/oracle --dry-run

⚠️ 迁移后的目录权限会设成 600/700。**不要**把它放进任何 git 仓库。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REQUIRED_KEYS = {"index", "region", "user", "fingerprint", "tenancy"}


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧的 OCI 账号清单")
    parser.add_argument("--source", required=True,
                        help="旧目录（含 accounts.json 和私钥）")
    parser.add_argument("--dest", default=os.environ.get("ORACLES_HOME",
                                                         "~/.oracles"),
                        help="目标配置目录（默认 $ORACLES_HOME 或 ~/.oracles）")
    parser.add_argument("--key-name", default=None,
                        help="私钥文件名（默认自动探测 si.pem / *.pem）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只显示会做什么，不写文件")
    parser.add_argument("--force", action="store_true",
                        help="目标已存在时覆盖")
    args = parser.parse_args()

    src = Path(args.source).expanduser().resolve()
    dest = Path(args.dest).expanduser().resolve()

    if not src.is_dir():
        print(f"❌ 源目录不存在：{src}", file=sys.stderr)
        return 1

    # ---- 读旧清单 ----
    src_json = src / "accounts.json"
    if not src_json.is_file():
        print(f"❌ 找不到 {src_json}", file=sys.stderr)
        return 1

    try:
        raw = json.loads(src_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"❌ {src_json} 不是合法 JSON：{exc}", file=sys.stderr)
        return 1
    if not isinstance(raw, list) or not raw:
        print("❌ 账号清单必须是非空数组", file=sys.stderr)
        return 1

    print(f"📋 读到 {len(raw)} 个账号")

    # ---- 校验并补全 ----
    accounts = []
    problems = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"  第 {i} 条不是对象")
            continue
        missing = REQUIRED_KEYS - set(item)
        if missing:
            problems.append(f"  第 {i} 条缺字段：{sorted(missing)}")
            continue
        # 旧格式里可能有我们没见过的字段，只保留已知的
        known = {k: item[k] for k in
                 ("index", "region", "user", "fingerprint", "tenancy", "alias",
                  "key_file", "auth_mode", "compartment_id", "default_shape",
                  "default_image_os", "default_ssh_key") if k in item}
        accounts.append(known)

    if problems:
        print("❌ 清单有问题：", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    indexes = [a["index"] for a in accounts]
    if len(set(indexes)) != len(indexes):
        dupes = sorted({x for x in indexes if indexes.count(x) > 1})
        print(f"❌ 序号重复：{dupes}", file=sys.stderr)
        return 1

    # ---- 找私钥 ----
    if args.key_name:
        key_src = src / args.key_name
    else:
        candidates = [src / "si.pem"] + sorted(src.glob("*.pem"))
        key_src = next((c for c in candidates if c.is_file()), None)

    if key_src is None or not key_src.is_file():
        print(f"⚠️ 在 {src} 里没找到私钥（*.pem）", file=sys.stderr)
        print("   可以用 --key-name 指定文件名", file=sys.stderr)
        return 1

    print(f"🔑 私钥：{key_src.name}")

    # ---- 目标 ----
    dest_json = dest / "accounts.json"
    dest_key = dest / "keys" / "oci_api_key.pem"

    print(f"\n目标目录：{dest}")
    print(f"  账号清单 → {dest_json}")
    print(f"  私钥     → {dest_key}")

    if dest_json.exists() and not args.force:
        print(f"\n❌ {dest_json} 已存在。加 --force 覆盖（原文件会备份成 .bak）",
              file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n（--dry-run：什么都没写）")
        print("\n将要写入的账号清单：")
        preview = [{**a, "key_file": str(dest_key)} for a in accounts]
        print(json.dumps(preview, ensure_ascii=False, indent=2)[:2000])
        return 0

    # ---- 落盘 ----
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    (dest / "keys").mkdir(exist_ok=True)
    os.chmod(dest / "keys", 0o700)

    if dest_json.exists():
        backup = dest_json.with_suffix(".json.bak")
        shutil.copy2(dest_json, backup)
        print(f"ℹ️ 原清单已备份到 {backup}")

    shutil.copy2(key_src, dest_key)
    os.chmod(dest_key, 0o600)

    # 统一把 key_file 指向新位置，这样多个账号共用一把私钥
    for acc in accounts:
        acc["key_file"] = str(dest_key)
    accounts.sort(key=lambda a: a["index"])

    dest_json.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(dest_json, 0o600)

    print("\n✅ 迁移完成")
    print(f"   {len(accounts)} 个账号 → {dest_json}")
    print(f"   私钥 → {dest_key}")

    # ---- 提示下一步 ----
    env_file = dest / "oracles.env"
    print("\n下一步：")
    if not env_file.exists():
        print(f"  1. 建环境变量文件：{env_file}")
        print("     TELEGRAM_BOT_TOKEN=...")
        print("     TELEGRAM_ALLOWED_USER_IDS=...")
        print(f"     ORACLES_HOME={dest}")
    print(f"  2. 自检：ORACLES_HOME={dest} python -m oracles.cli doctor")
    print(f"  3. 全流程演练：ORACLES_HOME={dest} python scripts/selftest.py")

    print(f"\n⚠️ {dest} 里有私钥和 OCID —— 别放进任何 git 仓库。")
    print("   本项目的 .gitignore 已经覆盖，但前提是它不在仓库目录里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
