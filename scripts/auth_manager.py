#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auth_manager.py —— tiny-translator 统一凭据解析模块。

按以下优先级查找凭据：
1. 命令行参数: --credentials
2. 用户级目录: ~/.config/tiny-translator/
3. Skill 本地目录: <skill_root>/auth/
4. 环境变量:
   - GOOGLE_APPLICATION_CREDENTIALS
   - GOOGLE_API_KEY
   - BAIDU_APP_ID / BAIDU_SECRET_KEY
   - VOLC_ACCESS_KEY / VOLC_SECRET_KEY
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("tiny_translator.auth")

SKILL_ROOT = Path(__file__).resolve().parent.parent


def get_user_config_dirs() -> list[Path]:
    """获取用户级配置目录列表（跨平台兼容）。"""
    home = Path.home()
    return [
        home / ".config" / "tiny-translator",
    ]


def get_skill_auth_dirs() -> list[Path]:
    """获取 Skill 内部的认证目录列表。"""
    return [
        SKILL_ROOT / "auth",
    ]


# auth.txt 可能由用户在任意编辑器保存（UTF-8 / GBK / Shift-JIS），按序尝试解码
TEXT_ENCODINGS = ("utf-8-sig", "gb18030", "cp932")

# 模板来源：本地可编辑的 auth/auth.txt 优先，其次随 skill 分发的 scripts/auth_template.txt
TEMPLATE_SOURCES = (
    SKILL_ROOT / "auth" / "auth.txt",
    SKILL_ROOT / "scripts" / "auth_template.txt",
)

BUILTIN_TEMPLATE = (
    "#请在这个文件夹内填写翻译服务的api key\n"
    "#please put your translation services api key here\n"
    "#填写标准可参考EXAMPLE_KEY\n"
    "#Refer to EXAMPLE_KEY for formatting instructions\n\n"
    "# CREDENTIALS_PATH=\n"
    "# GOOGLE_API_KEY=\n"
    "# BAIDU_APP_ID=\n"
    "# BAIDU_SECRET_KEY=\n"
    "# VOLC_ACCESS_KEY=\n"
    "# VOLC_SECRET_KEY=\n"
)


def _read_text_any(path: Path) -> str | None:
    """按 TEXT_ENCODINGS 依次尝试读取文本；文件不存在或全部失败时返回 None。"""
    if not path.is_file():
        return None
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError as e:
            logger.debug("读取 %s 出错: %s", path, e)
            return None
    return None


def ensure_auth_template_file() -> Path:
    """若各处均无凭据，在用户配置目录内创建 auth.txt 模板并返回路径。

    写入优先级 2 的 ~/.config/tiny-translator/，下次运行即可被自动发现。
    已存在且非空的 auth.txt 不会被覆盖，避免冲掉用户的填写内容。
    """
    target_dir = get_user_config_dirs()[0]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "auth.txt"

    existing = _read_text_any(target)
    if existing and existing.strip():
        return target

    content = next(
        (text for text in (_read_text_any(p) for p in TEMPLATE_SOURCES) if text and text.strip()),
        BUILTIN_TEMPLATE,
    )
    target.write_text(content, encoding="utf-8", newline="\n")
    return target


def format_auth_prompt(engine_name: str, auth_file: Path) -> str:
    """生成面向 AI Agent 和用户的友好指引信息。"""
    config_dir = auth_file.parent
    return f"""
================================================================================
[AUTH_REQUIRED] 未检测到任何翻译服务的有效 API Key 或认证文件！
目标引擎: {engine_name}

已自动在用户配置目录内生成凭据配置模板文件:
--> 配置文件路径: {auth_file.resolve()}
--> 配置目录: {config_dir.resolve()}

【请引导用户完成以下任意一种配置】
1. 打开并编辑上述 auth.txt 文件，填写你的 API Key 或凭据路径；
2. 如果是 Google 翻译，可直接将下载好的 Service Account (.json) 凭据放入上述配置目录；
3. 或用 --credentials 参数直接指定凭据文件路径（支持 auth.txt 或 .json）。

配置完成后，重新运行当前翻译指令即可！
================================================================================
"""


