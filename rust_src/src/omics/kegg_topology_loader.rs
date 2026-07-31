/// Loader de topologia KEGG (KGML) → `core_pathway` / `core_pathwaynode` / `core_pathwayedge`.
///
/// # Fluxo
///
/// Para cada `pathway_kegg_id` (ex.: `hsa04151`):
/// 1. Fetch `https://rest.kegg.jp/get/<id>/kgml` (streaming, teto de bytes M2).
/// 2. Parse XML `quick-xml` **em uma passada**:
///    - `<entry>` → `PathwayNode` (`node_type` do attr `type`, `kegg_ids` do attr `name`,
///      `gene_symbol` do `<graphics name>` — Decisão 6, `graphics_name` cru).
///    - `<relation>` → `PathwayEdge` (`source_node`=entry1, `target_node`=entry2,
///      `relation_type` do attr `type`, `subtypes` dos filhos `<subtype name>`,
///      `sign`+`interaction` pela tabela §Sinal).
/// 3. COPY UPSERT em `core_pathway`, `core_pathwaynode`, `core_pathwayedge` (ON CONFLICT NKs).
/// 4. Atualiza `n_nodes`/`n_edges`/`loaded_at` em `core_pathway`.
///
/// # §Sinal — mapeamento subtype.name → sign
///
/// | subtype.name             | sign | interaction |
/// |--------------------------|------|-------------|
/// | activation, expression   | +1   | activation / expression |
/// | inhibition, repression   | -1   | inhibition / repression |
/// | phosphorylation          | +1   | activation (ativante por padrão KEGG) |
/// | dephosphorylation        | -1   | inhibition |
/// | binding/association      | 0    | binding |
/// | indirect effect          | 0    | indirect |
/// | state change             | 0    | binding |
/// | (sem subtype)            | 0    | unknown |
/// | conflito activation+inhibition | 0 | unknown (ambos em subtypes) |
///
/// # Segurança (M2/M3)
///
/// - `validate_kegg_url`: prefixo `https://rest.kegg.jp/`, rejeita `..`.
/// - `validate_kegg_id`: `kegg_id` deve casar `^[a-z]{3,4}\d{5}$` — aplicado
///   antes de montar a URL e antes de montar o path de cache, impedindo que
///   `kegg_id` vire componente de path (`Path::join` com absoluto descarta a
///   base) ou injete segmentos na URL (hardening A3, laudo 007).
/// - Teto de bytes: 50 MB por KGML (arquivos KEGG são pequenos; cap generoso).
/// - Rate limit: KEGG documenta ≤ 3 req/s e proíbe bulk download sem
///   espaçamento. Com 372 vias (`hsa`, carga completa — Fase 3+), disparar
///   sem intervalo bloquearia o acesso. `load_kegg_topology_async` faz
///   throttle real entre downloads (não entre cache hits): mede o tempo
///   decorrido desde o início do fetch anterior via relógio monotônico
///   (`std::time::Instant`) e dorme só o restante até completar
///   `throttle_ms` (default 400 ms = 2,5 req/s, margem de segurança sob o
///   limite de 3 req/s). Ver `throttle_sleep_duration`.
/// - Cache local em `dest_dir/kegg/` (gitignored) — um KGML já cacheado
///   nunca gera requisição HTTP nem consome o throttle (ver `download_kgml`
///   vs. branch de cache em `load_kegg_topology_async`), então reexecuções
///   da mesma lista de vias são baratas.
/// - Fixtures de teste **SINTÉTICAS** — não commitar KGML bruto KEGG.
/// - `redirect::Policy::none()` no client HTTP (hardening A4, laudo 007):
///   `rest.kegg.jp` não depende de redirect (checado empiricamente); proibir
///   evita que a allowlist de host seja contornada por um 3xx para host externo.
///
/// # Manifesto retornado
///
/// `KeygTopologyManifest { n_pathways, n_nodes, n_edges, n_signed, n_unsigned, n_orphan_symbols, source_version }`
///
/// # Tipos de PK/FK (bug de produção corrigido — Fase 3)
///
/// Django usa `BigAutoField` para todas as PKs. Logo `core_pathway.id`,
/// `core_pathwaynode.id`/`pathway_id`, e `core_pathwayedge.id`/`pathway_id`/
/// `source_node_id`/`target_node_id` são **`bigint`** no Postgres — nunca `int4`.
/// `tokio-postgres` é estrito com tipos: ler uma coluna `int8` como `i32` causa
/// `PanicException` em runtime (não é pego por `cargo check`/testes com mock).
/// Toda leitura de PK/FK neste módulo **deve** usar `i64`, e qualquer tabela
/// temp de staging que receba essas colunas deve ser `BIGINT`, não `INT`.
///
/// # Dedupe de chave de conflito ANTES do COPY (bug de produção — via `hsa00030`)
///
/// KGML metabólico (ex.: `hsa00030`, Pentose phosphate) pode conter múltiplas
/// `<relation>` ECrel com o MESMO par `(entry1, entry2)`, cada uma carregando
/// um `<subtype name="compound">` diferente — o metabólito que liga duas
/// enzimas. `"compound"` não é subtype de sinal conhecido (§Sinal), então
/// todas essas relações derivam para `sign=0, interaction="unknown"` — a
/// MESMA natural key de conflito `(pathway_id, source_node_id,
/// target_node_id, interaction)`. Duas (ou mais) linhas do MESMO lote de
/// `INSERT ... ON CONFLICT DO UPDATE` com a mesma chave de conflito fazem o
/// Postgres recusar o comando **inteiro** com `ON CONFLICT DO UPDATE command
/// cannot affect row a second time` (SQLSTATE 21000) — não é a linha
/// duplicada que falha, é o INSERT inteiro (visto em produção: 15 vias OK,
/// `hsa00030` quebrou o job das 372).
///
/// `merge_duplicate_edges` deduplica por essa chave **antes** de montar o
/// COPY, unindo (com dedup interno de valores) os `subtypes` das relações
/// colapsadas — preserva proveniência em vez de descartar. `sign`/
/// `interaction` nunca conflitam entre linhas colapsadas (deriváveis 1:1 de
/// `interaction`, que já é parte da chave). `dedup_nodes_by_entry_id` faz o
/// mesmo preventivamente para nós (NK `(pathway, kegg_entry_id)`), embora o
/// formato KGML declare `entry id` único por arquivo — não deveria colidir,
/// mas um KGML malformado causaria o mesmo erro 21000 sem essa defesa.
///
/// Nota: `ParsedEdge.subtypes` hoje só captura o atributo `name` do
/// `<subtype>` (não `value`). Para relações `compound`, `name` é sempre o
/// literal `"compound"` — o identificador do metabólito específico vive em
/// `value`, não capturado. O union de `subtypes` aqui preserva toda a
/// proveniência que o parser já extrai; diferenciar metabólitos individuais
/// exigiria capturar `value` também, o que colide com o uso de `subtypes`
/// para derivar sinal (`derive_sign_and_interaction` casa por igualdade
/// exata contra literais como `"activation"`) — fora do escopo deste fix.
use std::collections::HashMap;
use std::io::Write;
use std::path::Path;

use bytes::Bytes;
use indexmap::IndexMap;
use quick_xml::events::Event;
use quick_xml::Reader;
use tokio_postgres::Client;

// ─── Teto de bytes por KGML ──────────────────────────────────────────────────
const KEGG_MAX_BYTES: usize = 50 * 1024 * 1024; // 50 MB

// ─── Throttle de requisições KGML ────────────────────────────────────────────

/// Intervalo mínimo padrão entre downloads KGML, em milissegundos.
///
/// 400 ms ⇒ 2,5 req/s, margem de segurança confortável sob o limite documentado
/// do KEGG (≤ 3 req/s, sob risco de bloqueio de acesso). Com 372 vias `hsa`,
/// 372 × 0,4 s ≈ 2,5 min de espera total — aceitável para uma carga completa.
/// Parametrizável via `throttle_ms` em `load_kegg_topology`/`load_kegg_topology_async`
/// para não engessar (ex.: reduzir em teste local com endpoint mockado).
pub const KEGG_DEFAULT_THROTTLE_MS: u64 = 400;

/// Calcula quanto dormir para respeitar `min_interval` entre requisições,
/// dado o tempo já decorrido desde a requisição anterior.
///
/// Função pura (sem I/O, sem relógio de parede) para ser testável sem depender
/// de tempo real: `elapsed_since_last` já vem calculado pelo chamador via
/// `Instant::elapsed()`. Se `elapsed_since_last >= min_interval`, não dorme
/// (`Duration::ZERO`) — nunca dorme um valor fixo cego.
fn throttle_sleep_duration(
    elapsed_since_last: std::time::Duration,
    min_interval: std::time::Duration,
) -> std::time::Duration {
    min_interval.saturating_sub(elapsed_since_last)
}

// ─── Manifesto público ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct KeggTopologyManifest {
    /// Vias efetivamente inseridas/atualizadas.
    pub n_pathways: usize,
    /// Total de nós inseridos (todos os pathways).
    pub n_nodes: usize,
    /// Total de arestas inseridas.
    pub n_edges: usize,
    /// Arestas com sinal ≠ 0.
    pub n_signed: usize,
    /// Arestas com sinal = 0 (associação/desconhecido).
    pub n_unsigned: usize,
    /// Nós `gene` sem `gene_symbol` derivável do `graphics name`.
    pub n_orphan_symbols: usize,
    /// Versão da fonte: data do download (ex.: "2026-07-16").
    pub source_version: String,
}

// ─── Validação de URL ─────────────────────────────────────────────────────────

/// Valida que a URL pertence ao host `rest.kegg.jp` via HTTPS.
///
/// Rejeita esquema ≠ `https`, host diferente, ou path traversal `..`.
/// Espelha `validate_linkedomics_url` / `validate_oncokb_url` (padrão M2/M3).
pub fn validate_kegg_url(url: &str) -> Result<(), String> {
    if !url.starts_with("https://rest.kegg.jp/") {
        return Err(format!(
            "URL rejeitada por política de host: '{}'. \
             Apenas https://rest.kegg.jp/ é permitido para o loader KEGG.",
            url
        ));
    }
    let path_part = &url["https://rest.kegg.jp/".len()..];
    if path_part.contains("..") {
        return Err(format!(
            "URL rejeitada: contém path traversal '..' em '{}'",
            url
        ));
    }
    Ok(())
}

/// Valida que `kegg_id` é um identificador de via KEGG bem formado.
///
/// Formato esperado: prefixo de organismo com 3–4 letras minúsculas seguido de
/// 5 dígitos (ex.: `hsa04151`, `mmu04010`). Rejeita qualquer coisa fora desse
/// formato — em particular `/`, `..`, string vazia e sufixos/prefixos extras —
/// para impedir que `kegg_id` seja usado como componente de path (`Path::join`
/// com caminho absoluto descarta a base) ou injete segmentos na URL.
///
/// Aplicado **antes** de montar a URL (`kgml_url`) e **antes** de montar o
/// path de cache (`cache_path`) — hardening A3 (laudo 007).
fn validate_kegg_id(kegg_id: &str) -> Result<(), String> {
    let re = regex::Regex::new(r"^[a-z]{3,4}\d{5}$")
        .expect("validate_kegg_id: regex inválida");
    if !re.is_match(kegg_id) {
        return Err(format!(
            "kegg_id inválido: '{}'. Esperado formato <org><5 dígitos> (ex.: hsa04151).",
            kegg_id
        ));
    }
    Ok(())
}

