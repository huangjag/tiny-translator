#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base.py —— 翻译引擎通用抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseEngine(ABC):
    """翻译引擎基类。"""

    name: str = "base"
    supports_html: bool = False  # 是否支持原生 format=html

    @abstractmethod
    def translate(
        self,
        texts: list[str],
        *,
        source: str = "auto",
        target: str = "zh",
        format_: str = "html",
    ) -> list[str]:
        """批量翻译接口。

        Args:
            texts: 待翻译的文本片段列表
            source: 源语言代码 (例如 'ja', 'en', 'auto')
            target: 目标语言代码 (例如 'zh', 'zh-CN')
            format_: 文本格式 ('html' 或 'text')

        Returns:
            list[str]: 翻译后的文本列表，顺序与输入严格一一对应
        """
        pass
