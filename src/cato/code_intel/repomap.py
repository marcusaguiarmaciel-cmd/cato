"""
Módulo de Mapa de Repositório com Grafo de Dependências + Personalized PageRank
e Extração Cirúrgica de Símbolos (Parte 4 do CATO).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from cato.code_intel.ast_skeleton import (
    DECLARATION_PATTERNS,
    generate_file_skeleton,
    is_code_file,
)
from cato.storage.db import estimate_tokens

IGNORED_DIRS = {
    ".git",
    ".claude",
    ".cato",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    "target",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}


def extract_symbol_source(file_path: str | Path, symbol_query: str) -> Tuple[str, int, int]:
    """
    Extrai cirurgicamente apenas o código-fonte do símbolo `symbol_query` (ex: `MyClass`,
    `my_func` ou `MyClass.my_method`) sem carregar o restante do arquivo.
    Retorna `(numbered_source, start_line, end_line)`.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")

    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    total_lines = len(lines)
    parts = [p.strip() for p in symbol_query.split(".") if p.strip()]
    target_name = parts[-1] if parts else symbol_query.strip()
    parent_name = parts[0] if len(parts) > 1 else None

    # 1. Extração via AST exata para arquivos Python
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(source, filename=str(path))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == target_name and parent_name is None:
                        s = getattr(node, "lineno", 1)
                        e = getattr(node, "end_lineno", s)
                        # Inclui decoradores acima
                        if getattr(node, "decorator_list", None):
                            s = min(s, *(getattr(d, "lineno", s) for d in node.decorator_list))
                        snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(s, e + 1))
                        return snippet, s, e

                    if isinstance(node, ast.ClassDef) and (parent_name is None or node.name == parent_name):
                        for child in node.body:
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_name:
                                s = getattr(child, "lineno", 1)
                                e = getattr(child, "end_lineno", s)
                                if getattr(child, "decorator_list", None):
                                    s = min(s, *(getattr(d, "lineno", s) for d in child.decorator_list))
                                snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(s, e + 1))
                                return snippet, s, e
        except SyntaxError:
            pass

    # 2. Extração estrutural por balanceamento de chaves/indentação para TS/JS/Rust/Go/Java/Python-fallback
    pattern = re.compile(rf"\b{re.escape(target_name)}\b")
    for idx, line in enumerate(lines):
        if pattern.search(line) and any(
            kw in line for kw in ("def ", "class ", "function ", "interface ", "type ", "struct ", "enum ", "fn ", "func ", "public ", "private ", "async ", "const ")
        ):
            start_line = idx + 1
            brace_balance = line.count("{") - line.count("}")
            end_idx = idx
            if "{" in line and brace_balance > 0:
                j = idx + 1
                while j < total_lines and brace_balance > 0:
                    brace_balance += lines[j].count("{") - lines[j].count("}")
                    end_idx = j
                    j += 1
            else:
                end_idx = min(total_lines - 1, idx + 45)
            end_line = end_idx + 1
            snippet = "\n".join(f"{ln_no}: {lines[ln_no - 1]}" for ln_no in range(start_line, end_line + 1))
            return snippet, start_line, end_line

    raise ValueError(f"Símbolo '{symbol_query}' não encontrado em {file_path}")


def _extract_file_defs_and_words(path: Path, source: str) -> Tuple[Set[str], Set[str]]:
    """Extrai o conjunto de símbolos definidos no arquivo e o conjunto de identificadores referenciados."""
    defs: Set[str] = set()
    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if len(node.name) >= 3 and not node.name.startswith("__"):
                        defs.add(node.name)
        except Exception:
            pass
    else:
        for line in source.splitlines():
            for pat in DECLARATION_PATTERNS:
                m = pat.match(line)
                if m and len(m.group(1)) >= 3:
                    defs.add(m.group(1))
                    break

    words = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", source))
    return defs, words


def _personalized_pagerank(
    nodes: List[str],
    edges: Dict[str, Dict[str, float]],
    personalization: Dict[str, float],
    alpha: float = 0.85,
    iterations: int = 18,
) -> Dict[str, float]:
    """
    Implementação pura em Python (zero dependências externas) do algoritmo Personalized PageRank
    sobre o grafo de dependências entre arquivos de código.
    """
    n = len(nodes)
    if n == 0:
        return {}

    total_p = sum(personalization.values())
    if total_p > 0:
        pers = {k: personalization.get(k, 0.0) / total_p for k in nodes}
    else:
        pers = {k: 1.0 / n for k in nodes}

    ranks = dict(pers)

    for _ in range(iterations):
        new_ranks = {k: (1.0 - alpha) * pers[k] for k in nodes}
        dangling_mass = 0.0

        for u in nodes:
            out_edges = edges.get(u, {})
            total_w = sum(out_edges.values())
            if total_w > 0:
                for v, w in out_edges.items():
                    new_ranks[v] = new_ranks.get(v, 0.0) + alpha * ranks[u] * (w / total_w)
            else:
                dangling_mass += ranks[u]

        if dangling_mass > 0:
            for k in nodes:
                new_ranks[k] += alpha * dangling_mass * pers[k]

        ranks = new_ranks

    return ranks


