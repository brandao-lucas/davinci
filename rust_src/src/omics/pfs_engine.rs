/// Motor PFS v1 — RWR assinado + leitura por Regras + escore contínuo por
/// permutação. OmnisPathway Objetivo 2, Fase 4.
///
/// Ver plano completo: `.claude/plans/2026-07-16-omnispathway-obj2-fase4-motor-pfs-v1.md`
/// (Decisões D1–D10, TRAVADAS, e o ADENDO com as decisões finais do usuário).
///
/// # Contrato de extensão (D10)
///
/// `propagate_signed_rwr(graph, seeds, params) -> Vec<f64>` é a função pura
/// isolada que qualquer propagador futuro (PCST, calor) precisa implementar.
/// Ela não conhece Parquet, Postgres nem permutação — só o grafo assinado em
/// CSR e o vetor de sementes.
///
/// # Truque de linearidade (ganho ~250×, ver ADENDO §Paralelismo)
///
/// O RWR é linear no vetor de sementes `s`. Por via, pré-computamos a
/// influência de CADA nó semeável (`propagate_signed_rwr` com semente unitária
/// nesse nó) uma única vez — matriz `F` (n_semeable × n_nodes). Qualquer
/// combinação de sementes (real ou permutada) vira uma combinação linear de
/// colunas de `F`, e a permutação inteira vira produto escalar contra um vetor
/// `ρ` pré-projetado (`ρ = Fᵀ·y`, uma projeção por (via, caso)).
///
/// # PRNG counter-based (obrigatório mesmo single-thread)
///
/// A semente de cada permutação é derivada de `(rng_seed, pathway_id,
/// sample_id, perm_index)` via `splitmix64` — nunca de um stream sequencial
/// compartilhado. Isso torna cada permutação reproduzível isoladamente,
/// independente de ordem de execução ou de paralelização futura (`rayon`).
///
/// # Contrato de COPY (migration 0037, cartografo)
///
/// 17 colunas em ordem exata (ver `apps/core/models.py::PathwayActivityScore`
/// e a migration `0037_pathway_activity_score.py`). `computed_at` é `NOW()`
/// no INSERT final, não entra no staging. Contadores são sempre `>= 0`
/// (nunca `-1` como sentinela — vira `INTEGER CHECK >= 0`); `null_sd == 0.0`
/// grava `z_score = 0.0` explicitamente (nunca `NaN`/`Infinity`).
///
/// # Segurança
///
/// `db_url` nunca logada.
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::time::Instant;

use arrow::array::{Array, Float32Array};
use bytes::Bytes;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use tokio_postgres::Client;

// =============================================================================
// §1 — MANIFESTO PÚBLICO
// =============================================================================

/// Manifesto retornado por `run_pfs_scoring`. Resumo pequeno (contadores +
/// agregados) — NUNCA a matriz de escores (skill django-rust-boundary: "Rust
/// nunca retorna objeto complexo"). O detalhe vive em `core_pathwayactivityscore`.
///
/// # Granularidade dos agregados de readout (leia antes de somar/dividir)
///
/// `n_readouts_mapped`, `n_readouts_with_value` e `n_readouts_missing` são
/// TODOS somados na MESMA granularidade — uma vez por LINHA de escore emitida
/// (`(via, amostra)`), exatamente como as colunas homônimas de
/// `core_pathwayactivityscore`. Isso preserva no manifesto a mesma identidade
/// que vale linha a linha no banco: `mapped == with_value + missing`, e a
/// razão `with_value / mapped` é a cobertura de readout REAL da execução (é o
/// 2º passo do diagnóstico anti-p-hacking do plano: seed → readout → arestas
/// sem sinal → nº de pares). Misturar `mapped` "uma vez por via" com
/// `with_value`/`missing` "uma vez por caso" quebra essa razão sem que o dado
/// gravado por linha esteja errado — foi exatamente esse bug de agregação
/// (não de gravação) que este comentário documenta para não se repetir.
///
/// `n_readouts_phospho`/`n_readouts_tf` são uma granularidade DIFERENTE, DE
/// PROPÓSITO: contam NÓS que efetivamente pontuaram (readout de fosfo /
/// TF com footprint ≥ `min_regulon_targets`), não linhas de
/// `PathwayReadoutFeature` nem pares TF-alvo. Um nó de TF agrega dezenas de
/// alvos; `n_readouts_tf` (nós) diverge muito de "linhas tf_target mapeadas"
/// por desenho — **não somar `n_readouts_phospho + n_readouts_tf` esperando
/// bater com `n_readouts_mapped`** (isso mistura node-count com row-count).
/// Mesma semântica de `PathwayActivityScore.n_readouts_phospho`/`.n_readouts_tf`
/// (ver `help_text` do model em `apps/core/models.py` — mudança de nome/doc
/// desses dois campos é handoff cartografo, fora do escopo `rust_src/`).
#[derive(Debug, Clone, Default)]
pub struct PfsRunManifest {
    pub n_pathways: u64,
    pub n_pathways_no_readout: u64,
    pub n_cases_scored: u64,
    pub n_rows_written: u64,
    pub n_seeds_total: u64,
    pub n_seeds_on_graph: u64,
    pub n_seeds_off_graph: u64,
    pub n_seeds_masked_by_neutral_v2: u64,
    /// Soma de `n_readouts_mapped` por LINHA de escore emitida (não por via) —
    /// ver nota de granularidade acima.
    pub n_readouts_mapped: u64,
    /// Soma de `n_readouts_with_value` por linha de escore emitida.
    pub n_readouts_with_value: u64,
    /// Soma de `n_readouts_missing` por linha de escore emitida.
    /// Invariante: `n_readouts_missing == n_readouts_mapped - n_readouts_with_value`.
    pub n_readouts_missing: u64,
    /// Soma de NÓS de fosfo que contribuíram (Regra 1) — node-count, não row-count.
    pub n_readouts_phospho: u64,
    /// Soma de NÓS de TF com footprint computado (Regra 2) — node-count, não
    /// contagem de pares TF-alvo.
    pub n_readouts_tf: u64,
    pub n_tf_below_min_targets: u64,
    pub n_tumor_unpaired_skipped: u64,
    pub n_zero_sd_null: u64,
    pub n_unsigned_edges: u64,
    pub n_signed_edges: u64,
    // Tempos por etapa (ms) — dado que decide sobre paralelizar com rayon.
    pub elapsed_ms_load: u64,
    pub elapsed_ms_graph_precompute: u64,
    pub elapsed_ms_readout_and_permutation: u64,
    pub elapsed_ms_copy: u64,
    pub elapsed_ms_total: u64,
}

// =============================================================================
// §2 — PRNG COUNTER-BASED (splitmix64 / xorshift64*) — SEM crate `rand`
// =============================================================================