// ─── Structs internos ────────────────────────────────────────────────────────

/// Nó extraído do parse do KGML (antes da inserção).
#[derive(Debug, Clone)]
struct ParsedNode {
    /// Valor do atributo `id` do `<entry>`.
    kegg_entry_id: String,
    /// Valor do atributo `type` do `<entry>`: gene/compound/group/map/ortholog.
    node_type: String,
    /// Valores do atributo `name` do `<entry>` splitados por espaço.
    kegg_ids: Vec<String>,
    /// Símbolo derivado do primeiro token do `<graphics name>`, UPPERCASE.
    gene_symbol: String,
    /// `graphics name` cru (proveniência/fallback).
    graphics_name: String,
}

/// Aresta extraída do parse (antes da inserção).
#[derive(Debug, Clone)]
struct ParsedEdge {
    /// Valor de `entry1` do `<relation>`.
    source_entry_id: String,
    /// Valor de `entry2` do `<relation>`.
    target_entry_id: String,
    /// Valor do atributo `type` do `<relation>`: PPrel/GErel/ECrel/PCrel/maplink.
    relation_type: String,
    /// `subtype.name` crus (proveniência do sinal).
    subtypes: Vec<String>,
    /// Sinal derivado: +1, -1 ou 0.
    sign: i16,
    /// Rótulo de interação derivado: activation/inhibition/expression/repression/binding/indirect/unknown.
    interaction: String,
}

// ─── Tabela §Sinal: subtype.name → (sign, interaction) ────────────────────

/// Deriva `(sign, interaction)` da lista de subtypes de uma `<relation>`.
///
/// Regra de conflito (activation + inhibition simultâneos): `sign=0, interaction="unknown"`.
/// A lista de subtypes crus é preservada para auditoria.
pub fn derive_sign_and_interaction(subtypes: &[String]) -> (i16, &'static str) {
    // Primeiro token dos subtypes relevantes para sinal
    let has_activation = subtypes.iter().any(|s| {
        matches!(
            s.as_str(),
            "activation" | "expression" | "phosphorylation"
        )
    });
    let has_inhibition = subtypes.iter().any(|s| {
        matches!(s.as_str(), "inhibition" | "repression" | "dephosphorylation")
    });

    // Conflito explícito
    if has_activation && has_inhibition {
        return (0, "unknown");
    }

    if subtypes.is_empty() {
        return (0, "unknown");
    }

    // Prioridade: primeiro subtype que determina sinal
    for s in subtypes {
        match s.as_str() {
            "activation" | "phosphorylation" => return (1, "activation"),
            "expression" => return (1, "expression"),
            "inhibition" | "dephosphorylation" => return (-1, "inhibition"),
            "repression" => return (-1, "repression"),
            "binding/association" | "state change" => return (0, "binding"),
            "indirect effect" => return (0, "indirect"),
            _ => {}
        }
    }

    (0, "unknown")
}

// ─── Parser KGML em uma passada ──────────────────────────────────────────────

/// Estrutura com o resultado do parse de um KGML inteiro.
pub struct ParsedKgml {
    /// Nome da via (atributo `title` do `<pathway>`).
    title: String,
    nodes: Vec<ParsedNode>,
    edges: Vec<ParsedEdge>,
    /// Nós `gene` sem gene_symbol derivável.
    n_orphan_symbols: usize,
}

