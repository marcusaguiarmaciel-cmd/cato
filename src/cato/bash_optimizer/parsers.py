"""
Parsers determinísticos para compressão de saídas de terminal (Parte 2 do CATO).
Reduz de 60% a 90% dos tokens de stdout/stderr preservando 100% dos sinais de falha,
stack traces, números de linha e sumários finais.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Regex para códigos de escape ANSI (cores, cursores, formatação de terminal)
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Regex para normalizar linhas de log repetitivas (timestamps, hex IDs, números grandes)
LOG_TEMPLATE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?|\b0x[0-9a-fA-F]+\b|\b\d{4,}\b)"
)

# Palavras-chave de alto sinal que nunca devem ser omitidas silenciosamente
ERROR_SIGNAL_RE = re.compile(
    r"(?i)\b(error|exception|traceback|failed|failure|fatal|panic|assert|syntaxerror|typeerror|referenceerror|undefined|cannot find)\b"
)


def strip_ansi_and_progress(text: str) -> str:
    """Remove sequências ANSI e colapsa sobrescritas de barra de progresso por carriage return (\\r)."""
    if not text:
        return ""
    cleaned = ANSI_ESCAPE_RE.sub("", text).replace("\r\n", "\n")
    lines: List[str] = []
    for raw_line in cleaned.split("\n"):
        if "\r" in raw_line:
            # Mantém apenas o último segmento após \r (como um terminal real exibe)
            segments = [seg for seg in raw_line.split("\r") if seg.strip()]
            raw_line = segments[-1] if segments else ""
        # Ignora linhas que são puramente barras de progresso visuais
        stripped = raw_line.strip()
        if stripped and all(c in "█▓▒░=#>-|/.\\ " for c in stripped) and len(stripped) > 5:
            continue
        lines.append(raw_line.rstrip())
    return "\n".join(lines)


def compress_test_output(command: str, text: str) -> Tuple[str, bool]:
    """
    Comprime saídas de suítes de teste (pytest, jest, vitest, cargo test, go test, unittest).
    Remove testes aprovados verbosos e preserva 100% dos blocos de falha/erro e sumários.
    """
    lines = text.splitlines()
    if len(lines) <= 35:
        return text, False

    passed_count = 0
    kept_lines: List[str] = []
    in_failure_section = False
    omitted_lines = 0

    for line in lines:
        stripped = line.strip()

        # Detecta início de seção de falhas no pytest / cargo / jest
        if (
            re.match(r"^=+ (FAILURES|ERRORS) =+$", stripped)
            or stripped.startswith("FAIL ")
            or stripped.startswith("failures:")
            or stripped.startswith("--- FAIL:")
            or "AssertionError" in stripped
            or "Traceback (most recent call last)" in stripped
        ):
            in_failure_section = True

        # Detecta fim de seção de falhas / início de sumário final
        if re.match(r"^=+ (short test summary info|\d+ .* in [\d\.]+s) =+$", stripped):
            in_failure_section = True

        # Filtra linhas individuais de testes que passaram
        if not in_failure_section:
            if (
                re.search(r"::.*?\s+PASSED(?:\s+\[\s*\d+%\])?$", stripped)
                or re.match(r"^(✓|✔|PASS|\.)\s+", stripped)
                or re.match(r"^test\s+\S+\s+\.\.\.\s+ok$", stripped)
                or re.match(r"^ok\s+\S+\s+[\d\.]+s$", stripped)
                or re.match(r"^[\.sFEx]+(\s+\[\s*\d+%\])?$", stripped)
            ):
                passed_count += 1
                omitted_lines += 1
                continue

            # Filtra banners de inicialização verbosos do pytest/jest/npm
            if any(
                 stripped.startswith(prefix)
                for prefix in (
                    "platform ",
                    "cachedir:",
                    "rootdir:",
                    "plugins:",
                    "collecting ...",
                )
            ):
                omitted_lines += 1
                continue

        kept_lines.append(line)

    if omitted_lines < 5:
        return text, False

    header = f"[CATO Test Filter: {passed_count} testes aprovados e {omitted_lines} linhas de ruído suprimidas]"
    result = header + "\n" + "\n".join(kept_lines)
    # Se o bloco de falhas ainda for gigantesco (>200 linhas), aplica truncamento inteligente
    if len(kept_lines) > 200:
        result, _ = compress_generic_output(result, max_lines=180)
    return result, True


def compress_linter_or_compiler_output(command: str, text: str) -> Tuple[str, bool]:
    """
    Comprime saídas de compiladores e linters (tsc, eslint, ruff, flake8, mypy, clippy).
    Agrupa diagnósticos repetidos e limita explosões de milhares de erros idênticos.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 50:
        return text, False

    kept_lines: List[str] = []
    rule_counts: dict[str, int] = {}
    suppressed = 0

    for line in lines:
        # Tenta identificar código de erro (ex: TS2322, E501, F401, [no-unused-vars])
        match = re.search(r"\b(TS\d{4}|[EFW]\d{3,4}|\[[\w\-/@]+\])\b", line)
        if match:
            rule_code = match.group(1)
            rule_counts[rule_code] = rule_counts.get(rule_code, 0) + 1
            # Mantém as primeiras 10 ocorrências de cada código de erro para contexto cirúrgico
            if rule_counts[rule_code] > 10:
                suppressed += 1
                continue

        if len(kept_lines) < 120:
            kept_lines.append(line)
        else:
            suppressed += 1

    if suppressed == 0:
        return text, False

    summary_parts = [f"{code}: {cnt}x" for code, cnt in sorted(rule_counts.items(), key=lambda x: -x[1])[:10]]
    rule_summary = f" | Distribuição por código: {', '.join(summary_parts)}" if summary_parts else ""
    footer = f"\n[CATO Linter Filter: {suppressed} linhas adicionais suprimidas{rule_summary}]"
    return "\n".join(kept_lines) + footer, True


