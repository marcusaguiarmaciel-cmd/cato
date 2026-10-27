"""
Guardião de Leitura (`cato-read-guard` — Parte 3 do CATO).
Implementa:
1. Deduplicação SHA-256 de leituras repetidas na mesma sessão
2. Esqueletonização AST automática para leituras cegas de arquivos grandes (>300 linhas sem offset/limit)
3. Circuit Breaker: se o modelo solicitar o mesmo arquivo grande uma 2ª vez, libera a leitura integral.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cato.code_intel.ast_skeleton import generate_file_skeleton, is_code_file
from cato.storage.db import CatoDB, estimate_tokens

# Limiar padrão de linhas a partir do qual uma leitura sem offset/limit é interceptada pelo AST Skeletonizer
DEFAULT_SKELETON_LINE_THRESHOLD = 300


@dataclass
class ReadDecision:
    allow: bool
    reason: Optional[str] = None
    additional_context: Optional[str] = None
    event_type: Optional[str] = None
    raw_tokens: int = 0
    optimized_tokens: int = 0


def evaluate_read_request(
    file_path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    session_id: str = "default",
    skeleton_threshold: int = DEFAULT_SKELETON_LINE_THRESHOLD,
    db: Optional[CatoDB] = None,
) -> ReadDecision:
    """
    Avalia uma chamada à ferramenta `Read` do Claude Code antes de sua execução (`PreToolUse`).
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return ReadDecision(allow=True)

    try:
        raw_bytes = path.read_bytes()
        source = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ReadDecision(allow=True)

    lines = source.splitlines()
    total_lines = len(lines)

    # Calcula o trecho efetivo que seria lido pelo Claude Code
    is_sliced = offset is not None or limit is not None
    start_idx = max(0, (offset - 1) if (offset and offset > 0) else 0)
    end_idx = min(total_lines, start_idx + limit) if limit else total_lines
    effective_text = "\n".join(lines[start_idx:end_idx])
    raw_tokens = estimate_tokens(effective_text)

    # Se o arquivo/fatia for pequeno (< 80 linhas ou < 300 tokens), sempre permite e registra hash
    file_sha256 = hashlib.sha256(effective_text.encode("utf-8", errors="replace")).hexdigest()
    norm_path = str(path.resolve())
    file_key = f"{norm_path}:{start_idx}:{end_idx}"

    db_instance = db or CatoDB()
    prior_read = db_instance.get_session_read(session_id, file_key)

    # 1. VERIFICAÇÃO DE DEDUPLICAÇÃO SHA-256 (Arquivo/fatia já está no contexto ativo e não mudou no disco)
    if prior_read is not None and prior_read["sha256"] == file_sha256:
        # Se da primeira vez retornamos apenas o 'skeleton' e agora o Claude pede o arquivo inteiro de novo,
        # o Circuit Breaker libera a leitura completa!
        if prior_read["read_mode"] == "skeleton":
            db_instance.upsert_session_read(
                session_id=session_id,
                file_key=file_key,
                file_path=norm_path,
                sha256=file_sha256,
                line_count=end_idx - start_idx,
                read_mode="full",
                reset_attempts=True,
            )
            return ReadDecision(
                allow=True,
                additional_context="[CATO Circuit Breaker: Leitura integral liberada na 2ª solicitação do arquivo.]",
            )

        # Se já foi lido em modo 'full' ou 'slice' e o hash SHA-256 é idêntico:
        # Circuit Breaker para deduplicação: bloqueia a 2ª leitura redundante, mas se insistir uma 3ª vez, libera.
        attempt = int(prior_read["attempt_count"]) + 1
        db_instance.upsert_session_read(
            session_id=session_id,
            file_key=file_key,
            file_path=norm_path,
            sha256=file_sha256,
            line_count=end_idx - start_idx,
            read_mode=prior_read["read_mode"],
        )
        if attempt == 2 and raw_tokens >= 250:
            msg = (
                f"[CATO Deduplication Guard] O arquivo `{path.name}` (linhas {start_idx + 1}-{end_idx}, "
                f"SHA-256 `{file_sha256[:8]}`) JÁ FOI LIDO nesta sessão e NÃO sofreu nenhuma modificação no disco. "
                f"Utilize o conteúdo que já está presente na sua janela de contexto acima para economizar {raw_tokens} tokens. "
                f"(Se realmente precisar forçar a releitura bruta, chame Read novamente)."
            )
            opt_tokens = estimate_tokens(msg)
            db_instance.record_telemetry(
                session_id=session_id,
                event_type="read_dedup",
                tool_name="Read",
                target=f"{path.name} ({start_idx + 1}-{end_idx})",
                raw_tokens=raw_tokens,
                optimized_tokens=opt_tokens,
            )
            return ReadDecision(
                allow=False,
                reason=msg,
                event_type="read_dedup",
                raw_tokens=raw_tokens,
                optimized_tokens=opt_tokens,
            )

        return ReadDecision(allow=True)

    # 2. ESQUELETONIZAÇÃO AST PARA ARQUIVOS GRANDES LIDOS SEM OFFSET/LIMIT
    if not is_sliced and total_lines >= skeleton_threshold and is_code_file(path):
        skeleton_text, _ = generate_file_skeleton(path, source)
        skeleton_tokens = estimate_tokens(skeleton_text)

        # Só substitui pelo esqueleto se ele for pelo menos 35% menor que o arquivo bruto
        if skeleton_tokens < int(raw_tokens * 0.65):
            db_instance.upsert_session_read(
                session_id=session_id,
                file_key=file_key,
                file_path=norm_path,
                sha256=file_sha256,
                line_count=total_lines,
                read_mode="skeleton",
            )
            reason_msg = (
                f"[CATO AST Skeleton Guard — Economia de {raw_tokens - skeleton_tokens} tokens "
                f"({raw_tokens} -> {skeleton_tokens} tok)]\n"
                f"O arquivo `{path.name}` possui {total_lines} linhas. Abaixo está o Esqueleto AST completo com "
                f"todas as classes, funções, assinaturas e seus números de linha exatos `[L<inicio>-L<fim>]`.\n"
                f"👉 Para inspecionar ou editar uma função específica, chame `Read` informando `offset` e `limit` "
                f"daquele bloco (ou chame `Read` sem parâmetros mais uma vez se realmente precisar do arquivo inteiro):\n\n"
                f"{skeleton_text}"
            )
            db_instance.record_telemetry(
                session_id=session_id,
                event_type="read_skeleton",
                tool_name="Read",
                target=f"{path.name} ({total_lines}L)",
                raw_tokens=raw_tokens,
                optimized_tokens=skeleton_tokens,
            )
            return ReadDecision(
                allow=False,
                reason=reason_msg,
                event_type="read_skeleton",
                raw_tokens=raw_tokens,
                optimized_tokens=skeleton_tokens,
            )

    # 3. LEITURA CIRÚRGICA FATIADA (`offset` / `limit`) OU ARQUIVO PEQUENO
    slice_lines = end_idx - start_idx
    if is_sliced and total_lines > slice_lines:
        full_file_tokens = estimate_tokens(source)
        # Contabiliza a economia da leitura fatiada apenas na 1ª vez que o arquivo é fatiado na sessão
        # (evitando somar o tamanho do arquivo inteiro múltiplas vezes se o Claude ler várias fatias)
        if (full_file_tokens - raw_tokens >= 100) and not db_instance.has_file_path_read(session_id, norm_path):
            db_instance.record_telemetry(
                session_id=session_id,
                event_type="read_slice",
                tool_name="Read",
                target=f"{path.name} (L{start_idx + 1}-L{end_idx} de {total_lines}L)",
                raw_tokens=full_file_tokens,
                optimized_tokens=raw_tokens,
            )

    db_instance.upsert_session_read(
        session_id=session_id,
        file_key=file_key,
        file_path=norm_path,
        sha256=file_sha256,
        line_count=slice_lines,
        read_mode="slice" if is_sliced else "full",
    )
    return ReadDecision(allow=True)
