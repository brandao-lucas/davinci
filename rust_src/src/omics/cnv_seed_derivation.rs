/// Derivação da seed CNV interpretada — Parquet do CNV + papel do gene → `VariantEffectSeed`.
///
/// # Fluxo
///
/// 1. Query `core_generole` (source='oncokb') → HashMap gene_symbol(UPPER) → role.
///    Genes com role `neither`/`unknown` são ignorados para fins de sinal
///    (nenhuma heurística de dosagem resolve sinal em genes sem papel).
/// 2. Query `core_omicmatrixsample` (matrix_id) → mapa column_index → (sample_id, case_id).
/// 3. Ler o Parquet CNV linha a linha (row-group stream):
///    - Coluna 0 = `feature` (gene symbol).
///    - Colunas 1..N = `T_<case_id>` (Float32, nullable) — log-ratio tumoral.
/// 4. Regra de sinal (dosagem × papel — heurística CNV v1):
///    - `role=tsg`   + cnv <= loss_threshold  → `inactivator`
///    - `role=oncogene` + cnv >= gain_threshold → `activator`
///    - `role=oncogene_and_tsg`               → ambíguo; pular (retornar `None`)
///    - sub-threshold / gene sem papel         → pular
///    - magnitude = |cnv_value| (log-ratio absoluto)
///    - confidence = 0.6 (heurística v1 — ver §DECISÕES)
/// 5. COPY UPSERT em `core_varianteffectseed`:
///    variant_key='', evidence_type='cnv', gene_role=role, effect_source='cnv_dosage_x_oncokb',
///    method_version, ON CONFLICT (matrix, sample, gene_symbol, variant_key,
///    evidence_type, method_version) DO UPDATE.  [natural key inclui method_version — migration 0034]
///
/// # DECISÕES tomadas (reportar ao usuário)
///
/// - **Guardar só activator/inactivator** (não neutro): a seed é insumo de perturbação do PFS;
///   neutro não é perturbação. Células sub-threshold / gene sem papel / oncogene_and_tsg
///   → puladas e contadas em `n_neutral_skipped`.
/// - **confidence = 0.6**: dosagem × papel é heurística v1 (não captura CNV×alelo);
///   valor fixo declarado na proveniência via `method_version`.
/// - **gain_threshold default = 0.3** (log-ratio positivo → amplificação moderada).
/// - **loss_threshold default = −0.3** (log-ratio negativo → deleção moderada).
/// - **oncogene_and_tsg → pular**: sinal ambíguo; não inventar direção; contado separado.
///
/// # Colunas Parquet
///
/// O Parquet gerado por `cnv_loader.rs` tem:
/// - Col 0: `feature` (gene symbol, sem prefixo).
/// - Colunas seguintes: `T_<case_id>` (Float32, nullable).
///
/// O mapeamento `case_id → column_index` usa a posição ordinal das colunas de amostra
/// (column_index 1-based no Parquet, 0-based em `OmicMatrixSample`).
/// O OmicMatrixSample armazena `column_index` (0-based Parquet) = 1 + i (i = posição da amostra
/// no vetor de amostras do manifesto). Este módulo converte para índice de coluna Arrow
/// (0-based no RecordBatch, excluindo a coluna `feature`).
///
/// # Segurança
///
/// `db_url` nunca logada.

use std::collections::HashMap;
use std::path::Path;

use bytes::Bytes;
use tokio_postgres::Client;

use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use arrow::array::{Array, Float32Array, StringArray};

// ─── Manifesto público ────────────────────────────────────────────────────────

/// Manifesto retornado por `seed_cnv_from_parquet`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_seeds_written` | `u64` | Linhas gravadas via COPY em core_varianteffectseed. |
/// | `n_activator` | `u64` | Seeds com direction='activator' (oncogene amplificado). |
/// | `n_inactivator` | `u64` | Seeds com direction='inactivator' (TSG deletado). |
/// | `n_neutral_skipped` | `u64` | Células puladas (ambíguo oncogene_and_tsg ou sub-threshold mas gene tem papel). |
/// | `n_genes_with_role` | `u64` | Genes encontrados no mapa GeneRole (oncogene+tsg+dual). |
/// | `n_genes_skipped_no_role` | `u64` | Genes sem role ou com neither/unknown → ignorados. |
/// | `n_cells_subthreshold` | `u64` | Células cujo |cnv_value| < threshold (abaixo dos dois thresholds). |
/// | `n_cells_null` | `u64` | Células com valor null/missing no Parquet. |
#[derive(Debug, Clone)]
pub struct CnvSeedManifest {
    pub n_seeds_written: u64,
    pub n_activator: u64,
    pub n_inactivator: u64,
    pub n_neutral_skipped: u64,
    pub n_genes_with_role: u64,
    pub n_genes_skipped_no_role: u64,
    pub n_cells_subthreshold: u64,
    pub n_cells_null: u64,
}

// ─── Regra de sinal pura (testável sem DB/Parquet) ───────────────────────────

