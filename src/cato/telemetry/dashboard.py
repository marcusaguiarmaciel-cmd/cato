"""
Painel de custo REAL (`cato dashboard`), `cato stats` e StatusLine (`cato statusline`) — Parte 6 do CATO.

Os números vêm de `cato.telemetry.usage_report` (campos `usage` faturados nos transcripts do Claude Code)
e da tabela oficial de preços. A economia declarada por CATO/RTK aparece separada e rotulada como
"declarada", nunca somada ao custo medido.
"""

from __future__ import annotations

import datetime
import html
import json
import shutil
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from cato.storage.db import CatoDB, get_cato_home
from cato.telemetry.pricing import price_for
from cato.telemetry.usage_report import (
    attribute_by_days,
    build_report,
    format_report_text,
    iter_transcripts,
    last_turn_state,
)

REPORT_CACHE_FILE = "report_cache.json"
DEFAULT_REPORT_DAYS = 30
STATUSLINE_MAX_REPORT_AGE_MIN = 180

_RTK_MEMO: Optional[Dict[str, Any]] = None
_RTK_TS: float = 0.0


def query_rtk_savings() -> Dict[str, Any]:
    """Se o binário `rtk` estiver instalado, consulta `rtk gain -d -f json` (total e por dia)."""
    global _RTK_MEMO, _RTK_TS

    now = time.time()
    if _RTK_MEMO is not None and (now - _RTK_TS) < 30.0:
        return _RTK_MEMO

    empty: Dict[str, Any] = {"available": False, "saved_tokens": 0, "total_commands": 0, "daily": {}}
    rtk_bin = shutil.which("rtk") or str(Path.home() / ".local" / "bin" / "rtk.exe")
    if not Path(rtk_bin).exists() and not shutil.which("rtk"):
        _RTK_MEMO, _RTK_TS = empty, now
        return empty

    try:
        proc = subprocess.run(
            [rtk_bin, "gain", "-d", "-f", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
        )
        data = json.loads(proc.stdout.strip())
        summary = data.get("summary") or {}
        daily_map: Dict[str, Dict[str, Any]] = {}
        for d in data.get("daily") or []:
            dt = str(d.get("date", ""))
            if dt:
                daily_map[dt] = {
                    "commands": int(d.get("commands") or 0),
                    "input_tokens": int(d.get("input_tokens") or 0),
                    "output_tokens": int(d.get("output_tokens") or 0),
                    "saved_tokens": int(d.get("saved_tokens") or 0),
                }
        res = {
            "available": True,
            "saved_tokens": int(summary.get("total_saved") or 0),
            "total_commands": int(summary.get("total_commands") or 0),
            "daily": daily_map,
        }
    except Exception:
        res = empty
    _RTK_MEMO, _RTK_TS = res, now
    return res


def _transcripts_fingerprint(project: Optional[str]) -> List[float]:
    count = mtime = size = 0.0
    for path in iter_transcripts(project):
        try:
            st = path.stat()
        except OSError:
            continue
        count += 1
        mtime = max(mtime, st.st_mtime)
        size += st.st_size
    return [count, mtime, size]


def collect_real_metrics(
    db: Optional[CatoDB] = None,
    days: Optional[float] = DEFAULT_REPORT_DAYS,
    project: Optional[str] = None,
    max_age: float = 300.0,
    cached_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Relatório real + RTK + intervenções do CATO + atribuição por dia, com cache em `~/.cato/report_cache.json`
    (a varredura completa leva segundos; o cache é reaproveitado enquanto os transcripts não mudarem).
    `cached_only=True` nunca varre (para statusline): devolve o último relatório salvo ou `None`.
    """
    cache_path = get_cato_home() / REPORT_CACHE_FILE
    key = json.dumps([days, project])
    cached: Optional[Dict[str, Any]] = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cached = None
    if cached and cached.get("key") == key:
        payload = dict(cached["payload"])
        payload["cache_created"] = float(cached.get("created", 0))
        if cached_only:
            return payload
        fresh = time.time() - payload["cache_created"] < max_age
        if fresh or cached.get("fingerprint") == _transcripts_fingerprint(project):
            return payload
    if cached_only:
        # Nunca devolve o relatório de outra janela/projeto: sem cache certo, nada
        return None

    fingerprint = _transcripts_fingerprint(project)
    report = build_report(days=days, project=project)
    rtk = query_rtk_savings()
    db_instance = db or CatoDB()
    db_instance.prune()  # fora dos hooks: aproveita a varredura completa para limpar dados de trabalho antigos
    cato_stats = db_instance.get_summary_stats()
    rtk_days = [d for d, v in rtk.get("daily", {}).items() if v.get("commands")]
    cato_days = [r["date"] for r in cato_stats.get("by_day", []) if r.get("count")]
    report["attributions"] = {}
    if rtk_days:
        report["attributions"]["RTK"] = attribute_by_days(report, rtk_days)
    if cato_days:
        report["attributions"]["CATO"] = attribute_by_days(report, cato_days)
    payload = {
        "updated_at": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "report": report,
        "rtk": rtk,
        "cato": cato_stats,
        "cache_created": time.time(),
    }
    try:
        cache_path.write_text(
            json.dumps({"key": key, "created": time.time(), "fingerprint": fingerprint, "payload": payload}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return payload


def format_stats_text(payload: Dict[str, Any]) -> str:
    """Texto do `cato stats`: relatório real + intervenções registradas pelo CATO."""
    text = format_report_text(payload["report"], rtk_claimed=payload.get("rtk"))
    by_type = payload.get("cato", {}).get("by_type", [])
    lines = [text, "  INTERVENÇÕES REGISTRADAS PELO CATO (economia declarada pelo próprio CATO, não medida na fatura)"]
    if not by_type:
        lines.append("    nenhuma")
    for row in by_type:
        lines.append(f"    {row['event_type']:<26}{row['count']:>6}x  -{row['saved_tokens']:,} tok declarados")
    return "\n".join(lines)


def format_statusline_text(db: Optional[CatoDB] = None, status_input: Optional[Dict[str, Any]] = None) -> str:
    """
    Linha única para a `statusLine` do Claude Code. Com o JSON que o Claude Code envia no stdin
    (`transcript_path`), mostra o contexto atual, o custo do próximo turno e quando o cache expira.
    """
    parts: List[str] = ["⚡ CATO"]
    transcript = (status_input or {}).get("transcript_path")
    state = last_turn_state(transcript) if transcript else None
    if state and state.get("compacted"):
        parts.append("ctx: compactado (mede de novo na próxima resposta)")
    elif state:
        price = price_for(state["model"])
        ctx = state["context"]
        parts.append(f"ctx {ctx / 1000:.0f}k")
        idle = time.time() - state["timestamp"] if state.get("timestamp") else 0.0
        ttl = state["ttl_seconds"]
        write_price = price.write_1h if ttl >= 3600 else price.write_5m
        if idle > ttl:
            parts.append(f"cache expirado: próximo turno ≈ ${ctx * write_price / 1e6:.2f}")
        else:
            parts.append(f"próximo turno ≈ ${ctx * price.read / 1e6:.3f}")
            parts.append(f"cache expira em {int((ttl - idle) // 60)}min")
    # O total do dia vem do último `cato stats`/`dashboard` (varrer os transcripts a cada 2 s seria caro):
    # mostra a idade do número e some quando ficou velho demais para orientar decisão
    payload = collect_real_metrics(db, cached_only=True)
    age_min = (time.time() - float(payload.get("cache_created", 0))) / 60 if payload else None
    if payload and age_min is not None and age_min <= STATUSLINE_MAX_REPORT_AGE_MIN:
        report = payload["report"]
        today = report.get("daily", {}).get(datetime.date.today().isoformat(), {})
        parts.append(f"hoje ${today.get('cost', 0.0):.2f} (há {int(age_min)}min)")
    elif not state:
        parts.append("rode `cato stats` para atualizar o custo do dia")
    return " | ".join(parts)


def _bar(value: float, total: float) -> str:
    pct = 100.0 * value / total if total else 0.0
    return f'<div class="bar"><span style="width:{min(100.0, pct):.1f}%"></span></div><small>{pct:.1f}%</small>'


def render_dashboard_html(payload: Dict[str, Any], auto_refresh_sec: int = 0) -> str:
    """HTML autocontido com o custo real (claro/escuro, sem dependências externas)."""
    report = payload["report"]
    total = report.get("total_cost") or 0.0
    esc = html.escape
    refresh = f'<meta http-equiv="refresh" content="{auto_refresh_sec}">' if auto_refresh_sec else ""
    labels = {
        "cache_read": "Releitura de contexto (cache read)",
        "cache_write_1h": "Escrita de cache 1h",
        "cache_write_5m": "Escrita de cache 5min",
        "output": "Saída (output)",
        "input": "Entrada sem cache",
    }

    def rows(items: List[str]) -> str:
        return "".join(items) or '<tr><td colspan="4">sem dados</td></tr>'

    cost_rows = rows(
        [
            f"<tr><td>{labels[k]}</td><td class=n>${v:,.2f}</td><td>{_bar(v, total)}</td></tr>"
            for k, v in report.get("cost", {}).items()
        ]
    )
    bucket_rows = rows(
        [
            f"<tr><td>{esc(b['bucket'])}</td><td class=n>{b['calls']:,}</td><td class=n>${b['cost']:,.2f}</td>"
            f"<td>{_bar(b['cost'], total)}</td></tr>"
            for b in report.get("context_buckets", [])
        ]
    )
    miss_rows = rows(
        [
            f"<tr><td>{esc(cause)}</td><td class=n>{v['count']}</td><td class=n>{v['tokens']:,}</td>"
            f"<td class=n>${v['cost']:,.2f}</td></tr>"
            for cause, v in report.get("cache_misses", {}).items()
        ]
    )
    tool_rows = rows(
        [
            f"<tr><td>{esc(name)}</td><td class=n>{v['calls']:,}</td><td class=n>{v['tokens']:,}</td>"
            f"<td class=n>{v['image_tokens']:,}</td></tr>"
            for name, v in list(report.get("tools", {}).items())[:12]
        ]
    )
    cmd_rows = rows(
        [
            f"<tr><td><code>{esc(c['command'])}</code></td><td class=n>{c['calls']:,}</td>"
            f"<td class=n>{c['tokens']:,}</td><td></td></tr>"
            for c in report.get("top_shell_commands", [])[:12]
        ]
    )
    att_rows = rows(
        [
            f"<tr><td>{esc(tool)} {label}</td><td class=n>{g['days']}</td><td class=n>${g['cost_per_call']:.3f}</td>"
            f"<td class=n>{int(g['avg_context']):,}</td></tr>"
            for tool, att in (report.get("attributions") or {}).items()
            for label, g in (("ativo", att["active"]), ("inativo", att["inactive"]))
        ]
    )
    event_rows = rows(
        [
            f"<tr><td>{esc(r['event_type'])}</td><td class=n>{r['count']}</td><td class=n>{r['saved_tokens']:,}</td><td></td></tr>"
            for r in payload.get("cato", {}).get("by_type", [])
        ]
    )
    rtk = payload.get("rtk") or {}
    rtk_line = (
        f"RTK declara {rtk.get('saved_tokens', 0):,} tokens poupados em {rtk.get('total_commands', 0):,} comandos "
        "(bytes/4, antes do truncamento nativo do Claude Code)."
        if rtk.get("available")
        else "RTK não encontrado."
    )
    since = report.get("window", {}).get("since")
    window = f"desde {datetime.datetime.fromtimestamp(since).strftime('%d/%m/%Y')}" if since else "todo o histórico"
    tk = report.get("tokens", {})

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>CATO — Custo Real</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--line:#e4e4df;--acc:#c2410c;--bar:#ecece7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ececea;--mut:#9a9a94;--line:#2e2e2b;--acc:#fb923c;--bar:#2a2a27}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:0 0 10px}} .mut{{color:var(--mut)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-top:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto}}
.big{{font-size:34px;font-weight:700;color:var(--acc)}}
table{{width:100%;border-collapse:collapse}} td{{padding:5px 6px;border-top:1px solid var(--line);vertical-align:middle}}
td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.bar{{display:inline-block;width:110px;height:8px;background:var(--bar);border-radius:4px;margin-right:6px;vertical-align:middle}}
.bar span{{display:block;height:100%;background:var(--acc);border-radius:4px}} code{{font-size:12px}}
</style></head><body><main>
<h1>CATO — Painel de Custo Real</h1>
<div class="mut">Custo equivalente de API calculado dos campos <code>usage</code> faturados nos transcripts do Claude Code
({report.get('files', 0)} transcripts, {report.get('calls', 0):,} chamadas, {window}). Atualizado em {esc(payload.get('updated_at', ''))}.</div>
<div class="grid">
 <div class="card"><h2>Custo total</h2><div class="big">${total:,.2f}</div>
  <div class="mut">cache read {tk.get('cache_read_input_tokens', 0):,} · cache write {tk.get('cache_creation_input_tokens', 0):,} · output {tk.get('output_tokens', 0):,} tokens</div>
  <table>{cost_rows}</table></div>
 <div class="card"><h2>Custo por tamanho de contexto</h2><div class="mut">O contexto inteiro é relido a cada turno.</div>
  <table>{bucket_rows}</table></div>
 <div class="card"><h2>Cache misses (reescrita &gt; 100k tokens)</h2><table>
  <tr class="mut"><td>causa</td><td class=n>vezes</td><td class=n>tokens</td><td class=n>custo</td></tr>{miss_rows}</table></div>
 <div class="card"><h2>Tokens que entraram no contexto por ferramenta</h2><table>
  <tr class="mut"><td>ferramenta</td><td class=n>chamadas</td><td class=n>texto</td><td class=n>imagem</td></tr>{tool_rows}</table></div>
 <div class="card"><h2>Comandos de shell com mais saída</h2><table>{cmd_rows}</table></div>
 <div class="card"><h2>Atribuição por dia (observacional)</h2>
  <div class="mut">Dias com e sem atuação da ferramenta. Tarefas diferentes em dias diferentes: só revela efeitos grandes.</div>
  <table><tr class="mut"><td></td><td class=n>dias</td><td class=n>$/chamada</td><td class=n>contexto médio</td></tr>{att_rows}</table>
  <p class="mut">{esc(rtk_line)}</p></div>
 <div class="card"><h2>Intervenções registradas pelo CATO</h2><div class="mut">Economia declarada pelo CATO, não medida na fatura.</div>
  <table>{event_rows}</table></div>
</div></main></body></html>
"""


def write_static_dashboard_html(db: Optional[CatoDB] = None, fast_mode: bool = False) -> Path:
    """Gera `~/.cato/dashboard.html`. `fast_mode` usa só o relatório em cache (sem varrer os transcripts)."""
    payload = collect_real_metrics(db, cached_only=fast_mode) or collect_real_metrics(db)
    out_path = get_cato_home() / "dashboard.html"
    out_path.write_text(render_dashboard_html(payload or {"report": {}}, auto_refresh_sec=0), encoding="utf-8")
    return out_path


def run_dashboard_server(port: int = 8765, open_browser: bool = True) -> int:
    """Servidor local em `http://127.0.0.1:<port>`; o relatório é recalculado quando os transcripts mudam."""
    db = CatoDB()
    static_file = write_static_dashboard_html(db)

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = collect_real_metrics(db, max_age=60.0) or {"report": {}}
            if self.path.startswith("/api/stats"):
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = render_dashboard_html(payload, auto_refresh_sec=60).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    url = f"http://127.0.0.1:{port}"
    print("============================================================")
    print("  ⚡ CATO DASHBOARD — Custo real dos transcripts do Claude Code")
    print("============================================================")
    print(f"  Servidor     : {url}")
    print(f"  Arquivo HTML : {static_file}")
    print("  Pressione Ctrl+C para encerrar.")
    print("============================================================")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()
    return 0
