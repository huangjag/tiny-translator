#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""volcengine.py —— 火山引擎机器翻译 (TranslateText) 核心实现。

官方文档参考：https://docs.volcengine.com/docs/6369/67269 (签名算法)
             https://docs.volcengine.com/docs/4640/35107 (语言支持)

特性：
- supports_html = False (纯文本引擎，通过 DOMTextNodeProcessor 隔离保护 HTML)
- 签名算法: 火山 HMAC-SHA256 v4，仅依赖标准库 hmac / hashlib / urllib
- 接口限制: TextList 单请求不超过 16 条，且总文本长度不超过 5000 字符
- 超长单条文本按行边界 (必要时硬切) 拆分后拼接，避免触发 -400
- 访问频率控制与指数退避重试: 应对 -429 (请求过于频繁) 与 -500 (服务内部错误)
- 语种代码映射: ISO-639-1 / BCP-47 至火山代码 (如 zh-TW -> zh-Hant, ja 保持 ja)
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import BaseEngine

logger = logging.getLogger("tiny_translator.volcengine")

HOST = "translate.volcengineapi.com"
REGION = "cn-north-1"
SERVICE = "translate"
ACTION = "TranslateText"
VERSION = "2020-06-01"
CONTENT_TYPE = "application/json"
ALGORITHM = "HMAC-SHA256"

# 接口硬限制
MAX_BATCH_ITEMS = 16
MAX_BATCH_CHARS = 5000

# 参与签名的 header (小写、按 ASCII 排序)
SIGNED_HEADERS = "content-type;host;x-content-sha256;x-date"

ERROR_MESSAGES: dict[str, str] = {
    "-400": "请求参数错误，请检查 TextList 条数 (≤16)、总长度 (≤5000) 与语言代码",
    "-415": "不支持的翻译语向，请检查源语言与目标语言组合",
    "-429": "请求过于频繁，请降低请求频率",
    "-500": "翻译服务内部错误，请重试",
}

RETRYABLE_CODES = {"-429", "-500"}

# 常见语种代码映射到火山规范；未命中时原样透传 (保留大小写，如 zh-Hant)
LANGUAGE_MAP: dict[str, str] = {
    "auto": "",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "cht": "zh-Hant",
    "en": "en",
    "ja": "ja",
    "jp": "ja",
    "ko": "ko",
    "kor": "ko",
    "fr": "fr",
    "fra": "fr",
    "de": "de",
    "ru": "ru",
    "es": "es",
    "spa": "es",
}


def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(content: str | bytes) -> str:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