/// splitmix64 — usado só para DERIVAR a semente de cada permutação a partir
/// de índices (rng_seed, pathway_id, sample_id, perm_index). NÃO é o gerador
/// dos sorteios em si (isso é `Xorshift64Star`, abaixo).
#[inline]
fn splitmix64(x: u64) -> u64 {
    let x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// Deriva a semente de UMA permutação a partir dos índices — counter-based,
/// nunca de um stream sequencial compartilhado. Reproduzível isoladamente,
/// em qualquer ordem/núcleo (pré-requisito para paralelizar com `rayon` no
/// futuro sem quebrar reprodutibilidade).
pub fn derive_permutation_seed(rng_seed: u64, pathway_id: i64, sample_id: i64, perm_index: u32) -> u64 {
    let mut state = rng_seed;
    state = splitmix64(state ^ (pathway_id as u64));
    state = splitmix64(state ^ (sample_id as u64));
    state = splitmix64(state ^ (perm_index as u64));
    state
}

/// Gerador xorshift64* — determinístico, ~15 linhas, sem dependência nova.
/// Usado SÓ dentro de uma permutação já semeada por `derive_permutation_seed`.
pub struct Xorshift64Star {
    state: u64,
}

impl Xorshift64Star {
    pub fn new(seed: u64) -> Self {
        // Estado 0 é um ponto fixo do xorshift — nunca deixar chegar a 0.
        Xorshift64Star { state: if seed == 0 { 0x9E37_79B9_7F4A_7C15 } else { seed } }
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// Sorteia um índice uniforme em `[0, n)`. `n == 0` retorna 0 (nunca
    /// panic — o chamador garante `n > 0` antes de chamar).
    pub fn gen_range(&mut self, n: usize) -> usize {
        if n == 0 {
            return 0;
        }
        (self.next_u64() as usize) % n
    }
}

// =============================================================================
// §3 — GRAFO ASSINADO EM CSR (D5) — função pura, sem I/O
// =============================================================================

/// Grafo de UMA via já em CSR: lista de out-edges por nó local, com pesos
/// assinados e normalizados (`Σ|W[j→i]| = 1` por nó `j` com out-edges reais;
/// nó dangling ganha auto-laço de peso 1.0 — D5 "massa fica no nó").
#[derive(Debug, Clone)]
pub struct PathwayGraphCsr {
    pub n_nodes: usize,
    /// `out_edges[j] = Vec<(i, w_normalizado)>` — out-edges de `j`.
    pub out_edges: Vec<Vec<(usize, f64)>>,
    /// Grau de saída CRU (antes do auto-laço dangling) — usado só para a
    /// estratificação por quartil da permutação (D7).
    pub out_degree_raw: Vec<usize>,
}

/// Constrói o CSR assinado a partir de arestas cruas `(source_idx, target_idx,
/// sign)`. `sign ∈ {-1, 0, +1}`; `sign == 0` (binding/indirect/unknown) vira
/// aresta ativante de peso reduzido `unsigned_weight` (D5).
///
/// Retorna `(csr, n_signed, n_unsigned)` — contadores para o manifesto
/// (gatilho D-2 do plano: `n_unsigned/n_edges > 0.5` merece reavaliação).
pub fn build_csr(
    n_nodes: usize,
    raw_edges: &[(usize, usize, i32)],
    unsigned_weight: f64,
) -> (PathwayGraphCsr, u64, u64) {
    let mut adj: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n_nodes];
    let mut out_degree_raw = vec![0usize; n_nodes];
    let mut n_signed = 0u64;
    let mut n_unsigned = 0u64;

    for &(src, tgt, sign) in raw_edges {
        let effective = if sign == 0 { unsigned_weight } else { sign as f64 };
        if sign == 0 {
            n_unsigned += 1;
        } else {
            n_signed += 1;
        }
        adj[src].push((tgt, effective));
        out_degree_raw[src] += 1;
    }

    let mut out_edges: Vec<Vec<(usize, f64)>> = vec![Vec::new(); n_nodes];
    for j in 0..n_nodes {
        if adj[j].is_empty() {
            // Dangling: massa fica no nó (auto-laço implícito, D5).
            out_edges[j] = vec![(j, 1.0)];
        } else {
            let sum_abs: f64 = adj[j].iter().map(|(_, w)| w.abs()).sum();
            out_edges[j] = adj[j]
                .iter()
                .map(|&(t, w)| (t, w / sum_abs))
                .collect();
        }
    }

    (
        PathwayGraphCsr { n_nodes, out_edges, out_degree_raw },
        n_signed,
        n_unsigned,
    )
}

/// Função pura isolada (D10, contrato de extensão) — RWR assinado por
/// iteração de potência: `p_{t+1} = (1-r)·Wᵀp_t + r·s`.
///
/// Convergência: `Σ|p_{t+1} - p_t| < tol` ou `max_iter` iterações.
/// Determinístico (sem aleatoriedade). Linear em `seeds` — ver `§4`.
pub fn propagate_signed_rwr(
    csr: &PathwayGraphCsr,
    seeds: &[f64],
    restart: f64,
    max_iter: usize,
    tol: f64,
) -> Vec<f64> {
    let n = csr.n_nodes;
    let mut p: Vec<f64> = seeds.to_vec();

    for _ in 0..max_iter {
        let mut next = vec![0.0f64; n];
        for j in 0..n {
            let pj = p[j];
            if pj == 0.0 {
                continue;
            }
            for &(i, w) in &csr.out_edges[j] {
                next[i] += (1.0 - restart) * w * pj;
            }
        }
        for i in 0..n {
            next[i] += restart * seeds[i];
        }

        let diff: f64 = next.iter().zip(p.iter()).map(|(a, b)| (a - b).abs()).sum();
        p = next;
        if diff < tol {
            break;
        }
    }

    p
}

// =============================================================================
// §4 — SEMENTES (D3/D4) — funções puras, sem I/O
// =============================================================================

/// Uma linha crua de `core_varianteffectseed` — inclui `neutral` de propósito
/// (a precedência D3 precisa ENXERGAR o neutral para mascarar corretamente).
#[derive(Debug, Clone)]
pub struct SeedCandidate {
    pub sample_id: i64,
    pub gene_symbol: String,
    pub direction: String, // activator | inactivator | neutral
    pub magnitude: f64,
    pub confidence: f64,
    pub method_version: String,
}

/// Seed efetiva pós-precedência (D3) — só existe para `activator`/`inactivator`.
#[derive(Debug, Clone, Copy)]
pub struct EffectiveSeed {
    /// `sign(direction) · magnitude · confidence`.
    pub signed_weight: f64,
}

/// Ordem de precedência D3 (TRAVADA): `fase2-cnv-v2 > fase2-snv-v1 >
/// fase2-cnv-v1`. Versões desconhecidas ficam no fim (rank alto) — não
/// quebra, mas nunca vencem uma versão conhecida.
pub fn precedence_rank(method_version: &str) -> u8 {
    match method_version {
        "fase2-cnv-v2" => 0,
        "fase2-snv-v1" => 1,
        "fase2-cnv-v1" => 2,
        _ => 200,
    }
}

/// Resolve precedência D3: uma seed efetiva por `(sample, gene)`.
///
/// - `neutral` NÃO é perturbação → não emite seed efetiva.
/// - Efeito colateral declarado (D3): um `fase2-cnv-v2` `neutral` MASCARA uma
///   seed real (`activator`/`inactivator`) de precedência menor para o MESMO
///   `(sample, gene)` — contado em `n_seeds_masked_by_neutral_v2`.
///
/// Retorna `(mapa (sample_id, gene_symbol) -> EffectiveSeed, n_masked)`.
pub fn resolve_seed_precedence(
    candidates: Vec<SeedCandidate>,
) -> (HashMap<(i64, String), EffectiveSeed>, u64) {
    let mut groups: HashMap<(i64, String), Vec<SeedCandidate>> = HashMap::new();
    for c in candidates {
        groups
            .entry((c.sample_id, c.gene_symbol.to_uppercase()))
            .or_default()
            .push(c);
    }

    let mut result: HashMap<(i64, String), EffectiveSeed> = HashMap::new();
    let mut n_masked = 0u64;

    for (key, mut group) in groups {
        group.sort_by_key(|c| precedence_rank(&c.method_version));
        let winner = group[0].clone();

        if winner.direction == "neutral" {
            let has_real_alt = group[1..].iter().any(|c| c.direction != "neutral");
            if winner.method_version == "fase2-cnv-v2" && has_real_alt {
                n_masked += 1;
            }
            continue;
        }

        let sign = if winner.direction == "activator" { 1.0 } else { -1.0 };
        result.insert(
            key,
            EffectiveSeed { signed_weight: sign * winner.magnitude * winner.confidence },
        );
    }

    (result, n_masked)
}

/// Constrói o vetor de sementes CRU (não normalizado em L1) sobre os nós de
/// UMA via, a partir das seeds efetivas de UMA amostra (D4):
/// junção por símbolo UPPERCASE, peso dividido `w/k` entre nós homônimos,
/// gene sem nó nesta via descartado + contado.
///
/// Retorna `(vetor_cru, n_on_graph, n_off_graph)`.
pub fn build_seed_vector(
    n_nodes: usize,
    gene_to_nodes: &HashMap<String, Vec<usize>>,
    sample_seeds: &[(String, f64)],
) -> (Vec<f64>, u64, u64) {
    let mut raw = vec![0.0f64; n_nodes];
    let mut n_on = 0u64;
    let mut n_off = 0u64;

    for (gene, w) in sample_seeds {
        match gene_to_nodes.get(gene) {
            Some(idxs) if !idxs.is_empty() => {
                let k = idxs.len() as f64;
                for &idx in idxs {
                    raw[idx] += w / k;
                }
                n_on += 1;
            }
            _ => {
                n_off += 1;
            }
        }
    }

    (raw, n_on, n_off)
}

// =============================================================================
// §5 — READOUT: escala por sd da coorte (D6, comum às duas Regras)
// =============================================================================

/// Desvio-padrão AMOSTRAL (ddof=1). `< 2` valores → `0.0` (sem sd definida).
pub fn sample_std(values: &[f64]) -> f64 {
    let n = values.len();
    if n < 2 {
        return 0.0;
    }
    let mean = values.iter().sum::<f64>() / n as f64;
    let var = values.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0);
    var.sqrt()
}

/// `Δx_f / sd_coorte(Δx_f)`, `sd == 0` → divisor `1.0` (D6 — escala, nunca
/// divide por zero, nunca produz `NaN`/`Infinity`).
#[inline]
pub fn scale_delta(raw_delta: f64, sd: f64) -> f64 {
    if sd == 0.0 {
        raw_delta
    } else {
        raw_delta / sd
    }
}

// =============================================================================
// §6 — ULM ASSINADO (D6, Regra 2 — forma fechada, ~30 linhas) — função pura
// =============================================================================

/// Atividade de TF = estatística t da inclinação `b1` da regressão
/// `Δx_alvo ~ b0 + b1·regulon_sign`. `pairs = [(regulon_sign, Δx_escalado)]`.
///
/// Retorna `None` quando a regressão é indefinida (menos de 2 alvos, `df<=0`,
/// sem variância em `x` — todos os alvos com o mesmo `regulon_sign` — ou erro
/// padrão nulo). O chamador aplica o gate `min_regulon_targets` ANTES de
/// chamar esta função (ela só decide se a regressão em si é bem-posta).
pub fn ulm_tf_activity(pairs: &[(f64, f64)]) -> Option<f64> {
    let n = pairs.len();
    if n < 3 {
        // df = n-2 precisa ser > 0 para estatística t.
        return None;
    }

    let mean_x = pairs.iter().map(|(x, _)| x).sum::<f64>() / n as f64;
    let mean_y = pairs.iter().map(|(_, y)| y).sum::<f64>() / n as f64;

    let sxx: f64 = pairs.iter().map(|(x, _)| (x - mean_x).powi(2)).sum();
    if sxx <= 1e-12 {
        // Sem variância em x (todos os alvos com o mesmo regulon_sign) —
        // inclinação indeterminada; não inventar direção.
        return None;
    }

    let sxy: f64 = pairs.iter().map(|(x, y)| (x - mean_x) * (y - mean_y)).sum();
    let b1 = sxy / sxx;
    let b0 = mean_y - b1 * mean_x;

    let df = (n - 2) as f64;
    let sse: f64 = pairs
        .iter()
        .map(|(x, y)| {
            let pred = b0 + b1 * x;
            (y - pred).powi(2)
        })
        .sum();
    let mse = sse / df;
    if mse <= 0.0 {
        // Ajuste perfeito (resíduo zero) → erro padrão nulo → t indefinido
        // (tenderia a infinito). Não emitir não-finito — descartar.
        return None;
    }

    let se_b1 = (mse / sxx).sqrt();
    let t = b1 / se_b1;
    if t.is_finite() {
        Some(t)
    } else {
        None
    }
}

// =============================================================================
// §7 — PERMUTAÇÃO ESTRATIFICADA (D7) — funções puras
// =============================================================================

/// Quartil de out-degree (0..3) de cada nó semeável, por RANK dentro do
/// conjunto de nós semeáveis desta via (não por valor absoluto).
pub fn compute_quartiles(semeable_idxs: &[usize], out_degree_raw: &[usize]) -> HashMap<usize, u8> {
    let mut sorted: Vec<usize> = semeable_idxs.to_vec();
    sorted.sort_by_key(|&idx| out_degree_raw[idx]);
    let n = sorted.len().max(1);

    let mut quartile_of = HashMap::with_capacity(sorted.len());
    for (rank, &idx) in sorted.iter().enumerate() {
        let q = ((rank * 4) / n).min(3) as u8;
        quartile_of.insert(idx, q);
    }
    quartile_of
}

/// Agrupa nós semeáveis por quartil — pool de candidatos ao sorteio.
pub fn build_quartile_pools(
    semeable_idxs: &[usize],
    quartile_of: &HashMap<usize, u8>,
) -> HashMap<u8, Vec<usize>> {
    let mut pools: HashMap<u8, Vec<usize>> = HashMap::new();
    for &idx in semeable_idxs {
        if let Some(&q) = quartile_of.get(&idx) {
            pools.entry(q).or_default().push(idx);
        }
    }
    pools
}

