# AGENTS.md

`tiny-translator` — multi-engine translation CLI (agent skill) with deterministic HTML tag protection. Python 3, stdlib-only core, no packaging config (`requirements.txt`/`pyproject.toml` do not exist). `tests/` is gitignored (local fixtures); `auth/` **ships** and must stay template-only.

## Standing check — `auth/` holds no real keys

`auth/` is published with the skill, so it is a template folder, never a credential store. At the start of every turn, probe it **without opening any file**:

```powershell
Get-ChildItem auth -File | Select-Object Name, Length
(Get-Item auth\auth.txt).Length -eq (Get-Item scripts\auth_template.txt).Length
(Get-FileHash auth\auth.txt).Hash -eq (Get-FileHash scripts\auth_template.txt).Hash
```

Any one of these is a leak signal — stop and warn the user immediately:
- a `*.json` in `auth/` (service-account files are always real credentials);
- `auth/auth.txt` size or hash differs from `scripts/auth_template.txt`.

On a hit: name the file and its size, ask the user to move the credentials to `~/.config/tiny-translator/` and restore `auth/auth.txt` from `scripts/auth_template.txt`, then re-probe. Never print, summarize, or diff the contents.

## Read first

- `SKILL.md` — agent-facing skill manifest (CLI flags, output contract, credential lookup order, engine notes). **Update it whenever CLI behavior changes**; it is the documented contract.
- `tests/google-translate-v2-html-knowledge.md` — measured Google Translate v2 behavior matrix (dated 2026-11, ja→zh); local-only (gitignored). `scripts/tiny_translator/tags.py` masking/straddle/split logic is designed against it. If you change tag-protection behavior or target a new language pair, re-run its probes (§5) against the real API.

## Commands

Run the CLI by file path from the repo root — `python -m scripts.translate` does not exist; the shim is `scripts/translate.py` (or `python -m tiny_translator` with `scripts/` as CWD):

```bash
python scripts/translate.py --text "<b>太字</b>のあとに<br>改行します。" -s ja -t zh
```

Tests are stdlib `unittest` (no pytest), hermetic — no network, no real credentials:

```bash
python -m unittest discover -s tests
```

- Filter with `-k <pattern>` or `-p test_tag_processor.py`.
- `tests/` is gitignored; `tests/test_tag_processor.py` inserts the repo root into `sys.path` itself and imports `tiny_translator`.
- Google service-account support needs `google-cloud-translate`. When it is missing, the CLI re-execs under `<repo>/.venv` or the path in `TRANSLATOR_VENV`; the API-key path is pure stdlib REST.

## Credentials are live — real billable API calls

- `tests/` may hold working Baidu/Volcengine/Google service-account keys (gitignored). Any CLI invocation (`--text`, `-f`, stdin) sends real requests: Baidu free tier ≈ 1 QPS, Google v2 bills per character and every retry re-bills. Verify logic with mocked unit tests, not live CLI runs. Real keys belong in `~/.config/tiny-translator/`, never in `auth/`.
- Engine auto-detect order: 1 baidu → 2 volcengine → 9 google, skipping engines without creds. Missing creds for an explicit engine → exit code 3 with an `[AUTH_REQUIRED]` prompt on stderr (exit 1 = input error, 2 = translation failure).
- Credential search order (`tiny_translator/auth.py`): `--credentials` → `~/.config/tiny-translator/` → `<repo>/auth/` → env vars. Within a directory, `*.json` is checked before `auth.txt` (`KEY=VALUE`).
- When nothing is found, `ensure_auth_template_file()` writes the template to `~/.config/tiny-translator/auth.txt` (then the CLI exits 3). It never overwrites a non-empty file.
- Template sources, in order: `<repo>/auth/auth.txt` (shipped copy users may edit in a sandbox) → `scripts/auth_template.txt` (**canonical**). Both are read with encoding fallback `utf-8-sig → gb18030 → cp932`. **Edit `scripts/auth_template.txt`, then copy it over `auth/auth.txt`** — the two must stay byte-identical (the standing check above relies on it).

