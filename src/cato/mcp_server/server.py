"""
Servidor MCP (Model Context Protocol) JSON-RPC 2.0 sobre `stdio` (Parte 4 do CATO).
Zero dependências externas obrigatórias (inicialização em <20ms).
Expõe 4 ferramentas de alta eficiência de tokens ao Claude Code:
  1. `cato_repo_map`: Mapa de repositório AST ranqueado por Personalized PageRank
  2. `cato_read_symbol`: Leitura cirúrgica de função/classe/método por nome de símbolo
  3. `cato_search_sandbox`: Busca textual BM25 (SQLite FTS5) em payloads MCP ou logs brutos
  4. `cato_token_savings`: Custo real (tokens faturados x preço oficial), cache misses e contexto
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from cato import __version__
from cato.code_intel.repomap import build_repository_map, extract_symbol_source
from cato.hooks.bash_permissions import _settings_paths
from cato.storage.db import CatoDB, get_logs_dir

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SANDBOX_ID_RE = re.compile(r"^(sbx|cmd)_[0-9a-f]{8}$")
# Arquivos que nunca saem por uma ferramenta do CATO, mesmo sem regra `deny` configurada
SENSITIVE_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*", "*secret*")


def _project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()


def check_readable_path(file_path: str) -> Path:
    """
    Valida um caminho pedido pelo modelo antes de ler: precisa estar dentro do projeto, não pode ser
    um arquivo sensível e não pode casar com uma regra `deny` de `Read(...)` dos settings do usuário
    (uma ferramenta MCP não passa pelas regras de permissão do `Read` nativo).
    """
    root = _project_root()
    path = Path(file_path)
    path = (path if path.is_absolute() else root / path).resolve()
    if root != path and root not in path.parents:
        raise PermissionError(f"{file_path} está fora do projeto ({root})")
    if any(fnmatch.fnmatch(path.name.lower(), pat) for pat in SENSITIVE_FILE_PATTERNS):
        raise PermissionError(f"{path.name} parece conter segredos e não é lido pelo CATO")
    rel = path.relative_to(root).as_posix()
    for settings_path in _settings_paths(str(root)):
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for rule in (data.get("permissions") or {}).get("deny", []):
            rule = str(rule).strip()
            if not (rule.startswith("Read(") and rule.endswith(")")):
                continue
            pattern = rule[5:-1].strip()
            pattern = pattern[2:] if pattern.startswith("./") else pattern.lstrip("/")
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.rstrip("*").rstrip("/") + "/*"):
                raise PermissionError(f"{rel} é bloqueado pela regra {rule} em {settings_path}")
    return path

MCP_TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "cato_repo_map",
        "description": (
            "Generate a compact AST repository map ranked by Personalized PageRank over the symbol dependency graph. "
            "Use this instead of running multiple Glob/Grep/Read loops when exploring a codebase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_dir": {
                    "type": "string",
                    "description": "Root directory of the project (defaults to current working directory '.')",
                },
                "focus_query": {
                    "type": "string",
                    "description": "Optional keywords, symbol names, or file names to bias PageRank personalization",
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Maximum token budget for the repository map (default: 1200)",
                },
            },
        },
    },
    {
        "name": "cato_read_symbol",
        "description": (
            "Read ONLY the exact AST source code lines of a specific class, function, or method (e.g. 'MyClass.my_method' "
            "or 'my_function') with line numbers, saving 75-95% of tokens compared to reading the entire file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the source code file",
                },
                "symbol_name": {
                    "type": "string",
                    "description": "Name of the symbol (e.g. 'AuthService.login' or 'compress_test_output')",
                },
            },
            "required": ["file_path", "symbol_name"],
        },
    },
    {
        "name": "cato_search_sandbox",
        "description": (
            "Query a virtualized MCP payload (sbx_xxxx) using SQLite FTS5 BM25 search, or grep inside a "
            "saved raw command log (cmd_xxxx) without loading the entire output into context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Sandbox ID (e.g. 'sbx_a1b2c3d4') or Command Log ID (e.g. 'cmd_1234abcd')",
                },
                "query": {
                    "type": "string",
                    "description": "Search keywords or substring to match",
                },
            },
            "required": ["id", "query"],
        },
    },
    {
        "name": "cato_token_savings",
        "description": "Get the real API-equivalent cost of recent Claude Code sessions (billed usage x official prices), cache misses and context-size breakdown.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def execute_mcp_tool(
    name: str,
    arguments: Dict[str, Any],
    db: Optional[CatoDB] = None,
) -> Dict[str, Any]:
    """Executa uma ferramenta MCP do CATO e retorna o objeto `result` do protocolo MCP."""
    db_instance = db or CatoDB()

    try:
        if name == "cato_repo_map":
            root_dir = str(arguments.get("root_dir") or ".")
            focus = arguments.get("focus_query")
            budget = int(arguments.get("token_budget") or 1200)
            # Sem telemetria de "economia": não há como medir o que o modelo leria sem o mapa
            root = check_readable_path(root_dir)
            repo_map = build_repository_map(root_dir=root, focus_query=focus, token_budget=budget)
            return {"content": [{"type": "text", "text": repo_map}], "isError": False}

        if name == "cato_read_symbol":
            file_path = check_readable_path(str(arguments["file_path"]))
            symbol_name = str(arguments["symbol_name"])
            snippet, s_line, e_line = extract_symbol_source(file_path, symbol_name)
            header = f"# [CATO Symbol Extract: {symbol_name} em {file_path} (L{s_line}-L{e_line})]\n"
            return {"content": [{"type": "text", "text": header + snippet}], "isError": False}

        if name == "cato_search_sandbox":
            target_id = str(arguments["id"]).lstrip("#")
            query = str(arguments["query"])
            if not SANDBOX_ID_RE.match(target_id):
                return {
                    "content": [{"type": "text", "text": f"ID inválido: {target_id} (esperado sbx_xxxxxxxx ou cmd_xxxxxxxx)."}],
                    "isError": True,
                }

            if target_id.startswith("sbx_"):
                matches = db_instance.search_sandbox(target_id, query, limit=5)
                if not matches:
                    return {
                        "content": [{"type": "text", "text": f"Nenhum resultado para '{query}' em {target_id}."}],
                        "isError": False,
                    }
                formatted = [
                    f"=== [{m['title']}] ===\n{m['content']}"
                    for m in matches
                ]
                return {"content": [{"type": "text", "text": "\n\n".join(formatted)}], "isError": False}

            # Busca em log de comando bruto (`cmd_xxxx`)
            log_path = get_logs_dir() / f"{target_id}.log"
            if not log_path.exists():
                return {
                    "content": [{"type": "text", "text": f"ID {target_id} não encontrado."}],
                    "isError": True,
                }
            log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            q_lower = query.lower()
            matched_lines = [
                f"L{idx + 1}: {ln}"
                for idx, ln in enumerate(log_lines)
                if q_lower in ln.lower()
            ][:50]
            text_res = "\n".join(matched_lines) if matched_lines else f"Nenhuma ocorrência de '{query}' em {target_id}."
            return {"content": [{"type": "text", "text": text_res}], "isError": False}

        if name == "cato_token_savings":
            from cato.telemetry.dashboard import collect_real_metrics, format_stats_text

            payload = collect_real_metrics(db_instance)
            return {
                "content": [{"type": "text", "text": format_stats_text(payload)}],
                "isError": False,
            }

        return {
            "content": [{"type": "text", "text": f"Ferramenta desconhecida: {name}"}],
            "isError": True,
        }
    except Exception as exc:
        return {
            "content": [{"type": "text", "text": f"Erro em {name}: {exc}"}],
            "isError": True,
        }


def handle_mcp_jsonrpc_message(
    msg: Dict[str, Any],
    db: Optional[CatoDB] = None,
) -> Optional[Dict[str, Any]]:
    """Processa uma mensagem JSON-RPC 2.0 do protocolo MCP e retorna a resposta (ou None para notificações)."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notificações (sem id) não exigem resposta JSON-RPC
    if msg_id is None:
        return None

    if method == "initialize":
        # Responde com a versão do cliente só se a suportamos; senão, com a mais recente que suportamos
        client_proto = params.get("protocolVersion")
        proto = client_proto if client_proto in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cato-mcp", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": MCP_TOOLS_SCHEMA},
        }

    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments") or {}
        result = execute_mcp_tool(tool_name, tool_args, db=db)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_mcp_stdio_server() -> int:
    """Loop principal de leitura/escrita JSON-RPC 2.0 linha-a-linha sobre `stdio`."""
    db = CatoDB()
    for line in sys.stdin:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
            resp = handle_mcp_jsonrpc_message(msg, db=db)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception:
            continue
    return 0