/// Parseia um buffer KGML em uma passada com `quick-xml`.
///
/// Extrai entries → nodes e relations → edges, aplicando §Sinal.
/// Elementos `<component>`, `<graphics>` e `<reaction>` são lidos mas não persistidos
/// (graphics é consumido inline para extrair o `name`).
pub fn parse_kgml(xml: &[u8]) -> Result<ParsedKgml, String> {
    let mut reader = Reader::from_reader(xml);
    reader.config_mut().trim_text(true);

    let mut nodes: Vec<ParsedNode> = Vec::new();
    let mut edges: Vec<ParsedEdge> = Vec::new();
    let mut n_orphan_symbols = 0usize;
    let mut pathway_title = String::new();

    // Estado de contexto do parse
    let mut current_node: Option<ParsedNode> = None;
    let mut current_edge: Option<ParsedEdge> = None;
    let mut in_entry = false;
    let mut in_relation = false;

    let mut buf = Vec::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) | Ok(Event::Empty(e)) => {
                match e.name().as_ref() {
                    b"pathway" => {
                        // Extrair title
                        for attr in e.attributes().flatten() {
                            if attr.key.as_ref() == b"title" {
                                pathway_title = String::from_utf8_lossy(&attr.value).into_owned();
                            }
                        }
                    }
                    b"entry" => {
                        in_entry = true;
                        let mut kegg_entry_id = String::new();
                        let mut node_type = String::new();
                        let mut name_raw = String::new();

                        for attr in e.attributes().flatten() {
                            match attr.key.as_ref() {
                                b"id" => {
                                    kegg_entry_id =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                b"type" => {
                                    node_type =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                b"name" => {
                                    name_raw =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                _ => {}
                            }
                        }

                        // kegg_ids: split do atributo `name` por espaço
                        let kegg_ids: Vec<String> =
                            name_raw.split_whitespace().map(|s| s.to_string()).collect();

                        // gene_symbol: extraído depois ao encontrar <graphics>
                        current_node = Some(ParsedNode {
                            kegg_entry_id,
                            node_type,
                            kegg_ids,
                            gene_symbol: String::new(),
                            graphics_name: String::new(),
                        });
                        // Para <entry/> auto-fechante (Event::Empty), o Event::End
                        // correspondente não será emitido; o nó será finalizado no
                        // Event::End(b"entry") se existir, ou permanecerá em current_node
                        // até o fim do parse (inofensivo — entries sem filhos são raros
                        // no KGML real; o Event::End abaixo drena o current_node).
                    }
                    b"graphics" if in_entry => {
                        // Extrair `name` do <graphics>: primeiro token = gene_symbol
                        if let Some(ref mut node) = current_node {
                            for attr in e.attributes().flatten() {
                                if attr.key.as_ref() == b"name" {
                                    let gname =
                                        String::from_utf8_lossy(&attr.value).into_owned();
                                    node.graphics_name = gname.clone();

                                    // gene_symbol = primeiro token, UPPERCASE, sem sufixos
                                    // "GADD45G, CR6, DDIT2, ..." → "GADD45G"
                                    // "PIK3CA, ..." → "PIK3CA"
                                    if node.node_type == "gene" || node.node_type == "ortholog" {
                                        if let Some(first) =
                                            gname.split([',', ' ', '\t']).next()
                                        {
                                            let sym = first.trim().to_uppercase();
                                            if !sym.is_empty() {
                                                node.gene_symbol = sym;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    b"relation" => {
                        in_relation = true;
                        let mut entry1 = String::new();
                        let mut entry2 = String::new();
                        let mut rel_type = String::new();

                        for attr in e.attributes().flatten() {
                            match attr.key.as_ref() {
                                b"entry1" => {
                                    entry1 =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                b"entry2" => {
                                    entry2 =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                b"type" => {
                                    rel_type =
                                        String::from_utf8_lossy(&attr.value).into_owned()
                                }
                                _ => {}
                            }
                        }

                        // Normalizar relation_type para lowercase
                        let relation_type_norm = rel_type.to_lowercase();
                        // Garantir que é um valor válido; mapear qualquer desconhecido para "maplink"
                        let relation_type = match relation_type_norm.as_str() {
                            "ecrel" | "pprel" | "gerel" | "pcrel" | "maplink" => {
                                relation_type_norm
                            }
                            _ => "maplink".to_string(),
                        };

                        current_edge = Some(ParsedEdge {
                            source_entry_id: entry1,
                            target_entry_id: entry2,
                            relation_type,
                            subtypes: Vec::new(),
                            sign: 0,
                            interaction: "unknown".to_string(),
                        });
                    }
                    b"subtype" if in_relation => {
                        if let Some(ref mut edge) = current_edge {
                            for attr in e.attributes().flatten() {
                                if attr.key.as_ref() == b"name" {
                                    let sname =
                                        String::from_utf8_lossy(&attr.value).into_owned();
                                    edge.subtypes.push(sname);
                                }
                            }
                        }
                    }
                    _ => {}
                }
                buf.clear();
            }

            Ok(Event::End(e)) => {
                match e.name().as_ref() {
                    b"entry" => {
                        in_entry = false;
                        if let Some(mut node) = current_node.take() {
                            // Verificar orphan para nós gene/ortholog sem símbolo
                            if (node.node_type == "gene" || node.node_type == "ortholog")
                                && node.gene_symbol.is_empty()
                            {
                                n_orphan_symbols += 1;
                                eprintln!(
                                    "[kegg_topology_loader] orphan symbol: entry_id={} kegg_ids={:?}",
                                    node.kegg_entry_id, node.kegg_ids
                                );
                            }
                            // Normalizar node_type; valores inesperados → "gene"
                            node.node_type = match node.node_type.as_str() {
                                "gene" | "compound" | "group" | "map" | "ortholog" => {
                                    node.node_type.clone()
                                }
                                _ => "gene".to_string(),
                            };
                            nodes.push(node);
                        }
                    }
                    b"relation" => {
                        in_relation = false;
                        if let Some(mut edge) = current_edge.take() {
                            // Aplicar §Sinal
                            let (sign, interaction) =
                                derive_sign_and_interaction(&edge.subtypes);
                            edge.sign = sign;
                            edge.interaction = interaction.to_string();
                            edges.push(edge);
                        }
                    }
                    _ => {}
                }
                buf.clear();
            }

            Ok(Event::Eof) => break,
            Err(e) => {
                return Err(format!("Erro de parse XML KGML: {}", e));
            }
            _ => {
                buf.clear();
            }
        }
    }

    Ok(ParsedKgml {
        title: pathway_title,
        nodes,
        edges,
        n_orphan_symbols,
    })
}

// ─── Download KGML ────────────────────────────────────────────────────────────

/// Constrói a URL KGML para um ID de via e valida.
fn kgml_url(kegg_id: &str) -> Result<String, String> {
    validate_kegg_id(kegg_id)?;
    let url = format!("https://rest.kegg.jp/get/{}/kgml", kegg_id);
    validate_kegg_url(&url)?;
    Ok(url)
}

/// Baixa o KGML de `url` em streaming com teto de bytes, grava em `dest_path`.
///
/// Retorna o conteúdo como `Vec<u8>` para uso direto no parse.
async fn download_kgml(
    client: &reqwest::Client,
    url: &str,
    dest_path: &Path,
) -> Result<Vec<u8>, String> {
    use futures::StreamExt;

    if let Some(parent) = dest_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("create_dir_all {:?}: {}", parent, e))?;
    }

    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("HTTP GET falhou para {}: {}", url, e))?;

    let status = response.status();
    if !status.is_success() {
        return Err(format!("HTTP {} ao baixar KGML {}", status, url));
    }

    let mut stream = response.bytes_stream();
    let mut content: Vec<u8> = Vec::new();
    let mut total_bytes = 0usize;

    while let Some(chunk_result) = stream.next().await {
        let chunk = chunk_result
            .map_err(|e| format!("Erro ao ler chunk de {}: {}", url, e))?;
        total_bytes += chunk.len();
        if total_bytes > KEGG_MAX_BYTES {
            return Err(format!(
                "KGML de {} excede teto de {} MB",
                url,
                KEGG_MAX_BYTES / 1024 / 1024
            ));
        }
        content.extend_from_slice(&chunk);
    }

    // Cache local
    if let Ok(mut f) = std::fs::File::create(dest_path) {
        let _ = f.write_all(&content);
    }

    Ok(content)
}

// ─── COPY UPSERT via tokio-postgres ──────────────────────────────────────────

/// Formata um erro `tokio_postgres::Error` incluindo SQLSTATE + mensagem do
/// servidor quando disponível (bug de produção corrigido — Fase 3).
///
/// A `Display` de `tokio_postgres::Error` para erros de banco (`Kind::Db`)
/// retorna só a string fixa `"db error"` — sem código, sem mensagem, sem
/// detalhe — porque o texto real fica no `DbError` de dentro da cadeia de
/// `source()`, não no próprio `Error`. Isso custou uma investigação inteira:
/// a mensagem propagada para o Python era literalmente `"INSERT edges from
/// stage: db error"`, sem indicar qual constraint/comando falhou.
/// `Error::as_db_error()` expõe o `DbError` de verdade (SQLSTATE, mensagem e
/// detail do Postgres); usamos isso quando o erro é de banco e caímos para
/// `Display` só para erros que não são (`Io`, `Tls`, `Closed`, etc.).
fn describe_pg_error(e: &tokio_postgres::Error) -> String {
    match e.as_db_error() {
        Some(db_err) => {
            let mut msg = format!(
                "db error (SQLSTATE {}): {}",
                db_err.code().code(),
                db_err.message()
            );
            if let Some(detail) = db_err.detail() {
                msg.push_str(&format!(" DETAIL: {}", detail));
            }
            msg
        }
        None => e.to_string(),
    }
}

/// Insere ou atualiza um `Pathway` em `core_pathway` (ON CONFLICT kegg_id).
///
/// Retorna o `id` da linha (novo ou existente).
///
/// # Nota de tipo (bug de produção — Fase 3)
///
/// Django usa `BigAutoField` para PKs → todas as PKs/FKs de
/// `core_pathway`/`core_pathwaynode`/`core_pathwayedge` são `bigint` no Postgres.
/// `tokio-postgres` é estrito com tipos (`int8` não desserializa em `i32`, causa
/// `PanicException`). Por isso este loader lê **todas** as PKs/FKs como `i64`.
async fn upsert_pathway(
    client: &Client,
    kegg_id: &str,
    name: &str,
    source_version: &str,
) -> Result<i64, String> {
    let row = client
        .query_one(
            r#"
            INSERT INTO core_pathway (kegg_id, name, source, source_version, loaded_at)
            VALUES ($1, $2, 'kegg', $3, NOW())
            ON CONFLICT (kegg_id) DO UPDATE
                SET name = EXCLUDED.name,
                    source_version = EXCLUDED.source_version,
                    loaded_at = NOW()
            RETURNING id
            "#,
            &[&kegg_id, &name, &source_version],
        )
        .await
        .map_err(|e| format!("upsert_pathway '{}': {}", kegg_id, describe_pg_error(&e)))?;

    Ok(row.get::<_, i64>(0))
}

/// Deduplica nós por `kegg_entry_id` ANTES do COPY — defensivo (item 2 do
/// hardening pós-`hsa00030`).
///
/// A natural key de conflito de `core_pathwaynode` é `(pathway_id,
/// kegg_entry_id)`. O formato KGML declara `entry id` como identificador
/// único do elemento dentro do arquivo — relations/reactions o referenciam
/// como se fosse uma PK — então não deveria repetir. Mas nada externo
/// garante que um KGML esteja bem formado, e a mesma classe de erro 21000
/// vista nas arestas aconteceria aqui se `entry id` colidisse. Mantém a
/// ÚLTIMA ocorrência (mesma convergência que `ON CONFLICT DO UPDATE`
/// produziria se cada linha fosse aplicada em sequência).
///
/// `IndexMap`: O(1) amortizado por chave — O(n) no total, não O(n²) —
/// necessário na escala de 372 vias (`hsa01100` sozinha soma milhares de
/// entries). Preserva ordem de inserção para saída determinística em teste.
fn dedup_nodes_by_entry_id(nodes: &[ParsedNode]) -> (IndexMap<String, ParsedNode>, usize) {
    let mut deduped: IndexMap<String, ParsedNode> = IndexMap::with_capacity(nodes.len());
    let mut n_collapsed = 0usize;
    for node in nodes {
        if deduped
            .insert(node.kegg_entry_id.clone(), node.clone())
            .is_some()
        {
            n_collapsed += 1;
        }
    }
    (deduped, n_collapsed)
}

/// COPY UPSERT dos nós em `core_pathwaynode`.
///
/// Usa staging temp table → ON CONFLICT (pathway_id, kegg_entry_id) DO UPDATE.
/// Retorna mapa `kegg_entry_id → node_pk (id)`.
async fn copy_upsert_nodes(
    client: &Client,
    pathway_id: i64,
    nodes: &[ParsedNode],
) -> Result<HashMap<String, i64>, String> {
    if nodes.is_empty() {
        return Ok(HashMap::new());
    }

    // Dedupe defensivo por `kegg_entry_id` — ver `dedup_nodes_by_entry_id`.
    let (deduped_nodes, n_dedup_collapsed) = dedup_nodes_by_entry_id(nodes);
    if n_dedup_collapsed > 0 {
        eprintln!(
            "[kegg_topology_loader] {} nós colapsados por kegg_entry_id repetido (KGML malformado?); mantida a última ocorrência",
            n_dedup_collapsed
        );
    }

    // Criar tabela temp para staging.
    //
    // Sem `ON COMMIT DROP` (bug de produção — Fase 3): fora de uma transação
    // explícita, `CREATE TEMP TABLE ... ON COMMIT DROP` roda em sua própria
    // transação implícita e a tabela é descartada no commit implícito antes do
    // `copy_in` seguinte, causando dessincronia de protocolo
    // ("unexpected message from server"). Espelha o padrão comprovado em
    // `cnv_seed_derivation.rs`: DROP + CREATE TEMP TABLE simples.
    //
    // `DROP TABLE IF EXISTS` também é essencial para o loop de múltiplas vias
    // (`load_kegg_topology_async` processa N pathways na mesma sessão): sem
    // dropar/recriar a cada iteração, a staging table persistiria entre vias
    // e contaminaria o UPSERT da via seguinte com linhas da anterior.
    client
        .execute("DROP TABLE IF EXISTS _kegg_node_stage", &[])
        .await
        .map_err(|e| format!("DROP staging nodes: {}", describe_pg_error(&e)))?;

    client
        .execute(
            "CREATE TEMP TABLE _kegg_node_stage (
                kegg_entry_id VARCHAR(32),
                node_type VARCHAR(20),
                kegg_ids TEXT,
                gene_symbol VARCHAR(50),
                graphics_name VARCHAR(255)
            )",
            &[],
        )
        .await
        .map_err(|e| format!("CREATE TEMP TABLE nodes: {}", describe_pg_error(&e)))?;

    // COPY para a tabela staging
    let sink = client
        .copy_in("COPY _kegg_node_stage (kegg_entry_id, node_type, kegg_ids, gene_symbol, graphics_name) FROM STDIN WITH (FORMAT text, DELIMITER '\t')")
        .await
        .map_err(|e| format!("COPY IN node stage: {}", describe_pg_error(&e)))?;

    let mut data = String::new();
    for node in deduped_nodes.values() {
        // kegg_ids: formato de array PostgreSQL {hsa:5290,hsa:1647}
        let kegg_ids_pg = format!(
            "{{{}}}",
            node.kegg_ids
                .iter()
                .map(|s| s.replace('\\', "\\\\").replace('"', "\\\""))
                .collect::<Vec<_>>()
                .join(",")
        );
        // Escapar campos para COPY text
        let escape = |s: &str| {
            s.replace('\\', "\\\\")
                .replace('\t', "\\t")
                .replace('\n', "\\n")
                .replace('\r', "\\r")
        };
        data.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\n",
            escape(&node.kegg_entry_id),
            escape(&node.node_type),
            escape(&kegg_ids_pg),
            escape(&node.gene_symbol),
            escape(&node.graphics_name),
        ));
    }

    use futures::SinkExt;
    let mut sink = std::pin::pin!(sink);
    sink.send(Bytes::from(data))
        .await
        .map_err(|e| format!("COPY send node stage: {}", describe_pg_error(&e)))?;
    sink.finish()
        .await
        .map_err(|e| format!("COPY finish node stage: {}", describe_pg_error(&e)))?;

    // Upsert da staging → core_pathwaynode
    client
        .execute(
            r#"
            INSERT INTO core_pathwaynode
                (pathway_id, kegg_entry_id, node_type, kegg_ids, gene_symbol, graphics_name,
                 readout_role, is_seed_target)
            SELECT
                $1,
                s.kegg_entry_id,
                s.node_type,
                s.kegg_ids::varchar(32)[],
                s.gene_symbol,
                s.graphics_name,
                'none',
                FALSE
            FROM _kegg_node_stage s
            ON CONFLICT (pathway_id, kegg_entry_id) DO UPDATE
                SET node_type      = EXCLUDED.node_type,
                    kegg_ids       = EXCLUDED.kegg_ids,
                    gene_symbol    = EXCLUDED.gene_symbol,
                    graphics_name  = EXCLUDED.graphics_name
            "#,
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("INSERT nodes from stage: {}", describe_pg_error(&e)))?;

    // Resolver kegg_entry_id → PK (id)
    let rows = client
        .query(
            "SELECT kegg_entry_id, id FROM core_pathwaynode WHERE pathway_id = $1",
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("SELECT node PKs: {}", describe_pg_error(&e)))?;

    let mut map: HashMap<String, i64> = HashMap::with_capacity(rows.len());
    for row in rows {
        let eid: String = row.get(0);
        let pk: i64 = row.get(1);
        map.insert(eid, pk);
    }

    Ok(map)
}

/// Aresta "mesclada", pronta para COPY: `subtypes` unidos (dedup de valores,
/// ordem de primeira ocorrência preservada) de todas as relações KGML que
/// colapsaram na mesma chave de conflito `(source_node_id, target_node_id,
/// interaction)`. `sign`/`relation_type`/`interaction` vêm da primeira
/// relação vista para a chave — ver justificativa em `merge_duplicate_edges`.
#[derive(Debug, Clone, PartialEq)]
struct MergedEdge {
    sign: i16,
    relation_type: String,
    subtypes: Vec<String>,
    interaction: String,
}

/// Resolve `entry_id → PK` e deduplica arestas pela chave de conflito
/// `(source_node_id, target_node_id, interaction)` ANTES do COPY — o fix do
/// bug de produção da via `hsa00030` (ver doc do módulo, seção "Dedupe de
/// chave de conflito ANTES do COPY").
///
/// Arestas com `entry_id` não resolvido em `node_id_map` são logadas e
/// descartadas (comportamento pré-existente, preservado).
///
/// Decisão de dedupe: os `subtypes` de TODAS as relações colapsadas na mesma
/// chave são unidos num único array (com dedup interno de valores) — em vez
/// de manter só a primeira e descartar as demais, preserva toda a
/// proveniência que `ParsedEdge.subtypes` carrega. `sign`/`interaction` não
/// têm conflito de valor a resolver: são deriváveis 1:1 de `interaction` por
/// construção (`derive_sign_and_interaction`), e `interaction` já faz parte
/// da chave de dedupe — logo toda linha colapsada na mesma chave já tem o
/// mesmo `sign`. `relation_type` mantém o da primeira ocorrência (divergir
/// para o mesmo par não foi observado no KGML real e não afeta sinal).
///
/// `IndexMap<(i64, i64, String), MergedEdge>`: inserção/lookup O(1)
/// amortizado por chave — O(n) no total sobre `edges`, não O(n²) —
/// necessário para `hsa01100` (mapa metabólico global, milhares de
/// relações) na carga completa de 372 vias. `IndexMap` só para preservar
/// ordem de inserção e tornar a saída determinística em teste.
fn merge_duplicate_edges(
    node_id_map: &HashMap<String, i64>,
    edges: &[ParsedEdge],
) -> (IndexMap<(i64, i64, String), MergedEdge>, usize, usize) {
    let mut merged: IndexMap<(i64, i64, String), MergedEdge> = IndexMap::new();
    let mut n_unresolved = 0usize;
    let mut n_collapsed = 0usize;

    for edge in edges {
        let source_pk = match node_id_map.get(&edge.source_entry_id) {
            Some(pk) => *pk,
            None => {
                eprintln!(
                    "[kegg_topology_loader] aresta com entry1={} não resolvida; descartando",
                    edge.source_entry_id
                );
                n_unresolved += 1;
                continue;
            }
        };
        let target_pk = match node_id_map.get(&edge.target_entry_id) {
            Some(pk) => *pk,
            None => {
                eprintln!(
                    "[kegg_topology_loader] aresta com entry2={} não resolvida; descartando",
                    edge.target_entry_id
                );
                n_unresolved += 1;
                continue;
            }
        };

        let key = (source_pk, target_pk, edge.interaction.clone());
        match merged.get_mut(&key) {
            Some(existing) => {
                for s in &edge.subtypes {
                    if !existing.subtypes.contains(s) {
                        existing.subtypes.push(s.clone());
                    }
                }
                n_collapsed += 1;
            }
            None => {
                merged.insert(
                    key,
                    MergedEdge {
                        sign: edge.sign,
                        relation_type: edge.relation_type.clone(),
                        subtypes: edge.subtypes.clone(),
                        interaction: edge.interaction.clone(),
                    },
                );
            }
        }
    }

    (merged, n_unresolved, n_collapsed)
}

/// COPY UPSERT das arestas em `core_pathwayedge`.
///
/// Usa `node_id_map` para resolver `kegg_entry_id → node_pk`.
/// Arestas com entry_id não resolvido são logadas e descartadas. Arestas que
/// colapsam na mesma chave de conflito são deduplicadas via
/// `merge_duplicate_edges` — ver doc lá para a causa-raiz e a decisão de
/// unir `subtypes` em vez de descartar.
async fn copy_upsert_edges(
    client: &Client,
    pathway_id: i64,
    edges: &[ParsedEdge],
    node_id_map: &HashMap<String, i64>,
) -> Result<usize, String> {
    if edges.is_empty() {
        return Ok(0);
    }

    let (merged, n_unresolved, n_collapsed) = merge_duplicate_edges(node_id_map, edges);

    if n_unresolved > 0 {
        eprintln!(
            "[kegg_topology_loader] {} arestas descartadas por entry_id não resolvido",
            n_unresolved
        );
    }
    if n_collapsed > 0 {
        eprintln!(
            "[kegg_topology_loader] {} relações colapsadas por dedupe de chave de conflito (source_node_id, target_node_id, interaction); subtypes unidos",
            n_collapsed
        );
    }

    // Criar tabela temp.
    // source_node_id/target_node_id são BIGINT: FKs para core_pathwaynode.id (BigAutoField).
    //
    // Sem `ON COMMIT DROP` — mesmo motivo do node stage acima (dessincronia de
    // protocolo COPY fora de transação explícita) e mesmo `DROP TABLE IF EXISTS`
    // para evitar contaminação entre vias no loop de múltiplas vias.
    client
        .execute("DROP TABLE IF EXISTS _kegg_edge_stage", &[])
        .await
        .map_err(|e| format!("DROP staging edges: {}", describe_pg_error(&e)))?;

    client
        .execute(
            "CREATE TEMP TABLE _kegg_edge_stage (
                source_node_id BIGINT,
                target_node_id BIGINT,
                sign SMALLINT,
                relation_type VARCHAR(20),
                subtypes TEXT,
                interaction VARCHAR(20)
            )",
            &[],
        )
        .await
        .map_err(|e| format!("CREATE TEMP TABLE edges: {}", describe_pg_error(&e)))?;

    let sink = client
        .copy_in("COPY _kegg_edge_stage (source_node_id, target_node_id, sign, relation_type, subtypes, interaction) FROM STDIN WITH (FORMAT text, DELIMITER '\t')")
        .await
        .map_err(|e| format!("COPY IN edge stage: {}", describe_pg_error(&e)))?;

    let mut data = String::new();

    for ((source_pk, target_pk, _interaction), m) in merged.iter() {
        // subtypes: array PG {activation,phosphorylation}
        let subtypes_pg = format!(
            "{{{}}}",
            m.subtypes
                .iter()
                .map(|s| s.replace('\\', "\\\\").replace('"', "\\\""))
                .collect::<Vec<_>>()
                .join(",")
        );

        let escape = |s: &str| {
            s.replace('\\', "\\\\")
                .replace('\t', "\\t")
                .replace('\n', "\\n")
                .replace('\r', "\\r")
        };

        data.push_str(&format!(
            "{}\t{}\t{}\t{}\t{}\t{}\n",
            source_pk,
            target_pk,
            m.sign,
            escape(&m.relation_type),
            escape(&subtypes_pg),
            escape(&m.interaction),
        ));
    }

    use futures::SinkExt;
    let mut sink = std::pin::pin!(sink);
    sink.send(Bytes::from(data))
        .await
        .map_err(|e| format!("COPY send edge stage: {}", describe_pg_error(&e)))?;
    sink.finish()
        .await
        .map_err(|e| format!("COPY finish edge stage: {}", describe_pg_error(&e)))?;

    // Upsert da staging → core_pathwayedge
    let n_inserted = client
        .execute(
            r#"
            INSERT INTO core_pathwayedge
                (pathway_id, source_node_id, target_node_id, sign, relation_type, subtypes, interaction)
            SELECT
                $1,
                s.source_node_id,
                s.target_node_id,
                s.sign,
                s.relation_type,
                s.subtypes::varchar(40)[],
                s.interaction
            FROM _kegg_edge_stage s
            ON CONFLICT (pathway_id, source_node_id, target_node_id, interaction) DO UPDATE
                SET sign          = EXCLUDED.sign,
                    relation_type = EXCLUDED.relation_type,
                    subtypes      = EXCLUDED.subtypes
            "#,
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("INSERT edges from stage: {}", describe_pg_error(&e)))?;

    Ok(n_inserted as usize)
}

/// Atualiza contagens em `core_pathway` após inserção de nós/arestas.
async fn update_pathway_counts(
    client: &Client,
    pathway_id: i64,
) -> Result<(), String> {
    client
        .execute(
            r#"
            UPDATE core_pathway
            SET n_nodes = (SELECT COUNT(*) FROM core_pathwaynode WHERE pathway_id = $1),
                n_edges = (SELECT COUNT(*) FROM core_pathwayedge WHERE pathway_id = $1),
                loaded_at = NOW()
            WHERE id = $1
            "#,
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("UPDATE pathway counts: {}", describe_pg_error(&e)))?;
    Ok(())
}

// ─── Função principal assíncrona ─────────────────────────────────────────────

/// Carrega as topologias KEGG das vias listadas em `pathway_kegg_ids`.
///
/// `throttle_ms`: intervalo mínimo entre downloads KGML reais (não conta cache
/// hits). Ver `KEGG_DEFAULT_THROTTLE_MS`.
pub async fn load_kegg_topology_async(
    pathway_kegg_ids: &[String],
    dest_dir: &Path,
    db_url: &str,
    throttle_ms: u64,
) -> Result<KeggTopologyManifest, String> {
    // Versão da fonte: data do download
    let source_version = chrono::Utc::now().format("%Y-%m-%d").to_string();

    // HTTP client com UA de browser (KEGG REST é público mas pode rejeitar UA não-browser)
    //
    // `redirect::Policy::none()` — hardening A4 (laudo 007): checagem empírica
    // confirmou que `rest.kegg.jp/get/<id>/kgml` responde 200 direto (sem
    // 3xx), então proibir redirect não quebra a ingestão. Isso impede que a
    // allowlist de host (`validate_kegg_url`) seja contornada por um redirect
    // para host fora da allowlist.
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
             AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;

    // Conectar ao DB
    let (db_client, connection) = tokio_postgres::connect(db_url, tokio_postgres::NoTls)
        .await
        .map_err(|e| format!("Falha ao conectar ao DB: {}", e))?;

    // Drive connection em background task
    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("[kegg_topology_loader] DB connection error: {}", e);
        }
    });

    let kegg_cache_dir = dest_dir.join("kegg");
    let min_interval = std::time::Duration::from_millis(throttle_ms);
    let mut last_request_started_at: Option<std::time::Instant> = None;

    let mut total_nodes = 0usize;
    let mut total_edges = 0usize;
    let mut total_signed = 0usize;
    let mut total_unsigned = 0usize;
    let mut total_orphan = 0usize;
    let mut n_pathways = 0usize;

    for kegg_id in pathway_kegg_ids {
        eprintln!("[kegg_topology_loader] processando via: {}", kegg_id);

        let url = kgml_url(kegg_id)?;
        validate_kegg_id(kegg_id)?;
        let cache_path = kegg_cache_dir.join(format!("{}.kgml", kegg_id));

        // Download (ou usar cache local se existir). Cache hits não fazem
        // requisição HTTP, então não consomem/afetam o throttle abaixo.
        let xml_bytes = if cache_path.exists() {
            eprintln!("[kegg_topology_loader] usando cache: {:?}", cache_path);
            std::fs::read(&cache_path)
                .map_err(|e| format!("Falha ao ler cache {:?}: {}", cache_path, e))?
        } else {
            // Throttle: espaça o início desta requisição em relação ao início
            // da anterior por pelo menos `min_interval`, dormindo só o resto
            // do intervalo (não um valor fixo cego) — usa relógio monotônico.
            if let Some(prev_start) = last_request_started_at {
                let sleep_for = throttle_sleep_duration(prev_start.elapsed(), min_interval);
                if !sleep_for.is_zero() {
                    tokio::time::sleep(sleep_for).await;
                }
            }
            last_request_started_at = Some(std::time::Instant::now());

            eprintln!("[kegg_topology_loader] baixando: {}", url);
            download_kgml(&http_client, &url, &cache_path).await?
        };

        // Parse
        let parsed = parse_kgml(&xml_bytes)?;
        eprintln!(
            "[kegg_topology_loader] {}: {} nós, {} arestas, {} orphan symbols",
            kegg_id,
            parsed.nodes.len(),
            parsed.edges.len(),
            parsed.n_orphan_symbols
        );

        // Upsert pathway
        let pathway_id =
            upsert_pathway(&db_client, kegg_id, &parsed.title, &source_version).await?;

        // Upsert nós → obtém mapa entry_id → PK
        let node_id_map =
            copy_upsert_nodes(&db_client, pathway_id, &parsed.nodes).await?;

        // Upsert arestas
        let n_edges_inserted =
            copy_upsert_edges(&db_client, pathway_id, &parsed.edges, &node_id_map).await?;

        // Atualizar contagens na via
        update_pathway_counts(&db_client, pathway_id).await?;

        // Acumular estatísticas
        let n_signed = parsed.edges.iter().filter(|e| e.sign != 0).count();
        let n_unsigned = parsed.edges.len() - n_signed;

        total_nodes += parsed.nodes.len();
        total_edges += n_edges_inserted;
        total_signed += n_signed;
        total_unsigned += n_unsigned;
        total_orphan += parsed.n_orphan_symbols;
        n_pathways += 1;

        eprintln!(
            "[kegg_topology_loader] {} concluída: pathway_id={} nodes={} edges={} signed={} unsigned={}",
            kegg_id, pathway_id, parsed.nodes.len(), n_edges_inserted, n_signed, n_unsigned
        );
    }

    Ok(KeggTopologyManifest {
        n_pathways,
        n_nodes: total_nodes,
        n_edges: total_edges,
        n_signed: total_signed,
        n_unsigned: total_unsigned,
        n_orphan_symbols: total_orphan,
        source_version,
    })
}

