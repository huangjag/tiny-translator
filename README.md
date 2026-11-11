# tiny-translator

给agent用的多引擎轻量级翻译工具。**接管agent的翻译工作**，节省你珍贵的token。

可翻译文本、文本文件或 HTML，**不破坏标签结构**，翻译 HTML 时保留 `<br>`、行内标签、属性、实体和 ruby 注音。。
支持多引擎，目前支持：百度、火山引擎、谷歌。需要自行配置key，有key的会先用，默认优先百度。

---

## 功能

| 能力 | 说明 |
|---|---|
| **多引擎** | `1` 百度 / `2` 火山引擎 / `9` 谷歌；不指定则按 1 → 2 → 9 探测，用第一个有凭据的服务 |
| **HTML标签模式** | 可输入带HTML的文字，不会破坏html标签：脆弱原子（`<rt>` 注音、`<wbr>`、实体）先掩码，再按 `<br>` 切分，最后本地还原；百度/火山等纯文本引擎：只提取文本节点翻译，标签和属性原样保留 |
| **三种输入** | `--text` 直接传字符串、`-f` 读文件（UTF-8 / GBK / Shift-JIS 自动识别）、stdin 管道 |
| **长文本自动处理** | 依照服务端的字符限制，自动对长文本进行分段翻译，之后自动组合输出。 |

## 安装

### 1. 前置条件

- **Python 3.9+**，核心功能不需要任何第三方包。下面的命令示例用 `python`，Linux / macOS 上可换成 `python3`。
- 可选依赖，按需安装：

```bash
# 用百度/火山翻译 HTML 时需要；未安装时这两家会退化为整段翻译，标签可能被破坏
pip install beautifulsoup4

# 用谷歌「服务账号」凭据时需要（API Key 方式不需要）
pip install google-cloud-translate
```

### 2. 获取代码

```bash
git clone <repo-url> tiny-translator
```

### 3. 安装到 agent 的 skills 目录

安装后无需额外配置：agent 读取 `description` 后，遇到翻译任务会自行调用。

### 4. 配置翻译服务的 key

首次运行如果没有任何凭据，CLI 会以退出码 `3` 结束，打印 `[AUTH_REQUIRED]`，并自动生成模板：

```
~/.config/tiny-translator/auth.txt
```

在模板里填任意一组即可（`KEY=VALUE`，一行一个）：

```ini
BAIDU_APP_ID=...
BAIDU_SECRET_KEY=...

VOLC_ACCESS_KEY=...
VOLC_SECRET_KEY=...

GOOGLE_API_KEY=...
```

谷歌也可以直接把下载的 service account `.json` 放进 `~/.config/tiny-translator/`，或写 `CREDENTIALS_PATH=<json 路径>`。

不想用文件时也可以走环境变量：`BAIDU_APP_ID` / `BAIDU_SECRET_KEY`、`VOLC_ACCESS_KEY` / `VOLC_SECRET_KEY`、`GOOGLE_API_KEY`、`GOOGLE_APPLICATION_CREDENTIALS`。

**高限制沙箱**（无法写用户目录、无法设环境变量）里，把 `auth.txt` 或凭据 JSON 放进本 skill 的 `auth/` 目录，然后重新打包安装 skill。

> `auth/` 会随仓库分发，只放模板，不要提交真实 key。真实凭据建议放在 `~/.config/tiny-translator/` 或环境变量里。

### 5. 验证

```bash
python scripts/translate.py --text "hello" -t zh
```

正常输出译文即成功；退出码 `3` 表示还没配置 key。

也可以完全跳过 agent，直接把 `scripts/translate.py` 当普通命令行工具用——`SKILL.md` + `scripts/` 两个东西拷到任何地方都能跑。

---

## 代码结构

```
tiny-translator/
├── SKILL.md                 # agent 入口：触发描述、工作流、参数/引擎/凭据说明
├── AGENTS.md                # 本仓库的开发规范（给改代码的 agent 看）
├── auth/
│   └── auth.txt             # 凭据模板，随包分发；不得放真实 key
└── scripts/                 # 自包含：单独拷这个目录也能运行
    ├── translate.py         # 主入口：CLI 参数、引擎选择、翻译编排、输出路由
    ├── auth_manager.py      # 凭据发现（4 级优先级）与模板生成
    ├── tag_processor.py     # 标签保护：掩码/切分/还原、DOM 文本节点隔离
    ├── auth_template.txt    # 模板权威副本，与 auth/auth.txt 保持一致
    └── engines/
        ├── base.py          # BaseEngine：translate(list[str]) -> list[str]
        ├── baidu.py         # 服务 1，纯文本引擎
        ├── volcengine.py    # 服务 2，纯文本引擎
        └── google_v2.py     # 服务 9，支持原生 HTML
```

一次翻译的调用链：

```
translate.py
  ├─ 读输入（--text / -f / stdin）→ 判断 text 还是 html
  ├─ select_and_init_engine() → 1 百度 → 2 火山 → 9 谷歌，取第一个有凭据的
  ├─ 纯文本：按行分批 → engine.translate() → 还原换行
  ├─ HTML
  │    ├─ 原生 HTML 引擎（谷歌）→ 掩码 → <br> 切分 → 还原 → 结构后检
  │    └─ 纯文本引擎（百度/火山）→ 提取文本节点 → 翻译 → 原位填回
  └─ 输出：< 5000 字符写 stdout；≥ 5000 写临时文件并打印 [OUTPUT_FILE] <path>
```

各引擎统一契约：`translate(list[str]) -> list[str]`，返回结果与输入**顺序严格一一对应**；批处理、QPS 限速、重试都在引擎内部完成，上层不需要关心。

---

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 输入错误（文件不存在、内容为空） |
| `2` | 翻译失败（网络、额度、服务端错误） |
| `3` | 缺少凭据（已生成配置模板） |
