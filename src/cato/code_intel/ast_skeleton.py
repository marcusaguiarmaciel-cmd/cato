"""
Extrator de Esqueleto Estrutural AST (Parte 3 do CATO).
Converte arquivos de código extensos (>300 linhas) em mapas estruturais compactos (~70-85% menores)
contendo imports, classes, tipos, docstrings e assinaturas completas de funções/métodos
com intervalos exatos de linha `[L<inicio>-L<fim>]`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Optional, Tuple

SUPPORTED_CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".java",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".kt",
    ".swift",
}


def is_code_file(file_path: str | Path) -> bool:
    """Verifica se a extensão do arquivo é suportada pelo extrator de esqueleto AST."""
    return Path(file_path).suffix.lower() in SUPPORTED_CODE_EXTENSIONS


def _format_python_func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: List[str]) -> str:
    """Extrai a assinatura tipada exata de uma função/método Python a partir da AST."""
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    try:
        args_str = ast.unparse(node.args)
    except Exception:
        args_str = "..."
    ret_str = ""
    if node.returns is not None:
        try:
            ret_str = f" -> {ast.unparse(node.returns)}"
        except Exception:
            ret_str = ""
    return f"{prefix}{node.name}({args_str}){ret_str}:"


def extract_python_skeleton(source: str, file_path: str = "") -> str:
    """
    Usa o módulo `ast` nativo do Python para gerar um esqueleto 100% preciso
    com números de linha `[L<start>-L<end>]`, decoradores, assinaturas tipadas e docstrings.
    """
    lines = source.splitlines()
    total_lines = len(lines)
    tree = ast.parse(source, filename=file_path or "<source>")

    out: List[str] = [
        f"# [CATO AST Skeleton] {file_path} ({total_lines} linhas totais)",
        "# Use Read(file_path, offset=L_INICIO, limit=QTD_LINHAS) para ler apenas a função/classe desejada.",
        "",
    ]

    imports: List[str] = []
    for node in tree.body:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start)

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            try:
                imports.append(f"[L{start}] {ast.unparse(node)}")
            except Exception:
                imports.append(f"[L{start}] {lines[start - 1].strip()}")

        elif isinstance(node, ast.Assign):
            # Captura constantes de nível de módulo (UPPER_CASE)
            targets = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()]
            if targets:
                snippet = lines[start - 1].strip()
                if len(snippet) > 90:
                    snippet = snippet[:87] + "..."
                out.append(f"[L{start}-L{end}] {snippet}")

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                try:
                    out.append(f"@{ast.unparse(dec)}")
                except Exception:
                    pass
            sig = _format_python_func_sig(node, lines)
            doc = ast.get_docstring(node)
            doc_summary = f'  """{doc.splitlines()[0].strip()}"""' if doc else ""
            out.append(f"[L{start}-L{end}] {sig}")
            if doc_summary:
                out.append(doc_summary)
            out.append(f"    # ... ({max(0, end - start)} linhas de implementação omitidas) ...")
            out.append("")

        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if node.bases else []
            base_str = f"({', '.join(bases)})" if bases else ""
            out.append(f"[L{start}-L{end}] class {node.name}{base_str}:")
            doc = ast.get_docstring(node)
            if doc:
                out.append(f'    """{doc.splitlines()[0].strip()}"""')

            for item in node.body:
                m_start = getattr(item, "lineno", start)
                m_end = getattr(item, "end_lineno", m_start)
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sig = _format_python_func_sig(item, lines)
                    m_doc = ast.get_docstring(item)
                    m_doc_str = f"  # {m_doc.splitlines()[0].strip()}" if m_doc else ""
                    out.append(f"    [L{m_start}-L{m_end}] {sig}{m_doc_str}")
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    try:
                        out.append(f"    [L{m_start}] {ast.unparse(item)}")
                    except Exception:
                        pass
            out.append("")

    if imports:
        header_imports = imports[:25]
        if len(imports) > 25:
            header_imports.append(f"# ... (+{len(imports) - 25} imports omitidos)")
        out = out[:3] + header_imports + [""] + out[3:]

    return "\n".join(out).strip()


# Padrões de declaração para linguagens baseadas em chaves (TS/JS/Rust/Go/Java/C#/C++)
DECLARATION_PATTERNS = [
    # TypeScript / JavaScript
    re.compile(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
        r"(?:class|interface|type|enum|function)\s+([A-Za-z0-9_$]+)"
    ),
    re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_$]+)\s*=>"
    ),
    # Métodos dentro de classes TS/JS/Java/C#
    re.compile(
        r"^\s*(?:public|private|protected|static|readonly|async|override|virtual)+\s+"
        r"(?:[A-Za-z0-9_<>\[\],\s]+\s+)?([A-Za-z0-9_$]+)\s*\("
    ),
    # Rust
    re.compile(
        r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
        r"(?:fn|struct|enum|trait|impl|mod|type)\s+([A-Za-z0-9_<>:]+)"
    ),
    # Go
    re.compile(r"^\s*(?:func|type)\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)"),
]

IMPORT_PATTERN = re.compile(
    r"^\s*(?:import\s+|export\s+.*from\s+|from\s+\S+\s+import\s+|use\s+|package\s+|#include\s+|using\s+)"
)


def extract_brace_language_skeleton(source: str, file_path: str = "") -> str:
    """
    Extrai o esqueleto estrutural para linguagens baseadas em chaves (TS, JS, Rust, Go, Java, C#, C++)
    rastreando o balanceamento de chaves `{ ... }` para determinar exatamente o intervalo `[L<start>-L<end>]`
    de cada classe, interface, struct ou função.
    """
    lines = source.splitlines()
    total_lines = len(lines)
    out: List[str] = [
        f"// [CATO Structural Skeleton] {file_path} ({total_lines} linhas totais)",
        "// Use Read(file_path, offset=L_INICIO, limit=QTD_LINHAS) para ler apenas o símbolo desejado.",
        "",
    ]

    imports: List[str] = []
    symbols: List[str] = []

    i = 0
    while i < total_lines:
        line = lines[i]
        lineno = i + 1
        stripped = line.strip()

        if not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            i += 1
            continue

        if IMPORT_PATTERN.match(line):
            if len(imports) < 25:
                imports.append(f"[L{lineno}] {stripped}")
            elif len(imports) == 25:
                imports.append("// ... (imports adicionais omitidos)")
            i += 1
            continue

        matched_decl = any(pat.match(line) for pat in DECLARATION_PATTERNS)
        if matched_decl:
            start_line = lineno
            indent = len(line) - len(line.lstrip())
            # Se for uma classe/interface/impl no nível raiz (indent == 0), queremos listar os métodos internos também
            is_container = bool(
                re.search(r"\b(class|interface|trait|impl)\b", stripped) and indent <= 2
            )

            # Calcula onde o bloco fecha contando chaves
            brace_balance = line.count("{") - line.count("}")
            end_idx = i
            if "{" in line and brace_balance > 0:
                j = i + 1
                while j < total_lines and brace_balance > 0:
                    brace_balance += lines[j].count("{") - lines[j].count("}")
                    end_idx = j
                    j += 1
            elif "{" not in line and i + 1 < total_lines and "{" in lines[i + 1]:
                brace_balance = lines[i + 1].count("{") - lines[i + 1].count("}")
                end_idx = i + 1
                j = i + 2
                while j < total_lines and brace_balance > 0:
                    brace_balance += lines[j].count("{") - lines[j].count("}")
                    end_idx = j
                    j += 1

            end_line = end_idx + 1
            sig_clean = stripped.rstrip("{").strip()
            if len(sig_clean) > 110:
                sig_clean = sig_clean[:107] + "..."

            prefix_pad = "  " if indent > 0 else ""
            symbols.append(f"{prefix_pad}[L{start_line}-L{end_line}] {sig_clean} {{ ... }}")

            # Se for container (class/interface/impl), avança apenas 1 linha para capturar assinaturas dos métodos filhos;
            # se for função/método, pula todo o corpo da função até `end_idx`!
            if is_container:
                i += 1
            else:
                i = max(i + 1, end_idx + 1)
            continue

        i += 1

    if imports:
        out.extend(imports)
        out.append("")
    if symbols:
        out.extend(symbols)
    else:
        # Fallback caso o arquivo tenha formato não-padrão: exibe as primeiras 50 linhas
        out.extend([f"[L{idx+1}] {ln}" for idx, ln in enumerate(lines[:50])])

    return "\n".join(out).strip()


def generate_file_skeleton(file_path: str | Path, source: Optional[str] = None) -> Tuple[str, int]:
    """
    Gera o esqueleto AST/estrutural de `file_path`.
    Retorna `(skeleton_text, total_lines)`.
    """
    path_obj = Path(file_path)
    if source is None:
        source = path_obj.read_text(encoding="utf-8", errors="replace")

    total_lines = len(source.splitlines())
    ext = path_obj.suffix.lower()

    if ext == ".py":
        try:
            return extract_python_skeleton(source, str(file_path)), total_lines
        except SyntaxError:
            # Se o arquivo Python tiver erro de sintaxe, cai para o extrator estrutural tolerante a falhas
            return extract_brace_language_skeleton(source, str(file_path)), total_lines
    else:
        return extract_brace_language_skeleton(source, str(file_path)), total_lines
