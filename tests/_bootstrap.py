"""测试引导：统一解决「仓库根目录不在 sys.path」的导入问题。

两种运行方式都能正常工作：

1. pytest（推荐）
       .venv/bin/python -m pytest            # 全量
       .venv/bin/python -m pytest tests/test_camera.py -v
   pytest.ini 里的 `pythonpath = . tests` 会把仓库根目录加进 sys.path。

2. 直接执行单个测试文件
       .venv/bin/python tests/test_camera.py
   此时 sys.path[0] 是 tests/，因此本文件可按普通模块名导入；导入本模块即完成
   sys.path 修复，文件末尾的 `if __name__ == "__main__": _bootstrap.run(__file__)`
   会用 pytest 把该文件真正跑起来（否则直接执行只会导入、不会执行任何测试）。

注意：本模块必须在导入项目代码（assistant / hardware / ai ...）**之前**被导入。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(test_file: str) -> None:
    """以 pytest 方式运行单个测试文件（供直接 python 执行时调用）。"""
    try:
        import pytest
    except ModuleNotFoundError:
        print(
            "未找到 pytest。请使用项目虚拟环境运行，例如：\n"
            f"    .venv/bin/python {Path(test_file).relative_to(ROOT)}\n"
            "或先安装依赖：pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(3)
    raise SystemExit(pytest.main([str(test_file), "-v"]))