/// `S(s) = Σ_i (value_i / total_l1) · ρ[node_i]` — o produto escalar que
/// substitui a iteração de potência inteira graças à linearidade do RWR
/// (§ADENDO Paralelismo). `seed_list` pode ter posições repetidas (colisão de
/// permutação) — a soma trata isso corretamente sem dedup.
pub fn score_from_positions(
    seed_list: &[(usize, f64)],
    rho: &HashMap<usize, f64>,
    total_l1: f64,
) -> f64 {
    if total_l1 <= 0.0 {
        return 0.0;
    }
    let mut s = 0.0;
    for &(node, value) in seed_list {
        let r = rho.get(&node).copied().unwrap_or(0.0);
        s += (value / total_l1) * r;
    }
    s
}

/// Permuta a POSIÇÃO de cada semente para um nó sorteado do MESMO quartil de
/// out-degree, preservando o multiconjunto de valores (D7). `seed_list` deve
/// vir ORDENADO por `node` pelo chamador — garante que o consumo do `rng` seja
/// sempre na mesma ordem, independente de como a lista original foi montada
/// (propriedade "independente de ordem" do DoD).
pub fn permute_seed_positions(
    seed_list_sorted: &[(usize, f64)],
    quartile_of: &HashMap<usize, u8>,
    pools: &HashMap<u8, Vec<usize>>,
    rng: &mut Xorshift64Star,
) -> Vec<(usize, f64)> {
    let mut result = Vec::with_capacity(seed_list_sorted.len());
    for &(node, value) in seed_list_sorted {
        let q = quartile_of.get(&node).copied().unwrap_or(0);
        match pools.get(&q) {
            Some(pool) if !pool.is_empty() => {
                let draw = rng.gen_range(pool.len());
                result.push((pool[draw], value));
            }
            _ => {
                // Sem pool (não deveria ocorrer — nó semeável sem quartil
                // atribuído) — mantém a posição original em vez de panicar.
                result.push((node, value));
            }
        }
    }
    result
}

/// Resultado do modelo nulo por permutação (D7) para um `(pathway, sample)`.
#[derive(Debug, Clone, Copy)]
pub struct NullResult {
    pub s_obs: f64,
    pub null_mean: f64,
    pub null_sd: f64,
    pub p_empirical: f64,
    pub permutations_run: u32,
}

/// Roda a nula inteira para um `(pathway, sample)`: `B` permutações,
/// estratificadas por quartil, PRNG counter-based por permutação.
///
/// Casos degenerados (SEM crash, SEM NaN — casos de teste de primeira classe):
/// - `total_l1 <= 0` (zero sementes no grafo) → `null_sd=0`, `z=0` (via
///   chamador), `p_empirical=1.0`, `permutations_run=0`.
/// - `n_permutations == 0` → idem.
#[allow(clippy::too_many_arguments)]
pub fn compute_null_and_score(
    seed_list: &[(usize, f64)],
    total_l1: f64,
    rho: &HashMap<usize, f64>,
    quartile_of: &HashMap<usize, u8>,
    pools: &HashMap<u8, Vec<usize>>,
    n_permutations: u32,
    rng_seed: u64,
    pathway_id: i64,
    sample_id: i64,
) -> NullResult {
    let s_obs = score_from_positions(seed_list, rho, total_l1);

    if total_l1 <= 0.0 || n_permutations == 0 || seed_list.is_empty() {
        return NullResult {
            s_obs,
            null_mean: 0.0,
            null_sd: 0.0,
            p_empirical: 1.0,
            permutations_run: 0,
        };
    }

    let mut sorted_seeds: Vec<(usize, f64)> = seed_list.to_vec();
    sorted_seeds.sort_by_key(|&(node, _)| node);

    let mut sum = 0.0f64;
    let mut sum_sq = 0.0f64;
    let mut count_ge = 0u64;

    for b in 0..n_permutations {
        let seed_material = derive_permutation_seed(rng_seed, pathway_id, sample_id, b);
        let mut rng = Xorshift64Star::new(seed_material);
        let perm = permute_seed_positions(&sorted_seeds, quartile_of, pools, &mut rng);
        let s_b = score_from_positions(&perm, rho, total_l1);
        sum += s_b;
        sum_sq += s_b * s_b;
        if s_b.abs() >= s_obs.abs() {
            count_ge += 1;
        }
    }

    let b_f = n_permutations as f64;
    let null_mean = sum / b_f;
    let variance = (sum_sq / b_f - null_mean * null_mean).max(0.0);
    let null_sd = variance.sqrt();
    let p_empirical = (1.0 + count_ge as f64) / (1.0 + b_f);

    NullResult { s_obs, null_mean, null_sd, p_empirical, permutations_run: n_permutations }
}

// =============================================================================
// §8 — LEITURA DO GRAFO / SEEDS / READOUTS / PARES via `db_url`
// =============================================================================

struct RawNode {
    id: i64,
    node_type: String,
    gene_symbol: String,
}

struct RawEdge {
    source_node_id: i64,
    target_node_id: i64,
    sign: i32,
}

/// Grafo de uma via já resolvido para índices locais 0-based (exclui nós
/// `map`, D5). `id_to_local` mapeia `PathwayNode.id` (bigint) → índice local.
struct LoadedGraph {
    n_nodes: usize,
    id_to_local: HashMap<i64, usize>,
    gene_to_nodes: HashMap<String, Vec<usize>>,
    semeable_idxs: Vec<usize>,
    csr: PathwayGraphCsr,
    n_signed: u64,
    n_unsigned: u64,
}

async fn load_pathway_id(client: &Client, kegg_id: &str) -> Result<Option<i64>, String> {
    let rows = client
        .query("SELECT id FROM core_pathway WHERE kegg_id = $1", &[&kegg_id])
        .await
        .map_err(|e| format!("Query core_pathway falhou: {e}"))?;
    Ok(rows.first().map(|r| r.get::<_, i64>(0)))
}

async fn load_graph(client: &Client, pathway_id: i64, unsigned_weight: f64) -> Result<LoadedGraph, String> {
    let node_rows = client
        .query(
            "SELECT id, node_type, gene_symbol FROM core_pathwaynode \
             WHERE pathway_id = $1 AND node_type != 'map'",
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("Query core_pathwaynode falhou: {e}"))?;

    let raw_nodes: Vec<RawNode> = node_rows
        .iter()
        .map(|r| RawNode {
            id: r.get(0),
            node_type: r.get(1),
            gene_symbol: r.get::<_, String>(2).to_uppercase(),
        })
        .collect();

    let n_nodes = raw_nodes.len();
    let mut id_to_local: HashMap<i64, usize> = HashMap::with_capacity(n_nodes);
    for (local, n) in raw_nodes.iter().enumerate() {
        id_to_local.insert(n.id, local);
    }

    let mut gene_to_nodes: HashMap<String, Vec<usize>> = HashMap::new();
    let mut semeable_idxs: Vec<usize> = Vec::new();
    for (local, n) in raw_nodes.iter().enumerate() {
        let is_semeable = matches!(n.node_type.as_str(), "gene" | "ortholog") && !n.gene_symbol.is_empty();
        if is_semeable {
            gene_to_nodes.entry(n.gene_symbol.clone()).or_default().push(local);
            semeable_idxs.push(local);
        }
    }

    let edge_rows = client
        .query(
            "SELECT source_node_id, target_node_id, sign FROM core_pathwayedge \
             WHERE pathway_id = $1",
            &[&pathway_id],
        )
        .await
        .map_err(|e| format!("Query core_pathwayedge falhou: {e}"))?;

    let raw_edges: Vec<RawEdge> = edge_rows
        .iter()
        .map(|r| RawEdge {
            source_node_id: r.get(0),
            target_node_id: r.get(1),
            sign: r.get::<_, i16>(2) as i32,
        })
        .collect();

    let mut local_edges: Vec<(usize, usize, i32)> = Vec::with_capacity(raw_edges.len());
    for e in &raw_edges {
        if let (Some(&src), Some(&tgt)) = (id_to_local.get(&e.source_node_id), id_to_local.get(&e.target_node_id)) {
            local_edges.push((src, tgt, e.sign));
        }
        // source/target ausente (nó `map` excluído) → aresta descartada por
        // construção (D5), sem contar erro — é o comportamento esperado.
    }

    let (csr, n_signed, n_unsigned) = build_csr(n_nodes, &local_edges, unsigned_weight);

    Ok(LoadedGraph {
        n_nodes,
        id_to_local,
        gene_to_nodes,
        semeable_idxs,
        csr,
        n_signed,
        n_unsigned,
    })
}

async fn load_seed_candidates(
    client: &Client,
    method_versions: &[String],
) -> Result<Vec<SeedCandidate>, String> {
    let rows = client
        .query(
            "SELECT sample_id, gene_symbol, direction, magnitude, confidence, method_version \
             FROM core_varianteffectseed WHERE method_version = ANY($1)",
            &[&method_versions],
        )
        .await
        .map_err(|e| format!("Query core_varianteffectseed falhou: {e}"))?;

    Ok(rows
        .iter()
        .map(|r| SeedCandidate {
            sample_id: r.get(0),
            gene_symbol: r.get::<_, String>(1).to_uppercase(),
            direction: r.get(2),
            magnitude: r.get(3),
            confidence: r.get(4),
            method_version: r.get(5),
        })
        .collect())
}

async fn load_matrix_sample_columns(client: &Client, matrix_id: i64) -> Result<HashMap<i64, i32>, String> {
    let rows = client
        .query(
            "SELECT sample_id, column_index FROM core_omicmatrixsample WHERE matrix_id = $1",
            &[&matrix_id],
        )
        .await
        .map_err(|e| format!("Query core_omicmatrixsample falhou: {e}"))?;

    Ok(rows.iter().map(|r| (r.get::<_, i64>(0), r.get::<_, i32>(1))).collect())
}

struct PairRow {
    pairing_id: i64,
    tumor_sample_id: i64,
    normal_sample_id: i64,
    #[allow(dead_code)]
    case_id: String,
}

async fn load_validated_pairs(client: &Client, matrix_id: i64) -> Result<Vec<PairRow>, String> {
    let rows = client
        .query(
            "SELECT id, tumor_sample_id, normal_sample_id, case_id FROM core_omicsamplepairing \
             WHERE matrix_id = $1 AND validated_overlap = true",
            &[&matrix_id],
        )
        .await
        .map_err(|e| format!("Query core_omicsamplepairing falhou: {e}"))?;

    Ok(rows
        .iter()
        .map(|r| PairRow {
            pairing_id: r.get(0),
            tumor_sample_id: r.get(1),
            normal_sample_id: r.get(2),
            case_id: r.get(3),
        })
        .collect())
}

