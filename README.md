# ⚡ CATO (`Claude Adaptive Token Optimizer`)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-success.svg)](pyproject.toml)
[![Tests Passing](https://img.shields.io/badge/tests-16%2F16%20passing-brightgreen.svg)](tests/test_cato.py)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **[🇧🇷 Versão em Português disponível aqui](README.pt-BR.md)**

CATO lowers **Claude Code** cost by targeting what real billing data shows actually drives it —
**context size × number of turns** and **prompt-cache misses** — and it reports **measured** cost,
computed from the billed `usage` fields in Claude Code's session transcripts and the official price
table. Works alongside [RTK](https://github.com/rtk-ai/rtk). Zero dependencies.

---

## 📊 What the real data shows

`cato report` over 30 real transcripts (8,300 API calls, Opus 5.5 as the main model):

| Measure | Value |
| :--- | :--- |
| Total cost (API-equivalent) | **$1,280** |
| Context re-reads (cache read) | **56%** |
| 1-hour-TTL cache writes | 29% |
| Output | 14% |
| Uncached input | ~0% |
| Cost spent with context above 400k tokens | **60%** |
| Cache rewrites after > 1h idle | 38x, **$138** |
| Cache rewrites from switching models mid-session | 21x, $45 |
| Identical file re-reads | 49x, only 1,744 tokens (Claude Code already dedups) |
| Savings claimed by RTK (`rtk gain`) | 277k tokens — under 0.01% of re-read tokens |

What follows from it:

1. **Compressing individual tool outputs barely moves the bill.** Two controlled RTK studies (JetBrains,
   425 pairs; Quesma, 1,740 attempts) found no net billed saving (−5% to +7.6%): compression can cause
   extra turns, and the baseline ignores Claude Code's own output truncation.
2. **The cost is the context re-read every turn**, plus **cache rewrites** when the TTL expires.
3. **Denying a read costs a turn** (≈ context × cache-read price), so it only pays off with measured gains.

---

## 🏗️ How it works

| Layer | File | What it does |
| :--- | :--- | :--- |
| **Real report** | `telemetry/usage_report.py`, `telemetry/pricing.py` | Cost by category and context-size band, cache misses by cause (idle past TTL, model switch, compaction), tokens per tool (images by real dimensions), noisiest shell commands, per-day RTK/CATO attribution. Dedups by `message.id` and `tool_use_id`. |
| **Cost guard** | `hooks/user_prompt_submit.py` | Before a prompt reaches the API: if the cache expired with a large context, or context crossed 250k/400k/600k, it holds the prompt **once** with the concrete cost and options (`/clear`, `/compact`). Resending goes through. `/commands` are never blocked. |
| **Bash: RTK + CATO** | `hooks/pre_tool_use.py`, `hooks/post_tool_use.py` | RTK rewrites commands (if RTK's global hook isn't installed, CATO delegates to it — only for commands your permission rules already allow). CATO compresses the **real output** in `PostToolUse` (`updatedToolOutput`) only for what RTK didn't handle: never errors, code reads (`sed`, `cat`, `grep`…) or output above the native 30k-character limit. Raw log in `~/.cato/logs/`. |
| **Syntax check** | `hooks/post_tool_use.py` | After `Edit`/`Write` on `.py`/`.json`, reports a syntax error in the same turn. |
| **MCP sandbox** | `hooks/post_tool_use.py`, `storage/db.py` | Text MCP responses > 1,500 tokens go to SQLite FTS5 (BM25 search via `cato_search_sandbox`). |
| **Post-compaction** | `hooks/session_lifecycle.py` | Saves Git state before `/compact` and re-injects it afterwards. |
| **Read guard (opt-in)** | `code_intel/dedup.py`, `code_intel/ast_skeleton.py` | AST skeleton / `Read` dedup. Off by default; `CATO_READ_GUARD=1` enables it. |
| **Code map** | `code_intel/repomap.py`, `mcp_server/server.py` | MCP server with `cato_repo_map` (PageRank) and `cato_read_symbol`. |

---

## 🚀 Installation

```bash
pip install git+https://github.com/marcusaguiarmaciel-cmd/cato.git
cato init --global      # or `cato init` inside a project
cato doctor
```

> [!IMPORTANT]
> **Run `cato init` on every machine, including after cloning this repository.** The hooks in `.claude/settings.json` and the MCP server in `.mcp.json` point to the absolute paths of *your* Python interpreter and CATO install, so these files are generated per machine and are intentionally **not committed** (they are listed in `.gitignore`). A fresh clone has no CATO hooks until you run `cato init`.

---

## 💻 Commands

| Command | Description |
| :--- | :--- |
| `cato report [--days N] [--project P] [--json]` | Real cost from Claude Code transcripts. |
| `cato stats` | Real report (30 days) + interventions recorded by CATO. |
| `cato dashboard [--port 8765]` | Web dashboard with the real cost (light/dark). |
| `cato statusline` | For Claude Code's `statusLine`: current context, next-turn cost, minutes until the cache expires, today's cost. |
| `cato doctor` | Audits configuration and gives recommendations from your real usage. |
| `cato init [--global]` | Installs hooks, rules, subagent and MCP server without overwriting your settings. |
| `cato exec <cmd>` | Runs a command and compresses its output (manual use). |
| `cato repomap` / `cato symbol` / `cato skeleton` / `cato search-sandbox` | Code navigation and sandbox search. |

### Environment variables

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `CATO_GUARD` | on | `0` disables the cost guard. |
| `CATO_CONTEXT_WARN` | `250000,400000,600000` | Context bands that trigger the warning. |
| `CATO_BASH_COMPRESS` | on | `0` disables Bash/PowerShell output compression. |
| `CATO_READ_GUARD` | off | `1` enables the AST skeleton / `Read` dedup. |
| `~/.cato/pricing.json` | official table | Overrides per-model prices. |

---

## 🧠 Prompt caching

CATO only changes **new** tool results (the conversation suffix), never history, the system prompt or
the tool list — so the cached prefix stays valid.

---

## 🧪 Tests

```bash
python -m unittest discover -s tests -v
```

The **16 tests** use synthetic transcripts and a temporary `CATO_HOME`; they never touch `~/.cato` or
your transcripts.

---

## 🤝 Contributing

See the [Contributing Guide](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md).

## 📄 License

MIT — see [`LICENSE`](LICENSE). Created by **[Marcus Maciel](https://github.com/marcusaguiarmaciel-cmd)** (2026).
