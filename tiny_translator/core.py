"""core.py —— 翻译编排：引擎选择、文本/HTML 分流、标签保护。

这里是给其他项目复用的库入口。本模块不调用 sys.exit、不写 stdout，
进程级事务（参数解析、输出路由、退出码）由 cli.py 负责。

用法::

    import tiny_translator as tt

    tt.translate_text("こんにちは", target="zh")      # 自动挑一个已配置的引擎
    tt.translate_html(page, target="zh")             # 保留标签结构

    engine = tt.BaiduEngine(app_id="...", secret_key="...")
    tt.translate_text("hello", target="zh", engine=engine)   # 注入引擎，跳过凭据发现
"""

from __future__ import annotations

import logging

from .auth import (
    ensure_auth_template_file,
    resolve_baidu_credentials,
    resolve_google_credentials,
    resolve_volcengine_credentials,
)
from .engines.baidu import BaiduEngine
from .engines.base import BaseEngine
from .engines.google_v2 import GoogleV2Engine
from .engines.volcengine import VolcengineEngine
from .errors import AuthMissingError, MissingDependencyError
from .tags import DOMTextNodeProcessor, GoogleHTMLProcessor

logger = logging.getLogger("tiny_translator")

ENGINE_MAP: dict[str, str] = {
    "1": "baidu",
    "baidu": "baidu",
    "2": "volcengine",
    "volc": "volcengine",
    "volcengine": "volcengine",
    "9": "google",
    "google": "google",
}

PROVIDER_LABELS: dict[str, str] = {
    "baidu": "百度翻译 (服务 1)",
    "volcengine": "火山引擎机器翻译 (服务 2)",
    "google": "谷歌翻译 (服务 9)",
}

# 默认探测顺序：1 (百度) -> 2 (火山引擎) -> 9 (谷歌)
DEFAULT_PROBE_ORDER = ("baidu", "volcengine", "google")


def _build_google_engine(auth_info: dict) -> GoogleV2Engine:
    """API Key 走纯 REST (仅标准库)，只有服务账号凭据才需要 google-cloud-translate SDK。"""
    if auth_info.get("credentials_path"):
        try:
            import google.cloud.translate_v2  # noqa: F401
        except ImportError:
            raise MissingDependencyError(
                "google-cloud-translate",
                "缺少 google-cloud-translate 依赖 (服务账号凭据需要该 SDK)。"
                "请执行 pip install google-cloud-translate，或改用 GOOGLE_API_KEY 走纯 REST 接口。",
            ) from None
    return GoogleV2Engine(
        api_key=auth_info.get("api_key"),
        credentials_path=auth_info.get("credentials_path"),
    )


def _auth_missing(engine_label: str) -> AuthMissingError:
    """生成用户配置模板（下次运行即可被发现）并返回缺凭据异常。"""
    return AuthMissingError(engine_label, ensure_auth_template_file())


def _build_engine(provider: str, credentials_path: str | None, *, required: bool) -> BaseEngine | None:
    """按 provider 构造引擎；凭据不全时返回 None（required=True 时抛 AuthMissingError）。"""
    if provider == "baidu":
        info = resolve_baidu_credentials(credentials_path)
        if info.get("app_id") and info.get("secret_key"):
            return BaiduEngine(app_id=info["app_id"], secret_key=info["secret_key"])
    elif provider == "volcengine":
        info = resolve_volcengine_credentials(credentials_path)
        if info.get("access_key") and info.get("secret_key"):
            return VolcengineEngine(access_key=info["access_key"], secret_key=info["secret_key"])
    elif provider == "google":
        info = resolve_google_credentials(credentials_path)
        if info.get("api_key") or info.get("credentials_path"):
            return _build_google_engine(info)
    else:
        raise ValueError(f"未知的翻译服务 '{provider}'")

    if required:
        raise _auth_missing(PROVIDER_LABELS[provider])
    return None


def resolve_engine(engine_name: str | None = None, credentials_path: str | None = None) -> BaseEngine:
    """解析并初始化引擎。

    - 显式指定编号/名称 (1/2/9 或 baidu/volc/google)：严格使用该服务，
      凭据缺失时抛 AuthMissingError。
    - 未指定：按 1 -> 2 -> 9 探测，跳过无凭据的服务。
    """
    if engine_name is not None:
        canonical = ENGINE_MAP.get(str(engine_name).lower().strip())
        if not canonical:
            raise ValueError(
                f"未知的翻译引擎编号或名称 '{engine_name}'。支持: 1 (百度), 2 (火山引擎), 9 (谷歌)"
            )
        engine = _build_engine(canonical, credentials_path, required=True)
        if engine is None:  # required=True 时不会发生，保留以防子类实现变化
            raise _auth_missing(PROVIDER_LABELS[canonical])
        return engine

    for provider in DEFAULT_PROBE_ORDER:
        engine = _build_engine(provider, credentials_path, required=False)
        if engine is not None:
            logger.info("采用 [%s]", PROVIDER_LABELS[provider])
            return engine
        logger.debug("[%s] 未检测到有效凭据，跳过", PROVIDER_LABELS[provider])

    raise _auth_missing("默认服务: 1 (百度)、2 (火山引擎) 或 9 (谷歌)")