async fn load_matrix_feature_row_indices(
    client: &Client,
    matrix_id: i64,
    feature_keys: &[String],
) -> Result<HashMap<String, i64>, String> {
    if feature_keys.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = client
        .query(
            "SELECT feature_key, row_index FROM core_omicmatrixfeature \
             WHERE matrix_id = $1 AND feature_key = ANY($2)",
            &[&matrix_id, &feature_keys],
        )
        .await
        .map_err(|e| format!("Query core_omicmatrixfeature falhou: {e}"))?;

    Ok(rows
        .iter()
        .map(|r| (r.get::<_, String>(0), r.get::<_, i32>(1) as i64))
        .collect())
}

struct ReadoutRow {
    node_id: i64,
    feature_key: String,
    regulon_sign: Option<i32>,
}

async fn load_readout_rows(
    client: &Client,
    pathway_id: i64,
    matrix_id: i64,
    rule: &str,
    mapping_version: &str,
) -> Result<Vec<ReadoutRow>, String> {
    let rows = client
        .query(
            "SELECT prf.node_id, prf.feature_key, prf.regulon_sign \
             FROM core_pathwayreadoutfeature prf \
             JOIN core_pathwaynode pn ON pn.id = prf.node_id \
             WHERE pn.pathway_id = $1 AND prf.matrix_id = $2 AND prf.rule = $3 \
             AND prf.mapping_version = $4",
            &[&pathway_id, &matrix_id, &rule, &mapping_version],
        )
        .await
        .map_err(|e| format!("Query core_pathwayreadoutfeature falhou: {e}"))?;

    Ok(rows
        .iter()
        .map(|r| ReadoutRow {
            node_id: r.get(0),
            feature_key: r.get::<_, String>(1).to_uppercase(),
            regulon_sign: r.get::<_, Option<i16>>(2).map(|v| v as i32),
        })
        .collect())
}

/// Lê SÓ as linhas de `needed_rows` do Parquet (col 0 = `feature`, ignorada
/// aqui — só o eixo de amostras importa). Retorna
/// `row_index -> vetor alinhado por column_index (0-based Parquet)`.
fn load_matrix_rows(
    parquet_path: &str,
    needed_rows: &HashSet<i64>,
) -> Result<HashMap<i64, Vec<Option<f32>>>, String> {
    if needed_rows.is_empty() {
        return Ok(HashMap::new());
    }

    let path = Path::new(parquet_path);
    let file = std::fs::File::open(path).map_err(|e| format!("Falha ao abrir Parquet {:?}: {}", path, e))?;

    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| format!("Falha ao inicializar leitor Parquet: {}", e))?;

    let schema = builder.schema().clone();
    if schema.fields().is_empty() {
        return Err(format!("Parquet sem colunas: {:?}", path));
    }
    if schema.field(0).name() != "feature" {
        return Err(format!(
            "Coluna 0 do Parquet é '{}'; esperava 'feature'.",
            schema.field(0).name()
        ));
    }
    let n_cols = schema.fields().len();

    let reader = builder
        .with_batch_size(1024)
        .build()
        .map_err(|e| format!("Falha ao construir leitor Parquet: {}", e))?;

    let mut result: HashMap<i64, Vec<Option<f32>>> = HashMap::with_capacity(needed_rows.len());
    let mut row_counter: i64 = 0;

    for batch_result in reader {
        let batch = batch_result.map_err(|e| format!("Falha ao ler RecordBatch do Parquet: {}", e))?;
        let n_rows = batch.num_rows();

        for local_row in 0..n_rows {
            let global_row = row_counter + local_row as i64;
            if !needed_rows.contains(&global_row) {
                continue;
            }
            let mut vals: Vec<Option<f32>> = Vec::with_capacity(n_cols);
            vals.push(None); // índice 0 = coluna 'feature', não usado
            for col in 1..n_cols {
                let arr = batch
                    .column(col)
                    .as_any()
                    .downcast_ref::<Float32Array>()
                    .ok_or_else(|| format!("Coluna {} do Parquet não é Float32Array", schema.field(col).name()))?;
                vals.push(if arr.is_null(local_row) { None } else { Some(arr.value(local_row)) });
            }
            result.insert(global_row, vals);
        }

        row_counter += n_rows as i64;
    }

    Ok(result)
}

/// Cohort statistics para as features de UMA matriz: sd amostral do `Δ =
/// tumor − normal` por feature, e o `Δ` cru por `(row_index, tumor_sample_id)`
/// — pronto para escalar por `scale_delta` (D6).
struct CohortStats {
    sd_by_row: HashMap<i64, f64>,
    delta_by_row_and_tumor: HashMap<i64, HashMap<i64, f64>>,
}

fn compute_cohort_stats(
    row_data: &HashMap<i64, Vec<Option<f32>>>,
    sample_columns: &HashMap<i64, i32>,
    pairs: &[PairRow],
) -> CohortStats {
    let mut sd_by_row = HashMap::new();
    let mut delta_by_row_and_tumor: HashMap<i64, HashMap<i64, f64>> = HashMap::new();

    for (&row_idx, vals) in row_data {
        let mut deltas: Vec<f64> = Vec::with_capacity(pairs.len());
        let mut per_tumor: HashMap<i64, f64> = HashMap::new();

        for pair in pairs {
            let tumor_col = match sample_columns.get(&pair.tumor_sample_id) {
                Some(&c) => c as usize,
                None => continue,
            };
            let normal_col = match sample_columns.get(&pair.normal_sample_id) {
                Some(&c) => c as usize,
                None => continue,
            };
            let tumor_val = vals.get(tumor_col).copied().flatten();
            let normal_val = vals.get(normal_col).copied().flatten();
            if let (Some(t), Some(n)) = (tumor_val, normal_val) {
                let delta = t as f64 - n as f64;
                deltas.push(delta);
                per_tumor.insert(pair.tumor_sample_id, delta);
            }
        }

        sd_by_row.insert(row_idx, sample_std(&deltas));
        delta_by_row_and_tumor.insert(row_idx, per_tumor);
    }

    CohortStats { sd_by_row, delta_by_row_and_tumor }
}

// =============================================================================
// §9 — COPY UPSERT em `core_pathwayactivityscore`
// =============================================================================

struct ScoreRow {
    pathway_id: i64,
    sample_id: i64,
    pairing_id: Option<i64>,
    score: f64,
    null_mean: f64,
    null_sd: f64,
    z_score: f64,
    p_empirical: f64,
    n_permutations: i32,
    n_seeds_on_graph: i32,
    n_seeds_off_graph: i32,
    n_readouts_mapped: i32,
    n_readouts_with_value: i32,
    n_readouts_missing: i32,
    n_readouts_phospho: i32,
    n_readouts_tf: i32,
    method_version: String,
}

#[inline]
fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

fn build_score_csv(rows: &[ScoreRow]) -> String {
    let mut csv = String::with_capacity(rows.len() * 160);
    for r in rows {
        let pairing_field = match r.pairing_id {
            Some(v) => v.to_string(),
            None => "NULL".to_string(),
        };
        csv.push_str(&format!(
            "{},{},{},{:.10},{:.10},{:.10},{:.10},{:.10},{},{},{},{},{},{},{},{},{}\n",
            r.pathway_id,
            r.sample_id,
            pairing_field,
            r.score,
            r.null_mean,
            r.null_sd,
            r.z_score,
            r.p_empirical,
            r.n_permutations,
            r.n_seeds_on_graph,
            r.n_seeds_off_graph,
            r.n_readouts_mapped,
            r.n_readouts_with_value,
            r.n_readouts_missing,
            r.n_readouts_phospho,
            r.n_readouts_tf,
            escape_csv_field(&r.method_version),
        ));
    }
    csv
}

/// Espelha o padrão de `cnv_seed_derivation.rs:500` — staging temp, `DROP
/// TABLE IF EXISTS` por chamada (SEM `ON COMMIT DROP`), `sink.finish()`.
///
/// Contrato de 17 colunas fechado pelo cartografo (ver cabeçalho do módulo).
async fn copy_upsert_scores(client: &Client, rows: &[ScoreRow]) -> Result<u64, String> {
    if rows.is_empty() {
        return Ok(0);
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_pfs_score", &[])
        .await
        .map_err(|e| format!("DROP staging falhou: {e}"))?;

    client
        .execute(
            "CREATE TEMP TABLE _staging_pfs_score (
                pathway_id            BIGINT,
                sample_id             BIGINT,
                pairing_id            BIGINT,
                score                 DOUBLE PRECISION,
                null_mean             DOUBLE PRECISION,
                null_sd               DOUBLE PRECISION,
                z_score               DOUBLE PRECISION,
                p_empirical           DOUBLE PRECISION,
                n_permutations        INTEGER,
                n_seeds_on_graph      INTEGER,
                n_seeds_off_graph     INTEGER,
                n_readouts_mapped     INTEGER,
                n_readouts_with_value INTEGER,
                n_readouts_missing    INTEGER,
                n_readouts_phospho    INTEGER,
                n_readouts_tf         INTEGER,
                method_version        VARCHAR(50)
            )",
            &[],
        )
        .await
        .map_err(|e| format!("CREATE TEMP TABLE falhou: {e}"))?;

    let csv = build_score_csv(rows);

    {
        use futures::SinkExt;
        use std::pin::pin;

        let sink = client
            .copy_in(
                "COPY _staging_pfs_score (
                    pathway_id, sample_id, pairing_id, score, null_mean, null_sd,
                    z_score, p_empirical, n_permutations, n_seeds_on_graph,
                    n_seeds_off_graph, n_readouts_mapped, n_readouts_with_value,
                    n_readouts_missing, n_readouts_phospho, n_readouts_tf,
                    method_version
                ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
            )
            .await
            .map_err(|e| format!("COPY IN falhou: {e}"))?;

        let mut sink = pin!(sink);
        sink.send(Bytes::from(csv.into_bytes()))
            .await
            .map_err(|e| format!("Envio do CSV falhou: {e}"))?;
        sink.finish().await.map_err(|e| format!("Finish COPY falhou: {e}"))?;
    }

    let affected = client
        .execute(
            "INSERT INTO core_pathwayactivityscore (
                pathway_id, sample_id, pairing_id, score, null_mean, null_sd,
                z_score, p_empirical, n_permutations, n_seeds_on_graph,
                n_seeds_off_graph, n_readouts_mapped, n_readouts_with_value,
                n_readouts_missing, n_readouts_phospho, n_readouts_tf,
                method_version, computed_at
            )
            SELECT
                pathway_id, sample_id, pairing_id, score, null_mean, null_sd,
                z_score, p_empirical, n_permutations, n_seeds_on_graph,
                n_seeds_off_graph, n_readouts_mapped, n_readouts_with_value,
                n_readouts_missing, n_readouts_phospho, n_readouts_tf,
                method_version, NOW()
            FROM _staging_pfs_score
            ON CONFLICT (pathway_id, sample_id, method_version)
            DO UPDATE SET
                pairing_id            = EXCLUDED.pairing_id,
                score                 = EXCLUDED.score,
                null_mean             = EXCLUDED.null_mean,
                null_sd               = EXCLUDED.null_sd,
                z_score               = EXCLUDED.z_score,
                p_empirical           = EXCLUDED.p_empirical,
                n_permutations        = EXCLUDED.n_permutations,
                n_seeds_on_graph      = EXCLUDED.n_seeds_on_graph,
                n_seeds_off_graph     = EXCLUDED.n_seeds_off_graph,
                n_readouts_mapped     = EXCLUDED.n_readouts_mapped,
                n_readouts_with_value = EXCLUDED.n_readouts_with_value,
                n_readouts_missing    = EXCLUDED.n_readouts_missing,
                n_readouts_phospho    = EXCLUDED.n_readouts_phospho,
                n_readouts_tf         = EXCLUDED.n_readouts_tf,
                computed_at           = NOW()",
            &[],
        )
        .await
        .map_err(|e| format!("INSERT UPSERT falhou: {e}"))?;

    client
        .execute("DROP TABLE IF EXISTS _staging_pfs_score", &[])
        .await
        .map_err(|e| format!("DROP staging final falhou: {e}"))?;

    Ok(affected)
}

