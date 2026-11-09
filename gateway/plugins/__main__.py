from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import re

import yaml

from gateway.plugins.packages import pack
from gateway.plugins.schema import read_manifest


def main():
    parser = argparse.ArgumentParser(description="Kemo Advanced Plugin SDK v1")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("destination", type=Path)
    create.add_argument("--id", required=True)
    for name in ("check", "pack"):
        command = subparsers.add_parser(name)
        command.add_argument("source", type=Path)
        if name == "pack":
            command.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    version = json.loads((root / "version.json").read_text(encoding="utf-8"))["version"]
    if args.operation == "create":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.id):
            parser.error("插件 ID 格式无效")
        destination = args.destination.resolve()
        shutil.copytree(root / "examples/advanced_plugins/local_text", destination,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in destination.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8").replace("local-text", args.id.replace("_", "-")).replace("local_text", args.id)
                path.write_text(text, encoding="utf-8")
        manifest_path = destination / "plugin.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest.update(id=args.id, name=args.id, author="", summary="本地文本 Provider；请按实际模型来源修改实现与能力声明。")
        manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
        read_manifest(destination, version)
        print(destination)
    elif args.operation == "pack":
        print(pack(args.source.resolve(), args.destination.resolve(), version))
    else:
        print(read_manifest(args.source.resolve(), version).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