def _parse_auth_txt(auth_file: Path) -> dict[str, str]:
    """解析 auth.txt (KEY=VALUE 格式)。"""
    res: dict[str, str] = {}
    if not auth_file.is_file():
        return res
    try:
        content = _read_text_any(auth_file) or ""
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            # 兼容 python 字符串字面量 r"C:\..."
            if v.lower().startswith("r") and len(v) > 2 and v[1] in ("'", '"'):
                v = v[1:]
            v = v.strip("\"'")
            if k and v:
                res[k] = v
    except Exception as e:
        logger.debug("读取 %s 出错: %s", auth_file, e)
    return res


def _is_gcp_service_account_json(path: Path) -> bool:
    """检测 JSON 文件是否为 Google Cloud Service Account 凭据。"""
    if not path.is_file() or path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("type") == "service_account"
    except Exception:
        return False


def _parse_baidu_json(path: Path) -> dict[str, str] | None:
    """检测 JSON 文件是否包含百度翻译凭据。"""
    if not path.is_file() or path.suffix.lower() != ".json":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            appid = data.get("baidu_app_id") or data.get("baidu_appid") or data.get("appid") or data.get("app_id")
            key = data.get("baidu_secret_key") or data.get("baidu_key") or data.get("secret_key") or data.get("key")
            if appid and key:
                return {"app_id": str(appid).strip(), "secret_key": str(key).strip()}
    except Exception:
        pass
    return None


def resolve_baidu_credentials(explicit_path: str | None = None) -> dict[str, Any]:
    """解析百度通用翻译凭据。

    返回 dict，可能包含：
      - "app_id": 百度开发者 APP ID
      - "secret_key": 百度开发者密钥
      - "source": 凭据来源说明
    """
    # 1. 显式指定的参数
    if explicit_path:
        p = Path(explicit_path).resolve()
        if p.is_file():
            b_json = _parse_baidu_json(p)
            if b_json:
                return {**b_json, "source": f"CLI argument: {p}"}
            auth_data = _parse_auth_txt(p)
            appid = auth_data.get("BAIDU_APP_ID") or auth_data.get("BAIDU_APPID")
            key = auth_data.get("BAIDU_SECRET_KEY") or auth_data.get("BAIDU_KEY")
            if appid and key:
                return {"app_id": appid, "secret_key": key, "source": f"CLI auth.txt -> {p}"}
        else:
            logger.warning("指定百度凭据路径不存在: %s", explicit_path)

    # 2. 用户级目录 (~/.config/tiny-translator/)
    for udir in get_user_config_dirs():
        if not udir.is_dir():
            continue
        # 查找 baidu_credentials.json 或 credentials.json
        for json_file in udir.glob("*.json"):
            b_json = _parse_baidu_json(json_file)
            if b_json:
                return {**b_json, "source": f"User config dir: {json_file}"}
        auth_file = udir / "auth.txt"
        if auth_file.is_file():
            auth_data = _parse_auth_txt(auth_file)
            appid = auth_data.get("BAIDU_APP_ID") or auth_data.get("BAIDU_APPID")
            key = auth_data.get("BAIDU_SECRET_KEY") or auth_data.get("BAIDU_KEY")
            if appid and key:
                return {"app_id": appid, "secret_key": key, "source": f"User auth.txt: {auth_file}"}

    # 3. Skill 本地目录 (<skill>/auth/)
    for sdir in get_skill_auth_dirs():
        if not sdir.is_dir():
            continue
        for json_file in sdir.glob("*.json"):
            b_json = _parse_baidu_json(json_file)
            if b_json:
                return {**b_json, "source": f"Skill auth dir: {json_file}"}
        auth_file = sdir / "auth.txt"
        if auth_file.is_file():
            auth_data = _parse_auth_txt(auth_file)
            appid = auth_data.get("BAIDU_APP_ID") or auth_data.get("BAIDU_APPID")
            key = auth_data.get("BAIDU_SECRET_KEY") or auth_data.get("BAIDU_KEY")
            if appid and key:
                return {"app_id": appid, "secret_key": key, "source": f"Skill auth.txt: {auth_file}"}

    # 4. 环境变量
    env_appid = os.environ.get("BAIDU_APP_ID") or os.environ.get("BAIDU_APPID")
    env_key = os.environ.get("BAIDU_SECRET_KEY") or os.environ.get("BAIDU_KEY")
    if env_appid and env_key:
        return {"app_id": env_appid.strip(), "secret_key": env_key.strip(), "source": "ENV: BAIDU_APP_ID"}

    return {}


