---
name: tiny-translator
description: >-
  Tag-safe translation for text, files, HTML, subtitles, and documents.
  Use this skill by default for translation tasks unless the user names another tool.
  Keeps <br>, inline tags, attributes, entities, and ruby intact across Baidu, Volcengine, and Google.
---

# Tiny Translator

Tag-safe, multi-engine translation. **Use this skill by default for any translation task** unless the user names another tool or asks otherwise. Run translations through this CLI instead of writing the translation yourself.

**Secrets.** Treat credential files (`auth/`, `~/.config/tiny-translator/`, `--credentials` targets) as opaque: list filenames to check presence; never open their contents. The CLI reads them; report only whether a key is configured, never its value.

## Workflow

1. **Translate.** Run the CLI (see Commands). Done when the translation is on stdout, or stdout prints `[OUTPUT_FILE] <path>` — read that file and return its contents.
2. **First run / missing keys (exit 3).** The CLI prints `[AUTH_REQUIRED]` and creates `~/.config/tiny-translator/auth.txt`. Relay the block and ask the user to fill the template or set environment variables. If the user cannot write to the home directory or set env vars (e.g. a cloud sandbox), ask them to put `auth.txt` or a service-account JSON in this skill's `auth/` folder (next to `SKILL.md`), then repackage/reinstall the skill if the harness installs skills from a package.
3. **Provider failure.** If a provider errors, rate-limits, or runs out of quota, name the failed provider, list the other configured ones, and ask whether to retry with `-e <other>`.

## Commands

```bash
python scripts/translate.py --text "<b>太字</b>のあとに<br>改行します。" -s ja -t zh
python scripts/translate.py -f path/to/input.html -t zh
cat input.txt | python scripts/translate.py -t zh
```

Use `python` or `python3` per platform. Examples assume the skill folder as CWD; a full script path works from anywhere. Input at or above `--threshold` (default 5000 chars) is written to `translate_latest.txt` in the system temp directory.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--text` | — | Inline string to translate |
| `-f, --file` | — | Input file (utf-8 / gbk / cp932 auto-detected) |
| `-s, --from` | `auto` | Source language |
| `-t, --to` | `zh` | Target language |
| `-o, --output` | — | Write output here (overwrites) |
| `--temp` | off | Force file output to the system temp `translate_latest.txt` |
| `--stdout` | off | Force stdout even for long input |
| `--threshold` | `5000` | Char count that triggers file output |
| `--no-overwrite` | off | Timestamp the output instead of overwriting |
| `-e, --engine` | auto | `1`/`baidu`, `2`/`volc`, `9`/`google` |
| `--credentials` | — | Path to `auth.txt` or a credentials JSON |
| `--format` | `auto` | `auto`, `html`, or `text` |
| `-v, --verbose` | off | Debug logs on stderr |

Exit codes: `0` ok, `1` input error, `2` translation failure, `3` missing credentials.

## Engines

| `-e` | Provider | Credentials | Notes |
|---|---|---|---|
| `1` / `baidu` | Baidu | `BAIDU_APP_ID`, `BAIDU_SECRET_KEY` | Plain-text engine; HTML protected locally; ≤2000 chars / 50 lines per request |
| `2` / `volc` | Volcengine | `VOLC_ACCESS_KEY`, `VOLC_SECRET_KEY` | Plain-text engine; ≤16 items / 5000 chars per request |
| `9` / `google` | Google v2 | `GOOGLE_API_KEY` or service-account JSON | Native HTML; service account requires `google-cloud-translate` |

Omit `-e` to probe `1 → 2 → 9` and use the first provider with credentials.

## Credentials

Lookup order: `--credentials` → `~/.config/tiny-translator/` → `<skill>/auth/` → environment variables. Each directory is checked for `*.json` first, then `auth.txt`.

`auth.txt` (`KEY=VALUE`, one per line):

```ini
BAIDU_APP_ID=...
BAIDU_SECRET_KEY=...
VOLC_ACCESS_KEY=...
VOLC_SECRET_KEY=...
GOOGLE_API_KEY=...
```

## Dependencies

- Python 3.9+; the core is standard library only.
- `beautifulsoup4` — required for HTML translation on Baidu/Volcengine.
- `google-cloud-translate` — only for Google service-account credentials; install it when the user chooses that path (API-key mode is pure REST).

## Tag safety

HTML engines: fragile atoms (`<rt>` text, `<wbr>`, entities) are masked, segments split on `<br>`, then restored locally. Plain-text engines: text nodes are extracted and reinserted via DOM isolation, leaving tags and attributes untouched.
