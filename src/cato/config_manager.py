"""
Gerenciador de Configuração e Diagnóstico do CATO (Parte 1).
Realiza merge não-destrutivo em `.claude/settings.json`, instala subagentes isolados (`Haiku`),
regras de eficiência de tokens (`.claude/rules/`) e executa auditoria (`cato doctor`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from cato.hooks.pre_tool_use import get_cato_executable_cmd

DEFAULT_DENY_RULES = [
    "Read(./node_modules/**)",
    "Read(./dist/**)",
    "Read(./build/**)",
    "Read(./coverage/**)",
    "Read(./.next/**)",
    "Read(./target/**)",
    "Read(./.venv/**)",
    "Read(./venv/**)",
    "Read(./*.min.js)",
    "Read(./*.min.css)",
    "Read(./package-lock.json)",
    "Read(./pnpm-lock.yaml)",
    "Read(./yarn.lock)",
    "Read(./Cargo.lock)",
    "Read(./poetry.lock)",
    "Read(./uv.lock)",
]

CATO_EXPLORER_AGENT_MD = """---
name: cato-explorer
description: Use proactively for broad codebase exploration, locating symbols across multiple files, or tracing dependencies so raw file reads do not pollute the main context window.
tools: Read, Grep, Glob
disallowedTools: Write, Edit, Bash
model: haiku
maxTurns: 12
---

You are **CATO Explorer**, a high-efficiency codebase localization subagent.
Your mission is to find the exact files, classes, functions, and line ranges needed for the parent agent's task while consuming minimal tokens:

1. Start with targeted `Glob` or bounded `Grep` (`head_limit: 40`).
2. When reading large files with `Read`, always specify `offset` and `limit` around the relevant symbol rather than reading entire files.
3. Return a **concise, high-signal report** (< 300 tokens) containing:
   - Exact file paths and `[L<start>-L<end>]` line numbers;
   - Exact function/class signatures and key findings;
   - Do NOT dump entire raw files in your final response.
"""

CATO_EFFICIENCY_RULE_MD = """# CATO Token Efficiency & Context Preservation Rules

1. **Surgical File Reads:** When reading files larger than 300 lines, prefer passing `offset` and `limit` to `Read` to inspect only the target function or class, or use the MCP tool `cato_read_symbol`. If CATO returns an AST Skeleton (`[L<start>-L<end>]`), use those exact line numbers in your next `Read` call.
2. **Repository Map over Blind Exploration:** Use `cato_repo_map` to get a PageRank-ranked AST overview of the codebase instead of chaining multiple blind `Glob` and `Grep` calls.
3. **Avoid Duplicate Reads:** Do not re-read a file that is already in the current conversation context unless it was modified externally.
4. **Delegate Broad Searches:** When you need to search across many unknown files to locate a feature or bug, delegate to the `cato-explorer` subagent so intermediate search outputs stay out of the main context window.
5. **Concise Outputs:** Never reprint large blocks of code in your final chat response that were already written or modified via `Edit` or `Write`. Summarize changes briefly with file and line references.
"""

CATO_STATS_SKILL_MD = """---
name: cato-stats
description: Exibe o painel completo de economia de tokens, taxa de Prompt Cache Hit e dólares poupados pelo sistema CATO.
disable-model-invocation: true
allowed-tools: Bash
---

Execute imediatamente o comando abaixo via `Bash` e apresente o resultado de forma clara ao usuário, informando também o caminho do painel visual HTML (`~/.cato/dashboard.html`):

