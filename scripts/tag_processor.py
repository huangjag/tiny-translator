#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""tag_processor.py —— HTML 标签保护、切分与还原器。

基于实测知识库 (google-translate-v2-html-knowledge.md) 设计：
1. GoogleHTMLProcessor (针对支持 format=html 的引擎，如 Google Translate v2)：
   - 脆弱原子保护: <rt>注音、<wbr>、文本节点中的 HTML 实体替换为 <span translate="no">⟦Gn⟧</span>
   - 骑跨预检: 检测是否有成对标签跨越 <br> 边界；若骑跨则走整段翻译避免孤儿标签
   - 捕获式切分: 按 (<br\b[^>]*\/?>) 切分，原样保留每个 br 的语法细节
   - 本地还原: 先对引擎输出 unescape，再将 ⟦Gn⟧ 还原为原始字面量，最后拼回原始 br
   - 结构后检: 比对输入/输出标签守恒性并预警
2. DOMTextNodeProcessor (针对不支持 HTML 的纯文本引擎，如百度翻译)：
   - DOM 树分离: 仅提取纯文本节点给引擎，标签/属性留在本地，100% 不破坏 HTML
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger("tiny_translator.tag_processor")

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"
}

BR_CAPTURE_RE = re.compile(r"(<br\b[^>]*\/?>)", re.IGNORECASE)
HTML_TAG_OR_TEXT_RE = re.compile(r"(<!--.*?-->|<[^>]+>|[^<]+)", re.DOTALL)
TAG_NAME_RE = re.compile(r"^<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)\b", re.IGNORECASE)
ENTITY_RE = re.compile(r"&[a-zA-Z0-9#]+;")
RT_RE = re.compile(r"(<rt\b[^>]*>)(.*?)(</rt>)", re.IGNORECASE | re.DOTALL)
WBR_RE = re.compile(r"(<wbr\b[^>]*\/?>)", re.IGNORECASE)
# 空元素或注释；用于把 BeautifulSoup 自闭合序列化还原为原始书写（注释不参与还原）
VOID_OR_COMMENT_RE = re.compile(
    r"(<!--.*?-->)|(<(?:%s)\b[^>]*\/?>)" % "|".join(sorted(VOID_TAGS)),
    re.IGNORECASE | re.DOTALL,
)

# 还原 token 匹配正则
SPAN_TOKEN_RE = re.compile(
    r'<span\b[^>]*translate=["\']no["\'][^>]*>\s*⟦\s*G\s*(\d+)\s*⟧\s*</span>',
    re.IGNORECASE,
)
BARE_TOKEN_RE = re.compile(r"⟦\s*G\s*(\d+)\s*⟧", re.IGNORECASE)
GENERAL_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)")

# 完整标签（含属性），用于逐位骨架比对。注释/DOCTYPE/PI 不匹配（名字必须以字母开头）。
SKELETON_TAG_RE = re.compile(
    r"<\s*(/?)\s*([a-zA-Z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.DOTALL,
)
ATTR_RE = re.compile(r"([:\w-]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+)))?")
# 这些属性的值会被翻译器改写，骨架比对只看属性名，避免误报
TRANSLATABLE_ATTRS = frozenset({"title", "alt", "placeholder", "aria-label", "value", "label"})


def _normalize_attrs(raw: str) -> tuple[tuple[str, str], ...]:
    """把属性串规范化为可比较的 (名字, 值) 元组，按名字排序。"""
    attrs: list[tuple[str, str]] = []
    for m in ATTR_RE.finditer(raw):
        name = m.group(1).lower()
        value = m.group(2)
        if value is None:
            value = m.group(3)
        if value is None:
            value = m.group(4) or ""
        if name in TRANSLATABLE_ATTRS:
            value = ""
        attrs.append((name, value.strip()))
    return tuple(sorted(attrs))


def _skeleton(text: str) -> list[tuple[bool, str, tuple[tuple[str, str], ...]]]:
    """提取有序标签骨架：(是否闭合, 标签名, 规范化属性)。"""
    out: list[tuple[bool, str, tuple[tuple[str, str], ...]]] = []
    for m in SKELETON_TAG_RE.finditer(text):
        closing = bool(m.group(1))
        name = m.group(2).lower()
        attrs = () if closing else _normalize_attrs(m.group(3))
        out.append((closing, name, attrs))
    return out