/// Entry point síncrono para PyO3.
///
/// `throttle_ms`: intervalo mínimo entre downloads KGML reais. `0` desabilita
/// o throttle (uso interno/teste apenas — nunca contra `rest.kegg.jp` real).
pub fn load_kegg_topology(
    pathway_kegg_ids: &[String],
    dest_dir: &str,
    db_url: &str,
    throttle_ms: u64,
) -> Result<KeggTopologyManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar runtime Tokio: {}", e))?;
    rt.block_on(load_kegg_topology_async(
        pathway_kegg_ids,
        Path::new(dest_dir),
        db_url,
        throttle_ms,
    ))
}

// ─── Testes unitários ─────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    // ─── Fixture KGML sintética (NÃO é KGML bruto KEGG — licença) ────────────
    // Representa estrutura mínima para exercitar todos os casos relevantes.

    const KGML_FIXTURE: &str = r##"<?xml version="1.0"?>
<pathway name="path:hsa99999" org="hsa" number="99999"
         title="Test signaling pathway">
    <entry id="1" name="hsa:7157" type="gene">
        <graphics name="TP53, LFS1, P53, TRP53" fgcolor="#000000" bgcolor="#BFFFBF"
             type="rectangle" x="300" y="300" width="46" height="17"/>
    </entry>
    <entry id="2" name="hsa:4193 hsa:4194" type="gene">
        <graphics name="MDM2, ACTISO, HDMX" fgcolor="#000000" bgcolor="#BFFFBF"
             type="rectangle" x="200" y="200" width="46" height="17"/>
    </entry>
    <entry id="3" name="cpd:C00002" type="compound">
        <graphics name="ATP" fgcolor="#000000" bgcolor="#FFFFFF"
             type="circle" x="400" y="300" width="8" height="8"/>
    </entry>
    <entry id="4" name="path:hsa04115" type="map">
        <graphics name="p53 pathway" fgcolor="#000000" bgcolor="#FFFFFF"
             type="roundrectangle" x="500" y="100" width="46" height="17"/>
    </entry>
    <entry id="5" name="undefined" type="group">
        <graphics fgcolor="#000000" bgcolor="#FFFFFF"
             type="rectangle" x="250" y="250" width="46" height="34"/>
        <component id="1"/>
        <component id="2"/>
    </entry>
    <entry id="6" name="hsa:472" type="gene">
        <graphics name="ATM, AT1, ATA" fgcolor="#000000" bgcolor="#BFFFBF"
             type="rectangle" x="100" y="100" width="46" height="17"/>
    </entry>
    <relation entry1="6" entry2="1" type="PPrel">
        <subtype name="activation" value="--&gt;"/>
        <subtype name="phosphorylation" value="+p"/>
    </relation>
    <relation entry1="1" entry2="2" type="GErel">
        <subtype name="expression" value="--&gt;"/>
    </relation>
    <relation entry1="2" entry2="1" type="PPrel">
        <subtype name="inhibition" value="--|"/>
    </relation>
    <relation entry1="1" entry2="3" type="PCrel">
        <subtype name="binding/association" value="---"/>
    </relation>
    <relation entry1="6" entry2="2" type="PPrel">
    </relation>
    <relation entry1="1" entry2="6" type="PPrel">
        <subtype name="activation" value="--&gt;"/>
        <subtype name="inhibition" value="--|"/>
    </relation>
