#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine.py —— 翻译引擎合集：BaseEngine 抽象基类 + 百度 / 火山引擎 / 谷歌 v2 实现。

(由原 engines/ 子目录的 base.py、baidu.py、volcengine.py、google_v2.py 合并而来；
三个引擎统一契约：translate(list[str]) -> list[str]，输出顺序与输入严格一一对应。)

- 百度 (服务 1): 纯文本引擎，supports_html = False
- 火山引擎 (服务 2): 纯文本引擎，supports_html = False
- 谷歌 v2 (服务 9): 原生支持 format=html，supports_html = True
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from text_splitter import expand, expand_bytes, expand_html

__all__ = ["BaseEngine", "BaiduEngine", "GoogleV2Engine", "VolcengineEngine"]

# 火山引擎与 Google v2 的单请求字符上限相同：官方 5000 字符，客户端留 100 余量
MAX_BATCH_CHARS = 4900


# ============================================================
# 通用抽象基类（原 engines/base.py）
# ============================================================

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


# ============================================================
# 百度通用翻译 API（原 engines/baidu.py）
#
# 官方文档参考：https://fanyi-api.baidu.com/product/113 / https://fanyi-api.baidu.com/doc/21
#
# 特性：
# - supports_html = False (纯文本引擎，通过 DOMTextNodeProcessor 隔离保护 HTML)
# - 签名算法: MD5(appid + q + salt + key)，小写 32 位，注意 q 签名时不经过 urlencode
# - 多行文本批处理: 利用换行符 \n 分隔多段文本合并单次请求，极大节省 QPS
# - 访问频率控制与指数退避重试: 优雅应对 54003 (QPS 受限)、54005 (长 query 频繁) 与 52001/52002 (超时与系统错误)
# - 请求限长: q 按 UTF-8 字节计算，客户端留余量用 5700 字节 / 1900 字符，避免触发 6000 字节上限
# - 语种代码映射: 全面映射 ISO-639-1 / BCP-47 至百度语种代码 (如 ja -> jp, ko -> kor, zh-TW -> cht)
# ============================================================

baidu_logger = logging.getLogger("tiny_translator.baidu")

# 百度翻译 API 接口地址
BAIDU_TRANSLATE_API = "https://fanyi-api.baidu.com/api/trans/vip/translate"

