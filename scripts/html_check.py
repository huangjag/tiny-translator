#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""html_check.py —— 翻译前的 HTML 结构体检与保守修复。

纯标准库，不依赖 bs4。默认只读；``--fix`` 生成 ``<name>.fixed.html``，
只有显式 ``--in-place`` 才覆盖原文件。

检查项：
1. 标签闭合：未闭合、游离闭合、错嵌套（带行号）
2. 截断的 ``</``（后面没有标签名）
3. 未闭合的内联标签（a / b / i / span / font 等）
4. charset 声明缺失或重复
5. 相对 href/src 断链（``--links``）

退出码：0 干净，1 发现问题，2 用法 / IO 错误。
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "big", "cite", "code", "del", "dfn",
    "em", "font", "i", "ins", "kbd", "label", "mark", "q", "s", "samp",
    "small", "span", "strike", "strong", "sub", "sup", "time", "tt", "u", "var",
})

# 一次扫描覆盖注释 / CDATA / DOCTYPE / PI / 普通标签；group 2 为空表示非普通标签
TOKEN_RE = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[.*?\]\]>"
    r"|<![^>]*>"
    r"|<\?.*?\?>"
    r"|<(/?)\s*([a-zA-Z][\w:-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.DOTALL,
)
TRUNCATED_CLOSE_RE = re.compile(r"</(?![a-zA-Z])")
CHARSET_RE = re.compile(r"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([\w-]+)", re.IGNORECASE)
ATTR_RE = re.compile(r"(href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))", re.IGNORECASE)
BGCOLOR_RE = re.compile(r"(?i)(bgcolor\s*=\s*[\"'])(?!#)([0-9a-f]{6})([\"'])")

SKIP_LINK_PREFIXES = (
    "#", "http://", "https://", "/", "mailto:", "javascript:", "data:", "tel:",
)


@dataclass
class Issue:
    path: str
    line: int
    code: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "code": self.code, "message": self.message}

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.code}] {self.message}"


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _tag_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _inside_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    starts = [s for s, _ in spans]
    i = bisect.bisect_right(starts, pos) - 1
    return i >= 0 and spans[i][0] <= pos < spans[i][1]


def _balance_issues(text: str, path: str) -> list[Issue]:
    issues: list[Issue] = []
    stack: list[tuple[str, int]] = []
    for m in TOKEN_RE.finditer(text):
        if m.group(2) is None:
            continue
        closing = bool(m.group(1))
        name = m.group(2).lower()
        pos = m.start()
        if closing:
            idx = next((i for i in range(len(stack) - 1, -1, -1) if stack[i][0] == name), None)
            if idx is None:
                issues.append(Issue(path, _line_of(text, pos), "stray-close", f"游离的闭合标签 </{name}>"))
                continue
            for open_name, open_pos in stack[idx + 1:]:
                code = "unclosed-inline" if open_name in INLINE_TAGS else "unclosed"
                issues.append(
                    Issue(path, _line_of(text, open_pos), code, f"<{open_name}> 未闭合（遇到 </{name}>）")
                )
            del stack[idx:]
        else:
            if name in VOID_TAGS or m.group(0).rstrip().endswith("/>"):
                continue
            stack.append((name, pos))

    for open_name, open_pos in stack:
        code = "unclosed-inline" if open_name in INLINE_TAGS else "unclosed"
        issues.append(Issue(path, _line_of(text, open_pos), code, f"<{open_name}> 到文件结尾仍未闭合"))
    return issues


def _truncated_issues(text: str, path: str, spans: list[tuple[int, int]]) -> list[Issue]:
    issues: list[Issue] = []
    for m in TRUNCATED_CLOSE_RE.finditer(text):
        if _inside_spans(m.start(), spans):
            continue
        issues.append(Issue(path, _line_of(text, m.start()), "truncated-close", '截断的 "</"（后面没有标签名）'))
    return issues


def _charset_issues(text: str, path: str) -> list[Issue]:
    found = list(CHARSET_RE.finditer(text))
    if not found:
        return [Issue(path, 1, "no-charset", "缺少 <meta charset=...> 声明")]
    if len(found) > 1:
        return [
            Issue(path, _line_of(text, m.start()), "duplicate-charset", f"重复的 charset 声明: {m.group(1)}")
            for m in found[1:]
        ]
    return []


def check_html(text: str, *, path: str = "<input>", check_charset: bool = True) -> list[Issue]:
    """检查 HTML 结构，返回问题列表（不读取外部资源）。"""
    spans = _tag_spans(text)
    issues = _truncated_issues(text, path, spans)
    issues += _balance_issues(text, path)
    if check_charset:
        issues += _charset_issues(text, path)
    return issues