```bash
cato stats
```
"""


def _ensure_hook_entry(
    hooks_list: List[Dict[str, Any]],
    matcher: str,
    command: str,
    marker: str = "cato",
) -> List[Dict[str, Any]]:
    """
    Atualiza ou adiciona um hook do CATO na lista de um evento sem duplicar entradas existentes
    e preservando outros hooks do usuário (como `rtk hook claude`).
    """
    updated = False
    new_list: List[Dict[str, Any]] = []

    for item in hooks_list:
        inner_hooks = item.get("hooks", [])
        is_cato_item = any(marker in str(h.get("command", "")).lower() for h in inner_hooks)
        if is_cato_item:
            if not updated:
                new_list.append(
                    {
                        "matcher": matcher,
                        "hooks": [{"type": "command", "command": command}],
                    }
                )
                updated = True
        else:
            new_list.append(item)

    if not updated:
        new_list.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command}],
            }
        )
    return new_list


def build_merged_settings(existing: Dict[str, Any]) -> Dict[str, Any]:
    """Faz o merge seguro das configurações de otimização do CATO em um dicionário `settings.json`."""
    merged = json.loads(json.dumps(existing)) if existing else {}

    # 1. Respeitar .gitignore por padrão
    if "respectGitignore" not in merged:
        merged["respectGitignore"] = True

    # 2. Variáveis de ambiente de economia de tokens
    env_block = merged.setdefault("env", {})
    env_block.setdefault("ENABLE_TOOL_SEARCH", "true")
    env_block.setdefault("CLAUDE_CODE_FORK_SUBAGENT", "0")
    env_block.setdefault("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "65")

    # 3. Permissões `deny` para artefatos pesados e lockfiles
    perms = merged.setdefault("permissions", {})
    deny_list: List[str] = list(perms.get("deny", []))
    for rule in DEFAULT_DENY_RULES:
        if rule not in deny_list:
            deny_list.append(rule)
    perms["deny"] = deny_list

    # 4. Hooks `PreToolUse`, `PostToolUse`, `PreCompact` e `SessionStart`
    cato_cmd = get_cato_executable_cmd()
    pre_cmd = f"{cato_cmd} hook pre-tool-use"
    post_cmd = f"{cato_cmd} hook post-tool-use"
    compact_cmd = f"{cato_cmd} hook pre-compact"
    session_cmd = f"{cato_cmd} hook session-reset"

    hooks_root = merged.setdefault("hooks", {})
    hooks_root["PreToolUse"] = _ensure_hook_entry(
        list(hooks_root.get("PreToolUse", [])),
        matcher="Read|Grep|Bash",
        command=pre_cmd,
    )
    hooks_root["PostToolUse"] = _ensure_hook_entry(
        list(hooks_root.get("PostToolUse", [])),
        matcher="Edit|Write|MultiEdit|mcp__.*",
        command=post_cmd,
    )
    hooks_root["PreCompact"] = _ensure_hook_entry(
        list(hooks_root.get("PreCompact", [])),
        matcher="manual|auto",
        command=compact_cmd,
    )
    hooks_root["SessionStart"] = _ensure_hook_entry(
        list(hooks_root.get("SessionStart", [])),
        matcher="clear|compact",
        command=session_cmd,
    )

    return merged


def install_cato(
    target_root: Path,
    is_global: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Instala o CATO em `target_root/.claude` (ou diretamente em `~/.claude` se `is_global=True`).
    Cria backup automático do `settings.json` existente.
    """
    claude_dir = target_root if (is_global and target_root.name == ".claude") else (target_root / ".claude")
    settings_path = claude_dir / "settings.json"
    agents_dir = claude_dir / "agents"
    rules_dir = claude_dir / "rules"

    existing_settings: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            existing_settings = {}

    merged_settings = build_merged_settings(existing_settings)
    actions: List[str] = []

    if not dry_run:
        claude_dir.mkdir(parents=True, exist_ok=True)
        agents_dir.mkdir(parents=True, exist_ok=True)
        rules_dir.mkdir(parents=True, exist_ok=True)

        if settings_path.exists():
            backup_path = claude_dir / "settings.json.cato-backup"
            shutil.copy2(settings_path, backup_path)
            actions.append(f"Backup criado em: {backup_path}")

        settings_path.write_text(
            json.dumps(merged_settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        actions.append(f"Configuração CATO aplicada em: {settings_path}")

        agent_file = agents_dir / "cato-explorer.md"
        agent_file.write_text(CATO_EXPLORER_AGENT_MD, encoding="utf-8")
        actions.append(f"Subagente econômico Haiku instalado em: {agent_file}")

        rule_file = rules_dir / "cato-token-efficiency.md"
        rule_file.write_text(CATO_EFFICIENCY_RULE_MD, encoding="utf-8")
        actions.append(f"Regra modular de eficiência instalada em: {rule_file}")

        skill_dir = claude_dir / "skills" / "cato-stats"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(CATO_STATS_SKILL_MD, encoding="utf-8")
        actions.append(f"Slash command `/cato-stats` instalado em: {skill_file}")

        # Se for instalação em projeto, registra também o servidor MCP no `.mcp.json` do projeto
        if not is_global:
            mcp_json_path = target_root / ".mcp.json"
            mcp_data: Dict[str, Any] = {}
            if mcp_json_path.exists():
                try:
                    mcp_data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
                except Exception:
                    mcp_data = {}
            import sys

            py_exe = str(Path(sys.executable).resolve()).replace("\\", "/")
            cli_script = str((Path(__file__).resolve().parent / "cli.py").resolve()).replace("\\", "/")
            mcp_servers = mcp_data.setdefault("mcpServers", {})
            mcp_servers["cato"] = {
                "command": py_exe,
                "args": [cli_script, "mcp-server"],
            }
            mcp_json_path.write_text(
                json.dumps(mcp_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            actions.append(f"Servidor MCP `cato` registrado em: {mcp_json_path}")
    else:
        actions.append(f"[DRY-RUN] Atualizaria {settings_path}, {agents_dir / 'cato-explorer.md'} e {rules_dir / 'cato-token-efficiency.md'}")

    return {
        "claude_dir": str(claude_dir),
        "settings_path": str(settings_path),
        "actions": actions,
        "merged_settings": merged_settings,
    }


def audit_environment(project_root: Path) -> Tuple[int, List[Dict[str, str]]]:
    """
    Executa diagnóstico completo (`cato doctor`) verificando configurações globais (`~/.claude`)
    e de projeto (`<project>/.claude`), atribuindo uma nota de 0 a 100.
    """
    findings: List[Dict[str, str]] = []
    score = 100

    global_settings_path = Path.home() / ".claude" / "settings.json"
    proj_settings_path = project_root / ".claude" / "settings.json"

    combined_settings: Dict[str, Any] = {}
    for p in (global_settings_path, proj_settings_path):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                combined_settings.update(data)
            except Exception:
                findings.append({"level": "WARN", "msg": f"Arquivo JSON inválido em {p}"})

    # 1. Verifica ENABLE_TOOL_SEARCH
    env_vars = combined_settings.get("env", {})
    if str(env_vars.get("ENABLE_TOOL_SEARCH", "")).lower() in ("true", "auto", "1"):
        findings.append({"level": "OK", "msg": "ENABLE_TOOL_SEARCH está ativo (economiza 85-95% de schemas MCP)."})
    else:
        score -= 15
        findings.append(
            {
                "level": "WARN",
                "msg": "ENABLE_TOOL_SEARCH não configurado em `env`. Execute `cato init` para ativar lazy loading de MCPs.",
            }
        )

    # 2. Verifica CLAUDE_CODE_FORK_SUBAGENT
    if str(env_vars.get("CLAUDE_CODE_FORK_SUBAGENT", "")) == "0":
        findings.append({"level": "OK", "msg": "CLAUDE_CODE_FORK_SUBAGENT=0 configurado (subagentes com contexto limpo)."})
    else:
        score -= 10
        findings.append(
            {
                "level": "WARN",
                "msg": "CLAUDE_CODE_FORK_SUBAGENT não está definido como '0' (risco de subagentes herdarem contexto pesado).",
            }
        )

    # 3. Verifica Hooks PreToolUse (CATO & RTK)
    hooks = combined_settings.get("hooks", {}).get("PreToolUse", [])
    hook_cmds = [
        h.get("command", "")
        for entry in hooks
        for h in entry.get("hooks", [])
    ]
    has_cato_hook = any("cato" in c.lower() for c in hook_cmds)
    has_rtk_hook = any("rtk" in c.lower() for c in hook_cmds)

    if has_cato_hook:
        findings.append({"level": "OK", "msg": "Hook CATO PreToolUse ativo (Read AST/Dedup + Grep Limit + Bash Optimizer)."})
    else:
        score -= 25
        findings.append(
            {
                "level": "WARN",
                "msg": "Hook CATO PreToolUse ainda não ativo neste escopo (`Read` e `Grep` estão sem proteção contra bloat).",
            }
        )

    if has_rtk_hook:
        findings.append({"level": "OK", "msg": "Hook RTK (`rtk hook claude`) detectado em PreToolUse (encadeado com CATO)."})

    # 4. Verifica model & effortLevel (alerta sobre Opus + xhigh)
    model = combined_settings.get("model", "")
    effort = combined_settings.get("effortLevel", "")
    if "opus" in str(model).lower() and str(effort).lower() == "xhigh":
        score -= 15
        findings.append(
            {
                "level": "WARN",
                "msg": "Alerta de Custo: `model: opus` combinado com `effortLevel: xhigh` gera blocos `thinking` massivos (cobrados a 5x como output). Considere `sonnet` ou `effortLevel: medium/high` para tarefas rotineiras.",
            }
        )

    # 5. Verifica tamanho do CLAUDE.md
    for md_path in (Path.home() / ".claude" / "CLAUDE.md", project_root / "CLAUDE.md", project_root / ".claude" / "CLAUDE.md"):
        if md_path.exists():
            lines = md_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 150:
                score -= 15
                findings.append(
                    {
                        "level": "WARN",
                        "msg": f"{md_path} possui {len(lines)} linhas (>150). Mova instruções específicas para `.claude/rules/` ou `.claude/skills/`.",
                    }
                )
            else:
                findings.append({"level": "OK", "msg": f"{md_path} está enxuto ({len(lines)} linhas)."})

    # 6. Verifica `.claudeignore` (mito comum)
    if (project_root / ".claudeignore").exists():
        findings.append(
            {
                "level": "WARN",
                "msg": "Arquivo `.claudeignore` detectado, mas o Claude Code NÃO suporta `.claudeignore` nativamente! Use `.gitignore` + `permissions.deny` em `.claude/settings.json`.",
            }
        )

    return max(0, score), findings
