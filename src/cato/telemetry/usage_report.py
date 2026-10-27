"""
Relatório de uso REAL a partir dos transcripts do Claude Code (`~/.claude/projects/**/*.jsonl`).

Tudo aqui vem de campos `usage` faturados pela API e dos tool results gravados no transcript;
nada é contrafactual. Regras de contagem:
- cada chamada de API conta uma vez (dedup por `message.id`; o Claude Code grava várias linhas
  por resposta e sessões retomadas/bifurcadas copiam o histórico para outros arquivos);
- respostas `<synthetic>` são ignoradas;
- tool results são deduplicados por `tool_use_id`;
- custo = preços oficiais de `cato.telemetry.pricing` (custo equivalente de API).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import struct
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from cato.telemetry.pricing import usage_cost

# Uma escrita de cache acima disso numa única chamada é uma reescrita do prefixo (cache miss)
CACHE_MISS_THRESHOLD = 100_000
CONTEXT_BUCKETS = (100_000, 200_000, 400_000, 600_000, 800_000)
CHARS_PER_TOKEN = 3.5
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
COST_KEYS = ("cache_read", "cache_write_1h", "cache_write_5m", "output", "input")

# Marcas que só aparecem em saídas REAIS do CATO (não em quem lê o código-fonte do CATO)
CATO_OUTPUT_RE = re.compile(
    r"\[CATO (#cmd_[0-9a-f]{8} \(|bash \d+ -> \d+ tok|MCP FTS5 Sandbox #sbx_[0-9a-f]{8}|AST Skeleton Guard — Economia de \d"
    r"|Deduplication Guard\] O arquivo `[^`{]+`)"
)


def claude_projects_dir() -> Path:
    override = os.environ.get("CATO_CLAUDE_PROJECTS_DIR")
    return Path(override) if override else Path.home() / ".claude" / "projects"


def parse_timestamp(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def context_tokens(usage: Dict[str, Any]) -> int:
    """Tamanho do prompt enviado nesta chamada (o que é relido a cada turno)."""
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )


def is_rtk_command(command: str) -> bool:
    try:
        first = shlex.split(command.strip(), posix=True)[0]
    except Exception:
        first = command.strip().split(" ", 1)[0] if command.strip() else ""
    return Path(first.replace("\\", "/")).name.lower() in ("rtk", "rtk.exe")


def image_tokens(b64_data: str) -> int:
    """Tokens de uma imagem pela dimensão real (`w*h/750`, teto de 1.600 após o redimensionamento da API)."""
    try:
        head = base64.b64decode(b64_data[:64] + "=" * (-len(b64_data[:64]) % 4))
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", head[16:24])
            return min(1600, width * height // 750)
        if head[:2] == b"\xff\xd8":
            raw = base64.b64decode(b64_data)
            i = 2
            while i + 9 < len(raw) and raw[i] == 0xFF:
                marker = raw[i + 1]
                seg_len = struct.unpack(">H", raw[i + 2 : i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    height, width = struct.unpack(">HH", raw[i + 5 : i + 9])
                    return min(1600, width * height // 750)
                i += 2 + seg_len
    except Exception:
        pass
    return 1600


def _result_tokens(content: Any) -> Tuple[int, int]:
    """Retorna `(tokens_de_texto, tokens_de_imagem)` de um tool_result."""
    if isinstance(content, str):
        return int(len(content) / CHARS_PER_TOKEN), 0
    text_tokens = image_tok = 0
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image":
            image_tok += image_tokens(str((item.get("source") or {}).get("data", "")))
        else:
            text_tokens += int(len(str(item.get("text", ""))) / CHARS_PER_TOKEN)
    return text_tokens, image_tok


def command_key(command: str) -> str:
    """Normaliza um comando para agrupamento (tira `cd ... &&`, caminho do python, etc.)."""
    cmd = re.sub(r"^(cd\s+[^&;]+(&&|;)\s*)+", "", command.strip())
    cmd = re.sub(r"^\S*python[0-9.]*(\.exe)?\"?\s+(-\S+\s+)*", "python ", cmd)
    return " ".join(cmd.split()[:2])[:40]


_SED_RANGE_RE = re.compile(r"^sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+(\S+)\s*$")


def code_read_key(tool: str, tool_input: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Identifica leituras de código (`Read`, `sed -n a,bp`, `cat`) como `(arquivo, faixa)`."""
    if tool == "Read":
        path = tool_input.get("file_path")
        if path:
            return str(path).replace("\\", "/").lower(), f"{tool_input.get('offset')}:{tool_input.get('limit')}"
        return None
    if tool not in SHELL_TOOLS:
        return None
    cmd = re.sub(r"^(cd\s+[^&;]+(&&|;)\s*)+", "", str(tool_input.get("command", "")).strip())
    m = _SED_RANGE_RE.match(cmd)
    if m:
        return m.group(3).strip("'\"").lower(), f"{m.group(1)}-{m.group(2)}"
    parts = cmd.split()
    if len(parts) == 2 and parts[0] == "cat":
        return parts[1].strip("'\"").lower(), "all"
    return None


