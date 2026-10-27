"""
Interface de Linha de Comando (CLI) principal do CATO (`cato`).
Subcomandos completos (Partes 1 a 6):
  - `cato init [--global | --project] [--dry-run]`
  - `cato doctor`
  - `cato stats [--clear-reads]`
  - `cato report [--days N] [--project P] [--json]`
  - `cato dashboard [--port 8765] [--no-browser] [--export-only]`
  - `cato statusline`
  - `cato skeleton <arquivo>`
  - `cato symbol <arquivo> <simbolo>`
  - `cato repomap [--dir .] [--focus QUERY] [--budget 1200]`
  - `cato search-sandbox <id> <query>`
  - `cato exec [--session-id ID] [--b64 CMD_B64 | <cmd...>]`
  - `cato mcp-server`
  - `cato hook <pre-tool-use | post-tool-use | user-prompt-submit | pre-compact | session-reset>`
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import List, Optional

# Garante que `src` esteja no sys.path mesmo quando invocado diretamente via `python src/cato/cli.py`
_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from cato.bash_optimizer.runner import execute_and_optimize
from cato.code_intel.ast_skeleton import generate_file_skeleton
from cato.code_intel.repomap import build_repository_map, extract_symbol_source
from cato.config_manager import audit_environment, install_cato
from cato.hooks.post_tool_use import main_stdio as post_tool_use_stdio
from cato.hooks.pre_tool_use import main_stdio as pre_tool_use_stdio
from cato.hooks.session_lifecycle import main_stdio as session_lifecycle_stdio
from cato.hooks.user_prompt_submit import main_stdio as user_prompt_submit_stdio
from cato.mcp_server.server import execute_mcp_tool, run_mcp_stdio_server
from cato.storage.db import CatoDB, estimate_tokens
from cato.telemetry.dashboard import (
    collect_real_metrics,
    format_stats_text,
    format_statusline_text,
    run_dashboard_server,
    write_static_dashboard_html,
)
from cato.telemetry.usage_report import build_report, format_report_text, recommendations_from_report


def _configure_utf8_stdio() -> None:
    """Força UTF-8 no stdout/stderr no Windows para evitar erros de charmap (cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def cmd_init(args: argparse.Namespace) -> int:
    target = Path.home() / ".claude" if args.global_scope else Path(args.dir).resolve()
    result = install_cato(target_root=target, is_global=args.global_scope, dry_run=args.dry_run)

    scope_label = "GLOBAL (~/.claude)" if args.global_scope else f"PROJETO ({target})"
    print(f"=== CATO Inicializado [{scope_label}] ===")
    for action in result["actions"]:
        print(f"  [✓] {action}")
    if args.dry_run:
        print("\n Prévia do settings.json:")
        print(json.dumps(result["merged_settings"], indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    project_dir = Path(args.dir).resolve()
    score, findings = audit_environment(project_dir)

    print("============================================================")
    print("  CATO DOCTOR — Diagnóstico de Eficiência no Claude Code")
    print("============================================================")
    for item in findings:
        lvl = item["level"]
        badge = "[OK]  " if lvl == "OK" else "[WARN]"
        print(f"  {badge} {item['msg']}")
    print("------------------------------------------------------------")
    print(f"  Índice de Saúde de Tokens: {score}/100")
    payload = collect_real_metrics()
    recs = recommendations_from_report(payload["report"]) if payload else []
    if recs:
        print("------------------------------------------------------------")
        print(f"  RECOMENDAÇÕES A PARTIR DO SEU USO REAL (custo medido: ${payload['report']['total_cost']:,.2f}):")
        for rec in recs:
            print(f"  • {rec}")
    print("============================================================")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = CatoDB()
    if args.clear_reads:
        cleared = db.clear_session_reads()
        print(f"[✓] Cache de deduplicação de leituras limpo ({cleared} registros removidos).")
    if getattr(args, "reset", False):
        deleted = db.reset_telemetry()
        print(f"[✓] Histórico de telemetria zerado ({deleted} registros removidos).")

    payload = collect_real_metrics(db)
    print(format_stats_text(payload))
    print(f"  Painel visual: {write_static_dashboard_html(db, fast_mode=True)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    report = build_report(days=args.days, project=args.project)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report_text(report))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    db = CatoDB()
    html_path = write_static_dashboard_html(db)
    if args.export_only:
        print(f"[✓] Painel HTML atualizado em: {html_path}")
        return 0
    return run_dashboard_server(port=args.port, open_browser=not args.no_browser)


def cmd_statusline() -> int:
    # O Claude Code envia o estado da sessão (com `transcript_path`) em JSON no stdin da statusLine
    status_input = None
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            status_input = json.loads(raw) if raw.strip() else None
        except Exception:
            status_input = None
    print(format_statusline_text(status_input=status_input))
    return 0


def cmd_watch() -> int:
    """Monitora em tempo real no terminal cada intervenção do CATO enquanto o Claude trabalha."""
    import datetime
    import time

    db = CatoDB()
    print("============================================================")
    print("  ⚡ CATO WATCH — Monitor em Tempo Real (Ctrl+C para sair)")
    print("============================================================")
    print(f"  {format_statusline_text(db)}")
    print("------------------------------------------------------------")

    seen_ids: set[int] = set()
    initial = db.get_summary_stats().get("recent", [])
    for ev in reversed(initial[:5]):
        ev_id = int(ev.get("id", 0))
        seen_ids.add(ev_id)
        ts = datetime.datetime.fromtimestamp(float(ev["timestamp"])).strftime("%H:%M:%S")
        raw_t = int(ev["raw_tokens"])
        opt_t = int(ev["optimized_tokens"])
        sav_t = int(ev["saved_tokens"])
        pct = (sav_t / raw_t * 100.0) if raw_t > 0 else 0.0
        print(
            f"  [{ts}] {ev['tool_name']:<16} | {ev['event_type']:<18} | "
            f"{ev['target'][:28]:<28} | -{sav_t:,} tok (-{pct:.0f}%)"
        )

    print("------------------------------------------------------------")
    print("  Aguardando novas chamadas de ferramentas do Claude Code...")
    try:
        while True:
            time.sleep(1.0)
            recent = db.get_summary_stats().get("recent", [])
            for ev in reversed(recent):
                ev_id = int(ev.get("id", 0))
                if ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    ts = datetime.datetime.fromtimestamp(float(ev["timestamp"])).strftime("%H:%M:%S")
                    raw_t = int(ev["raw_tokens"])
                    sav_t = int(ev["saved_tokens"])
                    pct = (sav_t / raw_t * 100.0) if raw_t > 0 else 0.0
                    print(
                        f"  ⚡ [{ts}] {ev['tool_name']:<16} | {ev['event_type']:<18} | "
                        f"{ev['target'][:28]:<28} | -{sav_t:,} tok (-{pct:.0f}%)"
                    )
    except KeyboardInterrupt:
        print("\nMonitoramento encerrado.")
    return 0


def cmd_skeleton(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        sys.stderr.write(f"Erro: Arquivo não encontrado: {path}\n")
        return 1
    source = path.read_text(encoding="utf-8", errors="replace")
    raw_tokens = estimate_tokens(source)
    skeleton, total_lines = generate_file_skeleton(path, source)
    opt_tokens = estimate_tokens(skeleton)
    pct = ((raw_tokens - opt_tokens) / raw_tokens * 100.0) if raw_tokens > 0 else 0.0

    print(skeleton)
    print(f"\n# [Resumo CATO: {total_lines} linhas | {raw_tokens} -> {opt_tokens} tokens (-{pct:.1f}%)]")
    return 0


def cmd_symbol(args: argparse.Namespace) -> int:
    res = execute_mcp_tool(
        "cato_read_symbol",
        {"file_path": args.file, "symbol_name": args.symbol},
    )
    text = res["content"][0]["text"]
    print(text)
    return 1 if res.get("isError") else 0


def cmd_repomap(args: argparse.Namespace) -> int:
    res = execute_mcp_tool(
        "cato_repo_map",
        {"root_dir": args.dir, "focus_query": args.focus, "token_budget": args.budget},
    )
    print(res["content"][0]["text"])
    return 0


def cmd_search_sandbox(args: argparse.Namespace) -> int:
    res = execute_mcp_tool(
        "cato_search_sandbox",
        {"id": args.id, "query": args.query},
    )
    print(res["content"][0]["text"])
    return 1 if res.get("isError") else 0


def cmd_exec(args: argparse.Namespace) -> int:
    if args.b64:
        command = base64.b64decode(args.b64.encode("ascii")).decode("utf-8")
    else:
        parts = list(args.command)
        if parts and parts[0] == "--":
            parts = parts[1:]
        command = " ".join(parts).strip()
    if not command:
        return 0
    return execute_and_optimize(command=command, session_id=args.session_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cato",
        description="CATO (Claude Adaptive Token Optimizer) — Kit completo de otimização de tokens para Claude Code",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # cato init
    p_init = sub.add_parser("init", help="Instala e configura o CATO no projeto atual ou globalmente")
    p_init.add_argument("--global", dest="global_scope", action="store_true", help="Instalar em ~/.claude global")
    p_init.add_argument("--dir", default=".", help="Diretório raiz do projeto (padrão: .)")
    p_init.add_argument("--dry-run", action="store_true", help="Apenas simular sem gravar arquivos")

    # cato doctor
    p_doc = sub.add_parser("doctor", help="Audita a configuração do Claude Code em busca de desperdício de tokens")
    p_doc.add_argument("--dir", default=".", help="Diretório raiz do projeto (padrão: .)")

    # cato stats
    p_stats = sub.add_parser("stats", help="Exibe estatísticas reais de tokens economizados pelo CATO")
    p_stats.add_argument("--clear-reads", action="store_true", help="Limpa o cache de deduplicação de leituras")
    p_stats.add_argument("--reset", action="store_true", help="Zera o histórico de telemetria de economia de tokens")

    # cato report
    p_rep = sub.add_parser("report", help="Custo REAL dos transcripts do Claude Code (tokens faturados x preço oficial)")
    p_rep.add_argument("--days", type=float, default=None, help="Só os últimos N dias (padrão: todo o histórico)")
    p_rep.add_argument("--project", default=None, help="Filtra pelo nome da pasta do projeto em ~/.claude/projects")
    p_rep.add_argument("--json", action="store_true", help="Saída em JSON")

    # cato dashboard
    p_dash = sub.add_parser("dashboard", help="Abre o Painel Visual Web de Economia de Tokens em tempo real")
    p_dash.add_argument("--port", type=int, default=8765, help="Porta HTTP local (padrão: 8765)")
    p_dash.add_argument("--no-browser", action="store_true", help="Não abrir o navegador automaticamente")
    p_dash.add_argument("--export-only", action="store_true", help="Apenas gerar ~/.cato/dashboard.html e sair")

    # cato statusline
    sub.add_parser("statusline", help="Imprime linha compacta de economia de tokens para a statusLine do Claude Code")

    # cato watch
    sub.add_parser("watch", help="Monitora ao vivo no terminal cada economia de tokens enquanto o Claude trabalha")

    # cato skeleton
    p_skel = sub.add_parser("skeleton", help="Extrai e imprime o Esqueleto AST de um arquivo de código")
    p_skel.add_argument("file", help="Caminho do arquivo de código")

    # cato symbol
    p_sym = sub.add_parser("symbol", help="Extrai cirurgicamente apenas uma função/classe/método pelo nome")
    p_sym.add_argument("file", help="Caminho do arquivo de código")
    p_sym.add_argument("symbol", help="Nome do símbolo (ex: MinhaClasse.meu_metodo)")

    # cato repomap
    p_map = sub.add_parser("repomap", help="Gera Mapa de Repositório AST ranqueado por Personalized PageRank")
    p_map.add_argument("--dir", default=".", help="Diretório raiz do projeto")
    p_map.add_argument("--focus", default=None, help="Termos ou arquivos para focar o PageRank")
    p_map.add_argument("--budget", type=int, default=1200, help="Orçamento máximo de tokens (padrão: 1200)")

    # cato search-sandbox
    p_sbx = sub.add_parser("search-sandbox", help="Busca via SQLite FTS5 (BM25) em payloads MCP ou logs brutos")
    p_sbx.add_argument("id", help="ID do sandbox (sbx_xxxx) ou do comando (cmd_xxxx)")
    p_sbx.add_argument("query", help="Termos de busca")

    # cato exec
    p_exec = sub.add_parser("exec", help="Executa um comando de terminal comprimindo sua saída de forma determinística")
    p_exec.add_argument("--session-id", default="default", help="ID da sessão do Claude Code")
    p_exec.add_argument("--b64", default="", help="Comando codificado em Base64")
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="Comando a ser executado")

    # cato mcp-server
    sub.add_parser("mcp-server", help="Inicia o Servidor MCP (JSON-RPC 2.0 stdio) do CATO")

    # cato hook
    p_hook = sub.add_parser("hook", help="Endpoints internos chamados pelos Hooks do Claude Code via stdio")
    p_hook.add_argument(
        "event",
        choices=["pre-tool-use", "post-tool-use", "user-prompt-submit", "pre-compact", "session-reset"],
        help="Nome do evento do hook",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "init":
        return cmd_init(args)
    if args.subcommand == "doctor":
        return cmd_doctor(args)
    if args.subcommand == "stats":
        return cmd_stats(args)
    if args.subcommand == "report":
        return cmd_report(args)
    if args.subcommand == "dashboard":
        return cmd_dashboard(args)
    if args.subcommand == "statusline":
        return cmd_statusline()
    if args.subcommand == "watch":
        return cmd_watch()
    if args.subcommand == "skeleton":
        return cmd_skeleton(args)
    if args.subcommand == "symbol":
        return cmd_symbol(args)
    if args.subcommand == "repomap":
        return cmd_repomap(args)
    if args.subcommand == "search-sandbox":
        return cmd_search_sandbox(args)
    if args.subcommand == "exec":
        return cmd_exec(args)
    if args.subcommand == "mcp-server":
        return run_mcp_stdio_server()
    if args.subcommand == "hook":
        if args.event == "pre-tool-use":
            return pre_tool_use_stdio()
        if args.event == "post-tool-use":
            return post_tool_use_stdio()
        if args.event == "user-prompt-submit":
            return user_prompt_submit_stdio()
        if args.event == "pre-compact":
            return session_lifecycle_stdio(mode="pre-compact")
        if args.event == "session-reset":
            return session_lifecycle_stdio(mode="session-reset")

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
