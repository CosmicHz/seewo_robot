# -*- coding: utf-8 -*-
"""从 pyproject.toml + uv.lock 同步生成 requirements.txt。

依赖的唯一真源是 pyproject.toml 和 uv.lock；
requirements.txt 仅作为非 uv 环境的兼容快照（pip install -r）。

用法:
    python sync_requirements.py
"""

import subprocess
import sys

OUTPUT_FILE = "requirements.txt"


def main() -> int:
    cmd = [
        "uv", "export",
        "--format", "requirements-txt",
        "--no-hashes",
        "--no-header",
        "--output-file", OUTPUT_FILE,
    ]
    print("同步依赖到", OUTPUT_FILE)
    print("$", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"导出失败 (exit {result.returncode})", file=sys.stderr)
        return result.returncode
    print(f"已生成 {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