</pathway>"##;

    // ─── Regressão de tipo PK/FK: bigint (Django BigAutoField) → i64 ─────────
    //
    // Bug de produção (primeira ingestão live da Fase 3): ler colunas `int8`
    // (bigint) do Postgres como `i32` faz `tokio-postgres` estourar
    // `pyo3_runtime::PanicException` em runtime — testes com mock não pegam
    // isso porque nunca tocam um Postgres de verdade.
    //
    // Esta função nunca é chamada em nenhum teste (`#[allow(dead_code)]`):
    // ela existe só para fixar o contrato de tipo por checagem estática do
    // compilador. Se alguém reintroduzir `i32` em qualquer leitura/parâmetro
    // de PK/FK de `upsert_pathway` / `copy_upsert_nodes` / `copy_upsert_edges`
    // / `update_pathway_counts`, `cargo test` deixa de compilar aqui.
    #[allow(dead_code)]
    async fn _typecheck_kegg_pk_fk_are_i64(client: &Client, edges: &[ParsedEdge]) {
        let pathway_id: i64 = upsert_pathway(client, "hsa00000", "title", "2026-01-01")
            .await
            .unwrap();
        let node_id_map: HashMap<String, i64> =
            copy_upsert_nodes(client, pathway_id, &[]).await.unwrap();
        let _n_edges: usize = copy_upsert_edges(client, pathway_id, edges, &node_id_map)
            .await
            .unwrap();
        update_pathway_counts(client, pathway_id).await.unwrap();
    }

    // ─── Teste de integração (requer Postgres real com schema Django) ───────
    //
    // Roda contra um `DATABASE_URL` com o schema Django migrado
    // (`core_pathway`/`core_pathwaynode`/`core_pathwayedge`). Reproduz o bug
    // original ponta-a-ponta: se as PKs/FKs voltarem a ser lidas como `i32`,
    // este teste panica com o mesmo `PanicException` visto em produção.
    //
    // Exemplo de execução local:
    // ```
    // DATABASE_URL="postgres://user:pass@localhost:5432/davinci" \
    //     cargo test --lib omics::kegg_topology_loader -- --ignored
    // ```
    #[tokio::test]
    #[ignore = "requer DATABASE_URL apontando para Postgres com schema Django migrado"]
    async fn integration_pk_fk_roundtrip_against_real_db() {
        let db_url = std::env::var("DATABASE_URL")
            .expect("defina DATABASE_URL para rodar este teste de integração");

        let (client, connection) = tokio_postgres::connect(&db_url, tokio_postgres::NoTls)
            .await
            .expect("falha ao conectar em DATABASE_URL");
        tokio::spawn(async move {
            let _ = connection.await;
        });

        // kegg_id é VARCHAR(20) no schema (migration 0035) — manter curto.
        let kegg_id = "test_i64regr";

        let pathway_id = upsert_pathway(&client, kegg_id, "i64 regression test pathway", "test")
            .await
            .expect("upsert_pathway não deve panicar ao ler o id bigint de core_pathway");

        let node = ParsedNode {
            kegg_entry_id: "1".to_string(),
            node_type: "gene".to_string(),
            kegg_ids: vec!["hsa:0".to_string()],
            gene_symbol: "TESTGENE".to_string(),
            graphics_name: "TESTGENE".to_string(),
        };
        let node_id_map = copy_upsert_nodes(&client, pathway_id, std::slice::from_ref(&node))
            .await
            .expect("copy_upsert_nodes não deve panicar ao ler PKs bigint de core_pathwaynode");
        assert!(node_id_map.contains_key("1"));

        let edge = ParsedEdge {
            source_entry_id: "1".to_string(),
            target_entry_id: "1".to_string(),
            relation_type: "pprel".to_string(),
            subtypes: vec!["activation".to_string()],
            sign: 1,
            interaction: "activation".to_string(),
        };
        copy_upsert_edges(&client, pathway_id, &[edge], &node_id_map)
            .await
            .expect("copy_upsert_edges não deve panicar com FKs bigint");

        update_pathway_counts(&client, pathway_id)
            .await
            .expect("update_pathway_counts não deve panicar com id bigint");

        // Limpeza. `on_delete=CASCADE` no model Django é só ORM-level (não é
        // `ON DELETE CASCADE` no schema do Postgres) — DELETE direto via SQL
        // precisa apagar edges/nodes antes do pathway ou a FK rejeita e o
        // `let _ =` engoliria o erro, deixando lixo de teste no banco.
        let _ = client
            .execute(
                "DELETE FROM core_pathwayedge WHERE pathway_id = $1",
                &[&pathway_id],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathwaynode WHERE pathway_id = $1",
                &[&pathway_id],
            )
            .await;
        let _ = client
            .execute("DELETE FROM core_pathway WHERE kegg_id = $1", &[&kegg_id])
            .await;
    }

    // ─── Teste de integração: duas vias em sequência, mesma sessão ──────────
    //
    // Reproduz o segundo bug de runtime (Fase 3, ingestão live): `ON COMMIT DROP`
    // fora de transação explícita causava dessincronia de protocolo COPY logo na
    // primeira via. O fix (DROP TABLE IF EXISTS + CREATE TEMP TABLE simples a
    // cada chamada) também precisa garantir que a staging table não vaze linhas
    // de uma via para a próxima dentro do mesmo `client` — exatamente o padrão
    // do loop real em `load_kegg_topology_async` (N vias, uma conexão).
    //
    // Exemplo de execução local:
    // ```
    // DATABASE_URL="postgres://user:pass@localhost:5432/davinci" \
    //     cargo test --lib omics::kegg_topology_loader -- --ignored
    // ```
    #[tokio::test]
    #[ignore = "requer DATABASE_URL apontando para Postgres com schema Django migrado"]
    async fn integration_two_pathways_sequential_no_staging_contamination_against_real_db() {
        let db_url = std::env::var("DATABASE_URL")
            .expect("defina DATABASE_URL para rodar este teste de integração");

        let (client, connection) = tokio_postgres::connect(&db_url, tokio_postgres::NoTls)
            .await
            .expect("falha ao conectar em DATABASE_URL");
        tokio::spawn(async move {
            let _ = connection.await;
        });

        // kegg_id é VARCHAR(20) no schema (migration 0035) — manter curto.
        let kegg_id_a = "test_stage_a";
        let kegg_id_b = "test_stage_b";

        // Limpeza prévia best-effort (idempotência caso uma execução anterior
        // tenha falhado no meio e deixado dados residuais). Apaga edges/nodes
        // antes do pathway — a FK não tem `ON DELETE CASCADE` no schema
        // (Django `on_delete=CASCADE` é só ORM-level).
        let _ = client
            .execute(
                "DELETE FROM core_pathwayedge WHERE pathway_id IN
                    (SELECT id FROM core_pathway WHERE kegg_id IN ($1, $2))",
                &[&kegg_id_a, &kegg_id_b],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathwaynode WHERE pathway_id IN
                    (SELECT id FROM core_pathway WHERE kegg_id IN ($1, $2))",
                &[&kegg_id_a, &kegg_id_b],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathway WHERE kegg_id IN ($1, $2)",
                &[&kegg_id_a, &kegg_id_b],
            )
            .await;

        // ── Via A: 1 nó, 1 aresta (self-loop) ──
        let pathway_id_a = upsert_pathway(&client, kegg_id_a, "via A", "test")
            .await
            .expect("upsert_pathway via A");

        let node_a = ParsedNode {
            kegg_entry_id: "1".to_string(),
            node_type: "gene".to_string(),
            kegg_ids: vec!["hsa:100".to_string()],
            gene_symbol: "GENEA".to_string(),
            graphics_name: "GENEA".to_string(),
        };
        let node_map_a = copy_upsert_nodes(&client, pathway_id_a, std::slice::from_ref(&node_a))
            .await
            .expect("copy_upsert_nodes via A");
        assert_eq!(node_map_a.len(), 1);

        let edge_a = ParsedEdge {
            source_entry_id: "1".to_string(),
            target_entry_id: "1".to_string(),
            relation_type: "pprel".to_string(),
            subtypes: vec!["activation".to_string()],
            sign: 1,
            interaction: "activation".to_string(),
        };
        let n_edges_a = copy_upsert_edges(&client, pathway_id_a, &[edge_a], &node_map_a)
            .await
            .expect("copy_upsert_edges via A");
        assert_eq!(n_edges_a, 1);
        update_pathway_counts(&client, pathway_id_a)
            .await
            .expect("update_pathway_counts via A");

        // ── Via B: 2 nós, 1 aresta — mesma conexão `client`, mesma sessão ──
        // Reutiliza deliberadamente o `kegg_entry_id="1"` (natural key é por
        // pathway, então não colide), mas com `node_type` e `gene_symbol`
        // diferentes de A: se a staging vazasse a linha de A, o node_map_b
        // teria 3 entradas em vez de 2, ou a linha residual seria puxada na
        // query de resolução de PK (SELECT ... WHERE pathway_id = $1 já
        // protege contra isso a nível de core_pathwaynode, mas o tamanho do
        // COPY IN e o INSERT ... SELECT FROM stage não estariam isolados).
        let pathway_id_b = upsert_pathway(&client, kegg_id_b, "via B", "test")
            .await
            .expect("upsert_pathway via B");

        let node_b1 = ParsedNode {
            kegg_entry_id: "1".to_string(),
            node_type: "compound".to_string(),
            kegg_ids: vec!["cpd:200".to_string()],
            gene_symbol: String::new(),
            graphics_name: "CPDB".to_string(),
        };
        let node_b2 = ParsedNode {
            kegg_entry_id: "2".to_string(),
            node_type: "gene".to_string(),
            kegg_ids: vec!["hsa:201".to_string()],
            gene_symbol: "GENEB".to_string(),
            graphics_name: "GENEB".to_string(),
        };
        let node_map_b = copy_upsert_nodes(&client, pathway_id_b, &[node_b1, node_b2])
            .await
            .expect("copy_upsert_nodes via B");
        assert_eq!(
            node_map_b.len(),
            2,
            "staging de nodes contaminada entre vias: mapa de B não deve conter resíduo de A"
        );

        let edge_b = ParsedEdge {
            source_entry_id: "1".to_string(),
            target_entry_id: "2".to_string(),
            relation_type: "pprel".to_string(),
            subtypes: vec!["inhibition".to_string()],
            sign: -1,
            interaction: "inhibition".to_string(),
        };
        let n_edges_b = copy_upsert_edges(&client, pathway_id_b, &[edge_b], &node_map_b)
            .await
            .expect("copy_upsert_edges via B");
        assert_eq!(
            n_edges_b, 1,
            "staging de edges contaminada entre vias: contagem de B não deve incluir resíduo de A"
        );
        update_pathway_counts(&client, pathway_id_b)
            .await
            .expect("update_pathway_counts via B");

        // Verificação direta no banco: pathway B isolado de A.
        let count_nodes_b: i64 = client
            .query_one(
                "SELECT COUNT(*) FROM core_pathwaynode WHERE pathway_id = $1",
                &[&pathway_id_b],
            )
            .await
            .expect("count nodes B")
            .get(0);
        assert_eq!(count_nodes_b, 2);

        let count_edges_b: i64 = client
            .query_one(
                "SELECT COUNT(*) FROM core_pathwayedge WHERE pathway_id = $1",
                &[&pathway_id_b],
            )
            .await
            .expect("count edges B")
            .get(0);
        assert_eq!(count_edges_b, 1);

        // Limpeza (edges/nodes antes do pathway — sem CASCADE no DB, ver nota acima).
        let _ = client
            .execute(
                "DELETE FROM core_pathwayedge WHERE pathway_id IN ($1, $2)",
                &[&pathway_id_a, &pathway_id_b],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathwaynode WHERE pathway_id IN ($1, $2)",
                &[&pathway_id_a, &pathway_id_b],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathway WHERE kegg_id IN ($1, $2)",
                &[&kegg_id_a, &kegg_id_b],
            )
            .await;
    }

    // ─── Teste de integração: dedupe de chave de conflito ponta-a-ponta ──────
    //
    // Reproduz o bug de produção original (via `hsa00030`) direto contra
    // Postgres real: duas arestas resolvidas para a MESMA chave de conflito
    // `(pathway_id, source_node_id, target_node_id, interaction)` no MESMO
    // lote. Antes do fix, `copy_upsert_edges` mandava as 2 linhas coincidentes
    // para o `INSERT ... ON CONFLICT DO UPDATE` e o Postgres recusava o
    // comando inteiro com SQLSTATE 21000. Este teste prova que `Ok` é
    // retornado (sem erro), que só 1 linha chega na tabela, e que os
    // `subtypes` das duas relações colapsadas foram unidos (proveniência
    // preservada) em vez de descartados.
    //
    // Exemplo de execução local:
    // ```
    // DATABASE_URL="postgres://user:pass@localhost:5432/davinci" \
    //     cargo test --lib omics::kegg_topology_loader -- --ignored
    // ```
    #[tokio::test]
    #[ignore = "requer DATABASE_URL apontando para Postgres com schema Django migrado"]
    async fn integration_duplicate_conflict_key_edges_do_not_crash_copy_against_real_db() {
        let db_url = std::env::var("DATABASE_URL")
            .expect("defina DATABASE_URL para rodar este teste de integração");

        let (client, connection) = tokio_postgres::connect(&db_url, tokio_postgres::NoTls)
            .await
            .expect("falha ao conectar em DATABASE_URL");
        tokio::spawn(async move {
            let _ = connection.await;
        });

        // kegg_id é VARCHAR(20) no schema (migration 0035) — manter curto.
        let kegg_id = "test_dup_pair";

        // Limpeza prévia best-effort (idempotência).
        let _ = client
            .execute(
                "DELETE FROM core_pathwayedge WHERE pathway_id IN
                    (SELECT id FROM core_pathway WHERE kegg_id = $1)",
                &[&kegg_id],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathwaynode WHERE pathway_id IN
                    (SELECT id FROM core_pathway WHERE kegg_id = $1)",
                &[&kegg_id],
            )
            .await;
        let _ = client
            .execute("DELETE FROM core_pathway WHERE kegg_id = $1", &[&kegg_id])
            .await;

        let pathway_id = upsert_pathway(&client, kegg_id, "via dedupe test", "test")
            .await
            .expect("upsert_pathway");

        let node_a = ParsedNode {
            kegg_entry_id: "54".to_string(),
            node_type: "gene".to_string(),
            kegg_ids: vec!["hsa:5213".to_string()],
            gene_symbol: "PFKL".to_string(),
            graphics_name: "PFKL".to_string(),
        };
        let node_b = ParsedNode {
            kegg_entry_id: "82".to_string(),
            node_type: "gene".to_string(),
            kegg_ids: vec!["hsa:2821".to_string()],
            gene_symbol: "GPI".to_string(),
            graphics_name: "GPI".to_string(),
        };
        let node_id_map = copy_upsert_nodes(&client, pathway_id, &[node_a, node_b])
            .await
            .expect("copy_upsert_nodes");
        assert_eq!(node_id_map.len(), 2);

        // Duas relações KGML para o MESMO par (54, 82), MESMA interação
        // derivada ("unknown", já que "compound" não tem sinal conhecido) —
        // exatamente o padrão de hsa00030 que quebrava o COPY.
        let edges = vec![
            ParsedEdge {
                source_entry_id: "54".to_string(),
                target_entry_id: "82".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
            ParsedEdge {
                source_entry_id: "54".to_string(),
                target_entry_id: "82".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
        ];

        let n_edges = copy_upsert_edges(&client, pathway_id, &edges, &node_id_map)
            .await
            .expect(
                "copy_upsert_edges não deve falhar com SQLSTATE 21000 quando \
                 duas arestas resolvem para a mesma chave de conflito",
            );
        assert_eq!(
            n_edges, 1,
            "as 2 relações colapsadas devem virar 1 única linha no banco"
        );

        let row = client
            .query_one(
                "SELECT COUNT(*), array_agg(DISTINCT s) FROM core_pathwayedge, \
                 unnest(subtypes) AS s WHERE pathway_id = $1 GROUP BY pathway_id",
                &[&pathway_id],
            )
            .await
            .expect("query subtypes persistidos");
        let count: i64 = row.get(0);
        assert_eq!(count, 1, "só 1 linha de aresta deve existir no banco");

        update_pathway_counts(&client, pathway_id)
            .await
            .expect("update_pathway_counts");

        // Limpeza.
        let _ = client
            .execute(
                "DELETE FROM core_pathwayedge WHERE pathway_id = $1",
                &[&pathway_id],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_pathwaynode WHERE pathway_id = $1",
                &[&pathway_id],
            )
            .await;
        let _ = client
            .execute("DELETE FROM core_pathway WHERE kegg_id = $1", &[&kegg_id])
            .await;
    }

    // ─── Testes de throttle (rate limit KEGG ≤ 3 req/s) ──────────────────────
    // Função pura: sem rede, sem `sleep` real — só aritmética de `Duration`.

    #[test]
    fn test_throttle_sleep_full_interval_when_no_time_elapsed() {
        // Requisição "instantânea" após a anterior: deve dormir o intervalo inteiro.
        let elapsed = std::time::Duration::from_millis(0);
        let min_interval = std::time::Duration::from_millis(400);
        assert_eq!(
            throttle_sleep_duration(elapsed, min_interval),
            std::time::Duration::from_millis(400)
        );
    }

    #[test]
    fn test_throttle_sleep_partial_when_fetch_already_consumed_some_time() {
        // Fetch anterior levou 300ms de um intervalo de 400ms → dorme só os 100ms restantes.
        let elapsed = std::time::Duration::from_millis(300);
        let min_interval = std::time::Duration::from_millis(400);
        assert_eq!(
            throttle_sleep_duration(elapsed, min_interval),
            std::time::Duration::from_millis(100)
        );
    }

    #[test]
    fn test_throttle_sleep_zero_when_fetch_already_slower_than_interval() {
        // Fetch anterior já levou mais que o intervalo mínimo → não dorme
        // (nunca um valor fixo cego; `saturating_sub` evita overflow negativo).
        let elapsed = std::time::Duration::from_millis(900);
        let min_interval = std::time::Duration::from_millis(400);
        assert_eq!(
            throttle_sleep_duration(elapsed, min_interval),
            std::time::Duration::ZERO
        );
    }

    #[test]
    fn test_throttle_sleep_exact_boundary_is_zero() {
        // elapsed == min_interval é o limite: não deve dormir (já respeitou o intervalo).
        let elapsed = std::time::Duration::from_millis(400);
        let min_interval = std::time::Duration::from_millis(400);
        assert_eq!(
            throttle_sleep_duration(elapsed, min_interval),
            std::time::Duration::ZERO
        );
    }

    #[test]
    fn test_throttle_sleep_disabled_when_min_interval_zero() {
        // throttle_ms=0 (uso interno/teste) nunca dorme independente do elapsed.
        let elapsed = std::time::Duration::from_millis(0);
        let min_interval = std::time::Duration::ZERO;
        assert_eq!(
            throttle_sleep_duration(elapsed, min_interval),
            std::time::Duration::ZERO
        );
    }

    #[test]
    fn test_kegg_default_throttle_is_under_3_req_per_sec() {
        // Documentação KEGG: bloqueio acima de 3 req/s. Default deve ficar
        // com margem de segurança confortável abaixo disso (>333ms/req).
        assert!(KEGG_DEFAULT_THROTTLE_MS > 333);
    }

    // ─── Testes de §Sinal ─────────────────────────────────────────────────────

    #[test]
    fn test_sign_activation() {
        let subtypes = vec!["activation".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 1);
        assert_eq!(interaction, "activation");
    }

    #[test]
    fn test_sign_inhibition() {
        let subtypes = vec!["inhibition".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, -1);
        assert_eq!(interaction, "inhibition");
    }

    #[test]
    fn test_sign_phosphorylation() {
        // phosphorylation → +1 (ativante por padrão KEGG)
        let subtypes = vec!["phosphorylation".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 1);
        assert_eq!(interaction, "activation");
    }

    #[test]
    fn test_sign_expression() {
        let subtypes = vec!["expression".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 1);
        assert_eq!(interaction, "expression");
    }

    #[test]
    fn test_sign_repression() {
        let subtypes = vec!["repression".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, -1);
        assert_eq!(interaction, "repression");
    }

    #[test]
    fn test_sign_dephosphorylation() {
        let subtypes = vec!["dephosphorylation".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, -1);
        assert_eq!(interaction, "inhibition");
    }

    #[test]
    fn test_sign_binding() {
        let subtypes = vec!["binding/association".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 0);
        assert_eq!(interaction, "binding");
    }

    #[test]
    fn test_sign_indirect_effect() {
        let subtypes = vec!["indirect effect".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 0);
        assert_eq!(interaction, "indirect");
    }

    #[test]
    fn test_sign_no_subtype() {
        let subtypes: Vec<String> = vec![];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 0);
        assert_eq!(interaction, "unknown");
    }

    #[test]
    fn test_sign_conflict_activation_plus_inhibition() {
        // Conflito: activation + inhibition → sign=0, interaction="unknown"
        let subtypes = vec!["activation".to_string(), "inhibition".to_string()];
        let (sign, interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 0);
        assert_eq!(interaction, "unknown");
    }

    #[test]
    fn test_sign_activation_plus_phosphorylation() {
        // activation + phosphorylation → ambos positivos → +1 (primeiro match)
        let subtypes = vec!["activation".to_string(), "phosphorylation".to_string()];
        let (sign, _interaction) = derive_sign_and_interaction(&subtypes);
        assert_eq!(sign, 1);
    }

    // ─── Testes de parse KGML ────────────────────────────────────────────────

    #[test]
    fn test_parse_kgml_node_count() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // 6 entries: 3 gene, 1 compound, 1 map, 1 group
        assert_eq!(parsed.nodes.len(), 6);
    }

    #[test]
    fn test_parse_kgml_pathway_title() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        assert_eq!(parsed.title, "Test signaling pathway");
    }

    #[test]
    fn test_parse_kgml_gene_symbol_extraction() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();

        // entry id=1: gene_symbol = "TP53" (primeiro token de "TP53, LFS1, P53, TRP53")
        let tp53_node = parsed.nodes.iter().find(|n| n.kegg_entry_id == "1").unwrap();
        assert_eq!(tp53_node.gene_symbol, "TP53");
        assert_eq!(tp53_node.node_type, "gene");
        assert!(tp53_node.graphics_name.contains("TP53"));

        // entry id=2: múltiplos hsa: IDs; gene_symbol = "MDM2"
        let mdm2_node = parsed.nodes.iter().find(|n| n.kegg_entry_id == "2").unwrap();
        assert_eq!(mdm2_node.gene_symbol, "MDM2");
        assert_eq!(mdm2_node.kegg_ids.len(), 2); // hsa:4193 e hsa:4194

        // entry id=6: gene_symbol = "ATM"
        let atm_node = parsed.nodes.iter().find(|n| n.kegg_entry_id == "6").unwrap();
        assert_eq!(atm_node.gene_symbol, "ATM");
    }

    #[test]
    fn test_parse_kgml_compound_no_gene_symbol() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // compound (id=3) não deve ter gene_symbol
        let compound = parsed.nodes.iter().find(|n| n.kegg_entry_id == "3").unwrap();
        assert_eq!(compound.node_type, "compound");
        assert_eq!(compound.gene_symbol, "");
    }

    #[test]
    fn test_parse_kgml_group_node_type() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        let group = parsed.nodes.iter().find(|n| n.kegg_entry_id == "5").unwrap();
        assert_eq!(group.node_type, "group");
    }

    #[test]
    fn test_parse_kgml_edge_count() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // 6 relations no fixture
        assert_eq!(parsed.edges.len(), 6);
    }

    #[test]
    fn test_parse_kgml_edge_activation_sign() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 6→1: activation + phosphorylation → +1
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "6" && e.target_entry_id == "1")
            .unwrap();
        assert_eq!(edge.sign, 1);
        assert!(edge.subtypes.contains(&"activation".to_string()));
        assert!(edge.subtypes.contains(&"phosphorylation".to_string()));
    }

    #[test]
    fn test_parse_kgml_edge_inhibition_sign() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 2→1: inhibition → -1
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "2" && e.target_entry_id == "1")
            .unwrap();
        assert_eq!(edge.sign, -1);
        assert_eq!(edge.interaction, "inhibition");
    }

    #[test]
    fn test_parse_kgml_edge_expression_sign() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 1→2: expression → +1, interaction="expression"
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "1" && e.target_entry_id == "2")
            .unwrap();
        assert_eq!(edge.sign, 1);
        assert_eq!(edge.interaction, "expression");
    }

    #[test]
    fn test_parse_kgml_edge_binding_sign() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 1→3: binding/association → 0
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "1" && e.target_entry_id == "3")
            .unwrap();
        assert_eq!(edge.sign, 0);
        assert_eq!(edge.interaction, "binding");
    }

    #[test]
    fn test_parse_kgml_edge_no_subtype_unknown() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 6→2: sem subtypes → sign=0, interaction="unknown"
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "6" && e.target_entry_id == "2")
            .unwrap();
        assert_eq!(edge.sign, 0);
        assert_eq!(edge.interaction, "unknown");
    }

    #[test]
    fn test_parse_kgml_edge_conflict_sign() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Relação 1→6: activation + inhibition → conflito → sign=0, interaction="unknown"
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "1" && e.target_entry_id == "6")
            .unwrap();
        assert_eq!(edge.sign, 0);
        assert_eq!(edge.interaction, "unknown");
        // Ambos subtypes preservados
        assert!(edge.subtypes.contains(&"activation".to_string()));
        assert!(edge.subtypes.contains(&"inhibition".to_string()));
    }

    #[test]
    fn test_parse_kgml_edge_direction_entry1_to_entry2() {
        let parsed = parse_kgml(KGML_FIXTURE.as_bytes()).unwrap();
        // Direção: entry1 = source, entry2 = target
        let edge = parsed
            .edges
            .iter()
            .find(|e| e.source_entry_id == "6" && e.target_entry_id == "1")
            .unwrap();
        assert_eq!(edge.source_entry_id, "6");
        assert_eq!(edge.target_entry_id, "1");
    }

    // ─── Testes de regressão: dedupe de chave de conflito (bug hsa00030) ─────
    //
    // Reproduzem a causa-raiz do bug de produção: KGML metabólico com
    // múltiplas `<relation>` no MESMO par `(entry1, entry2)`, cuja natural key
    // de conflito `(pathway_id, source_node_id, target_node_id, interaction)`
    // colide DENTRO do mesmo lote de COPY. Antes do fix, isso fazia o
    // Postgres recusar `INSERT ... ON CONFLICT DO UPDATE` inteiro com
    // SQLSTATE 21000. Estes testes provam, sem precisar de Postgres real, que
    // `merge_duplicate_edges`/`dedup_nodes_by_entry_id` colapsam a chave ANTES
    // do COPY — logo o precondicionante do crash (duas linhas com a mesma
    // chave no mesmo lote) nunca chega à camada de banco.

    /// Fixture sintética espelhando a estrutura real de `hsa00030`: duas
    /// `<relation>` ECrel para o MESMO par (entry1="54", entry2="82"), cada
    /// uma com um `<subtype name="compound" value="...">` distinto — o
    /// metabólito que liga as duas enzimas. NÃO é KGML bruto KEGG (licença).
    const KGML_FIXTURE_DUPLICATE_PAIR: &str = r##"<?xml version="1.0"?>