/// Resolve a direção e magnitude de uma célula CNV com base no papel do gene.
///
/// Retorna `Some(("activator"|"inactivator", magnitude_absoluta))` para seeds relevantes,
/// `None` para células puladas (sub-threshold, ambíguas, sem papel).
///
/// # Argumentos
/// - `role`: papel do gene ("oncogene", "tsg", "oncogene_and_tsg", "neither", "unknown").
/// - `cnv_value`: log-ratio da célula CNV (positivo = amplificação, negativo = deleção).
/// - `gain_threshold`: limiar para considerar amplificação relevante (default 0.3).
/// - `loss_threshold`: limiar para considerar deleção relevante (default -0.3, deve ser negativo).
///
/// # Regras de dosagem × papel (heurística CNV v1)
///
/// | Role | CNV | Decisão |
/// |---|---|---|
/// | oncogene | >= gain_threshold | activator |
/// | oncogene | <= loss_threshold | None (deleção de oncogene ≠ seed via dosagem) |
/// | tsg | <= loss_threshold | inactivator |
/// | tsg | >= gain_threshold | None (amplificação de TSG ≠ seed via dosagem) |
/// | oncogene_and_tsg | qualquer | None (ambíguo; não inventar direção) |
/// | neither / unknown / outro | qualquer | None (sem papel para resolver sinal) |
/// | sub-threshold (|cnv| < limiar) | qualquer | None |
pub fn resolve_cnv_direction(
    role: &str,
    cnv_value: f32,
    gain_threshold: f32,
    loss_threshold: f32,
) -> Option<(&'static str, f64)> {
    let magnitude = cnv_value.abs() as f64;

    match role {
        "oncogene" => {
            if cnv_value >= gain_threshold {
                Some(("activator", magnitude))
            } else {
                // deleção de oncogene ou sub-threshold → não é seed via dosagem
                None
            }
        }
        "tsg" => {
            if cnv_value <= loss_threshold {
                Some(("inactivator", magnitude))
            } else {
                // amplificação de TSG ou sub-threshold → não é seed via dosagem
                None
            }
        }
        // oncogene_and_tsg: ambíguo — não inventar direção; retornar None e contar separado
        "oncogene_and_tsg" => None,
        // neither, unknown, qualquer outra coisa: sem papel para resolver sinal
        _ => None,
    }
}

// ─── Função principal (async) ─────────────────────────────────────────────────

/// Lê o Parquet CNV local, cruza com GeneRole + OmicMatrixSample do DB,
/// aplica a regra dosagem×papel e faz COPY UPSERT em `core_varianteffectseed`.
///
/// Esta função espera que:
/// - O Parquet já esteja em `parquet_path` (Django fez o download do object storage).
/// - `matrix_id` seja o PK do OmicMatrix correspondente ao arquivo CNV.
/// - `core_generole` esteja populado (load_oncokb_gene_roles já rodou).
/// - `core_omicmatrixsample` esteja populado para `matrix_id` (vitruvio fez o registro).
pub async fn seed_cnv_from_parquet_async(
    parquet_path: &str,
    matrix_id: i64,
    db_url: &str,
    method_version: &str,
    gain_threshold: f32,
    loss_threshold: f32,
) -> Result<CnvSeedManifest, String> {
    // ── 1. Conectar ao banco ──────────────────────────────────────────────────
    let client = crate::db::connection::connect_db(db_url)
        .await
        .map_err(|e| format!("DB connection failed: {e}"))?;

    // ── 2. Carregar mapa GeneRole (source='oncokb') ───────────────────────────
    // Filtrar só roles relevantes para resolução de sinal (oncogene, tsg, dual).
    let gene_role_map = load_gene_role_map(&client).await?;
    eprintln!(
        "[cnv_seed] GeneRole carregado: {} genes com role relevante",
        gene_role_map.len()
    );

    // ── 3. Carregar mapa OmicMatrixSample (matrix_id → column_index → sample_id) ──
    // `column_index` é 0-based no Parquet (col 0 = feature; amostras >= 1).
    let column_to_sample = load_column_sample_map(&client, matrix_id).await?;
    eprintln!(
        "[cnv_seed] OmicMatrixSample carregado: {} amostras para matrix_id={}",
        column_to_sample.len(),
        matrix_id
    );

    if column_to_sample.is_empty() {
        return Err(format!(
            "Nenhuma OmicMatrixSample encontrada para matrix_id={}. \
             Execute o registro do manifesto (vitruvio) antes do seeding.",
            matrix_id
        ));
    }

    // ── 4. Ler o Parquet e derivar seeds ─────────────────────────────────────
    let (seeds, manifest_counts) = read_parquet_and_derive_seeds(
        parquet_path,
        &gene_role_map,
        &column_to_sample,
        matrix_id,
        gain_threshold,
        loss_threshold,
        method_version,
    )?;

    eprintln!(
        "[cnv_seed] Seeds derivadas: {} activator, {} inactivator",
        manifest_counts.n_activator,
        manifest_counts.n_inactivator,
    );

    // ── 5. COPY UPSERT em core_varianteffectseed ──────────────────────────────
    let n_seeds_written = if seeds.is_empty() {
        eprintln!("[cnv_seed] Nenhuma seed para gravar.");
        0u64
    } else {
        copy_variant_effect_seeds(&client, &seeds).await?
    };

    Ok(CnvSeedManifest {
        n_seeds_written,
        n_activator: manifest_counts.n_activator,
        n_inactivator: manifest_counts.n_inactivator,
        n_neutral_skipped: manifest_counts.n_neutral_skipped,
        n_genes_with_role: manifest_counts.n_genes_with_role,
        n_genes_skipped_no_role: manifest_counts.n_genes_skipped_no_role,
        n_cells_subthreshold: manifest_counts.n_cells_subthreshold,
        n_cells_null: manifest_counts.n_cells_null,
    })
}

