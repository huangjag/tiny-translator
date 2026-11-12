#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translate.py —— tiny-translator 核心 CLI 入口。

功能：
1. 输入：
   - 字符串: --text "..."
   - 文件: -f / --file "path/to/file"
   - 管道流: stdin (echo ... | python translate.py)
2. 语言：
   -s / --from: 源语言 (默认 auto)
   -t / --to: 目标语言 (默认 zh)
3. 输出与阈值规则：
   - 阈值: 5000 字符 (可由 --threshold 自定义)
   - 短文本 (< 5000): 默认在 stdout 打印，方便 Agent 零等待秒读
   - 长文本 (>= 5000): 默认自动重定向至系统临时文件 translate_latest.txt
   - 显式覆盖控制:
     - --temp: 强制输出至临时文件
     - --stdout: 强制终端输出 (忽略阈值)
     - -o / --output <path>: 显式指定输出路径
     - --no-overwrite: 默认覆写，传入此参数时若文件存在则生成时间戳唯一副本
4. 标签保护：
   - 原生支持 HTML 引擎 (Google): 脆弱原子掩码 + 骑跨预检 + br 捕获切分 + 本地还原
   - 纯文本引擎 (Baidu 等): DOM 树纯文本节点隔离提取，100% 不破坏标签
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

__version__ = "1.1"

# 编码设置，保证 Windows 终端不报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
REEXEC_FLAG = "TINY_TRANSLATOR_REEXECED"


def _reexec_under_venv() -> None:
    """当前解释器缺少所需依赖时，自动寻找带依赖的 .venv 重启本脚本。"""
    if os.environ.get(REEXEC_FLAG):
        return
    candidates = [
        SKILL_ROOT / ".venv",
    ]
    env_venv = os.environ.get("TRANSLATOR_VENV")
    if env_venv:
        candidates.insert(0, Path(env_venv))

    for base in candidates:
        rel = Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
        interpreter = base / rel
        if interpreter.is_file():
            env = {**os.environ, REEXEC_FLAG: "1"}
            argv = [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]]
            sys.exit(subprocess.call(argv, env=env))


from auth_manager import (  # noqa: E402
    ensure_auth_template_file,
    format_auth_prompt,
    resolve_baidu_credentials,
    resolve_google_credentials,
    resolve_volcengine_credentials,
)
from engine import BaseEngine, BaiduEngine, GoogleV2Engine, VolcengineEngine  # noqa: E402
from tag_processor import DOMTextNodeProcessor, GoogleHTMLProcessor  # noqa: E402

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
        decoded = raw.decode("utf-8", errors="replace")
        bad = decoded.count("\ufffd")
        if bad:
            logger.warning(
                "输入文件无法用 utf-8 / cp932 / gb18030 完整解码，已用替换字符兜底 (%d 处)；"
                "若译文出现乱码，请确认源文件编码",
                bad,
            )
        return decoded

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
        temp_dir = Path(tempfile.gettempdir())
        target = temp_dir / "translate_latest.txt"

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


def decide_output(
    stdout_flag: bool,
    output_arg: str | None,
    temp_flag: bool,
    char_count: int,
    threshold: int,
) -> tuple[bool, bool]:
    """决定输出方式，返回 ``(是否写文件, 是否自动重定向到临时文件)``。

    优先级：``--stdout`` > ``-o`` / ``--temp`` > 长度阈值。
    """
    if stdout_flag:
        return False, False
    if output_arg or temp_flag:
        return True, False
    if char_count >= threshold:
        return True, True
    return False, False


def write_output_text(path: Path, text: str) -> None:
    """按 UTF-8 写入译文。

    必须走 ``write_bytes``：``Path.write_text`` 在 Windows 上默认
    ``newline=None``，会把已有 ``\\r\\n`` 再转成 ``\\r\\r\\n``。
    """
    path.write_bytes(text.encode("utf-8"))


def _build_google_engine(auth_info: dict) -> GoogleV2Engine:
    """API Key 走纯 REST (仅标准库)，只有服务账号凭据才需要 google-cloud-translate SDK。"""
    if auth_info.get("credentials_path"):
        try:
            import google.cloud.translate_v2  # noqa: F401
        except ImportError:
            _reexec_under_venv()
            raise RuntimeError(
                "缺少 google-cloud-translate 依赖 (服务账号凭据需要该 SDK)。"
                "请执行 pip install google-cloud-translate，或改用 GOOGLE_API_KEY 走纯 REST 接口。"
            ) from None
    return GoogleV2Engine(
        api_key=auth_info.get("api_key"),
        credentials_path=auth_info.get("credentials_path"),
    )