// =============================================================================
// §10 — ORQUESTRAÇÃO (async) + entry point síncrono (PyO3)
// =============================================================================

/// Parâmetros de entrada de `run_pfs_scoring` — um único ponto de entrada
/// (D1 TRAVADO: "núcleo 100% Rust, órbita 100% Django").
pub struct PfsRunInput {
    pub pathway_kegg_ids: Vec<String>,
    pub phospho_parquet_path: String,
    pub proteome_parquet_path: String,
    pub phospho_matrix_id: i64,
    pub proteome_matrix_id: i64,
    pub seed_method_versions: Vec<String>,
    pub mapping_version: String,
    pub method_version: String,
    pub db_url: String,
    pub restart: f64,
    pub unsigned_weight: f64,
    pub n_permutations: u32,
    pub rng_seed: u64,
    pub min_regulon_targets: usize,
    pub dry_run: bool,
}

const RWR_MAX_ITER: usize = 100;
const RWR_TOL: f64 = 1e-8;

pub async fn run_pfs_scoring_async(input: PfsRunInput) -> Result<PfsRunManifest, String> {
    let t_total = Instant::now();
    let mut manifest = PfsRunManifest::default();

    let client = crate::db::connection::connect_db(&input.db_url)
        .await
        .map_err(|e| { eprintln!("[pfs_engine] connection error: {e}"); "DB connection failed".to_string() })?;

    // ── Carga (comum a todas as vias) ────────────────────────────────────
    let t_load = Instant::now();

    let seed_candidates = load_seed_candidates(&client, &input.seed_method_versions).await?;
    let (effective_seeds, n_masked) = resolve_seed_precedence(seed_candidates);
    manifest.n_seeds_total = effective_seeds.len() as u64;
    manifest.n_seeds_masked_by_neutral_v2 = n_masked;

    // sample_id -> Vec<(gene_symbol, signed_weight)>
    let mut seeds_by_sample: HashMap<i64, Vec<(String, f64)>> = HashMap::new();
    for ((sample_id, gene), eff) in &effective_seeds {
        seeds_by_sample.entry(*sample_id).or_default().push((gene.clone(), eff.signed_weight));
    }

    let phospho_sample_columns = load_matrix_sample_columns(&client, input.phospho_matrix_id).await?;
    let proteome_sample_columns = load_matrix_sample_columns(&client, input.proteome_matrix_id).await?;

    let phospho_pairs = load_validated_pairs(&client, input.phospho_matrix_id).await?;
    let proteome_pairs = load_validated_pairs(&client, input.proteome_matrix_id).await?;

    let phospho_paired_case_ids: HashSet<i64> = phospho_pairs.iter().map(|p| p.tumor_sample_id).collect();

    // n_tumor_unpaired_skipped: amostras tumor da matriz de proteoma sem par validado.
    let proteome_tumor_rows = client
        .query(
            "SELECT sample_id FROM core_omicmatrixsample WHERE matrix_id = $1 AND sample_role = 'tumor'",
            &[&input.proteome_matrix_id],
        )
        .await
        .map_err(|e| format!("Query core_omicmatrixsample (tumor) falhou: {e}"))?;
    let proteome_tumor_ids: HashSet<i64> = proteome_tumor_rows.iter().map(|r| r.get::<_, i64>(0)).collect();
    let paired_tumor_ids: HashSet<i64> = proteome_pairs.iter().map(|p| p.tumor_sample_id).collect();
    manifest.n_tumor_unpaired_skipped = proteome_tumor_ids.difference(&paired_tumor_ids).count() as u64;

    manifest.elapsed_ms_load = t_load.elapsed().as_millis() as u64;

    let mut all_rows: Vec<ScoreRow> = Vec::new();
    let mut cases_scored: HashSet<i64> = HashSet::new();
    let mut t_graph_precompute_total: u128 = 0;
    let mut t_readout_permutation_total: u128 = 0;

    for kegg_id in &input.pathway_kegg_ids {
        let pathway_id = match load_pathway_id(&client, kegg_id).await? {
            Some(id) => id,
            None => {
                return Err(format!(
                    "Via '{}' não encontrada em core_pathway — rode load_pathway_topology antes.",
                    kegg_id
                ));
            }
        };
        manifest.n_pathways += 1;

        let t_precompute = Instant::now();
        let graph = load_graph(&client, pathway_id, input.unsigned_weight).await?;
        manifest.n_signed_edges += graph.n_signed;
        manifest.n_unsigned_edges += graph.n_unsigned;

        // Readouts: fosfo (Regra 1) na matriz de fosfo; TF (Regra 2) na de proteoma.
        let phospho_readouts =
            load_readout_rows(&client, pathway_id, input.phospho_matrix_id, "phospho", &input.mapping_version)
                .await?;
        let tf_readouts =
            load_readout_rows(&client, pathway_id, input.proteome_matrix_id, "tf_target", &input.mapping_version)
                .await?;

        // `n_readouts_mapped_pathway` é constante para TODAS as linhas de escore
        // desta via (mesma via, mesma `mapping_version` → mesmas linhas de
        // `PathwayReadoutFeature`). NÃO some no manifesto aqui (seria uma
        // granularidade "uma vez por via" misturada com `with_value`/`missing`,
        // que são somados "uma vez por linha de escore" — quebra a identidade
        // `mapped == with_value + missing` e a razão de cobertura no manifesto,
        // mesmo com o dado gravado por linha em `core_pathwayactivityscore`
        // correto). O acúmulo correto acontece por linha de escore emitida,
        // junto de `with_value`/`missing`/`phospho`/`tf` (ver loop de casos).
        let n_readouts_mapped_pathway = (phospho_readouts.len() + tf_readouts.len()) as u64;

        // node_id -> local idx (só nós mantidos; ausência = nó excluído, não deveria ocorrer)
        let phospho_targets: Vec<(usize, String)> = phospho_readouts
            .iter()
            .filter_map(|r| graph.id_to_local.get(&r.node_id).map(|&idx| (idx, r.feature_key.clone())))
            .collect();

        // TF node -> lista de (feature_key, regulon_sign)
        let mut tf_targets_by_node: HashMap<usize, Vec<(String, i32)>> = HashMap::new();
        for r in &tf_readouts {
            if let Some(&idx) = graph.id_to_local.get(&r.node_id) {
                let sign = r.regulon_sign.unwrap_or(0);
                tf_targets_by_node.entry(idx).or_default().push((r.feature_key.clone(), sign));
            }
        }

        // Resolver feature_key -> row_index em cada matriz.
        let phospho_keys: Vec<String> = phospho_targets.iter().map(|(_, k)| k.clone()).collect();
        let mut tf_keys_set: HashSet<String> = HashSet::new();
        for targets in tf_targets_by_node.values() {
            for (k, _) in targets {
                tf_keys_set.insert(k.clone());
            }
        }
        let tf_keys: Vec<String> = tf_keys_set.into_iter().collect();

        let phospho_row_index_map =
            load_matrix_feature_row_indices(&client, input.phospho_matrix_id, &phospho_keys).await?;
        let proteome_row_index_map =
            load_matrix_feature_row_indices(&client, input.proteome_matrix_id, &tf_keys).await?;

        if phospho_row_index_map.is_empty() && proteome_row_index_map.is_empty() {
            // Via sem NENHUM readout utilizável (D9) — nenhuma linha de escore é
            // emitida para esta via, então NADA é somado ao manifesto aqui: os
            // agregados de readout são somas sobre as linhas de escore
            // efetivamente escritas (ver comentário acima), e esta via não
            // contribui nenhuma.
            manifest.n_pathways_no_readout += 1;
            t_graph_precompute_total += t_precompute.elapsed().as_millis();
            continue;
        }

        // Carregar linhas necessárias + estatísticas de coorte.
        let phospho_row_indices: HashSet<i64> = phospho_row_index_map.values().copied().collect();
        let proteome_row_indices: HashSet<i64> = proteome_row_index_map.values().copied().collect();

        let phospho_row_data = load_matrix_rows(&input.phospho_parquet_path, &phospho_row_indices)?;
        let proteome_row_data = load_matrix_rows(&input.proteome_parquet_path, &proteome_row_indices)?;

        let phospho_cohort = compute_cohort_stats(&phospho_row_data, &phospho_sample_columns, &phospho_pairs);
        let proteome_cohort = compute_cohort_stats(&proteome_row_data, &proteome_sample_columns, &proteome_pairs);

        // Pré-computar influência F por nó semeável (linearidade — ~250 solves/via).
        let mut f_map: HashMap<usize, Vec<f64>> = HashMap::with_capacity(graph.semeable_idxs.len());
        for &k in &graph.semeable_idxs {
            let mut e = vec![0.0f64; graph.n_nodes];
            e[k] = 1.0;
            let influence = propagate_signed_rwr(&graph.csr, &e, input.restart, RWR_MAX_ITER, RWR_TOL);
            f_map.insert(k, influence);
        }

        let quartile_of = compute_quartiles(&graph.semeable_idxs, &graph.csr.out_degree_raw);
        let pools = build_quartile_pools(&graph.semeable_idxs, &quartile_of);

        t_graph_precompute_total += t_precompute.elapsed().as_millis();

        // ── Por caso (proteoma é o loop primário — D8/adendo) ────────────
        let t_readout = Instant::now();

        for pair in &proteome_pairs {
            let tumor_id = pair.tumor_sample_id;
            cases_scored.insert(tumor_id);

            let sample_seeds: Vec<(String, f64)> =
                seeds_by_sample.get(&tumor_id).cloned().unwrap_or_default();
            let (raw_seed_vec, n_on_graph, n_off_graph) =
                build_seed_vector(graph.n_nodes, &graph.gene_to_nodes, &sample_seeds);

            manifest.n_seeds_on_graph += n_on_graph;
            manifest.n_seeds_off_graph += n_off_graph;

            let total_l1: f64 = raw_seed_vec.iter().map(|v| v.abs()).sum();
            let seed_list: Vec<(usize, f64)> = raw_seed_vec
                .iter()
                .enumerate()
                .filter(|(_, &v)| v != 0.0)
                .map(|(idx, &v)| (idx, v))
                .collect();

            // ── Regra 1 (fosfo) ──────────────────────────────────────────
            let mut phospho_contribs: HashMap<usize, f64> = HashMap::new();
            let mut phospho_missing = 0i32;
            let mut phospho_with_value = 0i32;

            let has_phospho_pair = phospho_paired_case_ids.contains(&tumor_id);
            for (node_idx, feature_key) in &phospho_targets {
                let row_idx = match phospho_row_index_map.get(feature_key) {
                    Some(&r) => r,
                    None => {
                        phospho_missing += 1;
                        continue;
                    }
                };
                if !has_phospho_pair {
                    phospho_missing += 1;
                    continue;
                }
                let raw_delta = phospho_cohort
                    .delta_by_row_and_tumor
                    .get(&row_idx)
                    .and_then(|m| m.get(&tumor_id))
                    .copied();
                match raw_delta {
                    None => {
                        phospho_missing += 1;
                    }
                    Some(delta) => {
                        let sd = phospho_cohort.sd_by_row.get(&row_idx).copied().unwrap_or(0.0);
                        let scaled = scale_delta(delta, sd);
                        *phospho_contribs.entry(*node_idx).or_insert(0.0) += scaled;
                        phospho_with_value += 1;
                    }
                }
            }
            let n_phospho_nodes = phospho_contribs.len() as i32;

            // ── Regra 2 (ULM assinado) ───────────────────────────────────
            let mut tf_contribs: HashMap<usize, f64> = HashMap::new();
            let mut tf_missing = 0i32;
            let mut tf_with_value = 0i32;
            let mut n_tf_below_min = 0u64;

            for (node_idx, targets) in &tf_targets_by_node {
                let mut ulm_pairs: Vec<(f64, f64)> = Vec::with_capacity(targets.len());
                let mut n_row_missing_here = 0i32;
                let mut n_row_with_value_here = 0i32;

                for (feature_key, regulon_sign) in targets {
                    let row_idx = match proteome_row_index_map.get(feature_key) {
                        Some(&r) => r,
                        None => {
                            n_row_missing_here += 1;
                            continue;
                        }
                    };
                    let raw_delta = proteome_cohort
                        .delta_by_row_and_tumor
                        .get(&row_idx)
                        .and_then(|m| m.get(&tumor_id))
                        .copied();
                    match raw_delta {
                        None => {
                            n_row_missing_here += 1;
                        }
                        Some(delta) => {
                            let sd = proteome_cohort.sd_by_row.get(&row_idx).copied().unwrap_or(0.0);
                            let scaled = scale_delta(delta, sd);
                            ulm_pairs.push((*regulon_sign as f64, scaled));
                            n_row_with_value_here += 1;
                        }
                    }
                }

                tf_missing += n_row_missing_here;
                tf_with_value += n_row_with_value_here;

                if ulm_pairs.len() < input.min_regulon_targets {
                    n_tf_below_min += 1;
                    continue;
                }
                match ulm_tf_activity(&ulm_pairs) {
                    Some(t_stat) => {
                        tf_contribs.insert(*node_idx, t_stat);
                    }
                    None => {
                        n_tf_below_min += 1;
                    }
                }
            }
            let n_tf_nodes = tf_contribs.len() as i32;
            manifest.n_tf_below_min_targets += n_tf_below_min;

            let n_readouts_with_value_row = phospho_with_value + tf_with_value;
            let n_readouts_missing_row = phospho_missing + tf_missing;

            // Mesma granularidade para os TRÊS agregados: soma por LINHA de
            // escore emitida (não por via). Preserva, no manifesto, a mesma
            // identidade que vale em cada linha de `core_pathwayactivityscore`:
            // `n_readouts_mapped == n_readouts_with_value + n_readouts_missing`.
            manifest.n_readouts_mapped += n_readouts_mapped_pathway;
            manifest.n_readouts_with_value += n_readouts_with_value_row as u64;
            manifest.n_readouts_missing += n_readouts_missing_row as u64;
            // `n_readouts_phospho`/`n_readouts_tf` contam NÓS (não linhas de
            // PathwayReadoutFeature/pares TF-alvo) que efetivamente pontuaram —
            // mesma semântica de `PathwayActivityScore.n_readouts_phospho`/
            // `.n_readouts_tf` (ver help_text do model). Um TF concentra dezenas
            // de alvos por nó, então `n_readouts_tf` diverge bastante da
            // contagem de linhas mapeadas para tf_target — não somar
            // `n_readouts_phospho + n_readouts_tf` esperando bater com
            // `n_readouts_mapped` (granularidades diferentes por desenho).
            manifest.n_readouts_phospho += n_phospho_nodes as u64;
            manifest.n_readouts_tf += n_tf_nodes as u64;

            // ── y vector combinado (D6: média por regra, não soma) ───────
            let mut y = vec![0.0f64; graph.n_nodes];
            if n_phospho_nodes > 0 {
                for (&node, &val) in &phospho_contribs {
                    y[node] += val / n_phospho_nodes as f64;
                }
            }
            if n_tf_nodes > 0 {
                for (&node, &val) in &tf_contribs {
                    y[node] += val / n_tf_nodes as f64;
                }
            }

            // ρ = Fᵀ·y — uma projeção por (via, caso); permutação vira produto escalar.
            let mut rho: HashMap<usize, f64> = HashMap::with_capacity(graph.semeable_idxs.len());
            for &k in &graph.semeable_idxs {
                let influence = &f_map[&k];
                let dot: f64 = y.iter().zip(influence.iter()).map(|(a, b)| a * b).sum();
                rho.insert(k, dot);
            }

            let null_result = compute_null_and_score(
                &seed_list,
                total_l1,
                &rho,
                &quartile_of,
                &pools,
                input.n_permutations,
                input.rng_seed,
                pathway_id,
                tumor_id,
            );

            let (z_score, null_sd) = if null_result.null_sd == 0.0 {
                manifest.n_zero_sd_null += 1;
                (0.0, 0.0)
            } else {
                ((null_result.s_obs - null_result.null_mean) / null_result.null_sd, null_result.null_sd)
            };

            // Defesa extra contra NaN/Infinity atravessando o COPY (nunca
            // deveria ocorrer dado o desenho acima — guarda explícita).
            let score = if null_result.s_obs.is_finite() { null_result.s_obs } else { 0.0 };
            let null_mean = if null_result.null_mean.is_finite() { null_result.null_mean } else { 0.0 };
            let z_score = if z_score.is_finite() { z_score } else { 0.0 };
            let p_empirical = if null_result.p_empirical.is_finite() { null_result.p_empirical } else { 1.0 };

            all_rows.push(ScoreRow {
                pathway_id,
                sample_id: tumor_id,
                pairing_id: Some(pair.pairing_id),
                score,
                null_mean,
                null_sd,
                z_score,
                p_empirical,
                n_permutations: null_result.permutations_run as i32,
                n_seeds_on_graph: n_on_graph as i32,
                n_seeds_off_graph: n_off_graph as i32,
                n_readouts_mapped: n_readouts_mapped_pathway as i32,
                n_readouts_with_value: n_readouts_with_value_row,
                n_readouts_missing: n_readouts_missing_row,
                n_readouts_phospho: n_phospho_nodes,
                n_readouts_tf: n_tf_nodes,
                method_version: input.method_version.clone(),
            });
        }

        t_readout_permutation_total += t_readout.elapsed().as_millis();
    }

    manifest.elapsed_ms_graph_precompute = t_graph_precompute_total as u64;
    manifest.elapsed_ms_readout_and_permutation = t_readout_permutation_total as u64;
    manifest.n_cases_scored = cases_scored.len() as u64;

    let t_copy = Instant::now();
    if input.dry_run {
        manifest.n_rows_written = 0;
    } else {
        manifest.n_rows_written = copy_upsert_scores(&client, &all_rows).await?;
    }
    manifest.elapsed_ms_copy = t_copy.elapsed().as_millis() as u64;

    manifest.elapsed_ms_total = t_total.elapsed().as_millis() as u64;

    Ok(manifest)
}