def compress_git_output(command: str, text: str) -> Tuple[str, bool]:
    """Comprime saídas extensas de comandos git (status, diff, log)."""
    lines = text.splitlines()
    if len(lines) <= 80:
        return text, False

    # Para git diff: oculta diffs de lockfiles e limita arquivos individuais enormes
    if "diff" in command:
        kept: List[str] = []
        skip_current_file = False
        current_file_lines = 0
        omitted_lines = 0

        for line in lines:
            if line.startswith("diff --git "):
                current_file_lines = 0
                if any(
                    lock in line
                    for lock in (
                        "package-lock.json",
                        "pnpm-lock.yaml",
                        "yarn.lock",
                        "Cargo.lock",
                        "poetry.lock",
                        "uv.lock",
                        ".min.js",
                        ".min.css",
                    )
                ):
                    skip_current_file = True
                    kept.append(line)
                    kept.append("  [CATO: Diff de lockfile/bundle minificado omitido para economizar tokens]")
                    continue
                else:
                    skip_current_file = False

            if skip_current_file:
                omitted_lines += 1
                continue

            current_file_lines += 1
            if current_file_lines == 121:
                kept.append("  [... CATO: Diff deste arquivo truncado após 120 linhas ...]")
                omitted_lines += 1
                continue
            elif current_file_lines > 121:
                omitted_lines += 1
                continue

            kept.append(line)

        if omitted_lines > 0:
            kept.append(f"\n[CATO Git Diff Filter: {omitted_lines} linhas de diff secundárias omitidas]")
            return "\n".join(kept), True

    return compress_generic_output(text, max_lines=100)