## Architecture

`scripts/tiny_translator/` is a stdlib-only package — the reusable library. Everything runnable lives under `scripts/`, so the skill folder is self-contained (copying `SKILL.md` + `scripts/` alone is enough). `scripts/translate.py` is a 10-line shim so `python scripts/translate.py ...` keeps working.

- `scripts/tiny_translator/core.py` — library entry: `translate`, `translate_text`, `translate_html`, `resolve_engine`, `available_engines`. Raises `AuthMissingError` instead of exiting; accepts an injected `engine=` to skip credential discovery.
- `scripts/tiny_translator/cli.py` — process-level concerns only: argparse, stdio UTF-8, output routing (auto encoding fallback utf-8-sig → utf-8 → cp932 → gb18030; 5000-char threshold), exit codes 0/1/2/3, venv re-exec. **Never import `cli` from library code** — it writes stdout and calls `sys.exit`.
- `scripts/tiny_translator/auth.py` — credential discovery (`--credentials` → user config → skill `auth/` → env) and template generation. `SKILL_ROOT` is `parents[2]` from the module file, so the `auth/` fallback only resolves inside this repo layout.
- `scripts/tiny_translator/tags.py` — protection core. `GoogleHTMLProcessor` (engines with `supports_html=True`): mask fragile atoms (`<rt>` content, `<wbr>`, entities in text nodes) as `<span translate="no">⟦Gn⟧</span>` → straddle-check (paired tag crossing `<br>` → translate whole segment) → capture-split on `<br>` → local restore. `DOMTextNodeProcessor` (plain-text engines: baidu/volcengine): extracts text nodes via `bs4`, translates them, reassembles; **silently degrades to raw translation if `bs4` is not installed**.
- `scripts/tiny_translator/errors.py` — `TranslationError(RuntimeError)` plus `AuthMissingError`, `MissingDependencyError`, `ProviderError`, `QuotaExceededError`, `RateLimitError`, `NetworkError`. Engines map provider codes onto these so a host can switch provider on quota/limit.
- `scripts/tiny_translator/engines/` — `baidu` (1), `volcengine` (2), `google_v2` (9) on `BaseEngine`. Core contract: `translate(list[str]) -> list[str]` must return strictly order-corresponding results; engines batch internally (baidu ≤2000 chars/50 lines joined by `\n`; volcengine ≤16 items/5000 chars with hard-split of oversized texts; google ≤128 segments/request).
- `tests/input/` / `tests/output/` are manual ja→zh novel/HTML fixtures (real-world quality checks, unused by unit tests). `tests/` is gitignored, so it is absent from the published repo.

Library usage from another project (copy `scripts/tiny_translator/` in, or put `<repo>/scripts` on `PYTHONPATH`):

```python
import tiny_translator as tt
tt.translate_text("こんにちは", target="zh")
tt.translate_html(page, target="zh")
engine = tt.BaiduEngine(app_id="...", secret_key="...")
tt.translate_text("hello", target="zh", engine=engine)   # 跳过凭据发现
```

## Quirks

- `--format auto` treats any input containing `<` and `>` as HTML (routed through tag protection). Force `--format text` for prose containing stray angle brackets.
- Output contract: < 5000 chars → stdout; ≥ 5000 → overwritten temp file `%TEMP%\translate_latest.txt` plus stdout marker `[OUTPUT_FILE] <path>`. `-o`, `--stdout`, `--no-overwrite` override. Consumers should parse the `[OUTPUT_FILE]` marker and read the file.
- The CLI forces UTF-8 stdio on Windows (`cli._configure_stdio`); importing `tiny_translator` leaves `sys.stdout` untouched.
- `tests/` is gitignored, so ripgrep-based searches skip it by default; pass explicit paths or `--no-ignore` when auditing. `auth/` is tracked — include it in secret scans.
