"""
Painel Visual Web (`cato dashboard`), StatusLine (`cato statusline`) e Analisador de Sessões Reais
do Claude Code (`~/.claude/projects/**/*.jsonl`) — Parte 6 do CATO.
"""

from __future__ import annotations

import datetime
import html
import json
import re
import shutil
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from cato.storage.db import CatoDB, get_cato_home


_CACHE_METRICS_MEMO: Optional[Dict[str, Any]] = None
_CACHE_METRICS_TS: float = 0.0


def scan_claude_code_cache_metrics(max_files: int = 8) -> Dict[str, Any]:
    """
    Lê os arquivos `.jsonl` reais das sessões recentes do Claude Code em `~/.claude/projects/`
    para extrair `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` e `output_tokens`.
    Usa cache em memória de 30s e leitura limitada para resposta em <15ms.
    """
    global _CACHE_METRICS_MEMO, _CACHE_METRICS_TS
    import time

    now = time.time()
    if _CACHE_METRICS_MEMO is not None and (now - _CACHE_METRICS_TS) < 30.0:
        return _CACHE_METRICS_MEMO

    projects_dir = Path.home() / ".claude" / "projects"
    totals = {
        "sessions_scanned": 0,
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_rate_pct": 0.0,
    }
    if not projects_dir.exists():
        return totals

    try:
        candidates: List[Path] = []
        for sub in projects_dir.iterdir():
            if sub.is_dir():
                candidates.extend(sub.glob("*.jsonl"))
        jsonl_files = sorted(
            candidates,
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max_files]
    except Exception:
        return totals

    seen_msg_ids: set[str] = set()
    for fpath in jsonl_files:
        totals["sessions_scanned"] += 1
        try:
            # Lê apenas os últimos 256 KB de cada sessão para ser instantâneo mesmo em sessões gigantes
            size = fpath.stat().st_size
            with fpath.open("rb") as fp:
                if size > 262_144:
                    fp.seek(size - 262_144)
                chunk = fp.read().decode("utf-8", errors="replace")
            for line in chunk.splitlines():
                if '"usage"' not in line and '"cache_read_input_tokens"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                msg_obj = data.get("message") if isinstance(data.get("message"), dict) else {}
                msg_id = str(msg_obj.get("id") or data.get("id") or "")
                # O Claude Code grava múltiplas linhas no .jsonl para o mesmo message.id (thinking, text, tool_use)
                # com o mesmo objeto `usage`. Deduplicamos por `msg_id` para não multiplicar os tokens por 4x-5x!
                if msg_id:
                    if msg_id in seen_msg_ids:
                        continue
                    seen_msg_ids.add(msg_id)
                usage = data.get("usage") or msg_obj.get("usage") or {}
                if not isinstance(usage, dict):
                    continue
                totals["input_tokens"] += int(usage.get("input_tokens") or 0)
                totals["cache_creation_input_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
                totals["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
                totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        except Exception:
            continue

    total_prompt = (
        totals["input_tokens"]
        + totals["cache_creation_input_tokens"]
        + totals["cache_read_input_tokens"]
    )
    if total_prompt > 0:
        totals["cache_hit_rate_pct"] = round(
            (totals["cache_read_input_tokens"] / total_prompt) * 100.0, 1
        )
    _CACHE_METRICS_MEMO = totals
    _CACHE_METRICS_TS = now
    return totals


_RTK_MEMO: Optional[Dict[str, Any]] = None
_RTK_TS: float = 0.0


def query_rtk_savings() -> Dict[str, Any]:
    """Se o binário `rtk` estiver instalado, consulta `rtk gain -d -f json` para obter o total geral e por dia."""
    global _RTK_MEMO, _RTK_TS
    import time

    now = time.time()
    if _RTK_MEMO is not None and (now - _RTK_TS) < 30.0:
        return _RTK_MEMO

    rtk_bin = shutil.which("rtk") or str(Path.home() / ".local" / "bin" / "rtk.exe")
    if not Path(rtk_bin).exists() and not shutil.which("rtk"):
        res: Dict[str, Any] = {"available": False, "saved_tokens": 0, "total_commands": 0, "daily": {}}
        _RTK_MEMO = res
        _RTK_TS = now
        return res

    try:
        proc = subprocess.run(
            [rtk_bin, "gain", "-d", "-f", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.5,
        )
        out = proc.stdout.strip()
        data = json.loads(out)
        summary = data.get("summary") or {}
        daily_list = data.get("daily") or []
        daily_map: Dict[str, Dict[str, Any]] = {}
        for d in daily_list:
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
        _RTK_MEMO = res
        _RTK_TS = now
        return res
    except Exception:
        res = {"available": False, "saved_tokens": 0, "total_commands": 0, "daily": {}}
        _RTK_MEMO = res
        _RTK_TS = now
        return res


def collect_full_dashboard_payload(db: Optional[CatoDB] = None, fast_mode: bool = False) -> Dict[str, Any]:
    """Consolida métricas de HOJE, POR DIA e NO GERAL (SQLite do CATO + Cache real do Claude Code + RTK)."""
    db_instance = db or CatoDB()
    cato_stats = db_instance.get_summary_stats()
    claude_cache = scan_claude_code_cache_metrics(max_files=3 if fast_mode else 8)
    rtk_info = query_rtk_savings()

    today_iso = datetime.date.today().isoformat()
    today_br = datetime.date.today().strftime("%d/%m/%Y")

    # Totais Gerais
    totals = cato_stats.get("totals", {})
    cato_saved = int(totals.get("total_saved_tokens", 0))
    cato_raw = int(totals.get("total_raw_tokens", 0))
    cato_opt = int(totals.get("total_optimized_tokens", 0))
    cato_events = int(totals.get("total_events", 0))

    rtk_saved = int(rtk_info.get("saved_tokens", 0))
    rtk_commands = int(rtk_info.get("total_commands", 0))
    combined_saved = cato_saved + rtk_saved

    # Totais de Hoje (Dia Atual)
    today_cato = cato_stats.get("today", {})
    today_cato_saved = int(today_cato.get("today_saved_tokens", 0))
    today_cato_raw = int(today_cato.get("today_raw_tokens", 0))
    today_cato_opt = int(today_cato.get("today_optimized_tokens", 0))
    today_cato_events = int(today_cato.get("today_events", 0))

    rtk_daily_map: Dict[str, Dict[str, Any]] = rtk_info.get("daily") or {}
    today_rtk_entry = rtk_daily_map.get(today_iso, {})
    today_rtk_saved = int(today_rtk_entry.get("saved_tokens", 0))
    today_rtk_cmds = int(today_rtk_entry.get("commands", 0))
    today_combined_saved = today_cato_saved + today_rtk_saved
    today_pct = round((today_cato_saved / today_cato_raw * 100.0), 1) if today_cato_raw > 0 else 0.0

    today_sonnet_usd = round((today_cato_saved / 1_000_000) * 3.0, 4)
    today_opus_usd = round((today_cato_saved / 1_000_000) * 15.0, 4)

    pct = round((cato_saved / cato_raw * 100.0), 1) if cato_raw > 0 else 0.0
    sonnet_usd = round((cato_saved / 1_000_000) * 3.0, 4)
    opus_usd = round((cato_saved / 1_000_000) * 15.0, 4)

    # Histórico Diário real do CATO (não mistura datas antigas de julho do RTK)
    cato_by_day_map: Dict[str, Dict[str, Any]] = {
        str(r["date"]): r for r in cato_stats.get("by_day", []) if r.get("date")
    }
    all_dates = sorted(
        set(cato_by_day_map.keys()) | {today_iso},
        reverse=True,
    )[:15]

    daily_history: List[Dict[str, Any]] = []
    for dt_iso in all_dates:
        c_row = cato_by_day_map.get(dt_iso, {})
        r_row = rtk_daily_map.get(dt_iso, {})
        c_sav = int(c_row.get("saved_tokens", 0))
        c_raw = int(c_row.get("raw_tokens", 0))
        c_opt = int(c_row.get("optimized_tokens", 0))
        c_cnt = int(c_row.get("count", 0))
        r_sav = int(r_row.get("saved_tokens", 0))
        r_cnt = int(r_row.get("commands", 0))
        tot_sav = c_sav + r_sav
        try:
            dt_label = datetime.date.fromisoformat(dt_iso).strftime("%d/%m/%Y")
        except Exception:
            dt_label = dt_iso
        daily_history.append(
            {
                "date_iso": dt_iso,
                "date_label": dt_label,
                "is_today": dt_iso == today_iso,
                "cato_events": c_cnt,
                "cato_raw": c_raw,
                "cato_opt": c_opt,
                "cato_saved": c_sav,
                "rtk_commands": r_cnt,
                "rtk_saved": r_sav,
                "total_saved": c_sav,
                "opus_usd": round((c_sav / 1_000_000) * 15.0, 4),
                "sonnet_usd": round((c_sav / 1_000_000) * 3.0, 4),
            }
        )

    return {
        "updated_at": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "cato": cato_stats,
        "claude_cache": claude_cache,
        "rtk": rtk_info,
        "daily_history": daily_history,
        "headline": {
            "today_label": today_br,
            "today_cato_events": today_cato_events,
            "today_cato_raw": today_cato_raw,
            "today_cato_opt": today_cato_opt,
            "today_cato_saved": today_cato_saved,
            "today_rtk_commands": today_rtk_cmds,
            "today_rtk_saved": today_rtk_saved,
            "today_combined_saved": today_combined_saved,
            "today_reduction_pct": today_pct,
            "today_sonnet_usd": today_sonnet_usd,
            "today_opus_usd": today_opus_usd,
            "total_events": cato_events,
            "rtk_total_commands": rtk_commands,
            "cato_raw_tokens": cato_raw,
            "cato_optimized_tokens": cato_opt,
            "cato_saved_tokens": cato_saved,
            "rtk_saved_tokens": rtk_saved,
            "combined_saved_tokens": combined_saved,
            "reduction_pct": pct,
            "sonnet_usd_saved": sonnet_usd,
            "opus_usd_saved": opus_usd,
            "cache_hit_rate_pct": claude_cache.get("cache_hit_rate_pct", 0.0),
        },
    }


def format_statusline_text(db: Optional[CatoDB] = None) -> str:
    """Gera string compacta de 1 linha com economia real de HOJE e NO GERAL do CATO."""
    data = collect_full_dashboard_payload(db)
    h = data["headline"]
    today_sav = h["today_cato_saved"]
    today_str = f"{today_sav / 1000:.1f}k" if today_sav >= 1000 else str(today_sav)
    total_sav = h["cato_saved_tokens"]
    total_str = f"{total_sav / 1000:.1f}k" if total_sav >= 1000 else str(total_sav)
    opus_usd = h["opus_usd_saved"]
    chr_pct = h["cache_hit_rate_pct"]
    return (
        f"⚡ CATO Hoje: -{today_str} tok | Geral: -{total_str} tok "
        f"(${opus_usd:.4f} poupados) | Cache Hit: {chr_pct:.0f}%"
    )


def render_dashboard_html(payload: Dict[str, Any], auto_refresh_sec: int = 0) -> str:
    """Renderiza o HTML autocontido do Painel de Economia de Tokens do CATO (Hoje, Por Dia e Geral)."""
    h = payload["headline"]
    by_type = payload["cato"].get("by_type", [])
    recent = payload["cato"].get("recent", [])
    daily_history = payload.get("daily_history", [])
    cc = payload["claude_cache"]

    meta_refresh = f'<meta http-equiv="refresh" content="{auto_refresh_sec}">' if auto_refresh_sec > 0 else ""

    max_daily_saved = max([int(d.get("cato_saved", 0)) for d in daily_history] + [1])
    daily_rows_html = ""
    for d in daily_history:
        d_cato = int(d["cato_saved"])
        d_raw = int(d.get("cato_raw", 0))
        d_opt = int(d.get("cato_opt", 0))
        d_pct = (d_cato / d_raw * 100.0) if d_raw > 0 else 0.0
        d_bar = min(100, max(4, int((d_cato / max_daily_saved) * 100))) if d_cato > 0 else 0
        today_badge = ' <span class="badge" style="background:rgba(63,185,80,0.2);color:#3fb950;border-color:rgba(63,185,80,0.4);">HOJE</span>' if d.get("is_today") else ""
        daily_rows_html += f"""
        <tr>
            <td><strong>{html.escape(str(d['date_label']))}</strong>{today_badge}</td>
            <td>{d['cato_events']}x</td>
            <td>{d_raw:,} &rarr; {d_opt:,}</td>
            <td class="saved">-{d_cato:,} tok (-{d_pct:.1f}%)</td>
            <td>
                <div class="bar-bg"><div class="bar-fill" style="width:{d_bar}%"></div></div>
                <span style="color:#d2a8ff;font-weight:600;">${d['opus_usd']:.4f}</span>
            </td>
        </tr>
        """
    if not daily_rows_html:
        daily_rows_html = '<tr><td colspan="5" style="text-align:center;color:#8b949e;">Sem histórico diário registrado.</td></tr>'

    type_rows_html = ""
    for row in by_type:
        r_raw = int(row["raw_tokens"])
        r_opt = int(row["optimized_tokens"])
        r_saved = int(row["saved_tokens"])
        r_pct = (r_saved / r_raw * 100.0) if r_raw > 0 else 0.0
        type_rows_html += f"""
        <tr>
            <td><span class="badge">{html.escape(str(row['event_type']))}</span></td>
            <td>{row['count']}x</td>
            <td>{r_raw:,}</td>
            <td>{r_opt:,}</td>
            <td class="saved">-{r_saved:,} tok</td>
            <td>
                <div class="bar-bg"><div class="bar-fill" style="width:{min(100, max(5, int(r_pct)))}%"></div></div>
                <span class="pct-label">-{r_pct:.1f}%</span>
            </td>
        </tr>
        """
    if not type_rows_html:
        type_rows_html = '<tr><td colspan="6" style="text-align:center;color:#8b949e;">Nenhuma intervenção registrada ainda. Use o Claude Code ou rode comandos CATO.</td></tr>'

    recent_rows_html = ""
    for item in recent:
        ts_str = datetime.datetime.fromtimestamp(float(item["timestamp"])).strftime("%d/%m %H:%M:%S")
        r_raw = int(item["raw_tokens"])
        r_opt = int(item["optimized_tokens"])
        r_saved = int(item["saved_tokens"])
        r_pct = (r_saved / r_raw * 100.0) if r_raw > 0 else 0.0
        recent_rows_html += f"""
        <tr>
            <td style="color:#8b949e;">{ts_str}</td>
            <td><code>{html.escape(str(item['tool_name']))}</code></td>
            <td><span class="badge">{html.escape(str(item['event_type']))}</span></td>
            <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{html.escape(str(item['target']))}</td>
            <td>{r_raw:,} &rarr; {r_opt:,}</td>
            <td class="saved">-{r_saved:,} (-{r_pct:.1f}%)</td>
        </tr>
        """
    if not recent_rows_html:
        recent_rows_html = '<tr><td colspan="6" style="text-align:center;color:#8b949e;">Sem eventos recentes.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta_refresh}
<title>CATO — Painel de Economia de Tokens (Claude Code)</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --blue: #58a6ff;
    --purple: #d2a8ff;
    --amber: #d29922;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 24px 32px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    margin-bottom: 24px;
  }}
  .header h1 {{ margin: 0; font-size: 22px; display: flex; align-items: center; gap: 10px; }}
  .status-pill {{
    background: rgba(63, 185, 80, 0.15);
    color: var(--green);
    border: 1px solid rgba(63, 185, 80, 0.4);
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }}
  .card-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.6px; }}
  .card-value {{ font-size: 28px; font-weight: 700; margin: 8px 0 4px; }}
  .card-sub {{ color: var(--muted); font-size: 12px; }}
  .section-title {{ font-size: 16px; font-weight: 600; margin: 24px 0 12px; color: var(--text); }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    font-size: 13px;
  }}
  th, td {{ padding: 11px 14px; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ background: #1c2128; color: var(--muted); font-weight: 600; font-size: 12px; }}
  .saved {{ color: var(--green); font-weight: 600; }}
  .badge {{
    background: rgba(88, 166, 255, 0.15);
    color: var(--blue);
    border: 1px solid rgba(88, 166, 255, 0.3);
    padding: 2px 8px;
    border-radius: 6px;
    font-family: monospace;
    font-size: 12px;
  }}
  .bar-bg {{
    display: inline-block;
    width: 90px;
    height: 8px;
    background: #21262d;
    border-radius: 4px;
    vertical-align: middle;
    margin-right: 8px;
    overflow: hidden;
  }}
  .bar-fill {{ height: 100%; background: var(--green); }}
  .pct-label {{ font-weight: 600; color: var(--green); }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>⚡ CATO — Painel de Economia de Tokens no Claude Code</h1>
      <div style="color:var(--muted);font-size:13px;margin-top:4px;">
        Monitoramento em tempo real (Hoje, Por Dia e Geral Acumulado) — CATO + RTK + Prompt Cache
      </div>
    </div>
    <div>
      <span class="status-pill">● ATIVO | Atualizado: {payload['updated_at']}</span>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">📅 Tokens Salvos Hoje ({h['today_label']})</div>
      <div class="card-value" style="color:var(--green);">-{h['today_cato_saved']:,}</div>
      <div class="card-sub">{h['today_cato_raw']:,} brutos &rarr; {h['today_cato_opt']:,} entregues ({h['today_cato_events']}x intervenções hoje)</div>
    </div>
    <div class="card">
      <div class="card-label">🌎 Tokens Salvos no Geral (Acumulado CATO)</div>
      <div class="card-value" style="color:var(--blue);">-{h['cato_saved_tokens']:,}</div>
      <div class="card-sub">{h['cato_raw_tokens']:,} brutos &rarr; {h['cato_optimized_tokens']:,} entregues ({h['total_events']}x intervenções totais)</div>
    </div>
    <div class="card">
      <div class="card-label">💰 Economia Direta de Input (Hoje / Geral)</div>
      <div class="card-value" style="color:var(--purple);">${h['today_opus_usd']:.4f} / ${h['opus_usd_saved']:.4f}</div>
      <div class="card-sub">Em Claude Opus $15/1M (Sonnet $3/1M: ${h['today_sonnet_usd']:.4f} hoje / ${h['sonnet_usd_saved']:.4f} geral)</div>
    </div>
    <div class="card">
      <div class="card-label">⚡ Taxa de Compressão &amp; Prompt Cache</div>
      <div class="card-value" style="color:var(--amber);">-{h['reduction_pct']:.1f}% | {h['cache_hit_rate_pct']:.1f}%</div>
      <div class="card-sub">Compressão CATO na origem | {cc['cache_read_input_tokens']:,} lidos em cache a 0.1x (deduplicados)</div>
    </div>
  </div>

  <div class="section-title">📆 Economia Real de Tokens por Dia (CATO)</div>
  <table>
    <thead>
      <tr>
        <th>Data</th>
        <th>Intervenções no Dia</th>
        <th>Tokens Brutos &rarr; Entregues</th>
        <th>Tokens Salvos no Dia</th>
        <th>Proporção &amp; Economia Direta (Opus)</th>
      </tr>
    </thead>
    <tbody>
      {daily_rows_html}
    </tbody>
  </table>

  <div class="section-title">⚙️ Economia Acumulada por Motor de Otimização (CATO)</div>
  <table>
    <thead>
      <tr>
        <th>Mecanismo CATO</th>
        <th>Acionamentos</th>
        <th>Tokens Brutos</th>
        <th>Tokens Otimizados</th>
        <th>Tokens Poupados</th>
        <th>Redução (%)</th>
      </tr>
    </thead>
    <tbody>
      {type_rows_html}
    </tbody>
  </table>

  <div class="section-title">⚡ Últimas Intervenções em Tempo Real</div>
  <table>
    <thead>
      <tr>
        <th>Data / Hora</th>
        <th>Ferramenta</th>
        <th>Mecanismo</th>
        <th>Alvo (Arquivo / Comando / MCP)</th>
        <th>Antes &rarr; Depois</th>
        <th>Economia</th>
      </tr>
    </thead>
    <tbody>
      {recent_rows_html}
    </tbody>
  </table>
</body>
</html>
"""


def write_static_dashboard_html(db: Optional[CatoDB] = None, fast_mode: bool = False) -> Path:
    """
    Atualiza o arquivo estático `~/.cato/dashboard.html` (e `./cato-dashboard.html` no projeto)
    para que o usuário possa visualizar os tokens salvos a qualquer momento sem precisar iniciar servidor.
    """
    payload = collect_full_dashboard_payload(db, fast_mode=fast_mode)
    html_str = render_dashboard_html(payload, auto_refresh_sec=5)
    out_path = get_cato_home() / "dashboard.html"
    out_path.write_text(html_str, encoding="utf-8")
    try:
        proj_root = Path(__file__).resolve().parents[3]
        if proj_root.exists():
            (proj_root / "cato-dashboard.html").write_text(html_str, encoding="utf-8")
    except Exception:
        pass
    return out_path


def run_dashboard_server(port: int = 8765, open_browser: bool = True) -> int:
    """Inicia o servidor web local em `http://127.0.0.1:<port>` com auto-refresh a cada 3 segundos."""
    db = CatoDB()
    static_file = write_static_dashboard_html(db)

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/stats"):
                payload = collect_full_dashboard_payload(db)
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            payload = collect_full_dashboard_payload(db)
            body = render_dashboard_html(payload, auto_refresh_sec=3).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    url = f"http://127.0.0.1:{port}"
    print("============================================================")
    print("  ⚡ CATO LIVE DASHBOARD — Monitor de Economia de Tokens")
    print("============================================================")
    print(f"  Servidor Ao Vivo : {url}")
    print(f"  Arquivo HTML     : {static_file}")
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