# 错误代码与人类可读提示
BAIDU_ERROR_MESSAGES: dict[str, str] = {
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
BAIDU_LANGUAGE_MAP: dict[str, str] = {
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

# 单请求文本安全上限
# 百度官方限制: q 不超过 6000 字节。中文 3 字节/字，因此同时卡字符与字节，
# 客户端各留一点余量（1900 字符 / 5700 字节），确保任何字符组合都不触顶。
MAX_QUERY_CHARS = 1900
MAX_QUERY_BYTES = 5700
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
        return BAIDU_LANGUAGE_MAP.get(normalized, normalized)

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
                    baidu_logger.warning("百度翻译网络错误 (%s)，将在 %.1fs 后重试...", e, backoff)
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
            err_msg = BAIDU_ERROR_MESSAGES.get(err_code_str, res_json.get("error_msg", "未知错误"))

            # 54003 (QPS限制)、54005 (长 query 频繁) 或 52001/52002 (超时/系统错): 触发退避重试
            if err_code_str in ("54003", "54005", "52001", "52002") and attempt < self.max_retries:
                backoff = (attempt + 1) * 1.2
                baidu_logger.warning("百度翻译触发限流或可恢复错误 [%s: %s]，将在 %.1fs 后重试 (第 %d 次)...", err_code_str, err_msg, backoff, attempt + 1)
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

        利用换行符 \n 进行高效合并批处理，严格保持输入与输出列表一一对应。
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

        # 2. 超长片段先按自然边界切开：百度 q 按 UTF-8 字节计（6000 上限），
        #    客户端用 5700 字节 / 1900 字符双保险。spans 记录每片对应的原片段范围。
        pieces, spans = expand_bytes(to_send_texts, MAX_QUERY_BYTES, MAX_QUERY_CHARS)
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

            # 将不含换行符的短片段打包为一个 batch（按 UTF-8 字节累加）
            batch_pos = [i]
            batch_bytes = len(current.encode("utf-8"))
            j = i + 1
            while j < total:
                nxt = pieces[send_order[j]]
                nxt_bytes = len(nxt.encode("utf-8"))
                if (
                    "\n" in nxt
                    or batch_bytes + nxt_bytes + 1 > MAX_QUERY_BYTES
                    or len(batch_pos) >= MAX_BATCH_LINES
                ):
                    break
                batch_pos.append(j)
                batch_bytes += nxt_bytes + 1     # +1 为拼接用的 \n
                j += 1

            # 用 \n 连接打包发送
            batch = [pieces[send_order[p]] for p in batch_pos]
            dst_lines = self._send_request_with_retry("\n".join(batch), from_lang, to_lang)

            if len(dst_lines) != len(batch):
                # 容错：返回行数与请求不符时按位置对齐，缺失的行保留原文
                baidu_logger.warning(
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


# ============================================================
# 火山引擎机器翻译 TranslateText（原 engines/volcengine.py）
#
# 官方文档参考：https://docs.volcengine.com/docs/6369/67269 (签名算法)
#              https://docs.volcengine.com/docs/4640/35107 (语言支持)
#
# 特性：
# - supports_html = False (纯文本引擎，通过 DOMTextNodeProcessor 隔离保护 HTML)
# - 签名算法: 火山 HMAC-SHA256 v4，仅依赖标准库 hmac / hashlib / urllib
# - 接口限制: TextList 单请求不超过 16 条，且总文本长度不超过 5000 字符
# - 客户端留余量: 按 4900 字符分批，避免恰好顶格触发 -400
# - 超长单条文本先按自然边界 (换行 > 句末 > 从句 > 硬切) 拆分，翻译后无损拼回
# - 访问频率控制与指数退避重试: 应对 -429 (请求过于频繁) 与 -500 (服务内部错误)
# - 语种代码映射: ISO-639-1 / BCP-47 至火山代码 (如 zh-TW -> zh-Hant, ja 保持 ja)
# ============================================================

volcengine_logger = logging.getLogger("tiny_translator.volcengine")

VOLC_HOST = "translate.volcengineapi.com"
VOLC_REGION = "cn-north-1"
VOLC_SERVICE = "translate"
VOLC_ACTION = "TranslateText"
VOLC_API_VERSION = "2020-06-01"
VOLC_CONTENT_TYPE = "application/json"
VOLC_ALGORITHM = "HMAC-SHA256"

# 接口硬限制
MAX_BATCH_ITEMS = 16

# 参与签名的 header (小写、按 ASCII 排序)
VOLC_SIGNED_HEADERS = "content-type;host;x-content-sha256;x-date"

VOLC_ERROR_MESSAGES: dict[str, str] = {
    "-400": "请求参数错误，请检查 TextList 条数 (≤16)、总长度 (≤5000) 与语言代码",
    "-415": "不支持的翻译语向，请检查源语言与目标语言组合",
    "-429": "请求过于频繁，请降低请求频率",
    "-500": "翻译服务内部错误，请重试",
}

VOLC_RETRYABLE_CODES = {"-429", "-500"}

# 常见语种代码映射到火山规范；未命中时原样透传 (保留大小写，如 zh-Hant)
VOLC_LANGUAGE_MAP: dict[str, str] = {
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


def _volc_hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _volc_sha256_hex(content: str | bytes) -> str:
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
        if normalized in VOLC_LANGUAGE_MAP:
            return VOLC_LANGUAGE_MAP[normalized]
        return lang.strip()

    @classmethod
    def canonical_query(cls) -> str:
        """正规化 QueryString：RFC3986 编码后按参数名 ASCII 排序。"""
        params = {"Action": VOLC_ACTION, "Version": VOLC_API_VERSION}
        return "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(params.items())
        )

    def build_headers(self, body: bytes, x_date: str) -> dict[str, str]:
        """计算火山 v4 签名并返回请求头。"""
        payload_hash = _volc_sha256_hex(body)
        canonical_headers = (
            f"content-type:{VOLC_CONTENT_TYPE}\n"
            f"host:{VOLC_HOST}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{x_date}\n"
        )
        canonical_request = "\n".join(
            ["POST", "/", self.canonical_query(), canonical_headers, VOLC_SIGNED_HEADERS, payload_hash]
        )
        credential_scope = f"{x_date[:8]}/{VOLC_REGION}/{VOLC_SERVICE}/request"
        string_to_sign = "\n".join(
            [VOLC_ALGORITHM, x_date, credential_scope, _volc_sha256_hex(canonical_request)]
        )

        k_date = _volc_hmac_sha256(self.secret_key.encode("utf-8"), x_date[:8])
        k_region = _volc_hmac_sha256(k_date, VOLC_REGION)
        k_service = _volc_hmac_sha256(k_region, VOLC_SERVICE)
        k_signing = _volc_hmac_sha256(k_service, "request")
        signature = _volc_hmac_sha256(k_signing, string_to_sign).hex()

        return {
            "Host": VOLC_HOST,
            "Content-Type": VOLC_CONTENT_TYPE,
            "Accept": "application/json",
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": (
                f"{VOLC_ALGORITHM} Credential={self.access_key}/{credential_scope}, "
                f"SignedHeaders={VOLC_SIGNED_HEADERS}, Signature={signature}"
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
        url = f"https://{VOLC_HOST}/?{self.canonical_query()}"

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
                volcengine_logger.debug("火山引擎返回 HTTP %s: %s", e.code, raw[:500])
            except Exception as e:
                if attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.5
                    volcengine_logger.warning("火山引擎网络错误 (%s)，将在 %.1fs 后重试...", e, backoff)
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
                msg = error.get("Message") or VOLC_ERROR_MESSAGES.get(code, "未知错误")
                hint = VOLC_ERROR_MESSAGES.get(code)
                if hint and hint not in msg:
                    msg = f"{msg} ({hint})"
                if code in VOLC_RETRYABLE_CODES and attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.2
                    volcengine_logger.warning(
                        "火山引擎触发限流或可恢复错误 [%s: %s]，将在 %.1fs 后重试 (第 %d 次)...",
                        code, msg, backoff, attempt + 1,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"火山引擎翻译 API 错误 [{code}]: {msg}")

            return [item.get("Translation", "") for item in res_json.get("TranslationList") or []]

        raise RuntimeError("火山引擎翻译请求超出最大重试次数")

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

        # 超长片段先按自然边界切开（换行 > 句末 > 从句 > 硬切），避免 -400；
        # spans 记录每片对应的原片段范围，翻译后无损拼回。
        pieces, spans = expand(to_send_texts, MAX_BATCH_CHARS)
        translated_pieces = list(pieces)  # 纯空白片段不发送，原样保留
        send_order = [k for k, p in enumerate(pieces) if p.strip()]

        i = 0
        total = len(send_order)
        while i < total:
            batch_pos = [i]
            batch_chars = len(pieces[send_order[i]])
            j = i + 1
            while j < total:
                nxt = pieces[send_order[j]]
                if (
                    len(batch_pos) >= MAX_BATCH_ITEMS
                    or batch_chars + len(nxt) > MAX_BATCH_CHARS
                ):
                    break
                batch_pos.append(j)
                batch_chars += len(nxt)
                j += 1

            batch = [pieces[send_order[p]] for p in batch_pos]
            dst = self._send_request_with_retry(batch, from_lang, to_lang)
            if len(dst) != len(batch):
                # 容错：返回条数与请求不符时按位置对齐，缺失的片段保留原文
                volcengine_logger.warning(
                    "火山引擎批处理返回条数与请求不匹配 (期望 %d, 实际 %d)，执行对齐修正",
                    len(batch),
                    len(dst),
                )
            for pos, tr in zip(batch_pos, dst):
                translated_pieces[send_order[pos]] = tr

            i = j

        translated_non_empty = [
            "".join(translated_pieces[start:end]) for start, end in spans
        ]
        results = list(texts)
        for idx, trans in zip(non_empty_indices, translated_non_empty):
            results[idx] = trans
        return results


# ============================================================
# Google Cloud Translation API v2（原 engines/google_v2.py）
#
# 特性：
# - supports_html = True (原生支持 format=html)
# - 凭据解析：优先使用 CLI 传入，其次检查用户级目录、Skill 目录、环境变量
# - 批量保护：单次请求最多 128 片段且不超过 5000 字符（官方建议上限），纯文本超长片段按自然边界切开
# - 支持 GCP 服务账号 (Service Account JSON) 和 API Key 双通道
# ============================================================

google_logger = logging.getLogger("tiny_translator.google_v2")
MAX_BATCH_SIZE = 128

# REST 通道可重试的 HTTP 状态码（限流 / 网关 / 服务端临时错误）
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class GoogleV2Engine(BaseEngine):
    name = "google"
    supports_html = True

    def __init__(
        self,
        api_key: str | None = None,
        credentials_path: str | None = None,
        max_retries: int = 4,
    ) -> None:
        self.api_key = api_key
        self.credentials_path = credentials_path
        self.max_retries = max_retries
        self._client: Any = None

        if self.credentials_path:
            self._init_service_account(self.credentials_path)

    def _init_service_account(self, cred_path: str) -> None:
        """初始化 GCP 服务账号客户端。"""
        p = Path(cred_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Google 凭据文件未找到: {p}")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(p)
        try:
            from google.cloud import translate_v2 as translate
            self._client = translate.Client()
            google_logger.info("已成功初始化 Google Translate v2 客户端 (凭据: %s)", p.name)
        except ImportError as e:
            google_logger.error("缺少 google-cloud-translate 依赖，请在对应环境安装或使用带该依赖的 python 运行")
            raise

    def translate(
        self,
        texts: list[str],
        *,
        source: str = "auto",
        target: str = "zh",
        format_: str = "html",
    ) -> list[str]:
        if not texts:
            return []

        # 纯文本按自然边界切；HTML 只在标签边界切，标签不会被切断
        if format_ == "text":
            pieces, spans = expand(texts, MAX_BATCH_CHARS)
        else:
            pieces, spans = expand_html(texts, MAX_BATCH_CHARS)

        translated = list(pieces)  # 纯空白片段不发送，原样保留
        send_order = [k for k, p in enumerate(pieces) if p.strip()]

        i = 0
        total = len(send_order)
        while i < total:
            batch_idx = [send_order[i]]
            batch_chars = len(pieces[send_order[i]])
            j = i + 1
            while j < total:
                nxt = pieces[send_order[j]]
                if len(batch_idx) >= MAX_BATCH_SIZE or batch_chars + len(nxt) > MAX_BATCH_CHARS:
                    break
                batch_idx.append(send_order[j])
                batch_chars += len(nxt)
                j += 1

            batch = [pieces[k] for k in batch_idx]
            if format_ != "text" and len(batch) == 1 and len(batch[0]) > MAX_BATCH_CHARS:
                google_logger.warning(
                    "单个 HTML 片段 %d 字符，超过单请求建议上限 %d；标签无法安全切开，将整段发送",
                    len(batch[0]),
                    MAX_BATCH_CHARS,
                )
            if self.api_key:
                batch_res = self._translate_rest(batch, source=source, target=target, format_=format_)
            elif self._client:
                batch_res = self._translate_client(batch, source=source, target=target, format_=format_)
            else:
                raise RuntimeError("Google Translate v2 未配置有效凭据 (缺少 Service Account JSON 或 API Key)")

            if len(batch_res) != len(batch):
                google_logger.warning(
                    "谷歌翻译返回条数与请求不匹配 (期望 %d, 实际 %d)，执行对齐修正",
                    len(batch),
                    len(batch_res),
                )
            for k, tr in zip(batch_idx, batch_res):
                translated[k] = tr

            i = j

        return ["".join(translated[start:end]) for start, end in spans]

    def _translate_client(
        self,
        texts: list[str],
        *,
        source: str,
        target: str,
        format_: str,
    ) -> list[str]:
        """使用 google-cloud-translate SDK 翻译。"""
        src_lang = None if source in ("auto", "", None) else source
        # SDK 参数：format_='html' / 'text'
        raw_res = self._client.translate(
            texts,
            target_language=target,
            source_language=src_lang,
            format_=format_,
        )
        if isinstance(raw_res, dict):
            raw_res = [raw_res]
        return [item["translatedText"] for item in raw_res]

    def _translate_rest(
        self,
        texts: list[str],
        *,
        source: str,
        target: str,
        format_: str,
    ) -> list[str]:
        """使用 Google REST API 直接请求 (使用 API Key)，带指数退避重试。"""
        url = f"https://translation.googleapis.com/language/translate/v2?key={self.api_key}"
        payload: dict[str, Any] = {
            "q": texts,
            "target": target,
            "format": format_,
        }
        if source and source not in ("auto", ""):
            payload["source"] = source

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return [t["translatedText"] for t in data["data"]["translations"]]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code in RETRYABLE_HTTP_CODES and attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.5
                    google_logger.warning(
                        "谷歌翻译返回 HTTP %s，将在 %.1fs 后重试 (第 %d 次)...",
                        e.code, backoff, attempt + 1,
                    )
                    time.sleep(backoff)
                    continue
                raise RuntimeError(
                    f"谷歌翻译 API 错误 [HTTP {e.code}]: {body[:300]}"
                ) from e
            except Exception as e:
                if attempt < self.max_retries:
                    backoff = (attempt + 1) * 1.5
                    google_logger.warning("谷歌翻译网络错误 (%s)，将在 %.1fs 后重试...", e, backoff)
                    time.sleep(backoff)
                    continue
                raise RuntimeError(f"谷歌翻译请求失败: {e}") from e

        raise RuntimeError("谷歌翻译请求超出最大重试次数")
