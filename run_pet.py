"""快捷方式/自启动用的入口脚本。

放到包外面是为了让 ``pythonw.exe run_pet.py`` 直接可用：脚本所在目录会自动
进入 ``sys.path``，因此不需要依赖 PYTHONPATH 环境变量。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finance_pet.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