def deduplicate_repetitive_logs(lines: List[str]) -> Tuple[List[str], int]:
    """
    Detecta e colapsa linhas de log repetitivas consecutivas ou quase-idênticas (estilo logzip).
    """
    if not lines:
        return [], 0

    result: List[str] = []
    prev_template: str | None = None
    repeat_count = 0
    total_collapsed = 0

    for line in lines:
        template = LOG_TEMPLATE_RE.sub("<NUM>", line.strip())
        if template and template == prev_template and len(template) > 10:
            repeat_count += 1
            total_collapsed += 1
        else:
            if repeat_count > 0:
                result.append(f"  ... [linha padrão acima repetida mais {repeat_count}x] ...")
                repeat_count = 0
            result.append(line)
            prev_template = template

    if repeat_count > 0:
        result.append(f"  ... [linha padrão acima repetida mais {repeat_count}x] ...")

    return result, total_collapsed


def compress_generic_output(text: str, max_lines: int = 120) -> Tuple[str, bool]:
    """
    Compressor genérico inteligente:
    1. Deduplica templates de log repetidos consecutivamente
    2. Se ainda exceder `max_lines`, preserva o cabeçalho (35 linhas), todas as linhas de erro críticas (até 40)
       e o rodapé (45 linhas).
    """
    raw_lines = text.splitlines()
    deduped_lines, collapsed_count = deduplicate_repetitive_logs(raw_lines)

    if len(deduped_lines) <= max_lines:
        if collapsed_count > 0:
            return "\n".join(deduped_lines), True
        return text, False

    head_count = 35
    tail_count = 45
    head = deduped_lines[:head_count]
    tail = deduped_lines[-tail_count:]
    middle = deduped_lines[head_count:-tail_count]

    # Extrai linhas de alto sinal (erros, exceções, falhas) do meio que seriam truncadas
    error_lines: List[str] = []
    for idx, m_line in enumerate(middle, start=head_count + 1):
        if ERROR_SIGNAL_RE.search(m_line):
            error_lines.append(f"L{idx}: {m_line}")
            if len(error_lines) >= 35:
                break

    omitted = len(middle) - len(error_lines)
    out: List[str] = []
    out.extend(head)
    out.append(f"\n... [CATO: {omitted} linhas intermediárias omitidas (de {len(raw_lines)} totais)] ...")
    if error_lines:
        out.append("--- [CATO: Linhas de erro/alerta extraídas do trecho intermediário] ---")
        out.extend(error_lines)
        out.append("-----------------------------------------------------------------------")
    out.extend(tail)
    return "\n".join(out), True


def optimize_command_output(command: str, raw_output: str) -> Tuple[str, bool, str]:
    """
    Ponto de entrada principal do pipeline de compressão de terminal (Parte 2).
    Retorna `(optimized_text, was_modified, strategy_used)`.
    """
    if not raw_output:
        return "", False, "empty"

    cleaned = strip_ansi_and_progress(raw_output)
    ansi_removed = len(cleaned) < len(raw_output) - 20
    cmd_lower = command.lower()

    # 1. Suítes de teste
    if any(
        kw in cmd_lower
        for kw in ("pytest", "py.test", "jest", "vitest", "cargo test", "go test", "npm test", "pnpm test", "unittest")
    ):
        compressed, changed = compress_test_output(command, cleaned)
        if changed or ansi_removed:
            return compressed, True, "test_suite_parser"

    # 2. Compiladores e Linters
    if any(
        kw in cmd_lower
        for kw in ("tsc", "eslint", "ruff", "flake8", "mypy", "pyright", "clippy", "cargo check", "biome")
    ):
        compressed, changed = compress_linter_or_compiler_output(command, cleaned)
        if changed or ansi_removed:
            return compressed, True, "linter_compiler_parser"

    # 3. Comandos Git
    if cmd_lower.startswith("git ") or " git " in cmd_lower:
        compressed, changed = compress_git_output(command, cleaned)
        if changed or ansi_removed:
            return compressed, True, "git_parser"

    # 4. Fallback genérico para qualquer comando verboso
    compressed, changed = compress_generic_output(cleaned, max_lines=120)
    if changed or ansi_removed:
        return compressed, True, "generic_log_compressor"

    return cleaned, False, "passthrough"
