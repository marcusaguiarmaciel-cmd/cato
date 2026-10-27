"""
Avaliação das regras de permissão `Bash(...)` do Claude Code sobre o comando ORIGINAL.

O Claude Code avalia as regras de permissão contra o `updatedInput` devolvido pelo hook, e não contra o
comando que o Claude enviou. Como o wrapper `cato exec` muda o comando, uma regra `deny` como
`Bash(rm:*)` nunca casaria com ele. Por isso o CATO só reescreve (e aprova) um comando quando as
regras do próprio usuário já o aprovariam; em qualquer outro caso o comando segue intocado pelo fluxo
normal de permissões.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Metacaracteres de shell que tornam a avaliação de regras por prefixo/glob insegura
# (comandos compostos, substituições, redirecionamentos e execução em background).
UNSAFE_SHELL_TOKENS = ("\n", "\r", ";", "&", "|", "`", "$(", ">", "<")


def _managed_settings_paths() -> List[Path]:
    if os.name == "nt":
        return [Path(r"C:\Program Files\ClaudeCode\managed-settings.json")]
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    return [Path("/etc/claude-code/managed-settings.json")]


def _settings_paths(cwd: Optional[str]) -> List[Path]:
    """Lista os `settings.json` que o Claude Code mescla (usuário, projeto, local e gerenciado)."""
    paths = [Path.home() / ".claude" / "settings.json"]
    project_dirs = {d for d in (os.environ.get("CLAUDE_PROJECT_DIR"), cwd) if d}
    for project_dir in sorted(project_dirs):
        claude_dir = Path(project_dir) / ".claude"
        paths.append(claude_dir / "settings.json")
        paths.append(claude_dir / "settings.local.json")
    paths.extend(_managed_settings_paths())
    return paths


def rtk_hook_installed(cwd: Optional[str] = None) -> bool:
    """Indica se algum settings já registra o hook do RTK (`rtk hook claude`) em `PreToolUse`."""
    for path in _settings_paths(cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in (data.get("hooks") or {}).get("PreToolUse") or []:
            for hook in item.get("hooks") or []:
                if "rtk hook" in str(hook.get("command", "")).lower():
                    return True
    return False


def load_bash_rules(cwd: Optional[str] = None) -> Dict[str, List[str]]:
    """Coleta as regras `allow`, `ask` e `deny` de todas as fontes de configuração."""
    rules: Dict[str, List[str]] = {"allow": [], "ask": [], "deny": []}
    for path in _settings_paths(cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        perms = data.get("permissions") or {}
        for key in rules:
            rules[key].extend(str(r) for r in perms.get(key, []) if isinstance(r, str))
    return rules


def bash_rule_matches(rule: str, command: str, lenient: bool = False) -> bool:
    """
    Verifica se uma regra `Bash(...)` casa com `command`.
    `lenient=True` (usado para `deny`/`ask`) faz `Bash(ls *)` casar também com o `ls` puro.
    """
    rule = rule.strip()
    if rule in ("Bash", "Bash(*)"):
        return True
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    pattern = rule[5:-1].strip()
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    if "*" in pattern:
        if lenient and pattern.endswith(" *") and command == pattern[:-2]:
            return True
        regex = "^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$"
        return re.match(regex, command, re.DOTALL) is not None
    return command == pattern


def _any_match(rules: Iterable[str], command: str, lenient: bool = False) -> bool:
    return any(bash_rule_matches(rule, command, lenient=lenient) for rule in rules)


def is_simple_command(command: str) -> bool:
    return not any(tok in command for tok in UNSAFE_SHELL_TOKENS)


def is_preapproved_bash_command(command: str, payload: Dict[str, Any]) -> bool:
    """
    Retorna `True` apenas se o Claude Code executaria `command` sem perguntar ao usuário:
    comando simples, sem regra `deny`/`ask` correspondente, e com regra `allow` correspondente
    (ou sessão em `bypassPermissions`).
    """
    command = command.strip()
    if not command or not is_simple_command(command):
        return False

    rules = load_bash_rules(payload.get("cwd"))
    if _any_match(rules["deny"], command, lenient=True) or _any_match(rules["ask"], command, lenient=True):
        return False
    if payload.get("permission_mode") == "bypassPermissions":
        return True
    return _any_match(rules["allow"], command)
