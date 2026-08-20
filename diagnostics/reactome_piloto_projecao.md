# Piloto de projeção Reactome → grafo assinado gene×gene

**Data:** 2026-08-20
**Motivo:** avaliar Reactome como substrato alternativo/complementar ao KEGG para o motor PFS,
antes de comprometer um parser completo.
**Conclusão curta:** o ganho aparente de volume é **inflação combinatória**, não mais biologia.
Reactome vale por **cobertura de genes**, não por densidade de arestas — e exige esquema de peso
que divida a evidência pela expansão de complexo.

## Contexto — por que se olhou para o Reactome

Medição prévia sobre a execução `fase4-pfs-v2-372vias` mostrou que os achados se concentram
onde o grafo KEGG **não existe**:

| Classe do grafo | Pares | Achados | Enriquecimento |
|---|---|---|---|
| ≥50% arestas assinadas | 5.570 (83,0%) | 25 (50%) | 0,60× (depletado) |
| <50% assinada | 422 (6,3%) | 10 (20%) | 3,18× |
| Com arestas, 0 assinada | 169 (2,5%) | 0 | 0,00× |
| Sem aresta nenhuma | 549 (8,2%) | 15 (30%) | **3,67×** |

Numa via sem arestas o RWR não propaga — o escore mede concordância gene a gene, não atividade
de via. Daí a hipótese: topologia melhor produziria achados melhores.

## Licença e acesso

- **Licença CC0** (domínio público). Ao contrário do KEGG, **não** há restrição de redistribuição:
  fixtures reais podem ser versionadas.
- Release verificado: **97**.
- Host `reactome.org` **autorizado pelo usuário** em 2026-08-20 para a allowlist de egress.

### Armadilha: os arquivos de interação prontos NÃO servem

`reactome.homo_sapiens.interactions.tab-delimited.txt` (58 MB) e o PSI-MITAB (209 MB) são
**coparticipação sem sinal e sem direção**:

| Tipo de interação | Amostra (124.866 linhas) |
|---|---|
| `physical association` | 113.560 (91,0%) |
| `enzymatic reaction` | 7.881 (6,3%) |
| demais tipos bioquímicos | 3.425 (2,7%) |

Nenhum tipo `positive/negative regulation`; todos os papéis vêm `unspecified role`.
Ingerir esses arquivos reconstruiria o mesmo problema do `ECrel` do KEGG.

### Onde o sinal realmente vive

Entidades de regulação (não exportadas nos arquivos planos):

| Classe | Instâncias |
|---|---|
| PositiveRegulation | 4.653 |
| NegativeRegulation | 3.299 |
| PositiveGeneExpressionRegulation | 1.355 |
| NegativeGeneExpressionRegulation | 352 |
| **Total assinado** | **9.659** |

Rota prática confirmada: **SBML por via** (`ContentService/exporter/event/{stId}.sbml`),
cujos modificadores carregam termos SBO. Significados confirmados contra a ontologia (EBI OLS),
não contra memória:

| Termo | Significado | Sinal |
|---|---|---|
| `SBO:0000020` | inhibitor | −1 |
| `SBO:0000459` | stimulator | +1 |
| `SBO:0000013` | catalyst | sem sinal |
| `SBO:0000010` / `SBO:0000011` | reactant / product | — |

Espécie→proteína sai de `bqbiol:is` (proteína simples) e `bqbiol:hasPart` (complexo).

### Custos de download

| Rota | Volume |
|---|---|
| SBML por via | ~200 KB–2 MB por via, sem download em massa |
| BioPAX completo (`biopax.zip`) | 165,9 MB |
| Dump Neo4j (`reactome.graphdb.tgz`) | 431,5 MB |

## Resultado do piloto

Quatro vias projetadas (regulador assinado → produtos da reação, complexos expandidos),
UniProt→símbolo resolvido para 636/637 acessos (99,8%).

| Reactome | KEGG | Assinadas RX | Assinadas KEGG | Sobrepostas | Inibição RX | Genes novos |
|---|---|---|---|---|---|---|
| R-HSA-3700989 (TP53) | hsa04115 | 598 | 57 | 19 | 22% | 166 |
| R-HSA-5683057 (MAPK) | hsa04010 | 1.332 | 129 | 10 | 95% | 68 |
| R-HSA-1257604 (PIP3→AKT) | hsa04151 | 104 | 53 | 2 | 98% | 33 |
| R-HSA-2219528 (PI3K/AKT cancer) | hsa04151 | **0** | 53 | 0 | — | 0 |

### O número que desmonta o ganho aparente

Arestas geradas por **fato curado** (um `modifierSpeciesReference` assinado):

| Via | Fatos curados | Arestas geradas | Média | Pior caso |
|---|---|---|---|---|
| MAPK | **27** | 5.671 | **210** | **1.225 de um só fato** |
| TP53 | 70 | 607 | 8,7 | 352 |
| PIP3→AKT | 41 | 846 | 20,6 | 64 |

As 1.332 arestas de MAPK repousam sobre 27 fatos. Quatro reguladores são complexos de
35 proteínas; cada proteína vira aresta para cada produto. Os 95% de inibição são artefato
desses quatro complexos.

**No nível de fato curado, Reactome é comparável ou menor que o KEGG nessas vias**
(27–70 fatos contra 53–129 arestas assinadas KEGG).

## Consequências para o desenho

1. **Peso obrigatório contra expansão.** Sem algo como `1/(|regulador|·|produtos|)`, um único
   fato sobre um complexo de 35 proteínas domina o RWR — o mesmo tipo de artefato que se busca
   eliminar.
2. **O ganho é cobertura, não densidade.** 166/68/33 genes ausentes do KEGG. A sobreposição de
   arestas é mínima (2–19): Reactome **não valida** o KEGG, é um grafo diferente.
3. **Vias de regulador simples se beneficiam.** TP53 tem 48 dos 70 reguladores como proteína
   única — sinal limpo. MAPK é o oposto.
4. **Nem o Reactome escapa de vias sem sinal** — R-HSA-2219528 rendeu zero arestas assinadas.

## Comparação de substrato (KEGG carregado hoje)

| | Arestas | Assinadas | Ativação | Inibição |
|---|---|---|---|---|
| KEGG, 372 vias | 22.020 | 12.533 (56,9%) | 86,9% | **13,1%** |

O KEGG assinado é quase todo ativação — um grafo em que 87% das arestas empurram na mesma
direção propaga quase como grafo sem sinal.

## Reprodutibilidade

Script do piloto e SBMLs ficaram fora do repo (scratchpad de sessão). Para refazer:
baixar os quatro SBML via `ContentService/exporter/event/{stId}.sbml`, parsear
`listOfModifiers` por `sboTerm`, expandir espécies por `bqbiol:hasPart`/`bqbiol:is`,
resolver UniProt→símbolo via `rest.uniprot.org/uniprotkb/accessions`.

**Host adicional que um carregador de produção exigiria:** `rest.uniprot.org` (para
UniProt→símbolo) — **ainda não autorizado** na allowlist.
