#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""google_v2.py —— Google Cloud Translation API v2 实现。

特性：
- supports_html = True (原生支持 format=html)
- 凭据解析：优先使用 CLI 传入，其次检查用户级目录、Skill 目录、环境变量
- 批量保护：单次请求最多 128 片段，避免超出 API 配额限制
- 支持 GCP 服务账号 (Service Account JSON) 和 API Key 双通道
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from .base import BaseEngine

logger = logging.getLogger("tiny_translator.google_v2")
MAX_BATCH_SIZE = 128


class GoogleV2Engine(BaseEngine):
    name = "google"
    supports_html = True

    def __init__(
        self,
        api_key: str | None = None,
        credentials_path: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.credentials_path = credentials_path
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
            logger.info("已成功初始化 Google Translate v2 客户端 (凭据: %s)", p.name)
        except ImportError as e:
            logger.error("缺少 google-cloud-translate 依赖，请在对应环境安装或使用带该依赖的 python 运行")
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

        results: list[str] = []
        # 按 MAX_BATCH_SIZE (128) 分批发送
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            if self.api_key:
                batch_res = self._translate_rest(batch, source=source, target=target, format_=format_)
            elif self._client:
                batch_res = self._translate_client(batch, source=source, target=target, format_=format_)
            else:
                raise RuntimeError("Google Translate v2 未配置有效凭据 (缺少 Service Account JSON 或 API Key)")
            results.extend(batch_res)

        return results

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
        """使用 Google REST API 直接请求 (使用 API Key)。"""
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [t["translatedText"] for t in data["data"]["translations"]]
