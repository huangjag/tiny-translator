#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""baidu.py —— 百度通用翻译 API 核心实现。

官方文档参考：https://fanyi-api.baidu.com/product/113 / https://fanyi-api.baidu.com/doc/21

特性：
- supports_html = False (纯文本引擎，通过 DOMTextNodeProcessor 隔离保护 HTML)
- 签名算法: MD5(appid + q + salt + key)，小写 32 位，注意 q 签名时不经过 urlencode
- 多行文本批处理: 利用换行符 \\n 分隔多段文本合并单次请求，极大节省 QPS
- 访问频率控制与指数退避重试: 优雅应对 54003 (QPS 受限) 与 52001/52002 (超时与系统错误)
- 语种代码映射: 全面映射 ISO-639-1 / BCP-47 至百度语种代码 (如 ja -> jp, ko -> kor, zh-TW -> cht)
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import urllib.parse
import urllib.request
from typing import Any

from text_splitter import expand

from .base import BaseEngine

logger = logging.getLogger("tiny_translator.baidu")

# 百度翻译 API 接口地址
BAIDU_TRANSLATE_API = "https://fanyi-api.baidu.com/api/trans/vip/translate"

# 错误代码与人类可读提示
ERROR_MESSAGES: dict[str, str] = {
    "52000": "成功",
    "52001": "请求超时，请重试",
    "52002": "系统错误，请重试",
    "52003": "未授权用户，请检查 appid 是否正确或服务是否开通",
    "54000": "必填参数为空，请检查参数",
    "54001": "签名错误，请检查 APPID 和密钥是否正确",
    "54003": "访问频率受限，超出 QPS 配额限制 (个人免费版通常为 1 QPS，高级版为 10 QPS)",
    "54004": "账户余额不足，请前往开放平台充值",
    "54005": "长 query 请求频繁，请降低长文本请求频率",
    "58000": "客户端 IP 非法，请检查百度开放平台设置的 IP 白名单",
    "58001": "译文语言方向不支持，请检查语种代码",
    "58002": "服务当前已关闭，请前往管理控制台开启",
}

# 常见语种代码映射到百度规范
LANGUAGE_MAP: dict[str, str] = {
    "auto": "auto",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-tw": "cht",
    "zh-hk": "cht",
    "zh-hant": "cht",
    "cht": "cht",
    "yue": "yue",
    "en": "en",
    "ja": "jp",
    "jp": "jp",
    "ko": "kor",
    "kor": "kor",
    "fr": "fra",
    "fra": "fra",
    "es": "spa",
    "spa": "spa",
    "ru": "ru",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "th": "th",
    "vi": "vie",
    "vie": "vie",
    "ar": "ara",
    "ara": "ara",
    "el": "el",
    "nl": "nl",
    "pl": "pl",
    "bg": "bul",
    "et": "est",
    "da": "dan",
    "fin": "fin",
    "fi": "fin",
    "cs": "cs",
    "rom": "rom",
    "ro": "rom",
    "slo": "slo",
    "sl": "slo",
    "swe": "swe",
    "sv": "swe",
    "hu": "hu",
}

# 单请求文本安全上限（字符数与段落数）
MAX_QUERY_CHARS = 2000
MAX_BATCH_LINES = 50