// ─── Contagens internas do loop ───────────────────────────────────────────────

#[derive(Default)]
struct LoopCounts {
    n_activator: u64,
    n_inactivator: u64,
    n_neutral_skipped: u64,
    n_genes_with_role: u64,
    n_genes_skipped_no_role: u64,
    n_cells_subthreshold: u64,
    n_cells_null: u64,
}

// ─── Struct interna de uma seed para o COPY ──────────────────────────────────

struct SeedRow {
    matrix_id: i64,
    sample_id: i64,
    gene_symbol: String,
    direction: &'static str,
    magnitude: f64,
    gene_role: String,
    method_version: String,
}

// ─── Carregamento do mapa GeneRole ───────────────────────────────────────────

/// Carrega `core_generole` (source='oncokb') → HashMap gene_symbol(UPPER) → role.
///
/// Filtra apenas roles que resolvem sinal (oncogene, tsg, oncogene_and_tsg).
/// `neither` e `unknown` são excluídos do mapa (nenhum sinal resolvível).
async fn load_gene_role_map(
    client: &Client,
) -> Result<HashMap<String, String>, String> {
    let rows = client
        .query(
            "SELECT gene_symbol, role FROM core_generole \
             WHERE source = 'oncokb' \
             AND role IN ('oncogene', 'tsg', 'oncogene_and_tsg')",
            &[],
        )
        .await
        .map_err(|e| format!("Query GeneRole falhou: {e}"))?;

    let map: HashMap<String, String> = rows
        .into_iter()
        .map(|row| {
            let sym: String = row.get(0);
            let role: String = row.get(1);
            (sym.to_uppercase(), role)
        })
        .collect();

    Ok(map)
}

// ─── Carregamento do mapa OmicMatrixSample ───────────────────────────────────

/// Carrega `core_omicmatrixsample` para `matrix_id` → HashMap column_index → sample_id.
///
/// `column_index` é 0-based (col 0 = feature no Parquet; amostras começam em 1).
/// No banco, column_index = posição 0-based da coluna no Parquet (incluindo col 0).
async fn load_column_sample_map(
    client: &Client,
    matrix_id: i64,
) -> Result<HashMap<i64, i64>, String> {
    let rows = client
        .query(
            "SELECT column_index, sample_id FROM core_omicmatrixsample \
             WHERE matrix_id = $1",
            &[&matrix_id],
        )
        .await
        .map_err(|e| format!("Query OmicMatrixSample falhou: {e}"))?;

    let map: HashMap<i64, i64> = rows
        .into_iter()
        .map(|row| {
            let col_idx: i32 = row.get(0);
            let sample_id: i64 = row.get(1);
            (col_idx as i64, sample_id)
        })
        .collect();

    Ok(map)
}

// ─── Leitura do Parquet e derivação de seeds ─────────────────────────────────

