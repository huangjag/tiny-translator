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
- `tests/google-translate-v2-html-knowledge.md` — measured Google Translate v2 behavior matrix (dated 2026-11, ja→zh); local-only (gitignored). `scripts/tag_processor.py` masking/straddle/split logic is designed against it. If you change tag-protection behavior or target a new language pair, re-run its probes (§5) against the real API.

## Commands

Run the CLI by file path from the repo root — never `python -m scripts.translate` (modules use flat imports; `scripts/` has no `__init__.py`):

```bash
python scripts/translate.py --text "<b>太字</b>のあとに<br>改行します。" -s ja -t zh
```

Tests are stdlib `unittest` (no pytest), hermetic — no network, no real credentials:

```bash
python -m unittest discover -s tests
```

- Filter with `-k <pattern>` or `-p test_tag_processor.py`.
- `tests/` is gitignored; `tests/test_tag_processor.py` and `tests/test_text_splitter.py` insert `scripts/` into `sys.path` themselves. Engine tests replace `_send_request_with_retry` / `_translate_rest` with mocks, so they never hit the network.
- Google service-account support needs `google-cloud-translate`. When it is missing, `translate.py` re-execs under `<repo>/.venv` or the path in `TRANSLATOR_VENV`; the API-key path is pure stdlib REST.

## Credentials are live — real billable API calls

- `tests/` may hold working Baidu/Volcengine/Google service-account keys (gitignored). Any CLI invocation (`--text`, `-f`, stdin) sends real requests: Baidu defaults to 10 QPS (lower it with `--qps` for accounts with a smaller quota), Google v2 bills per character and every retry re-bills. Verify logic with mocked unit tests, not live CLI runs. Real keys belong in `~/.config/tiny-translator/`, never in `auth/`.
- Engine auto-detect order: 1 baidu → 2 volcengine → 9 google, skipping engines without creds. Missing creds for an explicit engine → exit code 3 with an `[AUTH_REQUIRED]` prompt on stderr (exit 1 = input error, 2 = translation failure).
- Credential search order (`auth_manager.py`): `--credentials` → `~/.config/tiny-translator/` → `<repo>/auth/` → env vars. Within a directory, `*.json` is checked before `auth.txt` (`KEY=VALUE`).
- When nothing is found, `ensure_auth_template_file()` writes the template to `~/.config/tiny-translator/auth.txt` (then the CLI exits 3). It never overwrites a non-empty file.
- Template sources, in order: `<repo>/auth/auth.txt` (shipped copy users may edit in a sandbox) → `scripts/auth_template.txt` (**canonical**). Both are read with encoding fallback `utf-8-sig → gb18030 → cp932`. **Edit `scripts/auth_template.txt`, then copy it over `auth/auth.txt`** — the two must stay byte-identical (the standing check above relies on it).

## Architecture

- `scripts/translate.py` — CLI entry: input reading (auto encoding fallback utf-8-sig → utf-8 → cp932 → gb18030), 5000-char threshold output routing, engine selection.
- `scripts/tag_processor.py` — protection core. `GoogleHTMLProcessor` (engines with `supports_html=True`): mask fragile atoms (`<rt>` content, `<wbr>`, entities in text nodes) as `<span translate="no">⟦Gn⟧</span>` → straddle-check (paired tag crossing `<br>` → translate whole segment) → capture-split on `<br>` → local restore. `DOMTextNodeProcessor` (plain-text engines: baidu/volcengine): extracts text nodes via `bs4`, translates them, reassembles; **silently degrades to raw translation if `bs4` is not installed**.
- `scripts/engines/` — `baidu` (1), `volcengine` (2), `google_v2` (9) on `BaseEngine`. Core contract: `translate(list[str]) -> list[str]` must return strictly order-corresponding results; engines batch internally (baidu ≤2000 chars/50 lines joined by `\n`; volcengine ≤16 items/5000 chars; google ≤128 segments/5000 chars). Oversized single items are pre-split by `scripts/text_splitter.py` (`expand()` → pieces + spans) and rejoined losslessly.
- `scripts/text_splitter.py` — stdlib leaf. `split_text(text, n)` splits at natural boundaries (newline → sentence end → English period → clause → whitespace → hard cut) and is lossless (`"".join(...) == text`); `split_for_translation()` also peels edge whitespace into standalone pieces so providers cannot trim away paragraph breaks; `expand()` maps pieces back to their source item via spans. Tuned for translation: greedy fill, balanced split only when the tail would be shorter than 10% of the limit. `split_html()` / `expand_html()` do the same for HTML: cuts only at tag boundaries (quote-aware `<...>` scanning, prefers block-level tags, never cuts inside a tag or between `<br>` and `</br>`), closes open inline tags at the cut and reopens them in the next piece so every piece is tag-balanced; `"".join(...)` is content-equivalent (tags may be duplicated), and a single tag longer than the limit forces one oversized piece (engines warn).
- `tests/input/` / `tests/output/` are manual ja→zh novel/HTML fixtures (real-world quality checks, unused by unit tests). `tests/` is gitignored, so it is absent from the published repo.

## Quirks

- `--format auto` treats any input containing `<` and `>` as HTML (routed through tag protection). Force `--format text` for prose containing stray angle brackets.
- Output contract: < 5000 chars → stdout; ≥ 5000 → overwritten temp file `%TEMP%\translate_latest.txt` plus stdout marker `[OUTPUT_FILE] <path>`. `-o`, `--stdout`, `--no-overwrite` override. Consumers should parse the `[OUTPUT_FILE]` marker and read the file.
- All scripts force UTF-8 stdio on Windows (stdout/stderr reconfigure).
- `tests/` is gitignored, so ripgrep-based searches skip it by default; pass explicit paths or `--no-ignore` when auditing. `auth/` is tracked — include it in secret scans.