def last_turn_state(transcript_path: str, tail_bytes: int = 4_000_000) -> Optional[Dict[str, Any]]:
    """
    Lê só o final do transcript e devolve o estado da última chamada de API real:
    `{timestamp, model, context, ttl_seconds, usage}`. O TTL vem do último `usage` que gravou cache
    (1h se houve `ephemeral_1h`, 5 min se só `ephemeral_5m`; 1h se não houver evidência).
    """
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - tail_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None

    state: Optional[Dict[str, Any]] = None
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") or {}
        if entry.get("type") != "assistant" or not usage or message.get("model") == "<synthetic>":
            continue
        if state is None:
            state = {
                "timestamp": parse_timestamp(entry.get("timestamp")),
                "model": message.get("model") or "?",
                "context": context_tokens(usage),
                "ttl_seconds": None,
                "usage": usage,
            }
        creation = usage.get("cache_creation") or {}
        if creation.get("ephemeral_1h_input_tokens"):
            state["ttl_seconds"] = 3600
            break
        if creation.get("ephemeral_5m_input_tokens"):
            state["ttl_seconds"] = 300
            break
    if state is not None and state["ttl_seconds"] is None:
        state["ttl_seconds"] = 3600
    return state


def iter_transcripts(project: Optional[str] = None) -> Iterator[Path]:
    root = claude_projects_dir()
    if not root.exists():
        return
    for path in sorted(root.rglob("*.jsonl")):
        if project and project.lower() not in str(path.parent).lower():
            continue
        yield path


def _bucket_label(ctx: int) -> str:
    for limit in CONTEXT_BUCKETS:
        if ctx <= limit:
            return f"<= {limit // 1000}k"
    return f"> {CONTEXT_BUCKETS[-1] // 1000}k"