/// Lê o Parquet CNV por row-group e deriva seeds via regra dosagem×papel.
///
/// Retorna `(Vec<SeedRow>, LoopCounts)`.
fn read_parquet_and_derive_seeds(
    parquet_path: &str,
    gene_role_map: &HashMap<String, String>,
    column_to_sample: &HashMap<i64, i64>,
    matrix_id: i64,
    gain_threshold: f32,
    loss_threshold: f32,
    method_version: &str,
) -> Result<(Vec<SeedRow>, LoopCounts), String> {
    let path = Path::new(parquet_path);
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir Parquet {:?}: {}", path, e))?;

    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| format!("Falha ao inicializar leitor Parquet: {}", e))?;

    // Ler os nomes das colunas para mapear posição → case_id
    let schema = builder.schema().clone();
    let n_cols = schema.fields().len();

    if n_cols < 2 {
        return Err(format!(
            "Parquet tem {} colunas; esperava pelo menos 2 (feature + 1 amostra).",
            n_cols
        ));
    }

    // Verificar que coluna 0 é 'feature'
    if schema.field(0).name() != "feature" {
        return Err(format!(
            "Coluna 0 do Parquet é '{}'; esperava 'feature'.",
            schema.field(0).name()
        ));
    }

    // Mapear posição de coluna Arrow (1-based, excluindo 'feature') → sample_id
    // A coluna Arrow i (i >= 1) corresponde a column_index = i (Parquet 0-based: col 0 = feature).
    // OmicMatrixSample.column_index usa a mesma convenção do manifesto do cnv_loader:
    //   column_index = 1 + i, onde i é o índice 0-based no vetor de amostras.
    // Logo Arrow col i → OmicMatrixSample.column_index = i.
    let mut arrow_col_to_sample: HashMap<usize, i64> = HashMap::new();
    for arrow_col in 1..n_cols {
        // arrow_col = column_index no Parquet (col 0 = feature, col 1 = primeira amostra...)
        if let Some(&sample_id) = column_to_sample.get(&(arrow_col as i64)) {
            arrow_col_to_sample.insert(arrow_col, sample_id);
        }
    }

    eprintln!(
        "[cnv_seed] {} colunas de amostra no Parquet; {} mapeadas para OmicMatrixSample",
        n_cols - 1,
        arrow_col_to_sample.len()
    );

    // Configurar leitor para batch streaming por row-group
    let reader = builder
        .with_batch_size(1024)
        .build()
        .map_err(|e| format!("Falha ao construir leitor Parquet: {}", e))?;

    let mut seeds: Vec<SeedRow> = Vec::new();
    let mut counts = LoopCounts::default();

    for batch_result in reader {
        let batch = batch_result
            .map_err(|e| format!("Falha ao ler RecordBatch do Parquet: {}", e))?;

        let n_rows = batch.num_rows();

        // Coluna 0: feature (gene symbols)
        let feature_col = batch
            .column(0)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| "Coluna 'feature' não é StringArray".to_string())?;

        for row in 0..n_rows {
            let gene_raw = feature_col.value(row);
            let gene_upper = gene_raw.to_uppercase();

            // Checar se gene tem papel no mapa
            let role = match gene_role_map.get(&gene_upper) {
                Some(r) => r.as_str(),
                None => {
                    // Gene sem papel relevante (neither, unknown, ou ausente do catálogo)
                    counts.n_genes_skipped_no_role += 1;
                    continue;
                }
            };

            counts.n_genes_with_role += 1;

            // Iterar sobre colunas de amostra
            for arrow_col in 1..batch.num_columns() {
                let sample_id = match arrow_col_to_sample.get(&arrow_col) {
                    Some(&sid) => sid,
                    None => continue, // coluna não mapeada para amostra (deveria não ocorrer)
                };

                let col_array = batch.column(arrow_col);
                let float_col = col_array
                    .as_any()
                    .downcast_ref::<Float32Array>()
                    .ok_or_else(|| {
                        format!(
                            "Coluna {} do Parquet não é Float32Array",
                            schema.field(arrow_col).name()
                        )
                    })?;

                // Célula null → pular
                if float_col.is_null(row) {
                    counts.n_cells_null += 1;
                    continue;
                }

                let cnv_value = float_col.value(row);

                // Aplicar regra de sinal
                match resolve_cnv_direction(role, cnv_value, gain_threshold, loss_threshold) {
                    Some(("activator", magnitude)) => {
                        counts.n_activator += 1;
                        seeds.push(SeedRow {
                            matrix_id,
                            sample_id,
                            gene_symbol: gene_upper.clone(),
                            direction: "activator",
                            magnitude,
                            gene_role: role.to_string(),
                            method_version: method_version.to_string(),
                        });
                    }
                    Some(("inactivator", magnitude)) => {
                        counts.n_inactivator += 1;
                        seeds.push(SeedRow {
                            matrix_id,
                            sample_id,
                            gene_symbol: gene_upper.clone(),
                            direction: "inactivator",
                            magnitude,
                            gene_role: role.to_string(),
                            method_version: method_version.to_string(),
                        });
                    }
                    Some(_) => {
                        // Caso improvável — resolve_cnv_direction retorna só "activator"/"inactivator"/None
                        counts.n_neutral_skipped += 1;
                    }
                    None => {
                        // Verificar se é sub-threshold ou oncogene_and_tsg
                        if role == "oncogene_and_tsg" {
                            counts.n_neutral_skipped += 1;
                        } else {
                            // sub-threshold (|cnv| < limiar) ou deleção-de-oncogene/amplificação-de-TSG
                            counts.n_cells_subthreshold += 1;
                        }
                    }
                }
            }
        }
    }

    // n_genes_with_role foi contado por linha-de-gene, não por gene único;
    // dividir pelo número de amostras ficaria impreciso — manter como contagem
    // total de (gene, batch) com papel. Para o manifesto, reportar o mapa size.
    counts.n_genes_with_role = gene_role_map.len() as u64;

    Ok((seeds, counts))
}

// ─── COPY UPSERT em core_varianteffectseed ───────────────────────────────────

