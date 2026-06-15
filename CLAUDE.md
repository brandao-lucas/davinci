# CLAUDE.md — DaVinci

Este arquivo é a porta de entrada para qualquer agente Claude que trabalhe neste repositório. Ler inteiro antes da primeira edição.

## Visão geral

**DaVinci** é uma plataforma de pesquisa bioinformática do ecossistema **PlatOmics** (BioHub Solutions). Permite ao pesquisador ingerir literatura (PubMed/PMC) e metadados ômicos (GEO, SRA, BioProject, GWAS, ArrayExpress, TCGA) em projetos, aplicar NER + categorização, curar com auditoria e exportar para análise / IA generativa.

Detalhes: [.claude/info/contexto_projeto.md](.claude/info/contexto_projeto.md).

## Stack

| Camada | Tecnologia |
|---|---|
| Backend | Django 6.0 + DRF + Celery |
| Engine | Rust (PyO3 / Maturin) |
| Banco | PostgreSQL 16 + FTS |
| Fila | Redis + Celery |
| Auth | Firebase |
| Frontend | Next.js 16.2 + React 19 + TypeScript + Tailwind + Shadcn/ui |
| Desktop | Tauri 2 |

## Estrutura do repositório

```
davinci/
├── apps/                    # Django: accounts/, core/
├── config/                  # Django settings, Celery app, URLs raiz
├── rust_src/                # Engine Rust (ncbi/, omics/, db/, categorization/)
├── davinci-frontend/        # Next.js 16
│   └── src/ (app/, components/, lib/)
├── guidelines/              # DOCS CANÔNICOS — leitura obrigatória, não editar
├── scripts/                 # utilitários (seeds)
├── diagnostics/             # logs de validação / diagnóstico
├── docker-compose.yml       # Postgres + Redis dev
├── manage.py
└── .claude/                 # orquestração de agentes (este diretório)
    ├── agents/              # 9 agentes especializados
    ├── skills/              # 7 skills reutilizáveis
    ├── info/                # documentação viva (mantida pelo codice)
    ├── plans/               # planos de execução (escritos pelo fodao)
    ├── changelog/           # dossiês de mudança (escritos pelo mi6 e 007)
    ├── agent-memory/        # memória persistente por agente
    └── settings.local.json  # permissões Claude Code
```

## Regras de ouro

### Regra #-1 — Escopo estrito

Agentes fazem **apenas** o que foi pedido. Identificaram algo útil adjacente? **Propõem antes** de executar. Nada de refactor oportunista, nada de "limpar de passagem", nada de adicionar feature não solicitada.

### Regra #0 — Fronteira de camadas

| Camada | Pasta | Agente |
|---|---|---|
| Django backend | `apps/`, `config/`, `scripts/` | **vitruvio** |
| Rust engine | `rust_src/` | **ferris** |
| Frontend | `davinci-frontend/src/` | **atelier** |
| Schema / dados | `apps/*/models.py` + migrations | **cartografo** |

Um agente de camada **nunca** edita outra camada sem handoff explícito.

### Regra #1 — Django nunca processa dados brutos

Fetch HTTP em massa, parse XML, NER, COPY bulk insert vivem em Rust. Django orquestra via `IngestionJob` + Celery. Ver skill `django-rust-boundary`.

### Regra #2 — Rastreabilidade de curadoria é inegociável

Nunca perder `curated_at`, `exclusion_reason`, `notes`. Delete de registros curados é proibido — use `status = excluded`. Ver skill `curation-audit-trail`.

### Regra #3 — Isolamento por usuário

Todo queryset filtra por `request.user`. Todo serializer não vaza campo sensível. Todo endpoint testado contra acesso cruzado entre usuários. Ver skill `firebase-auth-guard`.

### Regra #4 — Frontend lê a doc antes de escrever

Next.js 16 tem breaking changes vs. training data. Antes de editar `davinci-frontend/`, ler `davinci-frontend/node_modules/next/dist/docs/`. Ver [davinci-frontend/AGENTS.md](davinci-frontend/AGENTS.md).

## Áreas protegidas

Só com autorização explícita do usuário:

- `config/firebase-service-account.json`
- `davinci-frontend/.env.local` e qualquer `.env*`
- `.venv/`, `node_modules/`, `rust_src/target/`, `.next/`
- `guidelines/` — docs canônicos; só o **codice** espelha em `.claude/info/`, **não** edita
- Migrations já aplicadas em `apps/*/migrations/` — só **cartografo** cria migrations **novas**