/// Wrapper síncrono para PyO3 — delega ao async via Tokio runtime. Único
/// ponto de entrada (D1 TRAVADO).
#[allow(clippy::too_many_arguments)]
pub fn run_pfs_scoring(
    pathway_kegg_ids: Vec<String>,
    phospho_parquet_path: String,
    proteome_parquet_path: String,
    phospho_matrix_id: i64,
    proteome_matrix_id: i64,
    seed_method_versions: Vec<String>,
    mapping_version: String,
    method_version: String,
    db_url: String,
    restart: f64,
    unsigned_weight: f64,
    n_permutations: u32,
    rng_seed: u64,
    min_regulon_targets: usize,
    dry_run: bool,
) -> Result<PfsRunManifest, String> {
    let rt = tokio::runtime::Runtime::new().map_err(|e| format!("Falha ao criar Tokio runtime: {e}"))?;
    rt.block_on(run_pfs_scoring_async(PfsRunInput {
        pathway_kegg_ids,
        phospho_parquet_path,
        proteome_parquet_path,
        phospho_matrix_id,
        proteome_matrix_id,
        seed_method_versions,
        mapping_version,
        method_version,
        db_url,
        restart,
        unsigned_weight,
        n_permutations,
        rng_seed,
        min_regulon_targets,
        dry_run,
    }))
}

