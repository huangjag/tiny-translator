#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 薄壳 —— 保持 `python scripts/translate.py ...` 这一既有调用方式可用。

实现全部在 tiny_translator 包内；这个文件只负责把仓库根目录放进 sys.path。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tiny_translator.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