def build_repository_map(
    root_dir: str | Path,
    focus_query: Optional[str] = None,
    token_budget: int = 1200,
    max_files_scanned: int = 300,
) -> str:
    """
    Constrói um Mapa de Repositório estilo Aider usando extração AST + Personalized PageRank,
    empacotando os esqueletos dos arquivos mais importantes dentro do `token_budget`.
    """
    root = Path(root_dir).resolve()
    code_files: List[Path] = []

    for p in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        if p.is_file() and is_code_file(p):
            code_files.append(p)
            if len(code_files) >= max_files_scanned:
                break

    if not code_files:
        return f"# [CATO RepoMap] Nenhum arquivo de código encontrado em {root}"

    rel_paths: List[str] = []
    sources: Dict[str, str] = {}
    defs_by_file: Dict[str, Set[str]] = {}
    words_by_file: Dict[str, Set[str]] = {}
    symbol_to_defining_files: Dict[str, List[str]] = {}

    for p in code_files:
        rel = str(p.relative_to(root)).replace("\\", "/")
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel_paths.append(rel)
        sources[rel] = src
        f_defs, f_words = _extract_file_defs_and_words(p, src)
        defs_by_file[rel] = f_defs
        words_by_file[rel] = f_words
        for sym in f_defs:
            symbol_to_defining_files.setdefault(sym, []).append(rel)

    # Constrói arestas: se arquivo U referencia símbolo S definido no arquivo V -> aresta U -> V
    edges: Dict[str, Dict[str, float]] = {u: {} for u in rel_paths}
    for u in rel_paths:
        for word in words_by_file[u]:
            targets = symbol_to_defining_files.get(word)
            if not targets:
                continue
            # Símbolos mais específicos (definidos em poucos arquivos e com nomes mais longos) têm maior peso
            weight = (1.5 if len(word) >= 8 else 1.0) / len(targets)
            for v in targets:
                if u != v:
                    edges[u][v] = edges[u].get(v, 0.0) + weight

    # Define vetor de personalização baseado em `focus_query`
    personalization: Dict[str, float] = {u: 1.0 for u in rel_paths}
    if focus_query:
        query_terms = [t.lower() for t in re.split(r"[\s,;:/\\]+", focus_query) if len(t) >= 2]
        for u in rel_paths:
            u_lower = u.lower()
            score_boost = 1.0
            for term in query_terms:
                if term in u_lower:
                    score_boost += 15.0
                elif any(term in d.lower() for d in defs_by_file[u]):
                    score_boost += 8.0
            personalization[u] = score_boost

    ranks = _personalized_pagerank(rel_paths, edges, personalization)
    ranked_files = sorted(rel_paths, key=lambda f: ranks.get(f, 0.0), reverse=True)

    # Empacota esqueletos AST respeitando `token_budget`
    header = [
        f"# [CATO PageRank Repository Map — {len(rel_paths)} arquivos analisados | Budget: {token_budget} tokens]",
        "",
    ]
    used_tokens = estimate_tokens("\n".join(header))
    sections: List[str] = []

    for rel in ranked_files:
        skel, total_lines = generate_file_skeleton(root / rel, sources[rel])
        # Remove as 2 linhas de comentário repetitivas do topo de cada esqueleto individual
        skel_lines = [
            ln for ln in skel.splitlines() if not ln.startswith("# [CATO") and not ln.startswith("# Use Read") and not ln.startswith("// [CATO") and not ln.startswith("// Use Read")
        ]
        compact_block = f"## {rel} ({total_lines}L | PageRank: {ranks[rel]:.3f})\n" + "\n".join(skel_lines).strip() + "\n"
        block_tokens = estimate_tokens(compact_block)

        if used_tokens + block_tokens > token_budget and sections:
            # Lista os demais arquivos apenas pelo caminho e símbolos principais
            remaining = ranked_files[len(sections) : len(sections) + 15]
            if remaining:
                sections.append(f"# Outros arquivos relevantes: {', '.join(remaining)}")
            break

        sections.append(compact_block)
        used_tokens += block_tokens

    return "\n".join(header + sections).strip()