// =============================================================================
// §11 — TESTES UNITÁRIOS (#[cfg(test)]) — SEM rede/DB
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    // ── PRNG counter-based ────────────────────────────────────────────────

    #[test]
    fn test_derive_permutation_seed_e_deterministico() {
        let a = derive_permutation_seed(42, 1, 100, 0);
        let b = derive_permutation_seed(42, 1, 100, 0);
        assert_eq!(a, b, "mesma entrada deve produzir a mesma semente");
    }

    #[test]
    fn test_derive_permutation_seed_varia_por_indice() {
        let s0 = derive_permutation_seed(42, 1, 100, 0);
        let s1 = derive_permutation_seed(42, 1, 100, 1);
        assert_ne!(s0, s1, "índices de permutação diferentes devem gerar sementes diferentes");
    }

    #[test]
    fn test_derive_permutation_seed_varia_por_pathway_e_sample() {
        let base = derive_permutation_seed(42, 1, 100, 0);
        let other_pathway = derive_permutation_seed(42, 2, 100, 0);
        let other_sample = derive_permutation_seed(42, 1, 200, 0);
        assert_ne!(base, other_pathway);
        assert_ne!(base, other_sample);
    }

    #[test]
    fn test_xorshift_gen_range_dentro_do_intervalo() {
        let mut rng = Xorshift64Star::new(12345);
        for _ in 0..1000 {
            let v = rng.gen_range(7);
            assert!(v < 7);
        }
    }

    // ── RWR assinado: propagação de sinal ──────────────────────────────────

    #[test]
    fn test_rwr_propaga_inibicao_como_negativo() {
        // Cadeia 0 -> 1, sign = -1.
        let (csr, n_signed, n_unsigned) = build_csr(2, &[(0, 1, -1)], 0.3);
        assert_eq!(n_signed, 1);
        assert_eq!(n_unsigned, 0);

        let seeds = vec![1.0, 0.0];
        let p = propagate_signed_rwr(&csr, &seeds, 0.3, 100, 1e-10);

        assert!(p[0] > 0.0, "nó semente deve manter influência positiva");
        assert!(p[1] < 0.0, "inibição deve propagar influência NEGATIVA a jusante");
    }

    #[test]
    fn test_rwr_propaga_ativacao_como_positivo() {
        let (csr, _, _) = build_csr(2, &[(0, 1, 1)], 0.3);
        let seeds = vec![1.0, 0.0];
        let p = propagate_signed_rwr(&csr, &seeds, 0.3, 100, 1e-10);

        assert!(p[1] > 0.0, "ativação deve propagar influência POSITIVA a jusante");
    }

    #[test]
    fn test_rwr_converge_em_menos_de_100_iteracoes() {
        // Grafo em cadeia de 5 nós, sinais alternados.
        let edges = vec![(0, 1, 1), (1, 2, -1), (2, 3, 1), (3, 4, -1)];
        let (csr, _, _) = build_csr(5, &edges, 0.3);
        let seeds = vec![1.0, 0.0, 0.0, 0.0, 0.0];
        // max_iter alto o suficiente; o teste em si valida que o resultado é finito e estável.
        let p1 = propagate_signed_rwr(&csr, &seeds, 0.3, 100, 1e-8);
        let p2 = propagate_signed_rwr(&csr, &seeds, 0.3, 200, 1e-8);
        for i in 0..5 {
            assert!(p1[i].is_finite());
            assert!((p1[i] - p2[i]).abs() < 1e-6, "deve convergir bem antes de 100 iterações");
        }
    }

    #[test]
    fn test_rwr_no_dangling_nao_vaza_massa() {
        // Nó 1 não tem out-edges (dangling) — deve reter massa via auto-laço.
        let edges = vec![(0, 1, 1)];
        let (csr, _, _) = build_csr(2, &edges, 0.3);
        assert_eq!(csr.out_edges[1], vec![(1, 1.0)], "nó dangling deve ter auto-laço de peso 1.0");

        let seeds = vec![1.0, 0.0];
        let p = propagate_signed_rwr(&csr, &seeds, 0.3, 100, 1e-10);
        let total_mass: f64 = p.iter().map(|v| v.abs()).sum();
        assert!(total_mass > 0.0 && total_mass.is_finite());
    }

    // ── Invariante de linearidade (truque do ganho ~250×) ─────────────────

    #[test]
    fn test_linearidade_soma_de_influencias_igual_solve_direto() {
        // Grafo sintético com 6 nós e mistura de sinais.
        let edges = vec![
            (0, 2, 1),
            (1, 2, -1),
            (2, 3, 1),
            (2, 4, -1),
            (3, 5, 1),
            (4, 5, 1),
        ];
        let (csr, _, _) = build_csr(6, &edges, 0.3);

        // Sementes em posições 0 e 1, com pesos diferentes e sinais opostos.
        let mut combined = vec![0.0; 6];
        combined[0] = 3.0;
        combined[1] = -2.0;

        let direct = propagate_signed_rwr(&csr, &combined, 0.3, 500, 1e-14);

        let mut e0 = vec![0.0; 6];
        e0[0] = 1.0;
        let f0 = propagate_signed_rwr(&csr, &e0, 0.3, 500, 1e-14);

        let mut e1 = vec![0.0; 6];
        e1[1] = 1.0;
        let f1 = propagate_signed_rwr(&csr, &e1, 0.3, 500, 1e-14);

        for i in 0..6 {
            let linear_combo = 3.0 * f0[i] + (-2.0) * f1[i];
            assert!(
                (direct[i] - linear_combo).abs() < 1e-9,
                "nó {}: direto={} combinado={} (diff={})",
                i,
                direct[i],
                linear_combo,
                (direct[i] - linear_combo).abs()
            );
        }
    }

    // ── ULM assinado — contra cálculo manual ───────────────────────────────

    #[test]
    fn test_ulm_bate_com_calculo_manual() {
        // x = regulon_sign, y = Δx escalado. Ver derivação manual no
        // cabeçalho do módulo/comentário do teste.
        let pairs = vec![(1.0, 3.0), (-1.0, 1.0), (1.0, 5.0), (-1.0, -1.0)];
        let t = ulm_tf_activity(&pairs).expect("regressão bem-posta deve retornar Some");
        let expected = 2.0 * std::f64::consts::SQRT_2; // ≈ 2.828427125
        assert!((t - expected).abs() < 1e-6, "t calculado={}, esperado={}", t, expected);
    }

    #[test]
    fn test_ulm_sem_variancia_em_x_retorna_none() {
        // Todos os alvos com o MESMO regulon_sign — inclinação indeterminada.
        let pairs = vec![(1.0, 3.0), (1.0, 1.0), (1.0, 5.0), (1.0, -1.0)];
        assert!(ulm_tf_activity(&pairs).is_none());
    }

    #[test]
    fn test_ulm_poucos_alvos_retorna_none() {
        let pairs = vec![(1.0, 3.0), (-1.0, 1.0)];
        assert!(ulm_tf_activity(&pairs).is_none(), "df=n-2 precisa ser > 0");
    }

    #[test]
    fn test_ulm_ajuste_perfeito_nao_produz_infinito() {
        // Sem ruído nenhum → resíduo zero → mse=0 → deve retornar None,
        // NUNCA Some(inf)/Some(NaN).
        let pairs = vec![(1.0, 2.0), (-1.0, -2.0), (1.0, 2.0)];
        let result = ulm_tf_activity(&pairs);
        if let Some(t) = result {
            assert!(t.is_finite(), "jamais retornar t não-finito");
        }
    }

    // ── Precedência de seed (D3) ────────────────────────────────────────

    fn candidate(sample: i64, gene: &str, dir: &str, mv: &str) -> SeedCandidate {
        SeedCandidate {
            sample_id: sample,
            gene_symbol: gene.to_string(),
            direction: dir.to_string(),
            magnitude: 0.8,
            confidence: 1.0,
            method_version: mv.to_string(),
        }
    }

    #[test]
    fn test_precedencia_cnv_v2_vence_snv_v1() {
        let candidates = vec![
            candidate(1, "TP53", "inactivator", "fase2-cnv-v2"),
            candidate(1, "TP53", "activator", "fase2-snv-v1"),
        ];
        let (result, n_masked) = resolve_seed_precedence(candidates);
        let seed = result.get(&(1, "TP53".to_string())).unwrap();
        assert!(seed.signed_weight < 0.0, "cnv-v2 (inactivator) deve vencer sobre snv-v1");
        assert_eq!(n_masked, 0);
    }

    #[test]
    fn test_precedencia_neutral_v2_mascara_snv_e_e_contado() {
        let candidates = vec![
            candidate(1, "TP53", "neutral", "fase2-cnv-v2"),
            candidate(1, "TP53", "activator", "fase2-snv-v1"),
        ];
        let (result, n_masked) = resolve_seed_precedence(candidates);
        assert!(result.get(&(1, "TP53".to_string())).is_none(), "neutral não deve virar seed efetiva");
        assert_eq!(n_masked, 1, "deve contar o mascaramento");
    }

    #[test]
    fn test_precedencia_neutral_sem_alternativa_nao_conta_mascaramento() {
        let candidates = vec![candidate(1, "TP53", "neutral", "fase2-cnv-v2")];
        let (result, n_masked) = resolve_seed_precedence(candidates);
        assert!(result.is_empty());
        assert_eq!(n_masked, 0, "sem alternativa real, não é 'mascaramento'");
    }

    #[test]
    fn test_precedencia_cnv_v1_usado_quando_nao_ha_v2_nem_snv() {
        let candidates = vec![candidate(1, "VHL", "inactivator", "fase2-cnv-v1")];
        let (result, _) = resolve_seed_precedence(candidates);
        assert!(result.contains_key(&(1, "VHL".to_string())));
    }

    // ── Mapeamento seed → nó (D4) ───────────────────────────────────────

    #[test]
    fn test_build_seed_vector_divide_peso_entre_homonimos() {
        let mut gene_to_nodes = HashMap::new();
        gene_to_nodes.insert("TP53".to_string(), vec![0usize, 2usize]);

        let sample_seeds = vec![("TP53".to_string(), 1.0)];
        let (raw, n_on, n_off) = build_seed_vector(4, &gene_to_nodes, &sample_seeds);

        assert_eq!(n_on, 1);
        assert_eq!(n_off, 0);
        assert!((raw[0] - 0.5).abs() < 1e-9);
        assert!((raw[2] - 0.5).abs() < 1e-9);
        assert_eq!(raw[1], 0.0);
        assert_eq!(raw[3], 0.0);
    }

    #[test]
    fn test_build_seed_vector_gene_sem_no_e_contado_off_graph() {
        let gene_to_nodes: HashMap<String, Vec<usize>> = HashMap::new();
        let sample_seeds = vec![("MISSING".to_string(), 1.0)];
        let (raw, n_on, n_off) = build_seed_vector(3, &gene_to_nodes, &sample_seeds);

        assert_eq!(n_on, 0);
        assert_eq!(n_off, 1);
        assert!(raw.iter().all(|&v| v == 0.0));
    }

    // ── Permutação: reprodutibilidade, ordem, estratificação ──────────────

    fn toy_rho() -> HashMap<usize, f64> {
        let mut rho = HashMap::new();
        for i in 0..8 {
            rho.insert(i, (i as f64) * 0.1 - 0.3);
        }
        rho
    }

    #[test]
    fn test_permutacao_reproducivel_com_mesma_seed() {
        let seed_list = vec![(0usize, 1.0), (1usize, -0.5)];
        let out_degree = vec![2usize, 2, 1, 1, 5, 5, 5, 5];
        let semeable: Vec<usize> = (0..8).collect();
        let quartile_of = compute_quartiles(&semeable, &out_degree);
        let pools = build_quartile_pools(&semeable, &quartile_of);
        let rho = toy_rho();
        let total_l1 = 1.5;

        let r1 = compute_null_and_score(&seed_list, total_l1, &rho, &quartile_of, &pools, 200, 999, 1, 42);
        let r2 = compute_null_and_score(&seed_list, total_l1, &rho, &quartile_of, &pools, 200, 999, 1, 42);

        assert_eq!(r1.null_mean, r2.null_mean);
        assert_eq!(r1.null_sd, r2.null_sd);
        assert_eq!(r1.p_empirical, r2.p_empirical);
    }

    #[test]
    fn test_permutacao_independente_de_ordem_da_lista_original() {
        let seed_list_a = vec![(0usize, 1.0), (1usize, -0.5), (2usize, 0.3)];
        let seed_list_b = vec![(2usize, 0.3), (0usize, 1.0), (1usize, -0.5)]; // mesma coisa, outra ordem

        let out_degree = vec![2usize, 2, 1, 1, 5, 5, 5, 5];
        let semeable: Vec<usize> = (0..8).collect();
        let quartile_of = compute_quartiles(&semeable, &out_degree);
        let pools = build_quartile_pools(&semeable, &quartile_of);
        let rho = toy_rho();
        let total_l1 = 1.8;

        let ra = compute_null_and_score(&seed_list_a, total_l1, &rho, &quartile_of, &pools, 300, 7, 5, 9);
        let rb = compute_null_and_score(&seed_list_b, total_l1, &rho, &quartile_of, &pools, 300, 7, 5, 9);

        assert_eq!(ra.s_obs, rb.s_obs);
        assert_eq!(ra.null_mean, rb.null_mean);
        assert_eq!(ra.null_sd, rb.null_sd);
        assert_eq!(ra.p_empirical, rb.p_empirical);
    }

    #[test]
    fn test_estratificacao_respeita_quartil() {
        // 8 nós com out-degree crescente — quartis bem definidos.
        let out_degree = vec![0usize, 0, 1, 1, 5, 5, 9, 9];
        let semeable: Vec<usize> = (0..8).collect();
        let quartile_of = compute_quartiles(&semeable, &out_degree);
        let pools = build_quartile_pools(&semeable, &quartile_of);

        // Nó 0 (menor grau) deve estar no quartil 0; nó 7 (maior grau) no quartil 3.
        let q0 = quartile_of[&0];
        let q7 = quartile_of[&7];
        assert_eq!(q0, 0);
        assert_eq!(q7, 3);

        // Permutação de uma semente no quartil de nó 0 só pode cair em nós do mesmo quartil.
        let mut rng = Xorshift64Star::new(555);
        for _ in 0..50 {
            let perm = permute_seed_positions(&[(0, 1.0)], &quartile_of, &pools, &mut rng);
            let (new_node, _) = perm[0];
            assert_eq!(quartile_of[&new_node], q0, "sorteio deve respeitar o quartil de origem");
        }
    }

    // ── null_sd == 0 ⇒ z = 0 (nunca NaN) ────────────────────────────────

    #[test]
    fn test_null_sd_zero_quando_todas_permutacoes_dao_o_mesmo_score() {
        // rho constante para todo nó semeável → qualquer permutação dá o mesmo S.
        let mut rho = HashMap::new();
        for i in 0..8 {
            rho.insert(i, 0.42);
        }
        let out_degree = vec![1usize; 8];
        let semeable: Vec<usize> = (0..8).collect();
        let quartile_of = compute_quartiles(&semeable, &out_degree);
        let pools = build_quartile_pools(&semeable, &quartile_of);

        let seed_list = vec![(0usize, 1.0)];
        let result = compute_null_and_score(&seed_list, 1.0, &rho, &quartile_of, &pools, 100, 1, 1, 1);

        assert_eq!(result.null_sd, 0.0);
        let z = if result.null_sd == 0.0 { 0.0 } else { (result.s_obs - result.null_mean) / result.null_sd };
        assert_eq!(z, 0.0);
        assert!(z.is_finite());
    }

    // ── Zero sementes ⇒ degenerado, sem crash/NaN ──────────────────────

    #[test]
    fn test_zero_sementes_produz_resultado_degenerado_sem_nan() {
        let rho = toy_rho();
        let quartile_of = HashMap::new();
        let pools = HashMap::new();
        let seed_list: Vec<(usize, f64)> = Vec::new();

        let result = compute_null_and_score(&seed_list, 0.0, &rho, &quartile_of, &pools, 1000, 1, 1, 1);

        assert_eq!(result.s_obs, 0.0);
        assert_eq!(result.null_mean, 0.0);
        assert_eq!(result.null_sd, 0.0);
        assert_eq!(result.permutations_run, 0);
        assert!(result.p_empirical.is_finite());
    }

    #[test]
    fn test_score_from_positions_total_l1_zero_nao_divide_por_zero() {
        let rho = toy_rho();
        let seed_list = vec![(0usize, 1.0)];
        let s = score_from_positions(&seed_list, &rho, 0.0);
        assert_eq!(s, 0.0);
        assert!(s.is_finite());
    }

    // ── Readout ausente: ignorado (função scale_delta / sd) ────────────

    #[test]
    fn test_sample_std_com_menos_de_dois_valores_e_zero() {
        assert_eq!(sample_std(&[]), 0.0);
        assert_eq!(sample_std(&[1.0]), 0.0);
    }

    #[test]
    fn test_scale_delta_com_sd_zero_usa_divisor_um() {
        assert_eq!(scale_delta(2.5, 0.0), 2.5);
    }

    #[test]
    fn test_scale_delta_normal() {
        assert!((scale_delta(2.0, 4.0) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn test_compute_cohort_stats_ignora_pares_com_valor_ausente() {
        // 3 pares; um deles tem valor tumor ausente (None) — deve ser IGNORADO
        // do cálculo de sd, não tratado como zero.
        let mut row_data = HashMap::new();
        // col0=feature(ignorada), col1=tumor par1, col2=normal par1,
        // col3=tumor par2 (ausente), col4=normal par2, col5=tumor par3, col6=normal par3
        row_data.insert(10i64, vec![None, Some(5.0f32), Some(2.0f32), None, Some(1.0f32), Some(6.0f32), Some(3.0f32)]);

        let mut sample_columns = HashMap::new();
        sample_columns.insert(100i64, 1); // tumor par1
        sample_columns.insert(101i64, 2); // normal par1
        sample_columns.insert(102i64, 3); // tumor par2 (dado ausente)
        sample_columns.insert(103i64, 4); // normal par2
        sample_columns.insert(104i64, 5); // tumor par3
        sample_columns.insert(105i64, 6); // normal par3

        let pairs = vec![
            PairRow { pairing_id: 1, tumor_sample_id: 100, normal_sample_id: 101, case_id: "C1".to_string() },
            PairRow { pairing_id: 2, tumor_sample_id: 102, normal_sample_id: 103, case_id: "C2".to_string() },
            PairRow { pairing_id: 3, tumor_sample_id: 104, normal_sample_id: 105, case_id: "C3".to_string() },
        ];

        let stats = compute_cohort_stats(&row_data, &sample_columns, &pairs);

        let deltas_for_row = stats.delta_by_row_and_tumor.get(&10).unwrap();
        // Par2 (tumor ausente) não deve aparecer.
        assert!(!deltas_for_row.contains_key(&102));
        assert!(deltas_for_row.contains_key(&100));
        assert!(deltas_for_row.contains_key(&104));
        assert_eq!(deltas_for_row.len(), 2);

        // sd calculada só sobre os 2 deltas presentes: (5-2)=3, (6-3)=3 → sd=0 (idênticos)
        let sd = stats.sd_by_row.get(&10).copied().unwrap();
        assert_eq!(sd, 0.0);
    }

    // ── CSR: nós `map` já vêm excluídos pelo carregamento (join defensivo) ─

    #[test]
    fn test_build_csr_conta_arestas_assinadas_e_nao_assinadas() {
        let edges = vec![(0, 1, 1), (1, 2, -1), (2, 0, 0)];
        let (csr, n_signed, n_unsigned) = build_csr(3, &edges, 0.3);
        assert_eq!(n_signed, 2);
        assert_eq!(n_unsigned, 1);
        // Normalização: nó 2 tem só uma aresta (sign=0 → peso 0.3) → normalizado para 1.0 abs.
        let (target, w) = csr.out_edges[2][0];
        assert_eq!(target, 0);
        assert!((w - 1.0).abs() < 1e-9, "aresta única deve normalizar para peso abs 1.0");
    }

    #[test]
    fn test_build_csr_normaliza_por_soma_absoluta() {
        // Nó 0 com duas out-edges: sign=+1 e sign=-1 → cada uma deve virar ±0.5.
        let edges = vec![(0, 1, 1), (0, 2, -1)];
        let (csr, _, _) = build_csr(3, &edges, 0.3);
        let mut weights: Vec<(usize, f64)> = csr.out_edges[0].clone();
        weights.sort_by_key(|(t, _)| *t);
        assert!((weights[0].1 - 0.5).abs() < 1e-9);
        assert!((weights[1].1 - (-0.5)).abs() < 1e-9);
    }
}