def _parse_volc_json(path: Path) -> dict[str, str] | None:
    """检测 JSON 文件是否包含火山引擎 AK/SK 凭据。"""
    if not path.is_file() or path.suffix.lower() != ".json":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            ak = (
                data.get("volc_access_key") or data.get("access_key_id")
                or data.get("access_key") or data.get("ak")
            )
            sk = (
                data.get("volc_secret_key") or data.get("secret_access_key")
                or data.get("secret_key") or data.get("sk")
            )
            if ak and sk:
                return {"access_key": str(ak).strip(), "secret_key": str(sk).strip()}
    except Exception:
        pass
    return None


def _volc_from_auth_data(auth_data: dict[str, str]) -> dict[str, str] | None:
    """从 auth.txt 解析结果中提取火山引擎 AK/SK。"""
    ak = (
        auth_data.get("VOLC_ACCESS_KEY") or auth_data.get("VOLC_ACCESS_KEY_ID")
        or auth_data.get("VOLCENGINE_ACCESS_KEY")
    )
    sk = (
        auth_data.get("VOLC_SECRET_KEY") or auth_data.get("VOLC_SECRET_ACCESS_KEY")
        or auth_data.get("VOLCENGINE_SECRET_KEY")
    )
    if ak and sk:
        return {"access_key": ak, "secret_key": sk}
    return None


def resolve_volcengine_credentials(explicit_path: str | None = None) -> dict[str, Any]:
    """解析火山引擎机器翻译凭据 (AK/SK)。

    返回 dict，可能包含：
      - "access_key": Access Key ID
      - "secret_key": Secret Access Key
      - "source": 凭据来源说明
    """
    # 1. 显式指定的参数
    if explicit_path:
        p = Path(explicit_path).resolve()
        if p.is_file():
            v_json = _parse_volc_json(p)
            if v_json:
                return {**v_json, "source": f"CLI argument: {p}"}
            creds = _volc_from_auth_data(_parse_auth_txt(p))
            if creds:
                return {**creds, "source": f"CLI auth.txt -> {p}"}
        else:
            logger.warning("指定火山引擎凭据路径不存在: %s", explicit_path)

    # 2~3. 用户级目录 -> Skill 本地目录
    dir_groups = (
        (get_user_config_dirs(), "User"),
        (get_skill_auth_dirs(), "Skill"),
    )
    for dirs, label in dir_groups:
        for d in dirs:
            if not d.is_dir():
                continue
            for json_file in d.glob("*.json"):
                v_json = _parse_volc_json(json_file)
                if v_json:
                    return {**v_json, "source": f"{label} config dir: {json_file}"}
            auth_file = d / "auth.txt"
            if auth_file.is_file():
                creds = _volc_from_auth_data(_parse_auth_txt(auth_file))
                if creds:
                    return {**creds, "source": f"{label} auth.txt: {auth_file}"}

    # 4. 环境变量
    env_ak = (
        os.environ.get("VOLC_ACCESS_KEY") or os.environ.get("VOLC_ACCESS_KEY_ID")
        or os.environ.get("VOLCENGINE_ACCESS_KEY")
    )
    env_sk = (
        os.environ.get("VOLC_SECRET_KEY") or os.environ.get("VOLC_SECRET_ACCESS_KEY")
        or os.environ.get("VOLCENGINE_SECRET_KEY")
    )
    if env_ak and env_sk:
        return {"access_key": env_ak.strip(), "secret_key": env_sk.strip(), "source": "ENV: VOLC_ACCESS_KEY"}

    return {}


