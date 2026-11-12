# tiny-translator

给agent用的多引擎轻量级翻译工具。**接管agent的翻译工作**，节省你珍贵的token。

如果skill调用触发不积极，可以考虑建一个**专用于翻译**的subagent。

如果只是想偶尔调用这个skill建议关闭skill的自动触发，或者直接[对SKILL.md修改](#6关闭自动触发)

可翻译文本、文本文件或 HTML，**不破坏标签结构**，保留 HTML 标签。
支持多引擎，目前支持：百度、火山引擎、谷歌。需要自行配置key，有key的会先用，默认优先百度。

---

## 功能

| 能力 | 说明 |
|---|---|
| **多引擎** | `1` 百度 / `2` 火山引擎 / `9` 谷歌；不指定则按 1 → 2 → 9 探测，用第一个有凭据的服务 |
| **HTML标签模式** | 可输入带 HTML 的文字，不会破坏标签：谷歌原生 HTML 走「掩码脆弱原子 → 按 `<br>` 切分 → 本地还原 → 结构后检」；百度/火山走「提取文本节点翻译 → 原位填回」，标签和属性原样保留 |
| **HTML 预检** | 翻译前用 `scripts/html_check.py` 检查标签平衡、截断 `</`、charset 和相对链接；`--fix` 只做保守机械修复，默认写 `<name>.fixed.html`，不覆盖原文件 |
| **三种输入** | `--text` 直接传字符串、`-f` 读文件、stdin 管道 |
| **长文本自动处理** | 依照服务端的字符限制，自动对长文本进行分段（依照翻译服务的限制）翻译，之后自动组合输出。 |

## 安装

### 1. 前置条件

- 不需要任何第三方包。如需使用以下功能时可按需安装：

```bash
# HTML: 用百度/火山翻译 HTML 时需要；未安装时这两家会退化为整段翻译，标签可能被破坏
pip install beautifulsoup4

# 谷歌JSON：用谷歌「服务账号」凭据(json文件)时需要（API Key 方式不需要）
pip install google-cloud-translate
```

### 2. 获取代码

直接下载release的skill压缩包，或直接让AI下载本repo。

### 3. 安装到 agent 的 skills 目录

agent 读取 `description` 后，遇到翻译任务会自行调用。

### 4. 配置翻译服务的 key

支持4种配置方式，优先级依照以下顺序：
1. 命令行参数传递 (--credentials)
2. 用户级目录配置文件 (~/.config/tiny-translator/)
3. skill本地目录 (<skill>/auth/)
4. 环境变量

首次运行如果没有任何凭据，CLI 会以退出码 `3` 结束，打印 `[AUTH_REQUIRED]`，并自动在用户级目录生成模板，你可以依照进行key配置：

```
~/.config/tiny-translator/auth.txt
```

在模板里填你的key即可（`KEY=VALUE`，一行一个）：

```ini
BAIDU_APP_ID=...
BAIDU_SECRET_KEY=...

VOLC_ACCESS_KEY=...
VOLC_SECRET_KEY=...

GOOGLE_API_KEY=...
```

谷歌也可以直接把下载的 service account `.json` 放进 `~/.config/tiny-translator/`，或在 `auth.txt` 里写 `CREDENTIALS_PATH=<json 路径>`（相对该 `auth.txt` 所在目录解析，不是环境变量）。

不想用文件时可以走环境变量：`BAIDU_APP_ID` / `BAIDU_SECRET_KEY`、`VOLC_ACCESS_KEY` / `VOLC_SECRET_KEY`、`GOOGLE_API_KEY`、`GOOGLE_APPLICATION_CREDENTIALS`。`--credentials` 也可以直接指向一个 `auth.txt` 或凭据 JSON；上述变量还有兼容别名（如 `BAIDU_APPID`、`VOLCENGINE_ACCESS_KEY`、`GOOGLE_TRANSLATE_API_KEY`），同样有效。

**高限制沙箱**（无法写用户目录、无法设环境变量）如kimi网页版等，可填写本skill内的 `auth.txt` 或将凭据 JSON 放入，然后重新打包 skill 后安装。

### 5. 翻译服务字符限制

| 服务 | 客户端单请求上限 | 官方限制 |
|---|---|---|
| 百度 | 1900 字符 / 5700 字节 / 50 行 | `q` 不超过 6000 字节 |
| 火山引擎 | 16 条 / 4900 字符 | 16 条、总长 5000 字符 |
| 谷歌 | 128 段 / 4900 字符 | 建议单请求不超过 5K code points |

上表是工具实际使用的值，都比官方上限留了一点余量，正常情况下不会触发超长报错。请求频率默认百度 10 QPS，账户配额较低时可用 `--qps` 调小。

超长文本会在本地按自然边界（换行 → 句末 → 从句 → 空白 → 硬切）切开，逐段翻译后无损拼回，不会把一句话从中间截断；百度额外按 UTF-8 字节数计算，避免中文、emoji 撑爆字节上限。HTML 只在标签边界处切分，必要时在切口闭合再重开行内标签，保证每一段标签都是平衡的。

### 6.关闭自动触发
删除 SKILL.md description中

```Use this skill by default for translation tasks unless the user says otherwise.``` 

和正文这部分内容即可

```**Use this skill by default for any translation task unless the user names another tool or asks otherwise.** Run translations through this CLI instead of writing the translation yourself.``` 




## 用法

```bash
# 直接翻译字符串（默认目标语言 zh）
python scripts/translate.py --text "こんにちは" -s ja

# 翻译文件并指定输出
python scripts/translate.py -f input.html -t zh -o output.html

# 管道输入
cat input.txt | python scripts/translate.py -s en -t zh
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--text` | — | 直接传入待翻译字符串 |
| `-f, --file` | — | 读取文件；也可用 stdin 管道 |
| `-s, --from` | `auto` | 源语言代码 |
| `-t, --to` | `zh` | 目标语言代码 |
| `-o, --output` | — | 输出文件路径（默认覆写） |
| `--temp` | off | 强制写入系统临时文件 |
| `--stdout` | off | 强制打印到终端，优先于 `-o` / `--temp` / 长度阈值 |
| `--threshold` | `5000` | 达到该字符数时自动改写临时文件 |
| `--no-overwrite` | off | 目标文件存在时生成带时间戳的副本 |
| `-e, --engine` | 自动探测 | `1`/`baidu`、`2`/`volc`、`9`/`google`；不指定时按 1 → 2 → 9 取第一个有凭据的 |
| `--credentials` | — | 凭据 JSON 或 `auth.txt` 路径 |
| `--qps` | 百度 10 | 请求频率上限（仅百度引擎使用） |
| `--format` | `auto` | `auto` / `html` / `text`；`auto` 下同时含 `<` 和 `>` 的文本会按 HTML 处理 |
| `-v, --verbose` | off | 打印调试日志 |
| `--version` | — | 打印版本号 |

输出规则：默认（未指定 `-o` / `--temp` / `--stdout`）小于 `--threshold` 时直接打印，达到阈值时写入 `%TEMP%\translate_latest.txt`（覆盖）并打印 `[OUTPUT_FILE] <path>`；用 `-o` / `--temp` 指定文件时同样打印该标记；`--stdout` 强制打印，优先级最高。输出文件固定 UTF-8，并保留原文的 `\r\n`。

## 二次开发注意
> `auth/` 会随仓库分发，二次开发时候只放模板，**不要放真实 key**。真实凭据建议放在 `~/.config/tiny-translator/` 或环境变量里。

## 代码结构

```
tiny-translator/
├── SKILL.md                 # agent 入口：触发描述、工作流、参数/引擎/凭据说明
├── AGENTS.md                # 本仓库的开发规范（给改代码的 agent 看）
├── auth/
│   └── auth.txt             # 凭据模板，随包分发；不得放真实 key
└── scripts/                 # 自包含：单独拷这个目录也能运行
    ├── translate.py         # 主入口：CLI 参数、引擎选择、翻译编排、输出路由
    ├── html_check.py        # 翻译前的 HTML 结构体检（标签平衡/截断/charset/链接）与保守修复
    ├── auth_manager.py      # 凭据发现（4 级优先级）与模板生成
    ├── tag_processor.py     # 标签保护：掩码/切分/还原、DOM 文本节点隔离、骨架后检
    ├── text_splitter.py     # 长文本与 HTML 的边界安全切分（字符 / 字节预算）
    ├── engine.py            # 三引擎合集：BaseEngine + baidu(1) / volcengine(2) / google_v2(9)
    ├── auth_template.txt    # 模板权威副本，与 auth/auth.txt 保持一致
```

一次翻译的调用链：

```
translate.py
  ├─ 读输入（--text / -f / stdin）→ 判断 text 还是 html
  ├─ HTML 输入：翻译前先手动运行 scripts/html_check.py 体检（agent 执行，非 CLI 自动）
  ├─ select_and_init_engine() → 1 百度 → 2 火山 → 9 谷歌，取第一个有凭据的
  ├─ 纯文本：按行分批 → engine.translate() → 还原换行
  ├─ HTML
  │    ├─ 原生 HTML 引擎（谷歌）→ 掩码 → <br> 切分 → 还原 → 结构后检
  │    └─ 纯文本引擎（百度/火山）→ 提取文本节点 → 翻译 → 原位填回
  └─ 输出：--stdout 强制打印；-o / --temp 写指定文件；否则 < 5000 字符打印，
            ≥ 5000 写临时文件并打印 [OUTPUT_FILE] <path>；--no-overwrite 时生成带时间戳副本
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

`2` 也会被 argparse 的参数错误占用（未知选项、缺少参数等）；`--help` 的退出码是 `0`。

`scripts/html_check.py` 使用自己的退出码：`0` 干净、`1` 发现问题、`2` 用法或 IO 错误。