def build_report(
    days: Optional[float] = None,
    project: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, Any]:
    """Varre os transcripts e devolve o relatório real (tokens, custo, misses, ferramentas, atribuição)."""
    if days is not None and since is None:
        since = time.time() - days * 86400

    def in_window(ts: Optional[float]) -> bool:
        if ts is None:
            return since is None and until is None
        return (since is None or ts >= since) and (until is None or ts < until)

    seen_messages: set = set()
    seen_results: set = set()
    tokens: Counter = Counter()
    cost: Counter = Counter()
    cost_by_model: Counter = Counter()
    buckets: Dict[str, Counter] = defaultdict(Counter)
    misses: Dict[str, Counter] = defaultdict(Counter)
    tools: Dict[str, Counter] = defaultdict(Counter)
    shell_cmds: Dict[str, Counter] = defaultdict(Counter)
    redundant: Counter = Counter()
    daily: Dict[str, Counter] = defaultdict(Counter)
    cato_hits: Counter = Counter()
    files = 0

    for path in iter_transcripts(project):
        files += 1
        calls: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        reads_seen: Dict[str, set] = defaultdict(set)
        prev_ts: Optional[float] = None
        prev_model: Optional[str] = None
        compact_pending = False
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                kind = entry.get("type")
                if kind == "system" and entry.get("subtype") == "compact_boundary":
                    compact_pending = True
                    continue
                message = entry.get("message") or {}
                ts = parse_timestamp(entry.get("timestamp"))

                if kind == "assistant":
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name, tool_input = block.get("name", "?"), block.get("input") or {}
                            calls[block.get("id")] = (name, tool_input)
                            if name in EDIT_TOOLS and tool_input.get("file_path"):
                                reads_seen.pop(str(tool_input["file_path"]).replace("\\", "/").lower(), None)
                    model = message.get("model") or "?"
                    usage = message.get("usage") or {}
                    msg_id = message.get("id")
                    if model == "<synthetic>" or not usage:
                        continue
                    # Chamada já contada em outro arquivo (sessão retomada): só avança a linha do tempo
                    already_counted = msg_id in seen_messages
                    seen_messages.add(msg_id)
                    if in_window(ts) and not already_counted:
                        ctx = context_tokens(usage)
                        parts = usage_cost(model, usage)
                        call_cost = sum(parts.values())
                        cost.update(parts)
                        cost_by_model[model] += call_cost
                        tokens["calls"] += 1
                        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                            tokens[key] += int(usage.get(key) or 0)
                        bucket = buckets[_bucket_label(ctx)]
                        bucket["calls"] += 1
                        bucket["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                        bucket["cost"] += call_cost
                        written = int(usage.get("cache_creation_input_tokens") or 0)
                        if written > CACHE_MISS_THRESHOLD:
                            gap = (ts - prev_ts) / 60 if (ts and prev_ts) else None
                            if prev_ts is None:
                                cause = "início/retomada de sessão"
                            elif compact_pending:
                                cause = "após compactação"
                            elif model != prev_model:
                                cause = "troca de modelo"
                            elif gap is not None and gap > 60:
                                cause = "ocioso > 60 min (TTL 1h expirou)"
                            elif gap is not None and gap > 5:
                                cause = "ocioso 5-60 min"
                            else:
                                cause = "prefixo mudou sem pausa"
                            misses[cause]["count"] += 1
                            misses[cause]["tokens"] += written
                            misses[cause]["cost"] += parts["cache_write_5m"] + parts["cache_write_1h"]
                        if ts:
                            day = daily[datetime.fromtimestamp(ts).date().isoformat()]
                            day["calls"] += 1
                            day["cost"] += call_cost
                            day["ctx_sum"] += ctx
                    prev_ts, prev_model, compact_pending = ts or prev_ts, model, False

                elif kind == "user" and isinstance(message.get("content"), list):
                    for block in message["content"]:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        use_id = block.get("tool_use_id")
                        name, tool_input = calls.get(use_id, ("?", {}))
                        content = block.get("content")
                        if use_id in seen_results or not in_window(ts):
                            continue
                        seen_results.add(use_id)
                        if CATO_OUTPUT_RE.search(str(content)[:20000]):
                            cato_hits[name] += 1
                        text_tok, img_tok = _result_tokens(content)
                        label = "__".join(name.split("__")[:2]) if name.startswith("mcp__") else name
                        tools[label]["calls"] += 1
                        tools[label]["tokens"] += text_tok
                        tools[label]["image_tokens"] += img_tok
                        if name in SHELL_TOOLS:
                            command = str(tool_input.get("command", ""))
                            if is_rtk_command(command):
                                rest = command.strip().split(" ", 1)[1] if " " in command.strip() else ""
                                if command.strip().startswith('"'):
                                    rest = command.strip().split('"', 2)[-1].strip()
                                key = "rtk " + command_key(rest)
                            else:
                                key = command_key(command)
                            shell_cmds[key]["calls"] += 1
                            shell_cmds[key]["tokens"] += text_tok
                        read_key = code_read_key(name, tool_input)
                        if read_key:
                            file_key, span = read_key
                            if span in reads_seen[file_key]:
                                redundant["count"] += 1
                                redundant["tokens"] += text_tok
                            reads_seen[file_key].add(span)

    total_cost = sum(cost.values())
    return {
        "generated_at": time.time(),
        "window": {"since": since, "until": until, "project": project},
        "files": files,
        "calls": tokens["calls"],
        "tokens": {k: v for k, v in tokens.items() if k != "calls"},
        "cost": {k: cost.get(k, 0.0) for k in COST_KEYS},
        "total_cost": total_cost,
        "cost_by_model": dict(cost_by_model.most_common()),
        "context_buckets": [
            {"bucket": label, **buckets[label]}
            for label in [f"<= {b // 1000}k" for b in CONTEXT_BUCKETS] + [f"> {CONTEXT_BUCKETS[-1] // 1000}k"]
            if label in buckets
        ],
        "cache_misses": {cause: dict(v) for cause, v in sorted(misses.items(), key=lambda kv: -kv[1]["cost"])},
        "tools": {name: dict(v) for name, v in sorted(tools.items(), key=lambda kv: -(kv[1]["tokens"] + kv[1]["image_tokens"]))},
        "top_shell_commands": [
            {"command": k, **v} for k, v in sorted(shell_cmds.items(), key=lambda kv: -kv[1]["tokens"])[:15]
        ],
        "redundant_code_reads": dict(redundant),
        "cato_outputs": dict(cato_hits),
        "daily": {day: dict(v) for day, v in sorted(daily.items())},
    }


def attribute_by_days(report: Dict[str, Any], active_days: Iterable[str]) -> Dict[str, Dict[str, float]]:
    """
    Compara dias em que uma ferramenta atuou com dias em que não atuou (custo por chamada e contexto médio).
    É observacional: tarefas diferentes em dias diferentes. Serve para detectar efeitos grandes, não 5%.
    """
    active = set(active_days)

    def summarize(days: List[Dict[str, Any]]) -> Dict[str, float]:
        calls = sum(d.get("calls", 0) for d in days)
        spent = sum(d.get("cost", 0.0) for d in days)
        return {
            "days": len(days),
            "calls": calls,
            "cost": spent,
            "cost_per_call": spent / calls if calls else 0.0,
            "avg_context": sum(d.get("ctx_sum", 0) for d in days) / calls if calls else 0.0,
        }

    daily = report.get("daily", {})
    return {
        "active": summarize([v for k, v in daily.items() if k in active]),
        "inactive": summarize([v for k, v in daily.items() if k not in active]),
    }


def recommendations_from_report(report: Dict[str, Any]) -> List[str]:
    """Recomendações concretas derivadas dos números medidos (só sugere; nada é aplicado)."""
    total = report.get("total_cost") or 0.0
    if total <= 0:
        return []
    recs: List[str] = []
    big_ctx = sum(b["cost"] for b in report.get("context_buckets", []) if b["bucket"].startswith(">") or b["bucket"] in ("<= 600k", "<= 800k"))
    if big_ctx / total > 0.25:
        recs.append(
            f"{100 * big_ctx / total:.0f}% do custo ({_usd(big_ctx)}) foi gasto com contexto acima de 400k tokens. "
            "Use /clear entre tarefas e /compact quando o guard avisar; delegue buscas amplas a subagentes."
        )
    misses = report.get("cache_misses", {})
    idle = misses.get("ocioso > 60 min (TTL 1h expirou)")
    if idle and idle["cost"] / total > 0.03:
        recs.append(
            f"Reescritas de cache por pausa > 1h custaram {_usd(idle['cost'])} ({idle['count']}x). "
            "Ao voltar de uma pausa longa, prefira /clear ou /compact a continuar com contexto grande."
        )
    switch = misses.get("troca de modelo")
    if switch and switch["cost"] / total > 0.02:
        recs.append(
            f"Trocar de modelo no meio da sessão invalidou o cache {switch['count']}x ({_usd(switch['cost'])}). "
            "Escolha o modelo no início ou use um subagente com outro modelo."
        )
    output_cost = report.get("cost", {}).get("output", 0.0)
    if output_cost / total > 0.10:
        recs.append(
            f"Output custou {_usd(output_cost)} ({100 * output_cost / total:.0f}%). `effortLevel` alto gera muito "
            "raciocínio; use `high`/`medium` em tarefas rotineiras."
        )
    red = report.get("redundant_code_reads", {})
    if red.get("tokens", 0) < 0.001 * max(1, report.get("tokens", {}).get("cache_read_input_tokens", 0)):
        recs.append(
            "Releituras idênticas de arquivo são desprezíveis (o Claude Code já deduplica): mantenha o Read guard "
            "do CATO desligado (padrão)."
        )
    return recs


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def format_report_text(report: Dict[str, Any], rtk_claimed: Optional[Dict[str, Any]] = None) -> str:
    """Relatório legível para terminal."""
    total = report["total_cost"] or 1e-9
    tk = report["tokens"]
    lines: List[str] = []
    bar = "=" * 72
    lines += [bar, "  CATO REPORT — uso real dos transcripts do Claude Code (custo equivalente de API)", bar]
    since = report["window"]["since"]
    if since:
        lines.append(f"  Janela: desde {datetime.fromtimestamp(since).strftime('%d/%m/%Y %H:%M')}")
    lines.append(f"  Transcripts: {report['files']} | chamadas de API: {report['calls']:,}")
    lines.append(f"  CUSTO TOTAL: {_usd(report['total_cost'])}")
    labels = {
        "cache_read": "Releitura de contexto (cache read)",
        "cache_write_1h": "Escrita de cache 1h",
        "cache_write_5m": "Escrita de cache 5min",
        "output": "Saída (output)",
        "input": "Entrada sem cache",
    }
    for key in COST_KEYS:
        lines.append(f"    {labels[key]:38}{_usd(report['cost'][key]):>12}  {100 * report['cost'][key] / total:5.1f}%")
    lines.append(
        f"  Tokens: cache read {tk.get('cache_read_input_tokens', 0):,} | cache write "
        f"{tk.get('cache_creation_input_tokens', 0):,} | output {tk.get('output_tokens', 0):,}"
    )

    lines += ["", "  CUSTO POR TAMANHO DE CONTEXTO (o contexto é relido a cada turno)"]
    for b in report["context_buckets"]:
        lines.append(f"    {b['bucket']:>8}: {b['calls']:6,} chamadas  {_usd(b['cost']):>11}  {100 * b['cost'] / total:5.1f}%")

    lines += ["", "  CACHE MISSES (reescrita > 100k tokens numa chamada)"]
    if not report["cache_misses"]:
        lines.append("    nenhum")
    for cause, v in report["cache_misses"].items():
        lines.append(f"    {cause:34}{v['count']:4}x {v['tokens']:>13,} tok {_usd(v['cost']):>10}")

    lines += ["", "  TOKENS QUE ENTRARAM NO CONTEXTO POR FERRAMENTA (texto + imagem)"]
    for name, v in list(report["tools"].items())[:10]:
        lines.append(f"    {name:30}{v['calls']:6} chamadas {v['tokens'] + v['image_tokens']:>11,} tok")

    lines += ["", "  COMANDOS DE SHELL QUE MAIS GERARAM SAÍDA"]
    for c in report["top_shell_commands"][:10]:
        lines.append(f"    {c['command']:40}{c['calls']:6}x {c['tokens']:>10,} tok")
    red = report["redundant_code_reads"]
    lines.append(
        f"  Releituras de código idênticas sem edição no meio: {red.get('count', 0)}x, {red.get('tokens', 0):,} tok"
    )

    outputs = report.get("cato_outputs") or {}
    lines.append(f"  Saídas reais do CATO no transcript: {sum(outputs.values())} ({', '.join(f'{k}: {v}' for k, v in outputs.items()) or '-'})")

    attributions = report.get("attributions") or {}
    if attributions:
        lines += ["", "  ATRIBUIÇÃO POR DIA (observacional: tarefas diferentes em dias diferentes — não é A/B)"]
        for tool, att in attributions.items():
            for label, key in (("ativo", "active"), ("inativo", "inactive")):
                g = att[key]
                lines.append(
                    f"    {tool:4} {label:8} {g['days']:3} dias {g['calls']:6,} chamadas  {_usd(g['cost_per_call'])}/chamada  "
                    f"contexto médio {int(g['avg_context']):,}"
                )
    if rtk_claimed and rtk_claimed.get("available"):
        lines.append(
            f"    RTK declara (rtk gain): {rtk_claimed.get('saved_tokens', 0):,} tokens poupados em "
            f"{rtk_claimed.get('total_commands', 0):,} comandos (bytes/4, antes do truncamento nativo do Claude Code)"
        )
    recs = recommendations_from_report(report)
    if recs:
        lines += ["", "  O QUE MAIS ECONOMIZA NO SEU USO"]
        lines += [f"  • {rec}" for rec in recs]
    lines.append(bar)
    return "\n".join(lines)