def resolve_google_credentials(explicit_path: str | None = None) -> dict[str, Any]:
    """解析 Google 翻译凭据。

    返回 dict，可能包含：
      - "credentials_path": 服务账号 JSON 路径
      - "api_key": API Key
      - "source": 凭据来源说明
    """
    # 1. 显式指定的参数
    if explicit_path:
        p = Path(explicit_path).resolve()
        if p.is_file():
            if _is_gcp_service_account_json(p):
                return {"credentials_path": str(p), "source": f"CLI argument: {p}"}
            # 如果是文本文件，尝试作为 auth.txt 或直接作为 key
            auth_data = _parse_auth_txt(p)
            if "CREDENTIALS_PATH" in auth_data:
                target = (p.parent / auth_data["CREDENTIALS_PATH"]).resolve()
                if target.is_file():
                    return {"credentials_path": str(target), "source": f"CLI auth.txt -> {target}"}
            if "GOOGLE_API_KEY" in auth_data:
                return {"api_key": auth_data["GOOGLE_API_KEY"], "source": f"CLI auth.txt -> GOOGLE_API_KEY"}
            return {"credentials_path": str(p), "source": f"CLI argument (raw): {p}"}
        else:
            logger.warning("指定凭据路径不存在: %s", explicit_path)

    # 2. 用户级目录 (~/.config/tiny-translator/)
    for udir in get_user_config_dirs():
        if not udir.is_dir():
            continue
        # 2.1 优先查找 service account json
        for json_file in udir.glob("*.json"):
            if _is_gcp_service_account_json(json_file):
                return {"credentials_path": str(json_file), "source": f"User config dir: {json_file}"}
        # 2.2 查找 auth.txt
        auth_file = udir / "auth.txt"
        if auth_file.is_file():
            auth_data = _parse_auth_txt(auth_file)
            if "CREDENTIALS_PATH" in auth_data:
                target = (udir / auth_data["CREDENTIALS_PATH"]).resolve()
                if target.is_file():
                    return {"credentials_path": str(target), "source": f"User auth.txt -> {target}"}
            if "GOOGLE_API_KEY" in auth_data:
                return {"api_key": auth_data["GOOGLE_API_KEY"], "source": f"User auth.txt -> GOOGLE_API_KEY"}

    # 3. Skill 本地目录 (<skill>/auth/)
    for sdir in get_skill_auth_dirs():
        if not sdir.is_dir():
            continue
        # 3.1 查找 service account json
        for json_file in sdir.glob("*.json"):
            if _is_gcp_service_account_json(json_file):
                return {"credentials_path": str(json_file), "source": f"Skill auth dir: {json_file}"}
        # 3.2 查找 auth.txt
        auth_file = sdir / "auth.txt"
        if auth_file.is_file():
            auth_data = _parse_auth_txt(auth_file)
            if "CREDENTIALS_PATH" in auth_data:
                target = (sdir / auth_data["CREDENTIALS_PATH"]).resolve()
                if target.is_file():
                    return {"credentials_path": str(target), "source": f"Skill auth.txt -> {target}"}
            if "GOOGLE_API_KEY" in auth_data:
                return {"api_key": auth_data["GOOGLE_API_KEY"], "source": f"Skill auth.txt -> GOOGLE_API_KEY"}

    # 4. 环境变量
    env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_creds and Path(env_creds).is_file():
        return {"credentials_path": env_creds, "source": "ENV: GOOGLE_APPLICATION_CREDENTIALS"}

    env_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if env_key:
        return {"api_key": env_key, "source": "ENV: GOOGLE_API_KEY"}

    return {}