def available_engines(credentials_path: str | None = None) -> list[str]:
    """返回当前已配置凭据的服务名列表（只查本地配置，不发网络请求）。"""
    names: list[str] = []
    for provider in DEFAULT_PROBE_ORDER:
        try:
            if _build_engine(provider, credentials_path, required=False) is not None:
                names.append(provider)
        except MissingDependencyError:
            # 凭据在，但缺 SDK：服务仍算已配置，调用时才会报错
            names.append(provider)
    return names


def _translate_plain(engine: BaseEngine, text: str, source: str, target: str) -> str:
    """纯文本模式：按行分批翻译，保留每行原有的换行符。"""
    lines = text.splitlines(keepends=True)
    clean_lines = [line.rstrip("\r\n") for line in lines]
    translated_clean = engine.translate(clean_lines, source=source, target=target, format_="text")

    reconstructed: list[str] = []
    for orig, trans in zip(lines, translated_clean):
        newline = orig[len(orig.rstrip("\r\n")):]
        reconstructed.append(trans + newline)
    return "".join(reconstructed)


def _translate_html(engine: BaseEngine, text: str, source: str, target: str) -> str:
    """HTML 模式：原生 HTML 引擎走掩码/切分/还原，纯文本引擎走 DOM 节点隔离。"""
    if not engine.supports_html:
        return DOMTextNodeProcessor.process_and_translate(
            text,
            engine.translate,
            source=source,
            target=target,
        )

    # 1. 脆弱原子保护
    masked_text, atom_map = GoogleHTMLProcessor.protect_atoms(text)

    # 2. 骑跨检测：成对标签跨越 br 时整段发送，避免孤儿标签
    if GoogleHTMLProcessor.is_straddling_br(masked_text):
        trans_res = engine.translate([masked_text], source=source, target=target, format_="html")
        raw_translated = trans_res[0] if trans_res else ""
    else:
        # 3. 捕获切分
        pairs = GoogleHTMLProcessor.split_by_br(masked_text)
        segments = [p[0] for p in pairs]
        delims = [p[1] for p in pairs]

        non_empty_indices = [idx for idx, seg in enumerate(segments) if seg.strip()]
        to_send = [segments[idx] for idx in non_empty_indices]
        translated_batch = (
            engine.translate(to_send, source=source, target=target, format_="html") if to_send else []
        )

        translated_segments = list(segments)
        for idx, trans_seg in zip(non_empty_indices, translated_batch):
            translated_segments[idx] = trans_seg

        reassembled: list[str] = []
        for seg, delim in zip(translated_segments, delims):
            reassembled.append(seg)
            if delim:
                reassembled.append(delim)
        raw_translated = "".join(reassembled)

    # 4. 本地还原
    final_html = GoogleHTMLProcessor.restore_atoms(raw_translated, atom_map)

    # 5. 结构后检
    GoogleHTMLProcessor.check_tags(text, final_html)
    return final_html


def translate(
    text: str,
    *,
    target: str = "zh",
    source: str = "auto",
    format: str = "auto",
    engine: BaseEngine | None = None,
    engine_name: str | None = None,
    credentials_path: str | None = None,
) -> str:
    """翻译一段文本或 HTML，返回同格式结果。

    Args:
        text: 待翻译内容。
        target: 目标语言代码，默认 'zh'。
        source: 源语言代码，默认 'auto'。
        format: 'auto' | 'text' | 'html'。'auto' 时含 `<` 与 `>` 即按 HTML 处理。
        engine: 已构造好的引擎；传入后跳过凭据发现与 engine_name。
        engine_name: '1'/'baidu'、'2'/'volc'、'9'/'google'；None 表示自动探测。
        credentials_path: 显式凭据文件路径（等价于 CLI 的 --credentials）。

    Raises:
        AuthMissingError: 没有可用凭据。
        TranslationError: 翻译失败（子类区分额度/限流/网络）。
    """
    if not text or not text.strip():
        return text

    if engine is None:
        engine = resolve_engine(engine_name, credentials_path)

    is_html = format == "html" or (format == "auto" and "<" in text and ">" in text)
    if is_html:
        return _translate_html(engine, text, source, target)
    return _translate_plain(engine, text, source, target)


def translate_text(text: str, **kwargs) -> str:
    """纯文本翻译（强制 text 模式，不做 HTML 探测）。"""
    kwargs.pop("format", None)
    return translate(text, format="text", **kwargs)


def translate_html(html: str, **kwargs) -> str:
    """HTML 翻译（强制 html 模式，保留标签、属性与实体）。"""
    kwargs.pop("format", None)
    return translate(html, format="html", **kwargs)