def select_and_init_engine(
    engine_name: str | None,
    credentials_path: str | None = None,
    qps: float | None = None,
) -> BaseEngine:
    """按编号与规则解析并初始化引擎。

    规则：
    1. 若未显式指定引擎 (默认)：
       按服务编号 1 (百度) -> 2 (火山引擎) -> 9 (谷歌) 顺序探测，凭据为空则跳过。
    2. 若显式指定编号 (如 1、2 或 9) 或名称 (如 baidu、volc 或 google)：
       严格使用该服务，若其凭据为空则输出配置引导。
    """
    if engine_name is not None:
        canonical = ENGINE_MAP.get(str(engine_name).lower().strip())
        if not canonical:
            raise ValueError(f"未知的翻译引擎编号或名称 '{engine_name}'。支持: 1 (百度), 2 (火山引擎), 9 (谷歌)")

        if canonical == "baidu":
            auth_info = resolve_baidu_credentials(credentials_path)
            if not auth_info.get("app_id") or not auth_info.get("secret_key"):
                auth_file = ensure_auth_template_file()
                sys.stderr.write(format_auth_prompt("百度翻译 (服务 1)", auth_file))
                sys.exit(3)
            return BaiduEngine(
                app_id=auth_info.get("app_id"),
                secret_key=auth_info.get("secret_key"),
                **({"qps": qps} if qps else {}),
            )
        elif canonical == "volcengine":
            auth_info = resolve_volcengine_credentials(credentials_path)
            if not auth_info.get("access_key") or not auth_info.get("secret_key"):
                auth_file = ensure_auth_template_file()
                sys.stderr.write(format_auth_prompt("火山引擎机器翻译 (服务 2)", auth_file))
                sys.exit(3)
            return VolcengineEngine(
                access_key=auth_info.get("access_key"), secret_key=auth_info.get("secret_key")
            )
        elif canonical == "google":
            auth_info = resolve_google_credentials(credentials_path)
            if not auth_info.get("api_key") and not auth_info.get("credentials_path"):
                auth_file = ensure_auth_template_file()
                sys.stderr.write(format_auth_prompt("谷歌翻译 (服务 9)", auth_file))
                sys.exit(3)
            return _build_google_engine(auth_info)

    # 默认模式：按 1 (百度) -> 2 (火山引擎) -> 9 (谷歌) 顺序探测
    baidu_auth = resolve_baidu_credentials(credentials_path)
    if baidu_auth.get("app_id") and baidu_auth.get("secret_key"):
        logger.info("默认采用 [服务 1] 百度通用翻译")
        return BaiduEngine(
            app_id=baidu_auth.get("app_id"),
            secret_key=baidu_auth.get("secret_key"),
            **({"qps": qps} if qps else {}),
        )

    logger.debug("[服务 1] 百度通用翻译未检测到有效 key，跳过此服务，尝试 [服务 2] 火山引擎...")

    volc_auth = resolve_volcengine_credentials(credentials_path)
    if volc_auth.get("access_key") and volc_auth.get("secret_key"):
        logger.info("采用 [服务 2] 火山引擎机器翻译")
        return VolcengineEngine(
            access_key=volc_auth.get("access_key"), secret_key=volc_auth.get("secret_key")
        )

    logger.debug("[服务 2] 火山引擎未检测到有效 AK/SK，跳过此服务，尝试 [服务 9] 谷歌翻译...")

    google_auth = resolve_google_credentials(credentials_path)
    if google_auth.get("api_key") or google_auth.get("credentials_path"):
        logger.info("采用 [服务 9] 谷歌翻译")
        return _build_google_engine(google_auth)

    # 三者均未配置
    auth_file = ensure_auth_template_file()
    sys.stderr.write(format_auth_prompt("默认服务: 1 (百度)、2 (火山引擎) 或 9 (谷歌)", auth_file))
    sys.exit(3)