class BaiduEngine(BaseEngine):
    name = "baidu"
    supports_html = False  # 纯文本引擎，DOM 标签由本地隔离

    def __init__(
        self,
        app_id: str | None = None,
        secret_key: str | None = None,
        qps: float = 10.0,
        max_retries: int = 4,
    ) -> None:
        self.app_id = (app_id or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.min_interval = 1.0 / max(0.1, qps)
        self.max_retries = max_retries
        self._last_request_time = 0.0

    @classmethod
    def map_language(cls, lang: str) -> str:
        """转换标准语言代码为百度专属代码。"""
        normalized = lang.lower().strip()
        return LANGUAGE_MAP.get(normalized, normalized)

    def _generate_sign(self, q: str, salt: str) -> str:
        """生成 32 位小写 MD5 签名。

        规则：MD5(appid + q + salt + key)
        注意：q 签名时严禁预先 URL encode！
        """
        sign_str = f"{self.app_id}{q}{salt}{self.secret_key}"
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest()

    def _wait_rate_limit(self) -> None:
        """QPS 频率速率平滑等待。"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _send_request_with_retry(
        self,
        q: str,
        from_lang: str,
        to_lang: str,
    ) -> list[str]:
        """向百度 API 发送单次 POST 请求，带自动指数退避重试。"""
        salt = str(random.randint(32768, 65536))
        sign = self._generate_sign(q, salt)

        payload = {
            "q": q,
            "from": from_lang,
            "to": to_lang,
            "appid": self.app_id,
            "salt": salt,
            "sign": sign,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            BAIDU_TRANSLATE_API,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        for attempt in range(self.max_retries + 1):
            self._wait_rate_limit()
            try:
                self._last_request_time = time.time()
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_data = resp.read().decode("utf-8")
                res_json = json.loads(resp_data)
            except Exception as e:
                if attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.5
                    logger.warning("百度翻译网络错误 (%s)，将在 %.1fs 后重试...", e, backoff)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"百度翻译请求失败: {e}") from e

            # 校验业务返回码
            error_code = res_json.get("error_code")
            if not error_code or str(error_code) == "52000":
                # 成功返回
                trans_result = res_json.get("trans_result", [])
                return [item["dst"] for item in trans_result]

            err_code_str = str(error_code)
            err_msg = ERROR_MESSAGES.get(err_code_str, res_json.get("error_msg", "未知错误"))

            # 54003 (QPS限制) 或 52001/52002 (超时/系统错): 触发退避重试
            if err_code_str in ("54003", "52001", "52002") and attempt < self.max_retries:
                backoff = (attempt + 1) * 1.2
                logger.warning("百度翻译触发限流或可恢复错误 [%s: %s]，将在 %.1fs 后重试 (第 %d 次)...", err_code_str, err_msg, backoff, attempt + 1)
                time.sleep(backoff)
                continue

            # 不可恢复错误直接抛出
            raise RuntimeError(f"百度翻译 API 错误 [{err_code_str}]: {err_msg}")

        raise RuntimeError("百度翻译请求超出最大重试次数")

    def translate(
        self,
        texts: list[str],
        *,
        source: str = "auto",
        target: str = "zh",
        format_: str = "text",
    ) -> list[str]:
        """批量翻译入口。

        利用换行符 \\n 进行高效合并批处理，严格保持输入与输出列表一一对应。
        """
        if not texts:
            return []
        if not self.app_id or not self.secret_key:
            raise RuntimeError("百度通用翻译引擎未配置凭据！请提供 app_id 与 secret_key。")

        from_lang = self.map_language(source)
        to_lang = self.map_language(target)

        # 1. 过滤并记录非空行索引，空行原样保留，彻底避免百度 API 忽略空行导致行数偏移
        non_empty_indices = [idx for idx, t in enumerate(texts) if t.strip()]
        if not non_empty_indices:
            return list(texts)

        to_send_texts = [texts[idx] for idx in non_empty_indices]

        # 2. 超长片段先按自然边界切开：百度 q 上限约 6000 字节，中文 2000 字即顶格，
        #    整段发送会触发 54005。spans 记录每片对应的原片段范围，翻译后拼回。
        pieces, spans = expand(to_send_texts, MAX_QUERY_CHARS)
        translated_pieces = list(pieces)  # 纯空白片段不发送，原样保留
        send_order = [k for k, p in enumerate(pieces) if p.strip()]

        i = 0
        total = len(send_order)

        while i < total:
            current = pieces[send_order[i]]
            # 片段自身含换行时必须单条发送：批量靠 \n 拼接，含换行会打乱行对应关系
            if "\n" in current:
                dst_lines = self._send_request_with_retry(current, from_lang, to_lang)
                # 百度多行返回以 \n 拼回原格式
                translated_pieces[send_order[i]] = "\n".join(dst_lines) if dst_lines else current
                i += 1
                continue

            # 将不含换行符的短片段打包为一个 batch
            batch_pos = [i]
            batch_chars = len(current)
            j = i + 1
            while j < total:
                nxt = pieces[send_order[j]]
                if (
                    "\n" in nxt
                    or batch_chars + len(nxt) + 1 > MAX_QUERY_CHARS
                    or len(batch_pos) >= MAX_BATCH_LINES
                ):
                    break
                batch_pos.append(j)
                batch_chars += len(nxt) + 1
                j += 1

            # 用 \n 连接打包发送
            batch = [pieces[send_order[p]] for p in batch_pos]
            dst_lines = self._send_request_with_retry("\n".join(batch), from_lang, to_lang)

            if len(dst_lines) != len(batch):
                # 容错：返回行数与请求不符时按位置对齐，缺失的行保留原文
                logger.warning(
                    "百度翻译批处理返回行数与请求不匹配 (期望 %d, 实际 %d)，执行对齐修正",
                    len(batch),
                    len(dst_lines),
                )
            for pos, tr in zip(batch_pos, dst_lines):
                translated_pieces[send_order[pos]] = tr

            i = j

        # 3. 按原片段范围拼回译文，再精准回填到原位置
        translated_non_empty = [
            "".join(translated_pieces[start:end]) for start, end in spans
        ]
        results = list(texts)
        for idx, trans in zip(non_empty_indices, translated_non_empty):
            results[idx] = trans

        return results
