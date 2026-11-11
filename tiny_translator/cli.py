#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli.py —— tiny-translator 命令行入口。

库能力见 core.py；本模块只处理进程级事务：参数解析、stdio 编码、输出路由、
退出码，以及缺依赖时的 venv 重执行。

输入: --text / -f <file> / stdin
输出: 长度 < --threshold 写 stdout；>= 阈值写系统临时文件并打印 [OUTPUT_FILE] <path>
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .auth import format_auth_prompt
from .core import translate
from .errors import AuthMissingError, MissingDependencyError

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REEXEC_FLAG = "TINY_TRANSLATOR_REEXECED"


def _configure_stdio() -> None:
    """保证 Windows 终端不因编码报错。只在 CLI 运行时执行，导入库不受影响。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _reexec_under_venv() -> None:
    """当前解释器缺少所需依赖时，自动寻找带依赖的 .venv 重启本入口。"""
    if os.environ.get(REEXEC_FLAG):
        return
    candidates = [PACKAGE_ROOT / ".venv"]
    env_venv = os.environ.get("TRANSLATOR_VENV")
    if env_venv:
        candidates.insert(0, Path(env_venv))

    for base in candidates:
        rel = Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
        interpreter = base / rel
        if not interpreter.is_file():
            continue
        env = {**os.environ, REEXEC_FLAG: "1"}
        entry = Path(sys.argv[0]).resolve()
        if entry.is_file() and entry.suffix == ".py":
            argv = [str(interpreter), str(entry), *sys.argv[1:]]
        else:
            argv = [str(interpreter), "-m", "tiny_translator", *sys.argv[1:]]
        sys.exit(subprocess.call(argv, env=env))


def read_input_text(file_path: str | None, direct_text: str | None) -> str:
    """按优先级读取输入内容：--text > --file > stdin。"""
    if direct_text is not None:
        return direct_text

    if file_path:
        p = Path(file_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"输入文件不存在: {file_path}")
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "cp932", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # 从管道流 (stdin) 读取
    if not sys.stdin.isatty():
        return sys.stdin.read()

    return ""


def determine_output_path(
    output_arg: str | None,
    to_temp: bool,
    no_overwrite: bool,
) -> Path | None:
    """计算最终输出文件的路径，处理覆盖与非覆盖逻辑。"""
    target: Path | None = None

    if output_arg:
        target = Path(output_arg).resolve()
    elif to_temp:
        target = Path(tempfile.gettempdir()) / "translate_latest.txt"

    if not target:
        return None

    # 若指定了 --no-overwrite 且文件已存在，则生成带时间戳与随机后缀的唯一文件名
    if no_overwrite and target.exists():
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        rand_suffix = uuid.uuid4().hex[:6]
        new_name = f"{target.stem}_{timestamp}_{rand_suffix}{target.suffix}"
        target = target.parent / new_name

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="tiny-translator —— agent 友好的通用多引擎翻译工具 (具备 HTML 标签守恒保护)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # 输入参数
    input_group = parser.add_argument_group("输入来源")
    input_group.add_argument("--text", type=str, default=None, help="待翻译的直接字符串")
    input_group.add_argument("-f", "--file", type=str, default=None, help="待翻译的输入文件路径")

    # 语言参数
    lang_group = parser.add_argument_group("语言设置")
    lang_group.add_argument("-s", "--from", dest="source", type=str, default="auto", help="源语言代码 (默认: auto)")
    lang_group.add_argument("-t", "--to", dest="target", type=str, default="zh", help="目标语言代码 (默认: zh)")

    # 输出与覆盖控制
    out_group = parser.add_argument_group("输出控制与覆写选项")
    out_group.add_argument("-o", "--output", type=str, default=None, help="输出文件路径 (默认覆写)")
    out_group.add_argument("--temp", action="store_true", help="强制输出到系统临时文件 translate_latest.txt")
    out_group.add_argument("--stdout", action="store_true", help="强制在终端直接输出 (忽略 5000 字符限制)")
    out_group.add_argument("--threshold", type=int, default=5000, help="自动判定为长文本并重定向至文件的字数阈值 (默认: 5000)")
    out_group.add_argument("--no-overwrite", action="store_true", help="禁止覆盖，若目标文件存在则生成时间戳唯一副本")

    # 引擎与高级设置
    engine_group = parser.add_argument_group("引擎与凭据")
    engine_group.add_argument(
        "-e", "--engine",
        type=str,
        default=None,
        help="翻译引擎编号或名称: 1 (百度, 默认), 2 (火山引擎), 9 (谷歌)。可直接传编号 1、2 或 9",
    )
    engine_group.add_argument("--credentials", type=str, default=None, help="服务账号凭据路径或 API 密钥文件")
    engine_group.add_argument("--format", type=str, choices=["auto", "html", "text"], default="auto", help="文本解析格式 (默认: auto)")
    engine_group.add_argument("-v", "--verbose", action="store_true", help="打印详细调试日志")
    return parser


def main() -> None:
    _configure_stdio()

    args = build_parser().parse_args()

    # 配置日志
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="[%(levelname)s] %(message)s", stream=sys.stderr)

    # 读取输入
    try:
        content = read_input_text(args.file, args.text)
    except Exception as e:
        sys.stderr.write(f"错误: 无法读取输入内容 - {e}\n")
        sys.exit(1)

    if not content:
        sys.stderr.write("错误: 输入内容为空。请提供 --text、--file 或管道输入。\n")
        sys.exit(1)

    input_char_count = len(content)

    # 输出目标规则:
    # 1. --stdout: 必须终端输出
    # 2. -o / --output 或 --temp: 输出到对应文件
    # 3. 都没指定: 长度 >= threshold 自动重定向到临时文件，否则终端输出
    auto_redirect_temp = False
    need_file_output = bool(args.output or args.temp)

    if not args.stdout and not need_file_output and input_char_count >= args.threshold:
        auto_redirect_temp = True
        need_file_output = True

    # 执行翻译
    try:
        translated = translate(
            content,
            engine_name=args.engine,
            source=args.source,
            target=args.target,
            format=args.format,
            credentials_path=args.credentials,
        )
    except AuthMissingError as e:
        auth_file = e.auth_file
        if auth_file is not None:
            sys.stderr.write(format_auth_prompt(e.engine_label, auth_file))
        else:
            sys.stderr.write(f"[AUTH_REQUIRED] 未检测到 {e.engine_label} 的有效凭据\n")
        sys.exit(3)
    except MissingDependencyError as e:
        _reexec_under_venv()
        sys.stderr.write(f"{e}\n")
        sys.exit(2)
    except Exception as e:
        sys.stderr.write(f"翻译执行失败: {e}\n")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(2)

    # 处理输出
    if need_file_output:
        to_temp = bool(args.temp or auto_redirect_temp)
        out_path = determine_output_path(args.output, to_temp, args.no_overwrite)
        if out_path:
            out_path.write_text(translated, encoding="utf-8")
            sys.stdout.write(f"[OUTPUT_FILE] {out_path.resolve()}\n")
            if auto_redirect_temp:
                sys.stdout.write(
                    f"(内容长度为 {input_char_count} 字符，已超过安全阈值 {args.threshold} 字，自动保存至临时文件)\n"
                )
    else:
        sys.stdout.write(translated)
