"""
Executor wrapper `cato exec` (Parte 2 do CATO).
Executa comandos de terminal, aplica compressão determinística, salva o log bruto completo
para fallback seguro (Circuit Breaker) e contabiliza a economia de tokens no SQLite.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from cato.bash_optimizer.parsers import optimize_command_output
from cato.storage.db import CatoDB, estimate_tokens, get_logs_dir


def execute_and_optimize(
    command: str,
    session_id: Optional[str] = None,
    db: Optional[CatoDB] = None,
) -> int:
    """
    Executa `command` no shell do sistema, captura stdout/stderr combinados,
    comprime o resultado se necessário e preserva o código de saída original (`returncode`).
    """
    session = session_id or os.environ.get("CLAUDE_SESSION_ID", "default")
    db_instance = db or CatoDB()

    try:
        # No Windows, o Claude Code executa comandos da ferramenta Bash via Git Bash (/usr/bin/bash).
        # Usamos o Git Bash se presente para suportar caminhos POSIX (/c/Users/...), export, tail, etc.
        git_bash_candidates = [
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
            Path(r"C:\Program Files\Git\bin\bash.exe"),
        ]
        bash_exe = next((str(p) for p in git_bash_candidates if p.exists()), None)
        if os.name == "nt" and bash_exe:
            proc = subprocess.run(
                [bash_exe, "-c", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        else:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        exit_code = proc.returncode
        combined_output = proc.stdout
        if proc.stderr:
            if combined_output and not combined_output.endswith("\n"):
                combined_output += "\n"
            combined_output += proc.stderr
    except Exception as exc:
        sys.stderr.write(f"[CATO Exec Error] Falha ao executar comando: {exc}\n")
        return 1

    final_text = compress_and_record(command, combined_output, session, db_instance, exit_code=exit_code)
    if final_text is not None:
        sys.stdout.write(final_text + "\n")
    else:
        sys.stdout.write(combined_output)
        if combined_output and not combined_output.endswith("\n"):
            sys.stdout.write("\n")

    return exit_code


def native_output_limit() -> int:
    """Limite em caracteres a partir do qual o Claude Code troca a saída do Bash por arquivo + prévia."""
    try:
        return int(os.environ.get("BASH_MAX_OUTPUT_LENGTH") or 30_000)
    except ValueError:
        return 30_000


def compress_and_record(
    command: str,
    output: str,
    session_id: str,
    db: Optional[CatoDB],
    exit_code: int = 0,
    tool_name: str = "Bash",
) -> Optional[str]:
    """
    Comprime `output` e devolve o texto final (com rodapé e log bruto salvo em `~/.cato/logs/`),
    ou `None` quando não vale a pena: saída pequena, ganho < 15% (e < 80 tokens) ou saída acima do
    limite nativo — nesse caso o Claude Code já entrega só um arquivo + prévia, o que é menor.
    """
    raw_tokens = estimate_tokens(output)
    if raw_tokens < 160 or len(output) > native_output_limit():
        return None

    optimized_output, was_modified, strategy = optimize_command_output(command, output)
    optimized_tokens = estimate_tokens(optimized_output)
    if not was_modified or (raw_tokens - optimized_tokens < 80 and optimized_tokens > int(raw_tokens * 0.85)):
        return None

    db = db or CatoDB()
    log_hash = hashlib.sha256(f"{time.time()}:{command}".encode("utf-8")).hexdigest()[:8]
    log_id = f"cmd_{log_hash}"
    log_file: Path = get_logs_dir() / f"{log_id}.log"
    log_file.write_text(output, encoding="utf-8")

    footer = (
        f"\n[CATO #{log_id} ({strategy}): {raw_tokens} -> {optimized_tokens} tokens "
        f"(-{raw_tokens - optimized_tokens} tok). Log bruto completo: {log_file}]"
    )
    final_text = optimized_output + footer
    final_tokens = estimate_tokens(final_text)

    db.record_command_log(
        log_id=log_id,
        session_id=session_id,
        command=command,
        exit_code=exit_code,
        raw_log_path=str(log_file),
        raw_tokens=raw_tokens,
        compressed_tokens=final_tokens,
    )
    db.record_telemetry(
        session_id=session_id,
        event_type=f"bash_{strategy}",
        tool_name=tool_name,
        target=command,
        raw_tokens=raw_tokens,
        optimized_tokens=final_tokens,
    )
    return final_text
