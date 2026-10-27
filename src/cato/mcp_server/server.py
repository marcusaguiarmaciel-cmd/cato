"""
Servidor MCP (Model Context Protocol) JSON-RPC 2.0 sobre `stdio` (Parte 4 do CATO).
Zero dependências externas obrigatórias (inicialização em <20ms).
Expõe 4 ferramentas de alta eficiência de tokens ao Claude Code:
  1. `cato_repo_map`: Mapa de repositório AST ranqueado por Personalized PageRank
  2. `cato_read_symbol`: Leitura cirúrgica de função/classe/método por nome de símbolo
  3. `cato_search_sandbox`: Busca textual BM25 (SQLite FTS5) em payloads MCP ou logs brutos
  4. `cato_token_savings`: Relatório em tempo real de tokens e dólares economizados
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from cato.code_intel.repomap import build_repository_map, extract_symbol_source
from cato.storage.db import CatoDB, estimate_tokens, get_logs_dir

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
        "description": "Get real-time token and cost savings statistics recorded by CATO.",
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
            repo_map = build_repository_map(root_dir=root_dir, focus_query=focus, token_budget=budget)
            opt_tok = estimate_tokens(repo_map)
            # Compara com o custo médio de ler os arquivos exploratoriamente (~4x o mapa)
            db_instance.record_telemetry(
                session_id="mcp",
                event_type="mcp_repo_map",
                tool_name="cato_repo_map",
                target=f"{root_dir} ({focus or 'all'})",
                raw_tokens=opt_tok * 4,
                optimized_tokens=opt_tok,
            )
            return {"content": [{"type": "text", "text": repo_map}], "isError": False}

        if name == "cato_read_symbol":
            file_path = str(arguments["file_path"])
            symbol_name = str(arguments["symbol_name"])
            full_text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            raw_tok = estimate_tokens(full_text)

            snippet, s_line, e_line = extract_symbol_source(file_path, symbol_name)
            header = f"# [CATO Symbol Extract: {symbol_name} em {file_path} (L{s_line}-L{e_line})]\n"
            out_text = header + snippet
            opt_tok = estimate_tokens(out_text)

            db_instance.record_telemetry(
                session_id="mcp",
                event_type="mcp_read_symbol",
                tool_name="cato_read_symbol",
                target=f"{Path(file_path).name}#{symbol_name}",
                raw_tokens=raw_tok,
                optimized_tokens=opt_tok,
            )
            return {"content": [{"type": "text", "text": out_text}], "isError": False}

        if name == "cato_search_sandbox":
            target_id = str(arguments["id"]).lstrip("#")
            query = str(arguments["query"])

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
            from cato.telemetry.dashboard import collect_full_dashboard_payload

            full_stats = collect_full_dashboard_payload(db_instance)
            return {
                "content": [{"type": "text", "text": json.dumps(full_stats, indent=2, ensure_ascii=False)}],
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
        client_proto = params.get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cato-mcp", "version": "0.1.0"},
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