def translate_content(
    text: str,
    engine_name: str | None,
    source: str,
    target: str,
    format_mode: str,
    credentials_path: str | None = None,
    qps: float | None = None,
) -> str:
    """执行翻译核心流程。"""
    if not text.strip():
        return text

    engine = select_and_init_engine(engine_name, credentials_path, qps)

    # 判断是否按 HTML 模式处理
    is_html = (format_mode == "html") or (format_mode == "auto" and "<" in text and ">" in text)

    if not is_html:
        # 纯文本模式：直接按段落分批翻译
        lines = text.splitlines(keepends=True)
        # 剥离换行符后发送
        clean_lines = [line.rstrip("\r\n") for line in lines]
        translated_clean = engine.translate(
            clean_lines,
            source=source,
            target=target,
            format_="text",
        )
        # 还原换行符
        reconstructed: list[str] = []
        for orig, trans in zip(lines, translated_clean):
            newline = orig[len(orig.rstrip("\r\n")) :]
            reconstructed.append(trans + newline)
        return "".join(reconstructed)

    # HTML 模式
    if engine.supports_html:
        # 1. 脆弱原子保护
        masked_text, atom_map = GoogleHTMLProcessor.protect_atoms(text)

        # 2. 骑跨检测：如果成对标签跨越 br，则整段发送避免孤儿标签
        if GoogleHTMLProcessor.is_straddling_br(masked_text):
            trans_res = engine.translate(
                [masked_text],
                source=source,
                target=target,
                format_="html",
            )
            raw_translated = trans_res[0] if trans_res else ""
        else:
            # 3. 捕获切分
            pairs = GoogleHTMLProcessor.split_by_br(masked_text)
            segments = [p[0] for p in pairs]
            delims = [p[1] for p in pairs]

            # 过滤出非空片段并发送
            non_empty_indices = [idx for idx, seg in enumerate(segments) if seg.strip()]
            to_send = [segments[idx] for idx in non_empty_indices]

            if to_send:
                translated_batch = engine.translate(
                    to_send,
                    source=source,
                    target=target,
                    format_="html",
                )
            else:
                translated_batch = []

            # 填回完整列表
            translated_segments = list(segments)
            for idx, trans_seg in zip(non_empty_indices, translated_batch):
                translated_segments[idx] = trans_seg

            # 原样拼回 br
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
    else:
        # 不支持 HTML 的引擎，走 DOM 纯文本隔离管道
        return DOMTextNodeProcessor.process_and_translate(
            text,
            engine.translate,
            source=source,
            target=target,
        )


def main() -> None:
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
        help="翻译引擎编号或名称: 1 (百度), 2 (火山引擎), 9 (谷歌)；不指定时按 1 → 2 → 9 自动探测",
    )
    engine_group.add_argument("--credentials", type=str, default=None, help="服务账号凭据路径或 API 密钥文件")
    engine_group.add_argument(
        "--qps", type=float, default=None,
        help="引擎请求频率上限（每秒请求数）。默认由引擎决定（百度 10 QPS）；配额较低时可调小",
    )
    engine_group.add_argument("--format", type=str, choices=["auto", "html", "text"], default="auto", help="文本解析格式 (默认: auto)")
    engine_group.add_argument("-v", "--verbose", action="store_true", help="打印详细调试日志")
    parser.add_argument("--version", action="version", version=f"tiny-translator {__version__}")

    args = parser.parse_args()
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

    # 判断输出目标：--stdout > -o / --temp > 长度阈值
    need_file_output, auto_redirect_temp = decide_output(
        args.stdout, args.output, args.temp, input_char_count, args.threshold
    )

    # 执行翻译
    try:
        translated = translate_content(
            content,
            engine_name=args.engine,
            source=args.source,
            target=args.target,
            format_mode=args.format,
            credentials_path=args.credentials,
            qps=args.qps,
        )
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
            write_output_text(out_path, translated)
            if auto_redirect_temp:
                sys.stdout.write(f"[OUTPUT_FILE] {out_path.resolve()}\n")
                sys.stdout.write(f"(内容长度为 {input_char_count} 字符，已超过安全阈值 {args.threshold} 字，自动保存至临时文件)\n")
            else:
                sys.stdout.write(f"[OUTPUT_FILE] {out_path.resolve()}\n")
    else:
        sys.stdout.write(translated)


if __name__ == "__main__":
    main()
