"""tiny_translator —— 标签安全的多引擎翻译库。

其他项目的最小用法::

    import tiny_translator as tt
    tt.translate_text("こんにちは", target="zh")
    tt.translate_html(page_html, target="zh")

CLI 用法见 `python scripts/translate.py --help`。
"""

from __future__ import annotations

from . import auth, errors, tags
from .core import (
    ENGINE_MAP,
    available_engines,
    resolve_engine,
    translate,
    translate_html,
    translate_text,
)
from .engines import BaiduEngine, BaseEngine, GoogleV2Engine, VolcengineEngine
from .errors import (
    AuthMissingError,
    MissingDependencyError,
    NetworkError,
    ProviderError,
    QuotaExceededError,
    RateLimitError,
    TranslationError,
)
from .tags import DOMTextNodeProcessor, GoogleHTMLProcessor

__version__ = "1.0.0"

__all__ = [
    "translate",
    "translate_text",
    "translate_html",
    "resolve_engine",
    "available_engines",
    "ENGINE_MAP",
    "BaseEngine",
    "BaiduEngine",
    "VolcengineEngine",
    "GoogleV2Engine",
    "GoogleHTMLProcessor",
    "DOMTextNodeProcessor",
    "TranslationError",
    "AuthMissingError",
    "MissingDependencyError",
    "ProviderError",
    "QuotaExceededError",
    "RateLimitError",
    "NetworkError",
    "auth",
    "tags",
    "errors",
    "__version__",
]