def check_links(text: str, base_dir: Path, *, path: str = "<input>") -> list[Issue]:
    """检查相对 href/src 是否指向存在的文件。"""
    issues: list[Issue] = []
    for m in TOKEN_RE.finditer(text):
        if m.group(2) is None or m.group(1):
            continue
        attrs = m.group(3) or ""
        for a in ATTR_RE.finditer(attrs):
            attr = a.group(1).lower()
            value = a.group(2)
            if value is None:
                value = a.group(3)
            if value is None:
                value = a.group(4) or ""
            value = value.strip()
            if not value or value.lower().startswith(SKIP_LINK_PREFIXES) or "{" in value:
                continue
            target_ref = urllib.parse.unquote(value.split("#", 1)[0].split("?", 1)[0])
            if not target_ref:
                continue
            if not (base_dir / target_ref).exists():
                issues.append(
                    Issue(path, _line_of(text, m.start()), "broken-link", f'{attr}="{value}" 目标不存在')
                )
    return issues


def _remove_stray_closes(text: str) -> tuple[str, int]:
    """删除没有匹配开标签的闭合标签，其余内容原样保留。"""
    out: list[str] = []
    last = 0
    stack: list[str] = []
    removed = 0
    for m in TOKEN_RE.finditer(text):
        if m.group(2) is None:
            continue
        closing = bool(m.group(1))
        name = m.group(2).lower()
        if closing:
            idx = next((i for i in range(len(stack) - 1, -1, -1) if stack[i] == name), None)
            if idx is None:
                out.append(text[last:m.start()])
                last = m.end()
                removed += 1
            else:
                del stack[idx:]
        elif name not in VOID_TAGS and not m.group(0).rstrip().endswith("/>"):
            stack.append(name)
    out.append(text[last:])
    return "".join(out), removed


def fix_html(text: str) -> tuple[str, list[str]]:
    """只做可证明安全的机械修复，返回 ``(修复后文本, 修复说明)``。

    修复范围：截断的 ``</``、游离闭合标签、``BGCOLOR="ffffff"`` 缺 ``#``。
    不重排 DOM、不补未闭合标签——那些需要人工判断。
    """
    notes: list[str] = []

    text, n = TRUNCATED_CLOSE_RE.subn("", text)
    if n:
        notes.append(f"删除 {n} 处截断的 </")

    text, n = _remove_stray_closes(text)
    if n:
        notes.append(f"删除 {n} 个游离闭合标签")

    text, n = BGCOLOR_RE.subn(r"\1#\2\3", text)
    if n:
        notes.append(f"为 {n} 处 BGCOLOR 补上 #")

    return text, notes


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "cp932", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HTML 结构体检：标签平衡、截断 </、charset、相对链接；可选保守修复。",
    )
    parser.add_argument("paths", nargs="+", help="待检查的 HTML 文件")
    parser.add_argument("--links", action="store_true", help="检查相对 href/src 是否存在")
    parser.add_argument("--no-charset", action="store_true", help="跳过 charset 检查")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出问题列表")
    parser.add_argument("--fix", action="store_true", help="执行保守修复，默认写 <name>.fixed.html")
    parser.add_argument("--in-place", action="store_true", help="配合 --fix 覆盖原文件")
    args = parser.parse_args(argv)

    if args.in_place and not args.fix:
        sys.stderr.write("错误: --in-place 必须与 --fix 一起使用\n")
        return 2

    all_issues: list[Issue] = []
    for raw_path in args.paths:
        p = Path(raw_path)
        if not p.is_file():
            sys.stderr.write(f"错误: 文件不存在: {raw_path}\n")
            return 2

        text, encoding = _read_text(p)
        issues = check_html(text, path=str(p), check_charset=not args.no_charset)
        if args.links:
            issues += check_links(text, p.parent, path=str(p))

        if args.fix:
            fixed, notes = fix_html(text)
            if fixed != text:
                target = p if args.in_place else p.with_name(f"{p.stem}.fixed{p.suffix}")
                target.write_bytes(fixed.encode(encoding))
                for note in notes:
                    sys.stderr.write(f"[FIX] {p} -> {target}: {note}\n")
                issues = check_html(fixed, path=str(target), check_charset=not args.no_charset)
                if args.links:
                    issues += check_links(fixed, p.parent, path=str(target))
            else:
                sys.stderr.write(f"[FIX] {p}: 无需机械修复\n")

        all_issues.extend(issues)

    all_issues.sort(key=lambda i: (i.path, i.line, i.code))
    if args.json:
        sys.stdout.write(json.dumps([i.as_dict() for i in all_issues], ensure_ascii=False, indent=2) + "\n")
    else:
        for issue in all_issues:
            sys.stdout.write(f"{issue}\n")

    sys.stderr.write(f"检查 {len(args.paths)} 个文件，发现 {len(all_issues)} 个问题\n")
    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(main())
