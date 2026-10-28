"""转到已经建好的虚拟环境，再启动 MCP。不安装依赖。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    )
    python = next((item for item in candidates if item.is_file()), None)
    if python is None:
        sys.stderr.write(
            "没有找到虚拟环境。请先在项目目录执行：\n"
            "macOS：python3.13 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt "
            "&& .venv/bin/python -m playwright install chromium\n"
            "Windows：py -3.13 -m venv .venv && .venv\\Scripts\\python.exe -m pip install -r requirements.txt "
            "&& .venv\\Scripts\\python.exe -m playwright install chromium\n"
        )
        raise SystemExit(1)
    os.chdir(ROOT)
    executable = str(python)
    os.execv(executable, [executable, "-m", "seller_auto.mcp_server"])


if __name__ == "__main__":
    main()