<pathway name="path:hsa00030" org="hsa" number="00030"
         title="Pentose phosphate pathway (regression fixture)">
    <entry id="54" name="hsa:5213" type="gene">
        <graphics name="PFKL, ATP-PFK, PFK-B, PFK-L" fgcolor="#000000" bgcolor="#BFFFBF"
             type="rectangle" x="100" y="100" width="46" height="17"/>
    </entry>
    <entry id="82" name="hsa:2821" type="gene">
        <graphics name="GPI, AMF, NLK, PGI" fgcolor="#000000" bgcolor="#BFFFBF"
             type="rectangle" x="200" y="100" width="46" height="17"/>
    </entry>
    <relation entry1="54" entry2="82" type="ECrel">
        <subtype name="compound" value="90"/>
    </relation>
    <relation entry1="54" entry2="82" type="ECrel">
        <subtype name="compound" value="91"/>
    </relation>
</pathway>"##;

    #[test]
    fn test_parse_kgml_duplicate_pair_produces_two_separate_edges_at_parse_time() {
        // O parser NÃO deduplica — cada <relation> vira um ParsedEdge próprio.
        // O dedupe acontece depois, em merge_duplicate_edges (camada de COPY).
        let parsed = parse_kgml(KGML_FIXTURE_DUPLICATE_PAIR.as_bytes()).unwrap();
        assert_eq!(parsed.nodes.len(), 2);
        assert_eq!(parsed.edges.len(), 2);
        assert!(parsed
            .edges
            .iter()
            .all(|e| e.source_entry_id == "54" && e.target_entry_id == "82"));
        // "compound" não é subtype de sinal conhecido → ambas colapsam para
        // a mesma interação derivada (a MESMA natural key de conflito).
        assert!(parsed.edges.iter().all(|e| e.interaction == "unknown"));
        assert!(parsed.edges.iter().all(|e| e.sign == 0));
    }

    #[test]
    fn test_merge_duplicate_edges_collapses_same_conflict_key_into_one_row() {
        // Reproduz hsa00030 pós-parse: as duas relações resolvidas para PKs
        // devem colapsar em UMA única linha de COPY (a chave de conflito é
        // idêntica), provando que o precondicionante do erro 21000 desaparece.
        let parsed = parse_kgml(KGML_FIXTURE_DUPLICATE_PAIR.as_bytes()).unwrap();
        let mut node_id_map = HashMap::new();
        node_id_map.insert("54".to_string(), 1000i64);
        node_id_map.insert("82".to_string(), 2000i64);

        let (merged, n_unresolved, n_collapsed) =
            merge_duplicate_edges(&node_id_map, &parsed.edges);

        assert_eq!(n_unresolved, 0);
        assert_eq!(n_collapsed, 1, "as 2 relações devem colapsar em 1 dedupe");
        assert_eq!(
            merged.len(),
            1,
            "COPY deve receber só 1 linha para o par (54,82) — nunca 2 com a mesma chave"
        );

        let entry = merged.get(&(1000, 2000, "unknown".to_string())).unwrap();
        assert_eq!(entry.sign, 0);
        assert_eq!(entry.interaction, "unknown");
        // O parser hoje só captura o atributo `name` do <subtype> (não
        // `value`); como ambas relações têm name="compound", o union
        // (com dedup) resulta em um único valor "compound" — comportamento
        // correto para o campo que de fato é capturado (ver nota de módulo
        // sobre a limitação de não capturar `value`).
        assert_eq!(entry.subtypes, vec!["compound".to_string()]);
    }

    #[test]
    fn test_merge_duplicate_edges_unions_distinct_subtype_values_without_losing_provenance() {
        // Prova genérica (independente da particularidade "compound") de que
        // o union preserva TODOS os valores distintos de subtypes vistos nas
        // relações colapsadas, em vez de manter só a primeira e descartar.
        let mut node_id_map = HashMap::new();
        node_id_map.insert("1".to_string(), 10i64);
        node_id_map.insert("2".to_string(), 20i64);

        let edges = vec![
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["hidden compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
        ];

        let (merged, n_unresolved, n_collapsed) = merge_duplicate_edges(&node_id_map, &edges);

        assert_eq!(n_unresolved, 0);
        assert_eq!(n_collapsed, 1);
        assert_eq!(merged.len(), 1);
        let entry = merged.get(&(10, 20, "unknown".to_string())).unwrap();
        assert_eq!(
            entry.subtypes,
            vec!["compound".to_string(), "hidden compound".to_string()],
            "ambos os valores distintos de subtype devem ser preservados, na ordem de primeira ocorrência"
        );
    }

    #[test]
    fn test_merge_duplicate_edges_does_not_dedup_same_subtype_value_twice_within_union() {
        // Se as duas relações colapsadas trazem o MESMO valor de subtype, o
        // union não deve duplicá-lo (dedup dos valores, não só concatenar).
        let mut node_id_map = HashMap::new();
        node_id_map.insert("1".to_string(), 10i64);
        node_id_map.insert("2".to_string(), 20i64);

        let edges = vec![
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "ecrel".to_string(),
                subtypes: vec!["compound".to_string()],
                sign: 0,
                interaction: "unknown".to_string(),
            },
        ];

        let (merged, _n_unresolved, n_collapsed) = merge_duplicate_edges(&node_id_map, &edges);
        assert_eq!(n_collapsed, 1);
        let entry = merged.get(&(10, 20, "unknown".to_string())).unwrap();
        assert_eq!(entry.subtypes, vec!["compound".to_string()]);
    }

    #[test]
    fn test_merge_duplicate_edges_keeps_distinct_interactions_separate() {
        // Mesmo par (entry1, entry2), mas interações DIFERENTES não devem
        // colapsar — a chave de conflito inclui `interaction`.
        let mut node_id_map = HashMap::new();
        node_id_map.insert("1".to_string(), 10i64);
        node_id_map.insert("2".to_string(), 20i64);

        let edges = vec![
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "pprel".to_string(),
                subtypes: vec!["activation".to_string()],
                sign: 1,
                interaction: "activation".to_string(),
            },
            ParsedEdge {
                source_entry_id: "1".to_string(),
                target_entry_id: "2".to_string(),
                relation_type: "pprel".to_string(),
                subtypes: vec!["inhibition".to_string()],
                sign: -1,
                interaction: "inhibition".to_string(),
            },
        ];

        let (merged, _n_unresolved, n_collapsed) = merge_duplicate_edges(&node_id_map, &edges);
        assert_eq!(n_collapsed, 0, "interações distintas não devem colapsar");
        assert_eq!(merged.len(), 2);
    }

    #[test]
    fn test_merge_duplicate_edges_reports_unresolved_entry_ids() {
        let node_id_map = HashMap::new(); // vazio: nada resolve
        let edges = vec![ParsedEdge {
            source_entry_id: "1".to_string(),
            target_entry_id: "2".to_string(),
            relation_type: "pprel".to_string(),
            subtypes: vec!["activation".to_string()],
            sign: 1,
            interaction: "activation".to_string(),
        }];

        let (merged, n_unresolved, n_collapsed) = merge_duplicate_edges(&node_id_map, &edges);
        assert_eq!(n_unresolved, 1);
        assert_eq!(n_collapsed, 0);
        assert!(merged.is_empty());
    }

    #[test]
    fn test_dedup_nodes_by_entry_id_collapses_repeated_entry_id_keeping_last() {
        // Defensivo (item 2 do pedido): KGML não deveria repetir <entry id>,
        // mas se repetir, o mesmo erro 21000 apareceria na NK
        // (pathway, kegg_entry_id) sem essa defesa.
        let nodes = vec![
            ParsedNode {
                kegg_entry_id: "1".to_string(),
                node_type: "gene".to_string(),
                kegg_ids: vec!["hsa:1".to_string()],
                gene_symbol: "FIRST".to_string(),
                graphics_name: "FIRST".to_string(),
            },
            ParsedNode {
                kegg_entry_id: "1".to_string(),
                node_type: "gene".to_string(),
                kegg_ids: vec!["hsa:1".to_string()],
                gene_symbol: "SECOND".to_string(),
                graphics_name: "SECOND".to_string(),
            },
        ];

        let (deduped, n_collapsed) = dedup_nodes_by_entry_id(&nodes);
        assert_eq!(n_collapsed, 1);
        assert_eq!(deduped.len(), 1);
        assert_eq!(deduped.get("1").unwrap().gene_symbol, "SECOND");
    }

    #[test]
    fn test_dedup_nodes_by_entry_id_is_noop_when_entry_ids_are_unique() {
        let nodes = vec![
            ParsedNode {
                kegg_entry_id: "1".to_string(),
                node_type: "gene".to_string(),
                kegg_ids: vec!["hsa:1".to_string()],
                gene_symbol: "A".to_string(),
                graphics_name: "A".to_string(),
            },
            ParsedNode {
                kegg_entry_id: "2".to_string(),
                node_type: "gene".to_string(),
                kegg_ids: vec!["hsa:2".to_string()],
                gene_symbol: "B".to_string(),
                graphics_name: "B".to_string(),
            },
        ];

        let (deduped, n_collapsed) = dedup_nodes_by_entry_id(&nodes);
        assert_eq!(n_collapsed, 0);
        assert_eq!(deduped.len(), 2);
    }

    // ─── Testes de validação de URL ──────────────────────────────────────────

    #[test]
    fn test_validate_kegg_url_accept() {
        assert!(validate_kegg_url("https://rest.kegg.jp/get/hsa04151/kgml").is_ok());
        assert!(validate_kegg_url("https://rest.kegg.jp/get/hsa04010/kgml").is_ok());
        assert!(validate_kegg_url("https://rest.kegg.jp/get/hsa04115/kgml").is_ok());
    }

    #[test]
    fn test_validate_kegg_url_reject_http() {
        assert!(validate_kegg_url("http://rest.kegg.jp/get/hsa04151/kgml").is_err());
    }

    #[test]
    fn test_validate_kegg_url_reject_wrong_host() {
        assert!(validate_kegg_url("https://evil.com/get/hsa04151/kgml").is_err());
        assert!(validate_kegg_url("https://www.kegg.jp/get/hsa04151/kgml").is_err());
        assert!(validate_kegg_url("https://kegg.jp/get/hsa04151/kgml").is_err());
    }

    #[test]
    fn test_validate_kegg_url_reject_path_traversal() {
        assert!(validate_kegg_url("https://rest.kegg.jp/get/../etc/passwd").is_err());
        assert!(validate_kegg_url("https://rest.kegg.jp/../admin").is_err());
    }

    #[test]
    fn test_validate_kegg_url_reject_omnipathdb() {
        // host diferente não deve ser aceito
        assert!(validate_kegg_url("https://omnipathdb.org/interactions").is_err());
    }

    // ─── Testes de validação de kegg_id (hardening A3) ───────────────────────

    #[test]
    fn test_validate_kegg_id_accept() {
        assert!(validate_kegg_id("hsa04151").is_ok());
        assert!(validate_kegg_id("hsa04010").is_ok());
        assert!(validate_kegg_id("hsa04115").is_ok());
        assert!(validate_kegg_id("mmu04010").is_ok());
    }

    #[test]
    fn test_validate_kegg_id_reject_path_traversal_and_slash() {
        assert!(validate_kegg_id("/etc/passwd").is_err());
        assert!(validate_kegg_id("../foo").is_err());
        assert!(validate_kegg_id("hsa04151/../x").is_err());
        assert!(validate_kegg_id("hsa04151/x").is_err());
        assert!(validate_kegg_id("hsa/04151").is_err());
    }

    #[test]
    fn test_validate_kegg_id_reject_empty_and_malformed() {
        assert!(validate_kegg_id("").is_err());
        assert!(validate_kegg_id("hsa041511").is_err());
        assert!(validate_kegg_id("hsa0415").is_err());
        assert!(validate_kegg_id("HSA04151").is_err());
        assert!(validate_kegg_id("hsaabcde").is_err());
    }

    #[test]
    fn test_kgml_url_rejects_malicious_kegg_id() {
        assert!(kgml_url("/etc/passwd").is_err());
        assert!(kgml_url("../../etc/passwd").is_err());
        assert!(kgml_url("hsa04151/../x").is_err());
    }
}
