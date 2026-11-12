#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""text_splitter.py —— 按自然边界把长文本切成不超过单请求上限的片段。

移植自 tests/text-splitter（TS 版），针对翻译场景做了三点改造：

1. **无损**：``"".join(split_text(t, n)) == t``，翻译后可以原样拼回；
   TS 版会丢弃纯空白块，拼回时会丢换行。
2. **确定性**：去掉 ``Date.now()`` 之类的 UI 字段，纯函数。
3. **边界空白独立成片**：``split_for_translation`` 把片段首尾的空白剥成独立片段，
   引擎跳过纯空白片段不送翻译，避免服务端 trim 吃掉段落换行。

切分优先级（高 → 低）：换行 → 句末标点 → 英文句点 → 从句标点 → 任意空白 → 硬切。

``split_html`` 是 HTML 版本：只在标签边界切，绝不切断 ``<...>``，并在切点补齐
未闭合标签，保证每一片都是标签平衡的 HTML 片段。
"""

from __future__ import annotations

import bisect
import re

__all__ = [
    "split_text",
    "split_for_translation",
    "expand",
    "expand_bytes",
    "split_html",
    "expand_html",
]

# 边界正则按优先级排列；切点取 match.end()，即边界字符归前一片，
# 这样下一片不会以空白开头（服务端更容易保持结构）。
_BOUNDARIES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\n"),                             # 换行（段落 / 行）
    re.compile(r"[。！？!?…]+[\"'”’）)】」』]*"),     # 句末标点（连同其后的引号括号）
    re.compile(r"(?<=\.)(?=\s)"),                  # 英文句点 + 空白
    re.compile(r"[，,、；;：:]+"),                   # 从句标点
    re.compile(r"\s+"),                            # 任意空白
)

# 切点低于窗口的这个比例时放弃该级边界，换下一级，避免把片段切得太碎。
_MIN_KEEP_RATIO = 0.5

# 贪心切分后剩余的尾片低于上限的这个比例时，改为对半切，避免出现极短尾片。
_MIN_TAIL_RATIO = 0.1


def _find_cut(window: str, min_keep: int) -> int:
    """在窗口内按优先级找最靠后的边界，返回切点；找不到则硬切到窗口末尾。"""
    for pattern in _BOUNDARIES:
        cut = 0
        for match in pattern.finditer(window):
            if match.end() > min_keep:
                cut = match.end()
        if cut:
            return cut
    return len(window)


def split_text(text: str, max_chars: int) -> list[str]:
    """把 text 切成每片不超过 max_chars 的片段，优先在自然边界处切。

    Args:
        text: 待切分文本。
        max_chars: 单片最大字符数（必须 >= 1）。

    Returns:
        片段列表；``"".join(结果) == text``，空串返回 ``[]``。
    """
    if max_chars < 1:
        raise ValueError("max_chars 必须 >= 1")
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    pos, total = 0, len(text)
    min_tail = max(1, int(max_chars * _MIN_TAIL_RATIO))
    while pos < total:
        remaining = total - pos
        if remaining <= max_chars:
            chunks.append(text[pos:])
            break
        # 贪心装满，尽量少发请求
        cut = _find_cut(text[pos : pos + max_chars], int(max_chars * _MIN_KEEP_RATIO))
        if remaining - cut < min_tail:
            # 尾片过短（例如刚好多出一个字）时改成对半切
            target = (remaining + 1) // 2
            cut = _find_cut(text[pos : pos + target], int(target * _MIN_KEEP_RATIO))
        chunks.append(text[pos : pos + cut])
        pos += cut
    return chunks


def split_for_translation(text: str, max_chars: int) -> list[str]:
    """同 split_text，但把片段首尾的空白剥离成独立片段。

    引擎应当跳过纯空白片段（不送翻译）并原样拼回，这样即使服务端 trim 掉
    首尾空白，段落换行也不会丢。拼接后仍然无损。
    """
    chunks: list[str] = []
    for piece in split_text(text, max_chars):
        if not piece.strip():          # 整片都是空白，原样保留
            chunks.append(piece)
            continue
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        if lead:
            chunks.append(piece[:lead])
        chunks.append(piece[lead : len(piece) - trail if trail else None])
        if trail:
            chunks.append(piece[len(piece) - trail :])
    return chunks


def expand(
    texts: list[str], max_chars: int
) -> tuple[list[str], list[tuple[int, int]]]:
    """把超过上限的片段展开成可发送的小片段。

    Returns:
        ``(pieces, spans)``：``spans[i]`` 是 ``texts[i]`` 在 pieces 中对应的
        ``[start, end)`` 范围，``"".join(pieces[start:end])`` 即该片段的完整译文。
    """
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    for text in texts:
        start = len(pieces)
        if len(text) > max_chars:
            pieces.extend(split_for_translation(text, max_chars))
        else:
            pieces.append(text)     # 未超限的片段保持原样（含首尾空白）
        spans.append((start, len(pieces)))
    return pieces, spans


def _widest_char_bytes(text: str) -> int:
    """返回文本中最宽字符的 UTF-8 字节数（至少 1）。"""
    widest = 1
    for ch in text:
        n = len(ch.encode("utf-8"))
        if n > widest:
            widest = n
            if widest >= 4:     # UTF-8 单字符最多 4 字节，无需继续扫
                break
    return widest


def expand_bytes(
    texts: list[str],
    max_bytes: int,
    max_chars: int | None = None,
) -> tuple[list[str], list[tuple[int, int]]]:
    """按 UTF-8 字节预算展开片段，可再叠加字符上限。

    部分接口的 ``q`` 上限按字节计算（百度 6000 字节，客户端留余量用 5700），
    而 :func:`expand` 只按字符数切。中文 3 字节/字、emoji 4 字节/字，2000
    字符可能到 8000 字节。这里先取文本中最宽字符的字节数，得到同时满足
    ``max_bytes`` 与 ``max_chars`` 的字符预算，再复用自然边界切分。

    Returns:
        ``(pieces, spans)``，契约与 :func:`expand` 相同。
    """
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    for text in texts:
        start = len(pieces)
        too_long = len(text.encode("utf-8")) > max_bytes or (
            max_chars is not None and len(text) > max_chars
        )
        if not too_long:
            pieces.append(text)     # 未超限的片段保持原样（含首尾空白）
        else:
            limit = max(1, max_bytes // _widest_char_bytes(text))
            if max_chars is not None:
                limit = min(limit, max_chars)
            pieces.extend(split_for_translation(text, max(1, limit)))
        spans.append((start, len(pieces)))
    return pieces, spans


# --- HTML 切分 -------------------------------------------------------------

# 块级标签：切在这些标签前后时片段天然成对，最安全
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})

_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w:-]*)")

# 掩码原子（<span translate="no">⟦Gn⟧</span> 里的 ⟦Gn⟧）：硬切不能落在它内部
_ATOM_RE = re.compile(r"⟦[^⟧\n]*⟧")

# 文本节点内部的切点：句末标点之后、空白之后
_TEXT_CUT_RE = re.compile(r"[。！？!?…]+[\"'”’）)】」』]*|(?<=\.)(?=\s)|\s+")

# 补齐未闭合标签时的预算（字符），避免补标签把片段顶出上限
_BALANCE_RESERVE = 16


def _tag_end(text: str, start: int) -> int:
    """返回从 start 处 '<' 开始的标签结束位置（'>' 之后）；未闭合则返回末尾。

    会跳过引号内的 '>'，避免把属性值里的 '>' 误当成标签结束。
    """
    n = len(text)
    if text.startswith("<!--", start):
        end = text.find("-->", start + 4)
        return n if end < 0 else end + 3
    if text.startswith("<![CDATA[", start):
        end = text.find("]]>", start + 9)
        return n if end < 0 else end + 3
    quote = ""
    i = start + 1
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i + 1
        i += 1
    return n


def _classify(raw: str) -> tuple[str, bool, bool]:
    """返回 (标签名, 是否闭合标签, 是否块级)；注释/声明返回空名字。"""
    if raw.startswith("<!") or raw.startswith("<?"):
        return "", False, False
    m = _TAG_NAME_RE.match(raw)
    if not m:
        return "", False, False
    name = m.group(2).lower()
    return name, m.group(1) == "/", name in _BLOCK_TAGS


def _text_cuts(text: str, start: int, end: int):
    """文本节点内部的切点：句末标点之后、空白之后。"""
    for m in _TEXT_CUT_RE.finditer(text, start, end):
        if start < m.end() < end:
            yield m.end()


def _boundary_score(prev, name: str | None, closing: bool, depth: int) -> int:
    """切点评分：0 最好（块级标签之前），5 最差（未闭合的行内标签内部）。"""
    if name and not closing and name in _VOID_TAGS:
        return 0                                        # 空元素（<br>、<hr>、<img>）之前，任何深度都安全
    if prev and prev[1] and prev[2] and depth == 0:
        return 1                                        # 紧跟块级闭合标签之后
    if name and not closing and name in _BLOCK_TAGS and depth == 0:
        return 0                                        # 块级标签之前（首选）
    if depth == 0:
        return 2
    if name and name in _BLOCK_TAGS:
        return 3
    return 5


def _html_candidates(text: str):
    """枚举所有安全切点与标签栈快照。

    Returns:
        (candidates, tag_spans, stacks)
        candidates: [(pos, score, stack)]，pos 升序，score 越小越好；
        tag_spans:  [(start, end)]，用于判断某个位置是否落在标签内部；
        stacks:     [(pos, stack)]，pos 为标签结束位置，stack 为该处之后的标签栈，
                    用于硬切时补齐标签（位置不必落在文本节点内）。
    """
    cands: list[tuple[int, int, tuple]] = []
    spans: list[tuple[int, int]] = []
    stacks: list[tuple[int, tuple]] = [(0, ())]
    stack: list[tuple[str, str]] = []      # [(标签名, 原始开标签文本)]
    prev: tuple[str, bool, bool, bool] | None = None   # (名字, 闭合, 块级, 空元素)
    i, n = 0, len(text)

    while i < n:
        lt = text.find("<", i)
        text_end = n if lt < 0 else lt

        if text_end > i:
            depth = len(stack)
            # 紧跟开标签（非空元素）的文本起点不记录：切在这里只会得到空片段，
            # 真正需要的切点是标签之前或文本内部的标点/空白。
            if not (prev and prev[0] and not prev[1] and not prev[3]):
                cands.append((i, _boundary_score(prev, None, False, depth), tuple(stack)))
            text_score = 2 if depth == 0 else 4
            for pos in _text_cuts(text, i, text_end):
                cands.append((pos, text_score, tuple(stack)))

        if lt < 0:
            break

        end = _tag_end(text, lt)
        spans.append((lt, end))
        raw = text[lt:end]
        name, closing, is_block = _classify(raw)
        is_void = (not closing) and (
            name in _VOID_TAGS or raw.rstrip().endswith("/>")
        )
        # <br></br> 这类“空元素 + 闭合标签”成对出现时整体不可切，
        # 否则会切在两者中间；切点应落在整对之前或之后。
        pair_close = bool(closing and name and prev and prev[0] == name and prev[3])
        depth = len(stack)
        if not pair_close:
            cands.append((lt, _boundary_score(prev, name, closing, depth), tuple(stack)))

        if not closing and name and not is_void:
            stack.append((name, raw))
        elif closing and name:
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == name:
                    del stack[k:]
                    break

        prev = (name, closing, is_block, is_void)
        i = end
        stacks.append((i, tuple(stack)))

    return cands, spans, stacks


def _choose_cut(cands, positions, lo: int, hi: int):
    """在 (lo, hi] 内选评分最低、位置最靠后的切点。"""
    idx = bisect.bisect_right(positions, lo)
    best = None
    while idx < len(cands) and cands[idx][0] <= hi:
        cand = cands[idx]
        if best is None or (cand[1], -cand[0]) < (best[1], -best[0]):
            best = cand
        idx += 1
    if best is None:
        return None, ()
    return best[0], best[2]


def _inside_tag(spans, pos: int) -> bool:
    idx = bisect.bisect_right(spans, (pos,)) - 1
    return idx >= 0 and spans[idx][0] < pos < spans[idx][1]


def _has_visible(fragment: str) -> bool:
    """片段里是否有标签之外的可见字符（纯标签片段没有翻译价值）。"""
    i, n = 0, len(fragment)
    while i < n:
        if fragment[i] == "<":
            end = _tag_end(fragment, i)
            if end > i:
                i = end
                continue
        if not fragment[i].isspace():
            return True
        i += 1
    return False


def _avoid_atom(pos: int, atoms) -> int:
    """硬切点落在掩码原子 ⟦Gn⟧ 内部时，退到原子起点。"""
    idx = bisect.bisect_right(atoms, (pos,)) - 1
    if idx >= 0 and atoms[idx][0] < pos < atoms[idx][1]:
        return atoms[idx][0]
    return pos


def _find_html_cut(scan, text, lo: int, hi: int):
    """在 (lo, hi] 内找切点，优先跳过只会产生纯标签片段的切点。"""
    cands, positions, spans, stacks, stack_positions, atoms = scan
    scan_pos = lo
    fallback = None
    while True:
        cand, stack = _choose_cut(cands, positions, scan_pos, hi)
        if cand is None:
            if _inside_tag(spans, hi):
                idx = bisect.bisect_right(spans, (hi,)) - 1
                tag_start, tag_end = spans[idx]
                # 窗口落在标签内部：退到标签起点；标签本身比窗口还长时，
                # 把整个标签带上（切点绝不能落在标签里）。
                hi = tag_start if tag_start > lo else tag_end
                scan_pos = lo
                continue
            # 标签外没有标点/空白可切：硬切，带上该处的标签栈以补齐标签
            cand = _avoid_atom(hi, atoms)
            if cand <= lo:
                # 窗口整个落在原子内部：把整个原子带上，不能切开它
                idx = bisect.bisect_right(atoms, (hi,)) - 1
                cand = atoms[idx][1] if idx >= 0 else hi
                if cand <= lo:
                    return None, ()
            stack = _stack_at(stacks, stack_positions, cand)
        fallback = (cand, stack)
        if _has_visible(text[lo:cand]):
            return cand, stack
        if cand <= scan_pos:
            return fallback
        scan_pos = cand


def _stack_at(stacks, stack_positions, pos: int) -> tuple:
    """返回 pos 处的标签栈（最后一个在 pos 之前结束的标签之后）。"""
    idx = bisect.bisect_right(stack_positions, pos) - 1
    return stacks[idx][1] if idx >= 0 else ()


def split_html(text: str, max_chars: int) -> list[str]:
    """切分 HTML，保证任何标签都不会被切断。

    切点优先级：块级标签之前 → 块级闭合标签之后 → 标签外的句末 → 标签外空白 →
    任意标签边界。切点落在未闭合标签内部时，给前一片补 ``</tag>``、给后一片补回
    ``<tag ...>``，因此每一片都是标签平衡的；拼回后内容不变（标签可能被拆成多对）。
    """
    if max_chars < 1:
        raise ValueError("max_chars 必须 >= 1")
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    cands, spans, stacks = _html_candidates(text)
    scan = (
        cands,
        [c[0] for c in cands],
        spans,
        stacks,
        [p for p, _ in stacks],
        [(m.start(), m.end()) for m in _ATOM_RE.finditer(text)],
    )
    pieces: list[str] = []
    pos = 0
    prefix = ""
    while pos < len(text):
        budget = max_chars - len(prefix)
        if len(text) - pos <= budget:
            pieces.append(prefix + text[pos:])
            break

        cut, cut_stack = None, ()
        content_limit = max(1, budget - _BALANCE_RESERVE)
        for _ in range(4):
            cand, cand_stack = _find_html_cut(scan, text, pos, pos + content_limit)
            if cand is None:
                break                    # 窗口落在一个超长标签内部，不能切
            cut, cut_stack = cand, cand_stack
            need = (cand - pos) + sum(len(n) + 3 for n, _ in cand_stack)
            if need <= budget:
                break
            # 补齐标签太长：排除当前切点，重选更靠前的切点
            content_limit = min(content_limit - (need - budget), cand - pos - 1)
            if content_limit < 1:
                break

        if cut is None:
            # 没有安全切点（超长标签）：整段发送，交给引擎告警
            pieces.append(prefix + text[pos:])
            break

        suffix = "".join(f"</{name}>" for name, _ in reversed(cut_stack))
        pieces.append(prefix + text[pos:cut] + suffix)
        prefix = "".join(raw for _, raw in cut_stack)
        pos = cut

    return pieces


def expand_html(
    texts: list[str], max_chars: int
) -> tuple[list[str], list[tuple[int, int]]]:
    """HTML 版的 expand：超长片段用 split_html 切分，标签不会被切断。"""
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    for text in texts:
        start = len(pieces)
        if len(text) > max_chars:
            pieces.extend(split_html(text, max_chars))
        else:
            pieces.append(text)
        spans.append((start, len(pieces)))
    return pieces, spans