/// Bulk COPY UPSERT das seeds CNV em `core_varianteffectseed`.
///
/// Schema alvo (migration 0030):
/// - matrix_id (FK), sample_id (FK), gene_symbol, variant_key='', evidence_type='cnv',
///   direction, magnitude, confidence=0.6, gene_role, effect_source='cnv_dosage_x_oncokb',
///   method_version, resolved_at (auto_now_add).
///
/// Natural key: UNIQUE(matrix_id, sample_id, gene_symbol, variant_key, evidence_type).
/// ON CONFLICT DO UPDATE atualiza campos de valor (idempotente).
async fn copy_variant_effect_seeds(
    client: &Client,
    seeds: &[SeedRow],
) -> Result<u64, String> {
    if seeds.is_empty() {
        return Ok(0);
    }

    // Criar tabela de staging temporária
    client
        .execute("DROP TABLE IF EXISTS _staging_vef_seed", &[])
        .await
        .map_err(|e| format!("DROP staging falhou: {e}"))?;

    client
        .execute(
            "CREATE TEMP TABLE _staging_vef_seed (
                matrix_id      BIGINT,
                sample_id      BIGINT,
                gene_symbol    VARCHAR(50),
                variant_key    VARCHAR(128),
                evidence_type  VARCHAR(10),
                direction      VARCHAR(20),
                magnitude      DOUBLE PRECISION,
                confidence     DOUBLE PRECISION,
                gene_role      VARCHAR(20),
                effect_source  VARCHAR(64),
                method_version VARCHAR(50)
            )",
            &[],
        )
        .await
        .map_err(|e| format!("CREATE TEMP TABLE falhou: {e}"))?;

    // Construir CSV
    let csv = build_seed_csv(seeds);

    // COPY INTO staging
    {
        use futures::SinkExt;
        use std::pin::pin;

        let sink = client
            .copy_in(
                "COPY _staging_vef_seed (
                    matrix_id, sample_id, gene_symbol, variant_key,
                    evidence_type, direction, magnitude, confidence,
                    gene_role, effect_source, method_version
                ) FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
            )
            .await
            .map_err(|e| format!("COPY IN falhou: {e}"))?;

        let mut sink = pin!(sink);
        sink.send(Bytes::from(csv.into_bytes()))
            .await
            .map_err(|e| format!("Envio do CSV falhou: {e}"))?;
        sink.finish()
            .await
            .map_err(|e| format!("Finish COPY falhou: {e}"))?;
    }

    // UPSERT staging → core_varianteffectseed
    let affected = client
        .execute(
            "INSERT INTO core_varianteffectseed (
                matrix_id, sample_id, gene_symbol, variant_key,
                evidence_type, direction, magnitude, confidence,
                gene_role, effect_source, method_version, resolved_at
            )
            SELECT
                matrix_id, sample_id, gene_symbol, variant_key,
                evidence_type, direction, magnitude, confidence,
                gene_role, effect_source, method_version, NOW()
            FROM _staging_vef_seed
            ON CONFLICT (matrix_id, sample_id, gene_symbol, variant_key, evidence_type, method_version)
            DO UPDATE SET
                direction      = EXCLUDED.direction,
                magnitude      = EXCLUDED.magnitude,
                confidence     = EXCLUDED.confidence,
                gene_role      = EXCLUDED.gene_role,
                effect_source  = EXCLUDED.effect_source",
            &[],
        )
        .await
        .map_err(|e| format!("INSERT UPSERT falhou: {e}"))?;

    client
        .execute("DROP TABLE IF EXISTS _staging_vef_seed", &[])
        .await
        .map_err(|e| format!("DROP staging final falhou: {e}"))?;

    eprintln!("[cnv_seed] COPY UPSERT: {} linhas gravadas", affected);

    Ok(affected)
}

// ─── CSV builder ──────────────────────────────────────────────────────────────

fn build_seed_csv(seeds: &[SeedRow]) -> String {
    let mut csv = String::with_capacity(seeds.len() * 120);
    for s in seeds {
        // confidence = 0.6 (valor fixo do método v1 — heurística dosagem×papel)
        csv.push_str(&format!(
            "{},{},{},{},{},{},{:.6},{:.6},{},{},{}\n",
            s.matrix_id,
            s.sample_id,
            escape_csv_field(&s.gene_symbol),
            // variant_key = '' para CNV (nível-gene, sem posição)
            escape_csv_field(""),
            // evidence_type = 'cnv'
            escape_csv_field("cnv"),
            escape_csv_field(s.direction),
            s.magnitude,
            0.6_f64, // confidence fixo v1
            escape_csv_field(&s.gene_role),
            // effect_source denorm
            escape_csv_field("cnv_dosage_x_oncokb"),
            escape_csv_field(&s.method_version),
        ));
    }
    csv
}

/// Wrap em aspas duplas e escapar aspas internas para CSV Postgres.
#[inline]
fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

// ─── Entry point síncrono ─────────────────────────────────────────────────────

/// Wrapper síncrono para PyO3 — delega ao async via Tokio runtime.
pub fn seed_cnv_from_parquet(
    parquet_path: &str,
    matrix_id: i64,
    db_url: &str,
    method_version: &str,
    gain_threshold: f32,
    loss_threshold: f32,
) -> Result<CnvSeedManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar Tokio runtime: {e}"))?;
    rt.block_on(seed_cnv_from_parquet_async(
        parquet_path,
        matrix_id,
        db_url,
        method_version,
        gain_threshold,
        loss_threshold,
    ))
}

