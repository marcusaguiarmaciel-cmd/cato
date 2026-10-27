"""
Suíte de Testes Automatizados das Partes 1, 2 e 3 do CATO:
- Parte 1: Merge seguro de `.claude/settings.json`, instalação de subagente Haiku e `cato doctor`
- Parte 2: Compressão determinística de terminal (`pytest`, `tsc`, `git diff`, logs repetitivos, ANSI stripping)
- Parte 3: Deduplicação SHA-256 de `Read`, Esqueleto AST para Python e TypeScript, e Circuit Breaker
- Protocolo oficial de Hooks do Claude Code (`PreToolUse` JSON e `SessionStart` reset)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Adiciona `src` ao path
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cato.bash_optimizer.parsers import (
    compress_git_output,
    compress_linter_or_compiler_output,
    compress_test_output,
    optimize_command_output,
    strip_ansi_and_progress,
)
from cato.code_intel.ast_skeleton import generate_file_skeleton
from cato.code_intel.dedup import evaluate_read_request
from cato.code_intel.repomap import build_repository_map, extract_symbol_source
from cato.config_manager import build_merged_settings, install_cato
from cato.hooks.post_tool_use import handle_post_tool_use
from cato.hooks.pre_tool_use import handle_pre_tool_use
from cato.hooks.session_lifecycle import handle_pre_compact, handle_session_reset
from cato.mcp_server.server import handle_mcp_jsonrpc_message
from cato.storage.db import CatoDB
from cato.telemetry.dashboard import format_statusline_text, write_static_dashboard_html


class TestCatoSystem(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db = CatoDB(db_path=self.root / "test_cato.db")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    # =========================================================================
    # TESTES DA PARTE 1: Config Manager & Instalador
    # =========================================================================
    def test_part1_non_destructive_settings_merge(self) -> None:
        existing = {
            "model": "opus",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "rtk hook claude"}],
                    }
                ]
            },
            "statusLine": {"type": "command", "command": "node status.js"},
        }
        merged = build_merged_settings(existing)

        # Deve preservar configurações originais do usuário e o hook do RTK
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["statusLine"]["command"], "node status.js")
        self.assertEqual(merged["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(merged["env"]["CLAUDE_CODE_FORK_SUBAGENT"], "0")
        self.assertIn("Read(./node_modules/**)", merged["permissions"]["deny"])

        pre_hooks = merged["hooks"]["PreToolUse"]
        self.assertEqual(len(pre_hooks), 2)

        # Rodar build_merged_settings uma segunda vez deve ser 100% idempotente (não duplicar o hook CATO)
        merged_twice = build_merged_settings(merged)
        self.assertEqual(len(merged_twice["hooks"]["PreToolUse"]), 2)

    def test_part1_install_creates_agents_and_rules(self) -> None:
        project_dir = self.root / "my_project"
        project_dir.mkdir()
        res = install_cato(project_dir, is_global=False, dry_run=False)

        self.assertTrue((project_dir / ".claude" / "settings.json").exists())
        self.assertTrue((project_dir / ".claude" / "agents" / "cato-explorer.md").exists())
        self.assertTrue((project_dir / ".claude" / "rules" / "cato-token-efficiency.md").exists())
        self.assertGreaterEqual(len(res["actions"]), 3)

    # =========================================================================
    # TESTES DA PARTE 2: Bash Optimizer & Parsers
    # =========================================================================
    def test_part2_ansi_and_progress_stripping(self) -> None:
        noisy = "\x1b[32mDownloading...\x1b[0m\n10%\r50%\r100% Complete\n████████████████████\nDone!"
        cleaned = strip_ansi_and_progress(noisy)
        self.assertNotIn("\x1b[", cleaned)
        self.assertIn("100% Complete", cleaned)
        self.assertNotIn("10%", cleaned)
        self.assertIn("Done!", cleaned)

    def test_part2_pytest_output_compression(self) -> None:
        passing_lines = [f"tests/test_mod_{i}.py::test_func_{i} PASSED [ {i}%]" for i in range(1, 80)]
        failure_block = [
            "================================== FAILURES ===================================",
            "_______________________________ test_broken_auth ______________________________",
            "    def test_broken_auth():",
            ">       assert login('user') == 200",
            "E       AssertionError: assert 401 == 200",
            "========================== 1 failed, 79 passed in 2.41s =======================",
        ]
        raw_output = "\n".join(passing_lines + failure_block)
        compressed, modified, strategy = optimize_command_output("pytest -v", raw_output)

        self.assertTrue(modified)
        self.assertEqual(strategy, "test_suite_parser")
        self.assertNotIn("test_mod_5.py::test_func_5 PASSED", compressed)
        self.assertIn("AssertionError: assert 401 == 200", compressed)
        self.assertIn("1 failed, 79 passed", compressed)
        self.assertLess(len(compressed), len(raw_output) * 0.35)

    def test_part2_linter_and_git_diff_compression(self) -> None:
        # Linter com 60 erros TS2322 repetidos
        tsc_lines = [f"src/file_{i}.ts({i},5): error TS2322: Type 'string' is not assignable to 'number'." for i in range(1, 65)]
        comp_tsc, mod_tsc = compress_linter_or_compiler_output("tsc --noEmit", "\n".join(tsc_lines))
        self.assertTrue(mod_tsc)
        self.assertIn("TS2322: 64x", comp_tsc)

        # Git diff com package-lock.json gigante
        diff_lines = ["diff --git a/package-lock.json b/package-lock.json"] + [f"+  \"dep_{i}\": \"1.0.{i}\"," for i in range(150)]
        comp_diff, mod_diff = compress_git_output("git diff", "\n".join(diff_lines))
        self.assertTrue(mod_diff)
        self.assertIn("lockfile/bundle minificado omitido", comp_diff)

    # =========================================================================
    # TESTES DA PARTE 3: AST Skeletonizer, Deduplicação SHA-256 & Circuit Breaker
    # =========================================================================
    def test_part3_python_ast_skeleton_and_read_guard(self) -> None:
        py_file = self.root / "big_service.py"
        # Cria um arquivo Python de ~360 linhas
        code_blocks = [
            "import os",
            "from typing import List, Optional",
            "",
            "MAX_RETRIES = 5",
            "",
            "class PaymentProcessor:",
            '    """Processa pagamentos e estornos com segurança."""',
        ]
        for idx in range(1, 12):
            code_blocks.append(f"    def process_step_{idx}(self, amount: float, user_id: str) -> bool:")
            code_blocks.append(f'        """Executa a etapa {idx} do fluxo financeiro."""')
            for body_ln in range(1, 30):
                code_blocks.append(f"        temp_{body_ln} = amount * {body_ln} + len(user_id)")
            code_blocks.append("        return True")
            code_blocks.append("")

        py_file.write_text("\n".join(code_blocks), encoding="utf-8")

        # 1ª Leitura sem offset/limit (>300 linhas) -> Deve retornar o Esqueleto AST (allow=False com esqueleto no reason)
        d1 = evaluate_read_request(str(py_file), session_id="sess_1", skeleton_threshold=300, db=self.db)
        self.assertFalse(d1.allow)
        self.assertEqual(d1.event_type, "read_skeleton")
        self.assertIn("class PaymentProcessor:", d1.reason)
        self.assertIn("def process_step_1(self, amount: float, user_id: str) -> bool:", d1.reason)
        self.assertNotIn("temp_15 = amount * 15", d1.reason)
        self.assertLess(d1.optimized_tokens, d1.raw_tokens * 0.35)

        # 2ª Leitura seguida sem offset/limit -> Circuit Breaker DEVE liberar a leitura integral (allow=True)!
        d2 = evaluate_read_request(str(py_file), session_id="sess_1", skeleton_threshold=300, db=self.db)
        self.assertTrue(d2.allow)
        self.assertIn("Circuit Breaker", d2.additional_context or "")

        # 3ª Leitura seguida do mesmo arquivo inalterado -> Deduplicador SHA-256 DEVE bloquear releitura redundante!
        d3 = evaluate_read_request(str(py_file), session_id="sess_1", skeleton_threshold=300, db=self.db)
        self.assertFalse(d3.allow)
        self.assertEqual(d3.event_type, "read_dedup")
        self.assertIn("JÁ FOI LIDO nesta sessão", d3.reason)

        # Após reset de sessão (/compact ou /clear), a memória ativa é limpa e a leitura volta ao estado inicial
        handle_session_reset({"session_id": "sess_1"}, db=self.db)
        d4 = evaluate_read_request(str(py_file), session_id="sess_1", skeleton_threshold=300, db=self.db)
        self.assertFalse(d4.allow)
        self.assertEqual(d4.event_type, "read_skeleton")

    def test_part3_typescript_skeleton_extraction(self) -> None:
        ts_file = self.root / "UserController.ts"
        ts_lines = [
            "import { Request, Response } from 'express';",
            "",
            "export interface UserDTO {",
            "  id: string;",
            "  email: string;",
            "}",
            "",
            "export class UserController {",
            "  public async getUser(req: Request, res: Response): Promise<void> {",
        ] + [f"    const step{i} = req.params.id + '{i}';" for i in range(40)] + [
            "    res.json({ ok: true });",
            "  }",
            "}",
        ]
        ts_file.write_text("\n".join(ts_lines), encoding="utf-8")
        skel, total = generate_file_skeleton(ts_file)
        self.assertEqual(total, len(ts_lines))
        self.assertIn("export interface UserDTO", skel)
        self.assertIn("export class UserController", skel)
        self.assertIn("public async getUser", skel)
        self.assertNotIn("const step25", skel)

    def test_pre_tool_use_hook_protocol_for_grep_and_bash(self) -> None:
        # Grep sem head_limit -> deve injetar head_limit=60 via updatedInput
        grep_payload = {
            "session_id": "sess_hook",
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO"},
        }
        grep_resp = handle_pre_tool_use(grep_payload, db=self.db)
        self.assertIsNotNone(grep_resp)
        self.assertEqual(grep_resp["hookSpecificOutput"]["updatedInput"]["head_limit"], 60)

        # Bash com comando otimizável (desativando delegação RTK para testar o wrapper CATO)
        import os

        os.environ["CATO_DISABLE_RTK_CHAIN"] = "1"
        try:
            bash_payload = {
                "session_id": "sess_hook",
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/"},
            }
            bash_resp = handle_pre_tool_use(bash_payload, db=self.db)
            self.assertIsNotNone(bash_resp)
            new_cmd = bash_resp["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertIn("exec --session-id sess_hook --b64", new_cmd)
        finally:
            os.environ.pop("CATO_DISABLE_RTK_CHAIN", None)

    # =========================================================================
    # TESTES DA PARTE 4: RepoMap PageRank, Extração Cirúrgica de Símbolo & MCP
    # =========================================================================
    def test_part4_repomap_pagerank_and_surgical_symbol_extraction(self) -> None:
        repo_dir = self.root / "mini_repo"
        repo_dir.mkdir()
        (repo_dir / "models.py").write_text(
            "class Invoice:\n"
            "    def calculate_tax(self, rate: float) -> float:\n"
            "        return 100.0 * rate\n\n"
            "    def render_pdf(self) -> str:\n"
            "        return 'PDF'\n",
            encoding="utf-8",
        )
        (repo_dir / "service.py").write_text(
            "from models import Invoice\n\n"
            "def process_invoice() -> float:\n"
            "    inv = Invoice()\n"
            "    return inv.calculate_tax(0.15)\n",
            encoding="utf-8",
        )

        repo_map = build_repository_map(repo_dir, focus_query="Invoice", max_files_scanned=10)
        self.assertIn("models.py", repo_map)
        self.assertIn("class Invoice", repo_map)
        self.assertIn("calculate_tax", repo_map)

        # Extração cirúrgica de método específico ClassName.method_name
        snippet, start_line, end_line = extract_symbol_source(repo_dir / "models.py", "Invoice.calculate_tax")
        self.assertEqual(start_line, 2)
        self.assertGreaterEqual(end_line, 3)
        self.assertIn("def calculate_tax", snippet)
        self.assertNotIn("def render_pdf", snippet)

    def test_part4_mcp_server_jsonrpc_tools(self) -> None:
        list_resp = handle_mcp_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, db=self.db)
        self.assertIsNotNone(list_resp)
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        self.assertIn("cato_repo_map", tool_names)
        self.assertIn("cato_read_symbol", tool_names)
        self.assertIn("cato_search_sandbox", tool_names)
        self.assertIn("cato_token_savings", tool_names)

    # =========================================================================
    # TESTES DA PARTE 5: PostToolUse (Syntax Check + Virtualização FTS5 MCP)
    # =========================================================================
    def test_part5_post_tool_use_syntax_check_and_mcp_fts5_virtualization(self) -> None:
        # 1. Zero-Turn Syntax Check em arquivo Python com erro de sintaxe após Edit
        broken_py = self.root / "broken.py"
        broken_py.write_text("def broken_func(x, y)\n    return x + y\n", encoding="utf-8")
        edit_payload = {
            "session_id": "sess_p5",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(broken_py)},
            "tool_response": {"success": True},
        }
        edit_resp = handle_post_tool_use(edit_payload, db=self.db)
        self.assertIsNotNone(edit_resp)
        self.assertIn("CATO Zero-Turn Syntax Check", edit_resp["hookSpecificOutput"]["additionalContext"])

        # 2. Virtualização FTS5 de resposta gigante de ferramenta MCP (>1500 tokens)
        huge_mcp_text = "\n\n".join(
            [
                f"Issue #{i}: Authentication token refresh race condition in OAuth2 middleware module_{i} "
                + ("detailed stacktrace and diagnostic logs " * 15)
                for i in range(1, 50)
            ]
        )
        mcp_payload = {
            "session_id": "sess_p5",
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__github__list_issues",
            "tool_input": {"repo": "org/app"},
            "tool_response": {"content": [{"type": "text", "text": huge_mcp_text}]},
        }
        mcp_resp = handle_post_tool_use(mcp_payload, db=self.db)
        self.assertIsNotNone(mcp_resp)
        updated_out = mcp_resp["hookSpecificOutput"]["updatedMCPToolOutput"]
        self.assertIn("CATO MCP FTS5 Sandbox", updated_out)
        self.assertLess(len(updated_out), len(huge_mcp_text) * 0.25)

        # Extrai o sandbox_id do resumo gerado e busca via BM25 FTS5
        import re
        m = re.search(r"(sbx_[0-9a-f]+)", updated_out)
        self.assertIsNotNone(m)
        sbx_id = m.group(1)
        hits = self.db.search_sandbox(sbx_id, "OAuth2 module_42", limit=3)
        self.assertGreaterEqual(len(hits), 1)
        self.assertIn("module_42", hits[0]["content"])

    # =========================================================================
    # TESTES DA PARTE 6: PreCompact State Recovery & Dashboard HTML / Statusline
    # =========================================================================
    def test_part6_precompact_recovery_and_dashboard_generation(self) -> None:
        # Registra eventos de economia para testar o Dashboard e a Statusline
        self.db.record_telemetry(
            session_id="sess_dash",
            event_type="read_skeleton",
            tool_name="Read",
            target="src/huge.py",
            raw_tokens=8500,
            optimized_tokens=1200,
        )
        self.db.record_telemetry(
            session_id="sess_dash",
            event_type="bash_compress",
            tool_name="Bash",
            target="pytest",
            raw_tokens=6000,
            optimized_tokens=450,
        )

        # Testa geração do Statusline 1-linha
        status_str = format_statusline_text(db=self.db)
        self.assertIn("CATO Hoje:", status_str)
        self.assertIn("Geral:", status_str)
        self.assertIn("poupados", status_str)

        # Testa exportação do Dashboard HTML visual
        out_path = write_static_dashboard_html(db=self.db)
        self.assertTrue(out_path.exists())
        html_content = out_path.read_text(encoding="utf-8")
        self.assertIn("CATO — Painel de Economia de Tokens", html_content)
        self.assertIn("read_skeleton", html_content)
        self.assertIn("bash_compress", html_content)

        # Testa ciclo PreCompact -> SessionStart (source=compact)
        handle_pre_compact({"session_id": "sess_dash", "cwd": str(self.root)}, db=self.db)
        compact_resp = handle_session_reset({"session_id": "sess_dash", "source": "compact"}, db=self.db)
        self.assertIsNotNone(compact_resp["additionalContext"])
        self.assertIn("CATO Post-Compaction State Recovery", compact_resp["additionalContext"])


if __name__ == "__main__":
    unittest.main()
