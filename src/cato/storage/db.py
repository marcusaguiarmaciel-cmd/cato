"""
Gerenciador de armazenamento SQLite local do CATO.
Responsável por:
1. Deduplicação SHA-256 de leituras de arquivos por sessão (`session_reads`)
2. Circuit Breaker para permitir leitura completa quando o Claude solicita novamente um arquivo esqueletonizado
3. Registro de telemetria de economia de tokens (`telemetry_events`)
4. Índice de logs brutos de comandos comprimidos (`command_logs`)
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


def get_cato_home() -> Path:
    """Retorna o diretório base do CATO (~/.cato por padrão ou CATO_HOME)."""
    env_home = os.environ.get("CATO_HOME")
    if env_home:
        base = Path(env_home)
    else:
        base = Path.home() / ".cato"
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_logs_dir() -> Path:
    """Retorna o diretório onde os logs brutos de fallback são salvos."""
    logs_dir = get_cato_home() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def estimate_tokens(text: str) -> int:
    """
    Estimativa rápida e determinística de tokens BPE (~3.8 caracteres por token em código/logs).
    Evita overhead de carregar tokenizadores pesados em hooks de <15ms.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.8))


class CatoDB:
    """Gerenciador SQLite thread-safe com WAL mode para baixa latência em Hooks."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or (get_cato_home() / "cato.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_reads (
                    session_id TEXT NOT NULL,
                    file_key TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    read_mode TEXT NOT NULL, -- 'full', 'slice', 'skeleton'
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    first_read_at REAL NOT NULL,
                    last_read_at REAL NOT NULL,
                    PRIMARY KEY (session_id, file_key)
                );

                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, -- 'bash_compress', 'read_dedup', 'read_skeleton', 'grep_limit'
                    tool_name TEXT NOT NULL,
                    target TEXT NOT NULL,
                    raw_tokens INTEGER NOT NULL,
                    optimized_tokens INTEGER NOT NULL,
                    saved_tokens INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS command_logs (
                    log_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    raw_log_path TEXT NOT NULL,
                    raw_tokens INTEGER NOT NULL,
                    compressed_tokens INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_telemetry_session ON telemetry_events(session_id);
                CREATE INDEX IF NOT EXISTS idx_telemetry_type ON telemetry_events(event_type);

                CREATE TABLE IF NOT EXISTS sandbox_meta (
                    sandbox_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    raw_tokens INTEGER NOT NULL,
                    summary_tokens INTEGER NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS sandbox_chunks USING fts5(
                    sandbox_id UNINDEXED,
                    chunk_index UNINDEXED,
                    title,
                    content
                );

                CREATE TABLE IF NOT EXISTS guard_acks (
                    session_id TEXT NOT NULL,
                    ack_key TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (session_id, ack_key)
                );

                CREATE TABLE IF NOT EXISTS compaction_snapshots (
                    session_id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    git_branch TEXT NOT NULL,
                    git_status TEXT NOT NULL,
                    git_diff_stat TEXT NOT NULL,
                    recent_files TEXT NOT NULL
                );
                """
            )

    def get_session_read(self, session_id: str, file_key: str) -> Optional[Dict[str, Any]]:
        """Busca um registro de leitura anterior na mesma sessão."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_reads WHERE session_id = ? AND file_key = ?",
                (session_id, file_key),
            ).fetchone()
            return dict(row) if row else None

    def has_file_path_read(self, session_id: str, file_path: str) -> bool:
        """Verifica se qualquer fatia ou esqueleto deste `file_path` já foi lido na sessão atual."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM session_reads WHERE session_id = ? AND file_path = ? LIMIT 1",
                (session_id, file_path),
            ).fetchone()
            return row is not None

    def upsert_session_read(
        self,
        session_id: str,
        file_key: str,
        file_path: str,
        sha256: str,
        line_count: int,
        read_mode: str,
        reset_attempts: bool = False,
    ) -> int:
        """
        Registra ou atualiza a leitura de um arquivo na sessão atual.
        Retorna o `attempt_count` atualizado.
        """
        now = time.time()
        existing = self.get_session_read(session_id, file_key)
        with self._connect() as conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO session_reads
                    (session_id, file_key, file_path, sha256, line_count, read_mode, attempt_count, first_read_at, last_read_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (session_id, file_key, file_path, sha256, line_count, read_mode, now, now),
                )
                return 1
            else:
                new_count = 1 if reset_attempts else (int(existing["attempt_count"]) + 1)
                conn.execute(
                    """
                    UPDATE session_reads
                    SET sha256 = ?, line_count = ?, read_mode = ?, attempt_count = ?, last_read_at = ?
                    WHERE session_id = ? AND file_key = ?
                    """,
                    (sha256, line_count, read_mode, new_count, now, session_id, file_key),
                )
                return new_count

    def clear_session_reads(self, session_id: Optional[str] = None) -> int:
        """
        Limpa o cache de deduplicação de leituras quando ocorre `/compact` ou `/clear`,
        garantindo que o Claude não seja impedido de reler arquivos que foram removidos do contexto ativo.
        """
        with self._connect() as conn:
            if session_id and session_id != "default":
                cur = conn.execute("DELETE FROM session_reads WHERE session_id = ?", (session_id,))
            else:
                cur = conn.execute("DELETE FROM session_reads")
            return cur.rowcount

    def reset_telemetry(self) -> int:
        """Zera o histórico de telemetria de economia de tokens."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM telemetry_events")
            return cur.rowcount

    def has_guard_ack(self, session_id: str, ack_key: str) -> bool:
        """Indica se o usuário já viu (e ignorou) este aviso do guard nesta sessão."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM guard_acks WHERE session_id = ? AND ack_key = ?", (session_id, ack_key)
            ).fetchone()
            return row is not None

    def add_guard_ack(self, session_id: str, ack_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guard_acks (session_id, ack_key, timestamp) VALUES (?, ?, ?)",
                (session_id, ack_key, time.time()),
            )

    def prune(self, older_than_days: float = 14.0) -> Dict[str, int]:
        """
        Apaga dados de trabalho antigos (logs brutos, payloads de sandbox, leituras e avisos de sessão).
        A telemetria fica: é o histórico que o usuário consulta.
        """
        cutoff = time.time() - older_than_days * 86400
        removed: Dict[str, int] = {}
        with self._connect() as conn:
            old_sbx = [r[0] for r in conn.execute("SELECT sandbox_id FROM sandbox_meta WHERE timestamp < ?", (cutoff,))]
            for sbx in old_sbx:
                conn.execute("DELETE FROM sandbox_chunks WHERE sandbox_id = ?", (sbx,))
            removed["sandboxes"] = conn.execute("DELETE FROM sandbox_meta WHERE timestamp < ?", (cutoff,)).rowcount
            removed["command_logs"] = conn.execute("DELETE FROM command_logs WHERE timestamp < ?", (cutoff,)).rowcount
            removed["session_reads"] = conn.execute("DELETE FROM session_reads WHERE last_read_at < ?", (cutoff,)).rowcount
            removed["guard_acks"] = conn.execute("DELETE FROM guard_acks WHERE timestamp < ?", (cutoff,)).rowcount
        removed["log_files"] = 0
        for log_file in get_logs_dir().glob("*.log"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    removed["log_files"] += 1
            except OSError:
                continue
        return removed

    def clear_guard_acks(self, session_id: str) -> int:
        """Após /compact ou /clear o contexto encolhe: os avisos do guard voltam a valer."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM guard_acks WHERE session_id = ?", (session_id,))
            return int(cur.rowcount or 0)

    def record_telemetry(
        self,
        session_id: str,
        event_type: str,
        tool_name: str,
        target: str,
        raw_tokens: int,
        optimized_tokens: int,
    ) -> int:
        """Registra um evento de economia de tokens na tabela de telemetria."""
        saved = max(0, raw_tokens - optimized_tokens)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO telemetry_events
                (timestamp, session_id, event_type, tool_name, target, raw_tokens, optimized_tokens, saved_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    session_id or "default",
                    event_type,
                    tool_name,
                    target[:300],
                    raw_tokens,
                    optimized_tokens,
                    saved,
                ),
            )
            row_id = int(cur.lastrowid or 0)
        # O painel HTML não é regenerado aqui: ele varre os transcripts reais e não pode rodar dentro de um hook.
        return row_id

    def store_sandbox_payload(
        self,
        sandbox_id: str,
        session_id: str,
        source_tool: str,
        chunks: List[Dict[str, str]],
        raw_tokens: int,
        summary_tokens: int,
    ) -> None:
        """Indexa blocos de texto grandes na tabela virtual SQLite FTS5 (BM25) para busca sob demanda."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sandbox_meta
                (sandbox_id, timestamp, session_id, source_tool, total_chunks, raw_tokens, summary_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sandbox_id,
                    time.time(),
                    session_id or "default",
                    source_tool,
                    len(chunks),
                    raw_tokens,
                    summary_tokens,
                ),
            )
            conn.execute("DELETE FROM sandbox_chunks WHERE sandbox_id = ?", (sandbox_id,))
            for idx, ch in enumerate(chunks):
                conn.execute(
                    "INSERT INTO sandbox_chunks (sandbox_id, chunk_index, title, content) VALUES (?, ?, ?, ?)",
                    (sandbox_id, idx, ch.get("title", f"Chunk {idx}"), ch.get("content", "")),
                )

    def search_sandbox(self, sandbox_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Executa busca textual BM25 via FTS5 (ou fallback LIKE) dentro de um payload virtualizado."""
        import re

        with self._connect() as conn:
            # Preserva identificadores com underscore (ex: module_42) ao montar os termos FTS5
            safe_terms = [w for w in re.findall(r"\w+", query) if len(w) >= 2]  # \w inclui acentos (ação)
            fts_query = " OR ".join(f'"{t}"' for t in safe_terms) if safe_terms else ""
            if fts_query:
                try:
                    rows = conn.execute(
                        """
                        SELECT chunk_index, title, content, bm25(sandbox_chunks) as score
                        FROM sandbox_chunks
                        WHERE sandbox_id = ? AND sandbox_chunks MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (sandbox_id, fts_query, limit),
                    ).fetchall()
                    if rows:
                        return [dict(r) for r in rows]
                except Exception:
                    pass

            # Fallback por substring simples caso FTS5 MATCH não encontre
            rows = conn.execute(
                """
                SELECT chunk_index, title, content, 0.0 as score
                FROM sandbox_chunks
                WHERE sandbox_id = ? AND (content LIKE ? OR title LIKE ?)
                ORDER BY chunk_index ASC
                LIMIT ?
                """,
                (sandbox_id, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_compaction_snapshot(
        self,
        session_id: str,
        git_branch: str,
        git_status: str,
        git_diff_stat: str,
        recent_files: str,
    ) -> None:
        """Salva o estado determinístico do repositório antes de `/compact`."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO compaction_snapshots
                (session_id, timestamp, git_branch, git_status, git_diff_stat, recent_files)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id or "default", time.time(), git_branch, git_status, git_diff_stat, recent_files),
            )

    def pop_compaction_snapshot(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Recupera o snapshot pré-compactação para reinjeção pós-`/compact`."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM compaction_snapshots WHERE session_id = ? ORDER BY timestamp DESC LIMIT 1",
                (session_id or "default",),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM compaction_snapshots ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None

    def record_command_log(
        self,
        log_id: str,
        session_id: str,
        command: str,
        exit_code: int,
        raw_log_path: str,
        raw_tokens: int,
        compressed_tokens: int,
    ) -> None:
        """Registra um log bruto salvo em disco para eventual recuperação (Circuit Breaker)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO command_logs
                (log_id, timestamp, session_id, command, exit_code, raw_log_path, raw_tokens, compressed_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    time.time(),
                    session_id or "default",
                    command[:500],
                    exit_code,
                    raw_log_path,
                    raw_tokens,
                    compressed_tokens,
                ),
            )

    def get_summary_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas agregadas de economia de tokens (hoje, por dia, geral e por tipo)."""
        import datetime

        today_start_ts = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        with self._connect() as conn:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) as total_events,
                    COALESCE(SUM(raw_tokens), 0) as total_raw_tokens,
                    COALESCE(SUM(optimized_tokens), 0) as total_optimized_tokens,
                    COALESCE(SUM(saved_tokens), 0) as total_saved_tokens
                FROM telemetry_events WHERE event_type NOT IN ('read_slice')
                """
            ).fetchone()

            today = conn.execute(
                """
                SELECT
                    COUNT(*) as today_events,
                    COALESCE(SUM(raw_tokens), 0) as today_raw_tokens,
                    COALESCE(SUM(optimized_tokens), 0) as today_optimized_tokens,
                    COALESCE(SUM(saved_tokens), 0) as today_saved_tokens
                FROM telemetry_events WHERE event_type NOT IN ('read_slice')
                AND timestamp >= ?
                """,
                (today_start_ts,),
            ).fetchone()

            by_day = conn.execute(
                """
                SELECT
                    strftime('%Y-%m-%d', timestamp, 'unixepoch', 'localtime') as date,
                    COUNT(*) as count,
                    COALESCE(SUM(raw_tokens), 0) as raw_tokens,
                    COALESCE(SUM(optimized_tokens), 0) as optimized_tokens,
                    COALESCE(SUM(saved_tokens), 0) as saved_tokens
                FROM telemetry_events WHERE event_type NOT IN ('read_slice')
                GROUP BY date
                ORDER BY date DESC
                LIMIT 30
                """
            ).fetchall()

            by_type = conn.execute(
                """
                SELECT
                    event_type,
                    COUNT(*) as count,
                    COALESCE(SUM(raw_tokens), 0) as raw_tokens,
                    COALESCE(SUM(optimized_tokens), 0) as optimized_tokens,
                    COALESCE(SUM(saved_tokens), 0) as saved_tokens
                FROM telemetry_events WHERE event_type NOT IN ('read_slice')
                GROUP BY event_type
                ORDER BY saved_tokens DESC
                """
            ).fetchall()

            recent = conn.execute(
                """
                SELECT id, timestamp, event_type, tool_name, target, raw_tokens, optimized_tokens, saved_tokens
                FROM telemetry_events WHERE event_type NOT IN ('read_slice')
                ORDER BY id DESC
                LIMIT 15
                """
            ).fetchall()

            return {
                "totals": dict(totals) if totals else {},
                "today": dict(today) if today else {},
                "by_day": [dict(r) for r in by_day],
                "by_type": [dict(r) for r in by_type],
                "recent": [dict(r) for r in recent],
            }