class GoogleHTMLProcessor:
    """适用于 Google Translate v2 (format=html) 的标签保护管线。"""

    @classmethod
    def protect_atoms(cls, text: str) -> tuple[str, dict[str, str]]:
        """脆弱原子掩码：保护 rt 注音、wbr、以及文本节点内的 HTML 实体。

        属性内部的实体不掩码，避免向属性内注入 span 破坏 HTML 语法。
        """
        atom_map: dict[str, str] = {}
        counter = 0

        def _make_token(original: str) -> str:
            nonlocal counter
            key = f"⟦G{counter}⟧"
            atom_map[str(counter)] = original
            counter += 1
            return f'<span translate="no">{key}</span>'

        # 1. 保护 <rt>...</rt> 内部内容 (保留 <rt> 标签结构以维持 ruby 内容模型)
        def _mask_rt(m: re.Match[str]) -> str:
            open_rt, content, close_rt = m.group(1), m.group(2), m.group(3)
            return open_rt + _make_token(content) + close_rt

        text = RT_RE.sub(_mask_rt, text)

        # 2. 保护 <wbr>
        def _mask_wbr(m: re.Match[str]) -> str:
            return _make_token(m.group(0))

        text = WBR_RE.sub(_mask_wbr, text)

        # 3. 扫描标签与文本，只对文本节点内部的 HTML 实体做掩码
        parts = HTML_TAG_OR_TEXT_RE.findall(text)
        new_parts: list[str] = []
        for part in parts:
            if part.startswith("<"):
                new_parts.append(part)
            else:
                def _mask_entity(m: re.Match[str]) -> str:
                    return _make_token(m.group(0))

                new_parts.append(ENTITY_RE.sub(_mask_entity, part))

        return "".join(new_parts), atom_map

    @classmethod
    def is_straddling_br(cls, text: str) -> bool:
        """骑跨预扫描：检测是否存在成对标签跨越 <br> 边界。

        如果任何 br 位于未闭合的成对标签内，返回 True（应作为整段发送，不可切分）。
        """
        stack: list[str] = []
        parts = HTML_TAG_OR_TEXT_RE.findall(text)
        for part in parts:
            if not part.startswith("<") or part.startswith("<!--"):
                continue
            m = TAG_NAME_RE.match(part)
            if not m:
                continue
            is_close, tag_name = m.group(1) == "/", m.group(2).lower()

            if is_close:
                # 弹栈至最近匹配
                if tag_name in stack:
                    while stack:
                        top = stack.pop()
                        if top == tag_name:
                            break
            elif tag_name in VOID_TAGS:
                if tag_name == "br":
                    if len(stack) > 0:
                        # 在成对标签内发现了 br，属于骑跨！
                        return True
            elif not part.rstrip().endswith("/>"):
                stack.append(tag_name)
        return False

    @classmethod
    def split_by_br(cls, text: str) -> list[tuple[str, str]]:
        r"""按 (<br\b[^>]*\/?>) 切分，捕获每个 br 原样写法。

        返回: [(segment_text, delimiter_br_or_empty), ...]
        """
        tokens = BR_CAPTURE_RE.split(text)
        pairs: list[tuple[str, str]] = []
        i = 0
        while i < len(tokens):
            seg = tokens[i]
            delim = tokens[i + 1] if i + 1 < len(tokens) else ""
            pairs.append((seg, delim))
            i += 2
        return pairs

    @classmethod
    def restore_atoms(cls, text: str, atom_map: dict[str, str]) -> str:
        """本地还原：解码 Google 转义的字符 (如 &#39;)，再还原 ⟦Gn⟧ 字节级原文。"""
        # Google v2 会将单引号等转义为 HTML 实体 (&#39;)，针对性解码，
        # 避免全局 html.unescape 篡改属性内部合法的 &amp; 等实体
        text = text.replace("&#39;", "'").replace("&#x27;", "'")
        text = text.replace("&quot;", '"')
        text = text.replace("&#10098;", "⟦").replace("&#10099;", "⟧")

        # 优先还原完整的 <span translate="no">⟦Gn⟧</span>
        def _replace_span(m: re.Match[str]) -> str:
            idx = m.group(1)
            return atom_map.get(idx, m.group(0))

        text = SPAN_TOKEN_RE.sub(_replace_span, text)

        # 兜底还原裸 ⟦Gn⟧（防止 Google 偶尔脱掉 span）
        def _replace_bare(m: re.Match[str]) -> str:
            idx = m.group(1)
            return atom_map.get(idx, m.group(0))

        text = BARE_TOKEN_RE.sub(_replace_bare, text)
        return text

    @classmethod
    def skeleton_diff(
        cls, original_html: str, translated_html: str, limit: int = 5
    ) -> list[str]:
        """逐位比对标签骨架（顺序 + 属性），返回可读的差异列表。

        只比标签序列与属性，不比文本内容；属性值中会被翻译器改写的字段
        （title / alt 等）只比名字，避免误报。
        """
        orig = _skeleton(original_html)
        trans = _skeleton(translated_html)
        diffs: list[str] = []
        matcher = difflib.SequenceMatcher(a=orig, b=trans, autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                continue
            diffs.append(
                f"{op}: 原文[{i1}:{i2}]={orig[i1:i2][:3]} -> 译文[{j1}:{j2}]={trans[j1:j2][:3]}"
            )
            if len(diffs) >= limit:
                break
        return diffs

    @classmethod
    def check_tags(cls, original_html: str, translated_html: str) -> dict[str, int]:
        """后检比对输入与输出中的标签守恒性。返回丢失标签及其计数。"""
        orig_counts = Counter(GENERAL_TAG_RE.findall(original_html.lower()))
        trans_counts = Counter(GENERAL_TAG_RE.findall(translated_html.lower()))
        lost = orig_counts - trans_counts
        if lost:
            logger.warning("标签后检发现数量差异: %s", dict(lost))
        for diff in cls.skeleton_diff(original_html, translated_html):
            logger.warning("标签骨架差异: %s", diff)
        return dict(lost)


class DOMTextNodeProcessor:
    """适用于纯文本引擎 (如百度翻译、腾讯翻译) 的 DOM 骨架抽取处理器。

    只提取纯文本节点交给引擎，DOM 标签与属性 100% 留在本地，彻底避免标签破坏。
    """

    @classmethod
    def _restore_void_literals(cls, serialized: str, original: str) -> str:
        """BeautifulSoup 序列化会把空元素统一写成自闭合 (如 <br/>)；按文档顺序还原为原始书写。"""
        originals = [m.group(2) for m in VOID_OR_COMMENT_RE.finditer(original) if m.group(2)]
        if not originals:
            return serialized
        it = iter(originals)

        def _sub(m: re.Match[str]) -> str:
            if m.group(1) is not None:  # 注释原样保留，不消耗还原序列
                return m.group(1)
            return next(it, m.group(2))

        return VOID_OR_COMMENT_RE.sub(_sub, serialized)

    @classmethod
    def process_and_translate(
        cls,
        html_content: str,
        translate_fn: Any,
        source: str = "auto",
        target: str = "zh",
    ) -> str:
        """提取 DOM 纯文本节点、批量翻译并无缝回填。"""
        try:
            from bs4 import BeautifulSoup, NavigableString
            from bs4.element import PreformattedString
        except ImportError:
            # 降级：若未安装 bs4，直接纯文本回退
            logger.warning("未安装 beautifulsoup4，无法执行 DOM 骨架隔离，降级为直接翻译")
            res = translate_fn([html_content], source=source, target=target, format_="text")
            return res[0] if res else ""

        soup = BeautifulSoup(html_content, "html.parser")
        text_nodes: list[NavigableString] = []
        raw_texts: list[str] = []

        for node in soup.descendants:
            if isinstance(node, NavigableString):
                # 忽略注释/CDATA/PI 等预格式化节点，以及 script、style 或纯空白节点
                if isinstance(node, PreformattedString):
                    continue
                if node.parent and node.parent.name in ("script", "style"):
                    continue
                val = str(node)
                if val.strip():
                    text_nodes.append(node)
                    raw_texts.append(val)

        if not raw_texts:
            return html_content

        # 批量翻译纯文本
        translated_texts = translate_fn(raw_texts, source=source, target=target, format_="text")

        # 灌回 DOM：按索引回填，避免 zip 静默截断；
        # 译文为空但原文非空时保留原文，防止引擎吞掉分隔符等内容。
        empty_kept = 0
        missing = 0
        for i, node in enumerate(text_nodes):
            src = raw_texts[i]
            if i < len(translated_texts):
                trans = translated_texts[i]
            else:
                trans = src
                missing += 1
            if not trans.strip() and src.strip():
                trans = src
                empty_kept += 1
            node.replace_with(trans)

        if missing:
            logger.warning(
                "引擎返回条数不足 (期望 %d, 实际 %d)，缺失部分保留原文",
                len(text_nodes), len(translated_texts),
            )
        if empty_kept:
            logger.warning("引擎返回空译文 %d 处，已保留原文避免内容丢失", empty_kept)

        return cls._restore_void_literals(str(soup), html_content)