class VolcengineEngine(BaseEngine):
    name = "volcengine"
    supports_html = False  # 纯文本引擎，DOM 标签由本地隔离

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        qps: float = 2.0,
        max_retries: int = 4,
    ) -> None:
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.min_interval = 1.0 / max(0.1, qps)
        self.max_retries = max_retries
        self._last_request_time = 0.0

    @classmethod
    def map_language(cls, lang: str) -> str:
        """转换标准语言代码为火山专属代码；auto 返回空串表示省略 SourceLanguage。"""
        normalized = lang.lower().strip()
        if normalized in LANGUAGE_MAP:
            return LANGUAGE_MAP[normalized]
        return lang.strip()

    @classmethod
    def canonical_query(cls) -> str:
        """正规化 QueryString：RFC3986 编码后按参数名 ASCII 排序。"""
        params = {"Action": ACTION, "Version": VERSION}
        return "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(params.items())
        )

    def build_headers(self, body: bytes, x_date: str) -> dict[str, str]:
        """计算火山 v4 签名并返回请求头。"""
        payload_hash = _sha256_hex(body)
        canonical_headers = (
            f"content-type:{CONTENT_TYPE}\n"
            f"host:{HOST}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{x_date}\n"
        )
        canonical_request = "\n".join(
            ["POST", "/", self.canonical_query(), canonical_headers, SIGNED_HEADERS, payload_hash]
        )
        credential_scope = f"{x_date[:8]}/{REGION}/{SERVICE}/request"
        string_to_sign = "\n".join(
            [ALGORITHM, x_date, credential_scope, _sha256_hex(canonical_request)]
        )

        k_date = _hmac_sha256(self.secret_key.encode("utf-8"), x_date[:8])
        k_region = _hmac_sha256(k_date, REGION)
        k_service = _hmac_sha256(k_region, SERVICE)
        k_signing = _hmac_sha256(k_service, "request")
        signature = _hmac_sha256(k_signing, string_to_sign).hex()

        return {
            "Host": HOST,
            "Content-Type": CONTENT_TYPE,
            "Accept": "application/json",
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": (
                f"{ALGORITHM} Credential={self.access_key}/{credential_scope}, "
                f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
            ),
        }

    def _wait_rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _send_request_with_retry(
        self,
        texts: list[str],
        source: str,
        target: str,
    ) -> list[str]:
        """发送单次 TranslateText 请求，带指数退避重试。"""
        payload: dict[str, Any] = {"TargetLanguage": target, "TextList": texts}
        # 省略 SourceLanguage 即代表自动检测源语言
        if source:
            payload["SourceLanguage"] = source
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"https://{HOST}/?{self.canonical_query()}"

        for attempt in range(self.max_retries + 1):
            self._wait_rate_limit()
            # 每次重试重新取时间戳签名，避免长退避后 X-Date 过期
            x_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            req = urllib.request.Request(
                url, data=body, headers=self.build_headers(body, x_date), method="POST"
            )
            try:
                self._last_request_time = time.time()
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                logger.debug("火山引擎返回 HTTP %s: %s", e.code, raw[:500])
            except Exception as e:
                if attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.5
                    logger.warning("火山引擎网络错误 (%s)，将在 %.1fs 后重试...", e, backoff)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"火山引擎翻译请求失败: {e}") from e

            try:
                res_json = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"火山引擎返回非 JSON 内容: {raw[:300]}") from e

            error = (res_json.get("ResponseMetadata") or {}).get("Error")
            if error:
                code = str(error.get("Code", ""))
                msg = error.get("Message") or ERROR_MESSAGES.get(code, "未知错误")
                hint = ERROR_MESSAGES.get(code)
                if hint and hint not in msg:
                    msg = f"{msg} ({hint})"
                if code in RETRYABLE_CODES and attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.2
                    logger.warning(
                        "火山引擎触发限流或可恢复错误 [%s: %s]，将在 %.1fs 后重试 (第 %d 次)...",
                        code, msg, backoff, attempt + 1,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"火山引擎翻译 API 错误 [{code}]: {msg}")

            return [item.get("Translation", "") for item in res_json.get("TranslationList") or []]

        raise RuntimeError("火山引擎翻译请求超出最大重试次数")

    @staticmethod
    def _split_oversized(text: str) -> list[str]:
        """把超过单请求字符上限的文本按行边界拆分，必要时硬切；拼接后可还原原文。"""
        chunks: list[str] = []
        buf = ""
        for line in text.splitlines(keepends=True):
            while len(line) > MAX_BATCH_CHARS:
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(line[:MAX_BATCH_CHARS])
                line = line[MAX_BATCH_CHARS:]
            if len(buf) + len(line) > MAX_BATCH_CHARS:
                chunks.append(buf)
                buf = line
            else:
                buf += line
        if buf:
            chunks.append(buf)
        return chunks

    def translate(
        self,
        texts: list[str],
        *,
        source: str = "auto",
        target: str = "zh",
        format_: str = "text",
    ) -> list[str]:
        """批量翻译入口，严格保持输入与输出列表一一对应。"""
        if not texts:
            return []
        if not self.access_key or not self.secret_key:
            raise RuntimeError("火山引擎翻译未配置凭据！请提供 access_key 与 secret_key。")

        from_lang = self.map_language(source)
        to_lang = self.map_language(target)

        # 空片段不发送，原样保留，避免 API 忽略空串导致索引错位
        non_empty_indices = [idx for idx, t in enumerate(texts) if t.strip()]
        if not non_empty_indices:
            return list(texts)

        to_send_texts = [texts[idx] for idx in non_empty_indices]
        translated_non_empty: list[str] = []

        i = 0
        total = len(to_send_texts)
        while i < total:
            current = to_send_texts[i]
            if len(current) > MAX_BATCH_CHARS:
                pieces = self._split_oversized(current)
                translated_pieces: list[str] = []
                for piece in pieces:
                    res = self._send_request_with_retry([piece], from_lang, to_lang)
                    translated_pieces.append(res[0] if res else piece)
                translated_non_empty.append("".join(translated_pieces))
                i += 1
                continue

            batch = [current]
            batch_chars = len(current)
            j = i + 1
            while j < total:
                nxt = to_send_texts[j]
                if (
                    len(nxt) > MAX_BATCH_CHARS
                    or len(batch) >= MAX_BATCH_ITEMS
                    or batch_chars + len(nxt) > MAX_BATCH_CHARS
                ):
                    break
                batch.append(nxt)
                batch_chars += len(nxt)
                j += 1

            dst = self._send_request_with_retry(batch, from_lang, to_lang)
            if len(dst) == len(batch):
                translated_non_empty.extend(dst)
            else:
                logger.warning(
                    "火山引擎批处理返回条数与请求不匹配 (期望 %d, 实际 %d)，执行对齐修正",
                    len(batch), len(dst),
                )
                for idx in range(len(batch)):
                    translated_non_empty.append(dst[idx] if idx < len(dst) else batch[idx])
            i = j

        results = list(texts)
        for idx, trans in zip(non_empty_indices, translated_non_empty):
            results[idx] = trans
        return results