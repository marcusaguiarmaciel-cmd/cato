# ⚡ CATO (`Claude Adaptive Token Optimizer`)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-success.svg)](pyproject.toml)
[![Tests Passing](https://img.shields.io/badge/tests-16%2F16%20passing-brightgreen.svg)](tests/test_cato.py)

> **[🇺🇸 English version available here](README.md)**

CATO reduz o custo do **Claude Code** atacando o que os dados reais mostram que pesa na fatura —
**tamanho de contexto × número de turnos** e **cache misses** — e mostra o custo **medido**, calculado
dos campos `usage` faturados nos transcripts do Claude Code com a tabela oficial de preços.
Funciona junto com o [RTK](https://github.com/rtk-ai/rtk). Zero dependências.

---

## 📊 O que os dados reais mostram

Saída de `cato report` em 30 transcripts reais (8.300 chamadas de API, Opus 5.5 como modelo principal):

| Medida | Valor |
| :--- | :--- |
| Custo total (equivalente de API) | **$1.280** |
| Releitura de contexto (cache read) | **56%** |
| Escrita de cache com TTL de 1h | 29% |
| Saída (output) | 14% |
| Entrada sem cache | ~0% |
| Custo com contexto acima de 400k tokens | **60%** |
| Reescritas de cache após > 1h parado | 38x, **$138** |
| Reescritas por troca de modelo no meio da sessão | 21x, $45 |
| Releituras idênticas de arquivo | 49x, só 1.744 tokens (o Claude Code já deduplica) |
| Economia declarada pelo RTK (`rtk gain`) | 277 mil tokens — menos de 0,01% dos tokens relidos |

Conclusões que guiam o CATO:

1. **Comprimir saídas individuais move pouco a fatura.** Dois estudos controlados do RTK (JetBrains,
   425 pares; Quesma, 1.740 tentativas) não acharam economia líquida (−5% a +7,6%), porque compressão
   pode gerar turnos extras e o baseline ignora o truncamento nativo do Claude Code.
2. **O que custa é o contexto relido a cada turno** e as **reescritas de cache** quando o TTL expira.
3. **Negar leituras custa um turno** (≈ contexto × preço de cache read), então só vale com ganho medido.

---

## 🏗️ Como funciona

| Camada | Arquivo | O que faz |
| :--- | :--- | :--- |
| **Relatório real** | `telemetry/usage_report.py`, `telemetry/pricing.py` | Custo por categoria, por faixa de contexto, cache misses por causa (pausa > TTL, troca de modelo, compactação), tokens por ferramenta (imagens pela dimensão real), comandos de shell mais verbosos e atribuição por dia de RTK/CATO. Dedup por `message.id` e `tool_use_id`. |
| **Guard de custo** | `hooks/user_prompt_submit.py` | Antes do prompt ir para a API: se o cache expirou com contexto grande, ou o contexto cruzou 250k/400k/600k, segura o prompt **uma vez** mostrando o custo concreto e as opções (`/clear`, `/compact`). Reenviar segue normal. `/comandos` nunca são bloqueados. |
| **Bash: RTK + CATO** | `hooks/pre_tool_use.py`, `hooks/post_tool_use.py` | O RTK reescreve comandos (se o hook global dele não estiver instalado, o CATO delega a ele — só para comandos que suas regras de permissão já aprovam). O CATO comprime a **saída real** em `PostToolUse` (`updatedToolOutput`) só do que o RTK não tratou: nunca erros, leitura de código (`sed`, `cat`, `grep`…) nem saídas acima do limite nativo de 30k caracteres. Log bruto em `~/.cato/logs/`. |
| **Verificação de sintaxe** | `hooks/post_tool_use.py` | Após `Edit`/`Write` em `.py`/`.json`, avisa o erro de sintaxe no mesmo turno. |
| **Sandbox de MCP** | `hooks/post_tool_use.py`, `storage/db.py` | Respostas MCP de texto > 1.500 tokens vão para SQLite FTS5 (busca BM25 via `cato_search_sandbox`). |
| **Pós-compactação** | `hooks/session_lifecycle.py` | Salva o estado do Git antes do `/compact` e o reinjeta depois. |
| **Read guard (opt-in)** | `code_intel/dedup.py`, `code_intel/ast_skeleton.py` | Esqueleto AST/dedup de `Read`. Desligado por padrão; `CATO_READ_GUARD=1` liga. |
| **Mapa de código** | `code_intel/repomap.py`, `mcp_server/server.py` | Servidor MCP com `cato_repo_map` (PageRank) e `cato_read_symbol`. |

---

## 🚀 Instalação

```bash
pip install git+https://github.com/marcusaguiarmaciel-cmd/cato.git
cato init --global      # ou `cato init` dentro de um projeto
cato doctor
```

> [!IMPORTANT]
> **Rode `cato init` em cada máquina, inclusive depois de clonar este repositório.** Os hooks em `.claude/settings.json` e o servidor MCP em `.mcp.json` apontam para os caminhos absolutos do *seu* Python e da *sua* instalação do CATO, por isso esses arquivos são gerados por máquina e, de propósito, **não são commitados** (estão no `.gitignore`). Um clone novo não tem os hooks do CATO até você rodar `cato init`.

---

## 💻 Comandos

| Comando | Descrição |
| :--- | :--- |
| `cato report [--days N] [--project P] [--json]` | Custo real dos transcripts do Claude Code. |
| `cato stats` | Relatório real (30 dias) + intervenções registradas pelo CATO. |
| `cato dashboard [--port 8765]` | Painel web com o custo real (claro/escuro). |
| `cato statusline` | Para a `statusLine` do Claude Code: contexto atual, custo do próximo turno, minutos até o cache expirar, custo do dia. |
| `cato doctor` | Audita a configuração e dá recomendações a partir do seu uso real. |
| `cato init [--global]` | Instala hooks, regras, subagente e servidor MCP sem apagar suas configurações. |
| `cato exec <cmd>` | Executa um comando comprimindo a saída (uso manual). |
| `cato repomap` / `cato symbol` / `cato skeleton` / `cato search-sandbox` | Ferramentas de navegação de código e busca no sandbox. |

### Variáveis de ambiente

| Variável | Padrão | Efeito |
| :--- | :--- | :--- |
| `CATO_GUARD` | ligado | `0` desliga o guard de custo. |
| `CATO_CONTEXT_WARN` | `250000,400000,600000` | Faixas de contexto que disparam o aviso. |
| `CATO_BASH_COMPRESS` | ligado | `0` desliga a compressão de saída de Bash/PowerShell. |
| `CATO_READ_GUARD` | desligado | `1` liga o esqueleto AST/dedup de `Read`. |
| `~/.cato/pricing.json` | tabela oficial | Sobrescreve preços por modelo. |

---

## 🧠 Prompt Cache

O CATO só altera resultados **novos** de ferramentas (sufixo da conversa) e nunca o histórico, o
system prompt ou a lista de ferramentas — então o prefixo em cache continua válido.

---

## 🧪 Testes

```bash
python -m unittest discover -s tests -v
```

Os **16 testes** usam transcripts sintéticos e um `CATO_HOME` temporário; não tocam em `~/.cato` nem
nos seus transcripts.

---

## 📄 Licença

MIT — veja [`LICENSE`](LICENSE). Criado por **[Marcus Maciel](https://github.com/marcusaguiarmaciel-cmd)** (2026).