## Agentes

| Agente | Papel | Edita código? |
|---|---|---|
| [vitruvio](.claude/agents/vitruvio.md) | Backend Django / DRF / Celery | ✅ `apps/`, `config/` |
| [ferris](.claude/agents/ferris.md) | Engine Rust (PyO3) | ✅ `rust_src/` |
| [atelier](.claude/agents/atelier.md) | Frontend Next.js 16 | ✅ `davinci-frontend/src/` |
| [cartografo](.claude/agents/cartografo.md) | Schema / migrations / FTS | ✅ `models.py` + migrations |
| [sentinela](.claude/agents/sentinela.md) | QA — testes e lint | ✅ apenas `tests/` |
| [007](.claude/agents/007.md) | Auditoria de segurança | ❌ só laudo |
| [fodao](.claude/agents/fodao.md) | Planejador estratégico | ❌ só `.claude/plans/` |
| [mi6](.claude/agents/mi6.md) | Dossiê de mudanças | ❌ só `.claude/changelog/` |
| [codice](.claude/agents/codice.md) | Documentação viva | ❌ só `.claude/info/` |

### Invocações automáticas obrigatórias

- Após mudança em `apps/accounts/**`, `apps/core/views/**`, `config/settings/**`, `davinci-frontend/src/lib/firebase.ts`, `davinci-frontend/src/components/auth/**` → **007** audita.
- Antes de tarefa que cruze camadas ou tenha risco não-trivial → **fodao** propõe plano em `.claude/plans/`.
- Após mudança estrutural (model novo, fluxo novo, camada nova) → **codice** atualiza `.claude/info/`.

### Invocações sob comando (não-automáticas)

- **mi6** — só escreve dossiê em `.claude/changelog/` quando **o usuário solicita explicitamente**, após ter validado os ajustes feitos. Nenhum agente deve acionar o mi6 por conta própria.

## Skills

| Skill | Quando usar |
|---|---|
| [django-rust-boundary](.claude/skills/django-rust-boundary/SKILL.md) | Decidir onde vai a lógica de processamento |
| [ingestion-contract](.claude/skills/ingestion-contract/SKILL.md) | Adicionar fonte / alterar parser / debugar job |
| [firebase-auth-guard](.claude/skills/firebase-auth-guard/SKILL.md) | Criar endpoint / revisar vazamento |
| [curation-audit-trail](.claude/skills/curation-audit-trail/SKILL.md) | Alterar curadoria / bulk ops / delete |
| [frontend-architecture-nextjs16](.claude/skills/frontend-architecture-nextjs16/SKILL.md) | Criar página/componente/hook no front |
| [sensitive-data-handling](.claude/skills/sensitive-data-handling/SKILL.md) | Adicionar log / expor campo / criar config |
| [postgres-fts-patterns](.claude/skills/postgres-fts-patterns/SKILL.md) | Criar endpoint de busca/listagem / otimizar query |

## Estilo

- **Português (br)**, objetivo, sem emojis salvo pedido explícito
- **Tabelas markdown** quando ajudam leitura
- **Sem narrativa longa** em dossiês, planos e changelog — direto ao ponto
- **Links `[arquivo](caminho)`** para navegação rápida

## Comandos úteis (dev local)

```bash
# Infra
docker-compose up -d

# Django
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
.venv/bin/python manage.py test apps/ -v 2

# Rust
cd rust_src && cargo check
maturin develop --release   # recompila rust_engine para o venv

# Celery
celery -A config worker -l info

# Frontend
cd davinci-frontend && npm run dev
npm run lint && npm run build
```

## Checklist antes de fechar uma tarefa

1. Arquivos afetados estão na camada correta do agente responsável?
2. Skills aplicáveis foram seguidas?
3. Testes foram rodados pela **sentinela**?
4. **007** foi invocado se tocou auth / dados sensíveis?
5. **codice** atualizou `.claude/info/` se houve mudança estrutural?
6. (Sob comando do usuário, após validação manual) — **mi6** escreve dossiê em `.claude/changelog/`.

---

_Este arquivo evolui. Edições em `CLAUDE.md` são raras e deliberadas — tratam de regras do repo, não de documentação de código._