// ─── Testes unitários (#[cfg(test)]) — SEM rede/DB ───────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use tempfile::TempDir;

    // ── Testes da função pura resolve_cnv_direction ───────────────────────────

    const GAIN: f32 = 0.3;
    const LOSS: f32 = -0.3;

    #[test]
    fn test_tsg_delecao_e_inactivator() {
        // TSG com deleção relevante → inactivator
        let result = resolve_cnv_direction("tsg", -0.5, GAIN, LOSS);
        assert!(result.is_some());
        let (dir, mag) = result.unwrap();
        assert_eq!(dir, "inactivator");
        assert!((mag - 0.5_f64).abs() < 1e-5, "magnitude deve ser |cnv_value|");
    }

    #[test]
    fn test_tsg_exatamente_no_limiar() {
        // TSG com cnv_value == loss_threshold → deve ser inactivator (<=)
        let result = resolve_cnv_direction("tsg", -0.3, GAIN, LOSS);
        assert!(result.is_some());
        assert_eq!(result.unwrap().0, "inactivator");
    }

    #[test]
    fn test_tsg_amplificacao_nao_e_seed() {
        // Amplificação de TSG por dosagem ≠ seed via heurística v1 → None
        let result = resolve_cnv_direction("tsg", 0.8, GAIN, LOSS);
        assert!(result.is_none(), "amplificação de TSG não deve ser seed");
    }

    #[test]
    fn test_tsg_subthreshold_nao_e_seed() {
        // |cnv| < threshold → None
        let result = resolve_cnv_direction("tsg", -0.1, GAIN, LOSS);
        assert!(result.is_none(), "sub-threshold TSG não deve ser seed");
    }

    #[test]
    fn test_oncogene_amplificacao_e_activator() {
        // Oncogene amplificado → activator
        let result = resolve_cnv_direction("oncogene", 0.6, GAIN, LOSS);
        assert!(result.is_some());
        let (dir, mag) = result.unwrap();
        assert_eq!(dir, "activator");
        assert!((mag - 0.6_f64).abs() < 1e-5);
    }

    #[test]
    fn test_oncogene_exatamente_no_limiar() {
        // cnv_value == gain_threshold → activator (>=)
        let result = resolve_cnv_direction("oncogene", 0.3, GAIN, LOSS);
        assert!(result.is_some());
        assert_eq!(result.unwrap().0, "activator");
    }

    #[test]
    fn test_oncogene_delecao_nao_e_seed() {
        // Deleção de oncogene ≠ seed via dosagem → None
        let result = resolve_cnv_direction("oncogene", -0.8, GAIN, LOSS);
        assert!(result.is_none(), "deleção de oncogene não deve ser seed");
    }

    #[test]
    fn test_oncogene_subthreshold_nao_e_seed() {
        let result = resolve_cnv_direction("oncogene", 0.1, GAIN, LOSS);
        assert!(result.is_none());
    }

    #[test]
    fn test_oncogene_and_tsg_e_ambiguo() {
        // Dual: qualquer valor → None (ambíguo, não inventar direção)
        assert!(resolve_cnv_direction("oncogene_and_tsg", 1.0, GAIN, LOSS).is_none());
        assert!(resolve_cnv_direction("oncogene_and_tsg", -1.0, GAIN, LOSS).is_none());
        assert!(resolve_cnv_direction("oncogene_and_tsg", 0.0, GAIN, LOSS).is_none());
    }

    #[test]
    fn test_neither_nao_e_seed() {
        assert!(resolve_cnv_direction("neither", 2.0, GAIN, LOSS).is_none());
        assert!(resolve_cnv_direction("neither", -2.0, GAIN, LOSS).is_none());
    }

    #[test]
    fn test_unknown_nao_e_seed() {
        assert!(resolve_cnv_direction("unknown", 1.5, GAIN, LOSS).is_none());
    }

    #[test]
    fn test_role_desconhecido_nao_e_seed() {
        // Role que não existe no catálogo
        assert!(resolve_cnv_direction("", 1.0, GAIN, LOSS).is_none());
        assert!(resolve_cnv_direction("tumor_suppressor", 1.0, GAIN, LOSS).is_none());
    }

    #[test]
    fn test_magnitude_e_valor_absoluto() {
        // magnitude = |cnv_value|
        let (_, mag_neg) = resolve_cnv_direction("tsg", -1.5, GAIN, LOSS).unwrap();
        let (_, mag_pos) = resolve_cnv_direction("oncogene", 1.5, GAIN, LOSS).unwrap();
        assert!((mag_neg - 1.5_f64).abs() < 1e-5);
        assert!((mag_pos - 1.5_f64).abs() < 1e-5);
    }

    #[test]
    fn test_thresholds_customizados() {
        // Com thresholds mais apertados
        let tight_gain = 0.5_f32;
        let tight_loss = -0.5_f32;

        // cnv=0.4 < 0.5 → sub-threshold oncogene → None
        assert!(resolve_cnv_direction("oncogene", 0.4, tight_gain, tight_loss).is_none());
        // cnv=0.6 >= 0.5 → activator
        assert!(resolve_cnv_direction("oncogene", 0.6, tight_gain, tight_loss).is_some());

        // cnv=-0.4 > -0.5 → sub-threshold TSG → None
        assert!(resolve_cnv_direction("tsg", -0.4, tight_gain, tight_loss).is_none());
        // cnv=-0.6 <= -0.5 → inactivator
        assert!(resolve_cnv_direction("tsg", -0.6, tight_gain, tight_loss).is_some());
    }

    // ── Teste de integração com Parquet sintético (SEM DB) ───────────────────

    /// Cria um Parquet sintético CNV e deriva seeds usando mapas injetados.
    /// Não usa DB real — injeta gene_role_map e column_to_sample diretamente.
    #[test]
    fn test_derivacao_com_parquet_sintetico() {
        use arrow::array::{Float32Array, StringArray};
        use arrow::datatypes::{DataType, Field, Schema};
        use arrow::record_batch::RecordBatch;
        use parquet::arrow::ArrowWriter;
        use std::sync::Arc;

        let dir = TempDir::new().unwrap();
        let parquet_path = dir.path().join("cnv_test.parquet");

        // ── Criar Parquet sintético ──────────────────────────────────────────
        // Schema: feature | T_C3L-00001 | T_C3L-00002 | T_C3L-00003
        // Genes: TP53 (tsg), EGFR (oncogene), VHL (tsg), KRAS (oncogene), DUAL (oncogene_and_tsg)
        let schema = Arc::new(Schema::new(vec![
            Field::new("feature", DataType::Utf8, false),
            Field::new("T_C3L-00001", DataType::Float32, true),
            Field::new("T_C3L-00002", DataType::Float32, true),
            Field::new("T_C3L-00003", DataType::Float32, true),
        ]));

        // feature col
        let genes = StringArray::from(vec![
            "TP53",   // tsg: col1=-0.8 (inact), col2=+0.5 (skip), col3=-0.1 (subth)
            "EGFR",   // oncogene: col1=+0.6 (activ), col2=-0.5 (skip), col3=+0.1 (subth)
            "VHL",    // tsg: col1=-1.5 (inact), col2=null (skip null), col3=-0.4 (inact)
            "KRAS",   // oncogene: col1=+1.2 (activ), col2=+0.3 (activ exact), col3=null
            "DUAL",   // oncogene_and_tsg: ambíguo → tudo None
            "NOPROLE",// não está no mapa → skip
        ]);

        // col 1 (T_C3L-00001): TP53=-0.8, EGFR=+0.6, VHL=-1.5, KRAS=+1.2, DUAL=+0.5, NOPROLE=0.9
        let col1 = Float32Array::from(vec![
            Some(-0.8_f32),
            Some(0.6_f32),
            Some(-1.5_f32),
            Some(1.2_f32),
            Some(0.5_f32),
            Some(0.9_f32),
        ]);
        // col 2 (T_C3L-00002): TP53=+0.5, EGFR=-0.5, VHL=null, KRAS=+0.3, DUAL=-0.8, NOPROLE=0.0
        let col2 = Float32Array::from(vec![
            Some(0.5_f32),
            Some(-0.5_f32),
            None,
            Some(0.3_f32),
            Some(-0.8_f32),
            Some(0.0_f32),
        ]);
        // col 3 (T_C3L-00003): TP53=-0.1, EGFR=+0.1, VHL=-0.4, KRAS=null, DUAL=0.0, NOPROLE=-1.0
        let col3 = Float32Array::from(vec![
            Some(-0.1_f32),
            Some(0.1_f32),
            Some(-0.4_f32),
            None,
            Some(0.0_f32),
            Some(-1.0_f32),
        ]);

        let batch = RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(genes),
                Arc::new(col1),
                Arc::new(col2),
                Arc::new(col3),
            ],
        )
        .unwrap();

        let file = std::fs::File::create(&parquet_path).unwrap();
        let mut writer = ArrowWriter::try_new(file, schema, None).unwrap();
        writer.write(&batch).unwrap();
        writer.close().unwrap();

        // ── Injetar mapas (sem DB) ───────────────────────────────────────────
        let mut gene_role_map: HashMap<String, String> = HashMap::new();
        gene_role_map.insert("TP53".to_string(), "tsg".to_string());
        gene_role_map.insert("EGFR".to_string(), "oncogene".to_string());
        gene_role_map.insert("VHL".to_string(), "tsg".to_string());
        gene_role_map.insert("KRAS".to_string(), "oncogene".to_string());
        gene_role_map.insert("DUAL".to_string(), "oncogene_and_tsg".to_string());
        // NOPROLE não está no mapa → será pulado

        // column_to_sample: column_index (1-based Parquet) → sample_id fictício
        let mut column_to_sample: HashMap<i64, i64> = HashMap::new();
        column_to_sample.insert(1, 101); // T_C3L-00001 → sample_id=101
        column_to_sample.insert(2, 102); // T_C3L-00002 → sample_id=102
        column_to_sample.insert(3, 103); // T_C3L-00003 → sample_id=103

        // ── Rodar derivação ──────────────────────────────────────────────────
        let (seeds, counts) = read_parquet_and_derive_seeds(
            parquet_path.to_str().unwrap(),
            &gene_role_map,
            &column_to_sample,
            99,         // matrix_id fictício
            0.3,        // gain_threshold
            -0.3,       // loss_threshold
            "test-v1",  // method_version
        )
        .unwrap();

        // ── Verificações de contagem ─────────────────────────────────────────
        // Esperado:
        // TP53 (tsg):
        //   col1=-0.8 → inactivator  ✓
        //   col2=+0.5 → None (amplificação de TSG)
        //   col3=-0.1 → None (sub-threshold)
        // EGFR (oncogene):
        //   col1=+0.6 → activator   ✓
        //   col2=-0.5 → None (deleção de oncogene)
        //   col3=+0.1 → None (sub-threshold)
        // VHL (tsg):
        //   col1=-1.5 → inactivator ✓
        //   col2=null → n_cells_null
        //   col3=-0.4 → inactivator ✓
        // KRAS (oncogene):
        //   col1=+1.2 → activator   ✓
        //   col2=+0.3 → activator   ✓ (limiar exato: >=0.3)
        //   col3=null → n_cells_null
        // DUAL (oncogene_and_tsg):
        //   col1=+0.5 → None → n_neutral_skipped
        //   col2=-0.8 → None → n_neutral_skipped
        //   col3=0.0  → None → n_neutral_skipped
        // NOPROLE:
        //   → n_genes_skipped_no_role (mas gene_role_map.len() = 5 genes COM papel)

        let n_expected_activator = 3_u64; // EGFR+col1, KRAS+col1, KRAS+col2
        let n_expected_inactivator = 3_u64; // TP53+col1, VHL+col1, VHL+col3
        let n_expected_null = 2_u64; // VHL+col2, KRAS+col3
        let n_expected_neutral_skipped = 3_u64; // DUAL * 3 colunas
        // sub-threshold: TP53+col3(-0.1), EGFR+col3(+0.1), TP53+col2(+0.5 TSG→amplif),
        // EGFR+col2(-0.5 oncogene→del) — esses últimos dois são "amplif-de-TSG/del-de-oncogene"
        // tratados como sub-threshold na contagem

        assert_eq!(counts.n_activator, n_expected_activator,
            "Esperava {} activators, obteve {}", n_expected_activator, counts.n_activator);
        assert_eq!(counts.n_inactivator, n_expected_inactivator,
            "Esperava {} inactivators, obteve {}", n_expected_inactivator, counts.n_inactivator);
        assert_eq!(counts.n_cells_null, n_expected_null,
            "Esperava {} nulls, obteve {}", n_expected_null, counts.n_cells_null);
        assert_eq!(counts.n_neutral_skipped, n_expected_neutral_skipped,
            "Esperava {} neutral_skipped, obteve {}", n_expected_neutral_skipped, counts.n_neutral_skipped);

        // Total de seeds
        assert_eq!(
            seeds.len() as u64,
            n_expected_activator + n_expected_inactivator,
            "Total de seeds deve ser activator + inactivator"
        );

        // Verificar campos específicos de uma seed
        let tp53_inact = seeds
            .iter()
            .find(|s| s.gene_symbol == "TP53" && s.direction == "inactivator" && s.sample_id == 101)
            .expect("Esperava seed TP53-inactivator para sample_id=101");

        assert_eq!(tp53_inact.gene_role, "tsg");
        assert_eq!(tp53_inact.method_version, "test-v1");
        assert_eq!(tp53_inact.matrix_id, 99);
        assert!((tp53_inact.magnitude - 0.8_f64).abs() < 1e-5,
            "magnitude de TP53 deve ser 0.8");

        // KRAS +0.3 (limiar exato) → activator
        let kras_exact = seeds
            .iter()
            .find(|s| s.gene_symbol == "KRAS" && s.sample_id == 102)
            .expect("Esperava seed KRAS-activator para sample_id=102 (limiar exato 0.3)");
        assert_eq!(kras_exact.direction, "activator");
        assert!((kras_exact.magnitude - 0.3_f64).abs() < 1e-5);

        // NOPROLE não deve gerar nenhuma seed
        assert!(
            seeds.iter().all(|s| s.gene_symbol != "NOPROLE"),
            "NOPROLE não deve gerar seeds"
        );

        // DUAL não deve gerar seeds
        assert!(
            seeds.iter().all(|s| s.gene_symbol != "DUAL"),
            "DUAL (oncogene_and_tsg) não deve gerar seeds"
        );
    }

    #[test]
    fn test_build_seed_csv_formato() {
        let seeds = vec![SeedRow {
            matrix_id: 1,
            sample_id: 42,
            gene_symbol: "TP53".to_string(),
            direction: "inactivator",
            magnitude: 0.8,
            gene_role: "tsg".to_string(),
            method_version: "fase2-cnv-v1".to_string(),
        }];

        let csv = build_seed_csv(&seeds);
        // Verificar que o CSV tem o formato correto
        assert!(csv.contains("\"TP53\""));
        assert!(csv.contains("\"inactivator\""));
        assert!(csv.contains("\"cnv\"")); // evidence_type
        assert!(csv.contains("\"\"")); // variant_key vazio
        assert!(csv.contains("\"tsg\""));
        assert!(csv.contains("\"cnv_dosage_x_oncokb\""));
        assert!(csv.contains("0.600000")); // confidence = 0.6
    }

    #[test]
    fn test_escape_csv_field() {
        assert_eq!(escape_csv_field("hello"), "\"hello\"");
        assert_eq!(escape_csv_field(""), "\"\"");
        assert_eq!(escape_csv_field("with\"quote"), "\"with\"\"quote\"");
    }
}
