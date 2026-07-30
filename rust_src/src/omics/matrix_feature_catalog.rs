/// Catálogo de features de matriz — promoção A→C (Obj 2, Fase 3, migration 0036).
///
/// Lê o eixo de linhas (`feature`, coluna 0) de um Parquet de matriz já
/// carregado (`matrix_loader.rs` é quem escreve — proteoma e fosfoproteoma
/// CPTAC CCRCC hoje) e popula `core_omicmatrixfeature`: uma linha por
/// `(matrix_id, feature_key) → row_index`.
///
/// Escopo: só o catálogo (índice). NENHUM valor numérico da matriz sobe ao
/// Postgres — o corpo denso continua exclusivamente no Parquet.
///
/// # Formato de entrada esperado
///
/// Confirmado lendo `matrix_loader.rs` (não assumido): coluna 0 do Parquet é
/// sempre `feature` (Utf8, não-nullable) — símbolo de gene ou fosfo-sítio.
/// Não há suposição de nome alternativo: se a coluna 0 não se chamar
/// `feature`, a função falha explicitamente em vez de adivinhar.
///
/// # Natural key / idempotência
///
/// `UNIQUE(matrix_id, feature_key)` e `UNIQUE(matrix_id, row_index)`
/// (migration 0036). Rodar duas vezes com o MESMO Parquet não duplica.
///
/// Decisão sobre re-execução com Parquet DIFERENTE (loader mudou de versão e
/// alterou a posição de uma feature): **limpar** (`DELETE`) todas as
/// `OmicMatrixFeature` da matriz antes de repopular, em vez de UPSERT parcial
/// por `(matrix_id, feature_key)`. Motivo: um UPSERT que só teste conflito em
/// `(matrix_id, feature_key)` pode deixar uma linha ANTIGA ocupando um
/// `row_index` que a carga nova atribuiu a OUTRA feature — violando
/// `omicmatrixfeature_matrix_row_uniq` sem que o `ON CONFLICT` tenha como
/// resolver (o conflito acontece na constraint errada). `DELETE` + reinserção
/// total é idempotente por construção e elimina esse risco. O `ON CONFLICT
/// (matrix_id, feature_key) DO UPDATE` do COPY final fica como defesa
/// adicional (ex.: duas chamadas concorrentes para a mesma matriz), mas no
/// fluxo normal (uma chamada por vez) todas as linhas entram como INSERT.
///
/// # Segurança
///
/// `db_url` nunca logado.
use std::collections::HashMap;
use std::path::Path;

use arrow::array::{Array, StringArray};
use bytes::Bytes;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use tokio_postgres::Client;

// ─── Manifesto público ────────────────────────────────────────────────────────

/// Manifesto retornado por `catalog_matrix_features`.
///
/// | Campo | Tipo | Descrição |
/// |---|---|---|
/// | `n_features` | `u64` | Features distintas (após normalização UPPERCASE e dedup) catalogadas. |
/// | `n_inserted` | `u64` | Linhas gravadas como INSERT novo (normal: = n_features, pois a matriz é limpa antes). |
/// | `n_updated` | `u64` | Linhas que colidiram em `(matrix_id, feature_key)` no COPY final (defesa; normalmente 0). |
#[derive(Debug, Clone)]
pub struct FeatureCatalogManifest {
    pub n_features: u64,
    pub n_inserted: u64,
    pub n_updated: u64,
}

// ─── Leitura do eixo de linhas do Parquet ────────────────────────────────────

/// Lê a coluna 0 (`feature`) do Parquet, em ordem de arquivo.
///
/// Retorna um `Vec<String>` já normalizado para UPPERCASE, onde a posição no
/// vetor É o `row_index` 0-based (mesma convenção de posição usada por
/// `OmicMatrixSample.column_index` no eixo de colunas).
///
/// Falha explicitamente (não assume) se a coluna 0 não se chamar `feature` ou
/// não for `Utf8`/`StringArray` — layout real confirmado em `matrix_loader.rs`.
fn read_feature_column(parquet_path: &str) -> Result<Vec<String>, String> {
    let path = Path::new(parquet_path);
    let file = std::fs::File::open(path)
        .map_err(|e| format!("Falha ao abrir Parquet {:?}: {}", path, e))?;

    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| format!("Falha ao inicializar leitor Parquet: {}", e))?;

    let schema = builder.schema().clone();
    if schema.fields().is_empty() {
        return Err(format!("Parquet sem colunas: {:?}", path));
    }
    let col0_name = schema.field(0).name();
    if col0_name != "feature" {
        return Err(format!(
            "Coluna 0 do Parquet é '{}'; esperava 'feature' (eixo de linhas \
             escrito por matrix_loader.rs). Recusando adivinhar layout.",
            col0_name
        ));
    }

    let reader = builder
        .with_batch_size(1024)
        .build()
        .map_err(|e| format!("Falha ao construir leitor Parquet: {}", e))?;

    let mut features: Vec<String> = Vec::new();

    for batch_result in reader {
        let batch = batch_result.map_err(|e| format!("Falha ao ler RecordBatch do Parquet: {}", e))?;

        let feature_col = batch
            .column(0)
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| "Coluna 'feature' não é StringArray (Utf8)".to_string())?;

        for row in 0..batch.num_rows() {
            if feature_col.is_null(row) {
                return Err(format!(
                    "feature_key nulo na linha {} do Parquet — arquivo malformado",
                    features.len()
                ));
            }
            let key = feature_col.value(row).trim().to_uppercase();
            if key.is_empty() {
                return Err(format!(
                    "feature_key vazio na linha {} do Parquet — arquivo malformado",
                    features.len()
                ));
            }
            features.push(key);
        }
    }

    if features.is_empty() {
        return Err(format!("Parquet sem linhas de feature: {:?}", path));
    }

    Ok(features)
}

/// Deduplica features por chave normalizada, mantendo a PRIMEIRA ocorrência
/// (e seu `row_index` original no Parquet).
///
/// Não deveria ocorrer em um Parquet bem formado (o loader não deveria emitir
/// símbolo duplicado), mas normalizar para UPPERCASE pode colidir dois
/// símbolos que eram distintos em case-sensitivity (`Tp53` vs `TP53`) — a NK
/// da tabela é case-sensitive sobre o valor JÁ normalizado, então dedup aqui
/// é a linha de defesa contra violar `omicmatrixfeature_matrix_feature_uniq`.
///
/// Retorna `(Vec<(feature_key, row_index)>, n_duplicates_descartadas)`.
fn dedup_features(features: Vec<String>) -> (Vec<(String, i64)>, u64) {
    let mut seen: HashMap<String, i64> = HashMap::with_capacity(features.len());
    let mut ordered: Vec<(String, i64)> = Vec::with_capacity(features.len());
    let mut n_duplicates = 0u64;

    for (idx, key) in features.into_iter().enumerate() {
        if let Some(&first_idx) = seen.get(&key) {
            n_duplicates += 1;
            eprintln!(
                "[matrix_feature_catalog] feature_key duplicada '{}' na linha {} \
                 — mantendo a primeira ocorrência (linha {})",
                key, idx, first_idx
            );
            continue;
        }
        seen.insert(key.clone(), idx as i64);
        ordered.push((key, idx as i64));
    }

    (ordered, n_duplicates)
}

// ─── COPY UPSERT em core_omicmatrixfeature ───────────────────────────────────

struct FeatureRow {
    matrix_id: i64,
    feature_key: String,
    row_index: i64,
}

fn build_feature_csv(rows: &[FeatureRow]) -> String {
    let mut csv = String::with_capacity(rows.len() * 40);
    for r in rows {
        csv.push_str(&format!(
            "{},{},{}\n",
            r.matrix_id,
            escape_csv_field(&r.feature_key),
            r.row_index
        ));
    }
    csv
}

#[inline]
fn escape_csv_field(s: &str) -> String {
    format!("\"{}\"", s.replace('"', "\"\""))
}

/// Limpa o catálogo antigo da matriz e faz COPY + INSERT ON CONFLICT em
/// `core_omicmatrixfeature`. Ver decisão de re-execução no cabeçalho do módulo.
///
/// Retorna `(n_inserted, n_updated)` via `RETURNING (xmax = 0)` — no fluxo
/// normal (DELETE já limpou a matriz) tudo entra como INSERT; `n_updated`
/// só é > 0 se outra chamada concorrente inseriu a mesma `(matrix_id,
/// feature_key)` entre o DELETE e o INSERT desta chamada.
async fn copy_upsert_features(
    client: &Client,
    matrix_id: i64,
    rows: &[FeatureRow],
) -> Result<(u64, u64), String> {
    if rows.is_empty() {
        return Ok((0, 0));
    }

    // Staging: DROP no início da chamada (não ON COMMIT DROP fora de
    // transação — bug já mordido em cnv_seed_derivation/kegg_topology_loader).
    client
        .execute("DROP TABLE IF EXISTS _staging_matrix_feature", &[])
        .await
        .map_err(|e| format!("DROP staging falhou: {e}"))?;

    client
        .execute(
            "CREATE TEMP TABLE _staging_matrix_feature (
                matrix_id   BIGINT,
                feature_key VARCHAR(128),
                row_index   INTEGER
            )",
            &[],
        )
        .await
        .map_err(|e| format!("CREATE TEMP TABLE falhou: {e}"))?;

    let csv = build_feature_csv(rows);

    {
        use futures::SinkExt;
        use std::pin::pin;

        let sink = client
            .copy_in(
                "COPY _staging_matrix_feature (matrix_id, feature_key, row_index) \
                 FROM STDIN WITH (FORMAT csv, NULL 'NULL')",
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

    // Limpar o catálogo antigo desta matriz ANTES do INSERT final — decisão
    // documentada no cabeçalho do módulo (evita colisão na constraint
    // (matrix_id, row_index) quando a posição de uma feature muda entre
    // versões do loader).
    client
        .execute(
            "DELETE FROM core_omicmatrixfeature WHERE matrix_id = $1",
            &[&matrix_id],
        )
        .await
        .map_err(|e| format!("DELETE features antigas falhou: {e}"))?;

    let result_rows = client
        .query(
            "INSERT INTO core_omicmatrixfeature (matrix_id, feature_key, row_index)
             SELECT matrix_id, feature_key, row_index FROM _staging_matrix_feature
             ON CONFLICT (matrix_id, feature_key)
             DO UPDATE SET row_index = EXCLUDED.row_index
             RETURNING (xmax = 0) AS inserted",
            &[],
        )
        .await
        .map_err(|e| format!("INSERT UPSERT falhou: {e}"))?;

    let mut n_inserted = 0u64;
    let mut n_updated = 0u64;
    for row in &result_rows {
        let inserted: bool = row.get(0);
        if inserted {
            n_inserted += 1;
        } else {
            n_updated += 1;
        }
    }

    client
        .execute("DROP TABLE IF EXISTS _staging_matrix_feature", &[])
        .await
        .map_err(|e| format!("DROP staging final falhou: {e}"))?;

    Ok((n_inserted, n_updated))
}

// ─── Função principal (async) ─────────────────────────────────────────────────

/// Lê o eixo de linhas do Parquet, normaliza para UPPERCASE, dedup e faz COPY
/// UPSERT em `core_omicmatrixfeature`. Wrapper síncrono via `catalog_matrix_features`.
///
/// # Pré-requisitos
///
/// - `parquet_path`: caminho local do Parquet da matriz (Django baixou do
///   object storage e passa o caminho — I/O de storage é Django).
/// - `matrix_id`: PK bigint de `OmicMatrix` já registrado (o vitruvio grava o
///   `OmicMatrix` a partir do `MatrixLoadManifest` antes de chamar esta função).
pub async fn catalog_matrix_features_async(
    parquet_path: &str,
    matrix_id: i64,
    db_url: &str,
) -> Result<FeatureCatalogManifest, String> {
    let raw_features = read_feature_column(parquet_path)?;
    eprintln!(
        "[matrix_feature_catalog] {} linhas de feature lidas de {}",
        raw_features.len(),
        parquet_path
    );

    let (deduped, n_duplicates) = dedup_features(raw_features);
    if n_duplicates > 0 {
        eprintln!(
            "[matrix_feature_catalog] {} feature_key(s) duplicada(s) após \
             normalização UPPERCASE — mantida apenas a primeira ocorrência de cada",
            n_duplicates
        );
    }

    let n_features = deduped.len() as u64;
    let rows: Vec<FeatureRow> = deduped
        .into_iter()
        .map(|(feature_key, row_index)| FeatureRow {
            matrix_id,
            feature_key,
            row_index,
        })
        .collect();

    let client = crate::db::connection::connect_db(db_url)
        .await
        .map_err(|e| format!("DB connection failed: {e}"))?;

    let (n_inserted, n_updated) = copy_upsert_features(&client, matrix_id, &rows).await?;

    eprintln!(
        "[matrix_feature_catalog] matrix_id={} — {} features catalogadas \
         ({} inseridas, {} atualizadas)",
        matrix_id, n_features, n_inserted, n_updated
    );

    Ok(FeatureCatalogManifest {
        n_features,
        n_inserted,
        n_updated,
    })
}

// ─── Entry point síncrono ─────────────────────────────────────────────────────

/// Wrapper síncrono para PyO3 — delega ao async via Tokio runtime.
pub fn catalog_matrix_features(
    parquet_path: &str,
    matrix_id: i64,
    db_url: &str,
) -> Result<FeatureCatalogManifest, String> {
    let rt = tokio::runtime::Runtime::new()
        .map_err(|e| format!("Falha ao criar Tokio runtime: {e}"))?;
    rt.block_on(catalog_matrix_features_async(parquet_path, matrix_id, db_url))
}

// ─── Testes unitários (#[cfg(test)]) — SEM rede/DB ───────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use arrow::array::{ArrayRef, StringArray as ArrowStringArray};
    use arrow::datatypes::{DataType, Field, Schema};
    use arrow::record_batch::RecordBatch;
    use parquet::arrow::ArrowWriter;
    use parquet::file::properties::WriterProperties;
    use std::path::PathBuf;
    use std::sync::Arc;
    use tempfile::TempDir;

    /// Grava um Parquet sintético só com a coluna `feature` (Utf8), imitando
    /// o eixo de linhas escrito por `matrix_loader.rs` (sem as colunas de
    /// amostra — irrelevantes para o catálogo).
    fn write_feature_only_parquet(dir: &Path, filename: &str, features: &[&str]) -> PathBuf {
        let path = dir.join(filename);

        let schema = Arc::new(Schema::new(vec![Field::new("feature", DataType::Utf8, false)]));
        let array: ArrayRef = Arc::new(ArrowStringArray::from(features.to_vec()));
        let batch = RecordBatch::try_new(schema.clone(), vec![array]).unwrap();

        let props = WriterProperties::builder().build();
        let file = std::fs::File::create(&path).unwrap();
        let mut writer = ArrowWriter::try_new(file, schema, Some(props)).unwrap();
        writer.write(&batch).unwrap();
        writer.close().unwrap();

        path
    }

    /// Grava um Parquet sintético com a primeira coluna nomeada de forma
    /// diferente de `feature` — para testar a rejeição explícita de layout.
    fn write_wrong_column_name_parquet(dir: &Path, filename: &str) -> PathBuf {
        let path = dir.join(filename);
        let schema = Arc::new(Schema::new(vec![Field::new("gene_symbol", DataType::Utf8, false)]));
        let array: ArrayRef = Arc::new(ArrowStringArray::from(vec!["TP53"]));
        let batch = RecordBatch::try_new(schema.clone(), vec![array]).unwrap();

        let props = WriterProperties::builder().build();
        let file = std::fs::File::create(&path).unwrap();
        let mut writer = ArrowWriter::try_new(file, schema, Some(props)).unwrap();
        writer.write(&batch).unwrap();
        writer.close().unwrap();

        path
    }

    #[test]
    fn test_read_feature_column_happy_path_uppercases() {
        let dir = TempDir::new().unwrap();
        let path = write_feature_only_parquet(dir.path(), "f.parquet", &["tp53", "EGFR", "Brca1"]);

        let features = read_feature_column(path.to_str().unwrap()).unwrap();

        assert_eq!(features, vec!["TP53", "EGFR", "BRCA1"]);
    }

    #[test]
    fn test_read_feature_column_preserves_row_order_as_index() {
        let dir = TempDir::new().unwrap();
        let path = write_feature_only_parquet(dir.path(), "f.parquet", &["AAA", "BBB", "CCC", "DDD"]);

        let features = read_feature_column(path.to_str().unwrap()).unwrap();

        // Posição no vetor == row_index 0-based esperado no Parquet.
        assert_eq!(features[0], "AAA");
        assert_eq!(features[1], "BBB");
        assert_eq!(features[2], "CCC");
        assert_eq!(features[3], "DDD");
    }

    #[test]
    fn test_read_feature_column_rejects_wrong_column_name() {
        let dir = TempDir::new().unwrap();
        let path = write_wrong_column_name_parquet(dir.path(), "wrong.parquet");

        let result = read_feature_column(path.to_str().unwrap());
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("gene_symbol"), "erro deve citar o nome real da coluna: {}", err);
        assert!(err.contains("feature"), "erro deve citar o nome esperado: {}", err);
    }

    #[test]
    fn test_read_feature_column_rejects_empty_file() {
        let dir = TempDir::new().unwrap();
        // Schema com feature mas zero linhas.
        let path = dir.path().join("empty.parquet");
        let schema = Arc::new(Schema::new(vec![Field::new("feature", DataType::Utf8, false)]));
        let array: ArrayRef = Arc::new(ArrowStringArray::from(Vec::<&str>::new()));
        let batch = RecordBatch::try_new(schema.clone(), vec![array]).unwrap();
        let props = WriterProperties::builder().build();
        let file = std::fs::File::create(&path).unwrap();
        let mut writer = ArrowWriter::try_new(file, schema, Some(props)).unwrap();
        writer.write(&batch).unwrap();
        writer.close().unwrap();

        let result = read_feature_column(path.to_str().unwrap());
        assert!(result.is_err());
    }

    #[test]
    fn test_dedup_features_keeps_first_occurrence() {
        let features = vec![
            "TP53".to_string(),
            "EGFR".to_string(),
            "TP53".to_string(), // duplicata (ex.: 'Tp53' e 'TP53' após uppercase)
            "BRCA1".to_string(),
        ];

        let (deduped, n_duplicates) = dedup_features(features);

        assert_eq!(n_duplicates, 1);
        assert_eq!(deduped.len(), 3);
        // TP53 mantém row_index da PRIMEIRA ocorrência (0), não da segunda (2).
        assert_eq!(deduped[0], ("TP53".to_string(), 0));
        assert_eq!(deduped[1], ("EGFR".to_string(), 1));
        assert_eq!(deduped[2], ("BRCA1".to_string(), 3));
    }

    #[test]
    fn test_dedup_features_no_duplicates_is_noop() {
        let features = vec!["AAA".to_string(), "BBB".to_string(), "CCC".to_string()];
        let (deduped, n_duplicates) = dedup_features(features);

        assert_eq!(n_duplicates, 0);
        assert_eq!(deduped.len(), 3);
        assert_eq!(deduped[0], ("AAA".to_string(), 0));
        assert_eq!(deduped[1], ("BBB".to_string(), 1));
        assert_eq!(deduped[2], ("CCC".to_string(), 2));
    }

    #[test]
    fn test_build_feature_csv_format() {
        let rows = vec![
            FeatureRow { matrix_id: 42, feature_key: "TP53".to_string(), row_index: 0 },
            FeatureRow { matrix_id: 42, feature_key: "with\"quote".to_string(), row_index: 1 },
        ];
        let csv = build_feature_csv(&rows);

        assert_eq!(csv, "42,\"TP53\",0\n42,\"with\"\"quote\",1\n");
    }

    #[test]
    fn test_escape_csv_field() {
        assert_eq!(escape_csv_field("hello"), "\"hello\"");
        assert_eq!(escape_csv_field("with\"quote"), "\"with\"\"quote\"");
    }

    #[test]
    fn test_full_pipeline_realistic_fixture_matches_manifest_counts() {
        // Fixture imitando o formato real: coluna feature + genes em UPPERCASE
        // já (o loader real grava símbolos como vieram do .cct, nem sempre
        // uppercase — este teste simula um caso misto).
        let dir = TempDir::new().unwrap();
        let path = write_feature_only_parquet(
            dir.path(),
            "matrix.parquet",
            &["TP53", "egfr", "BRCA1", "MYC", "kras"],
        );

        let raw = read_feature_column(path.to_str().unwrap()).unwrap();
        assert_eq!(raw.len(), 5);
        assert!(raw.contains(&"EGFR".to_string()));
        assert!(raw.contains(&"KRAS".to_string()));

        let (deduped, n_duplicates) = dedup_features(raw);
        assert_eq!(n_duplicates, 0);
        assert_eq!(deduped.len(), 5);

        let rows: Vec<FeatureRow> = deduped
            .into_iter()
            .map(|(k, i)| FeatureRow { matrix_id: 1, feature_key: k, row_index: i })
            .collect();
        let csv = build_feature_csv(&rows);
        assert_eq!(csv.lines().count(), 5);
    }

    // ─── Regressão de tipo PK/FK: bigint (Django BigAutoField) → i64 ─────────
    //
    // `matrix_id` é FK bigint (core_omicmatrix.id). Ler/escrever como i32
    // já causou `pyo3_runtime::PanicException` em outros loaders desta sessão
    // (kegg_topology_loader). Esta função nunca é chamada em teste algum
    // (`#[allow(dead_code)]`); existe só para fixar o contrato de tipo por
    // checagem estática do compilador.
    #[allow(dead_code)]
    async fn _typecheck_matrix_id_is_i64(client: &Client, rows: &[FeatureRow]) {
        let matrix_id: i64 = 1;
        let _: (u64, u64) = copy_upsert_features(client, matrix_id, rows).await.unwrap();
    }

    // ─── Teste de integração (requer Postgres real com schema Django) ───────
    //
    // Cria um OmicDataset + OmicMatrix descartáveis (accession de teste,
    // limpos no início e no fim), roda `catalog_matrix_features_async` duas
    // vezes com o MESMO Parquet sintético e valida idempotência (mesma
    // contagem, sem duplicar linhas).
    //
    // Exemplo de execução local:
    // ```
    // DATABASE_URL="postgres://user:pass@localhost:5432/davinci" \
    //     cargo test --lib omics::matrix_feature_catalog -- --ignored
    // ```
    #[tokio::test]
    #[ignore = "requer DATABASE_URL apontando para Postgres com schema Django migrado"]
    async fn integration_idempotent_catalog_against_real_db() {
        let db_url = std::env::var("DATABASE_URL")
            .expect("defina DATABASE_URL para rodar este teste de integração");

        let (client, connection) = tokio_postgres::connect(&db_url, tokio_postgres::NoTls)
            .await
            .expect("falha ao conectar em DATABASE_URL");
        tokio::spawn(async move {
            let _ = connection.await;
        });

        let test_accession = "TEST_MATRIX_FEATURE_CATALOG_FIXTURE";

        // Limpeza prévia best-effort (idempotência caso uma execução anterior
        // tenha falhado no meio). Ordem: feature -> matrix -> dataset
        // (sem ON DELETE CASCADE físico garantido).
        let _ = client
            .execute(
                "DELETE FROM core_omicmatrixfeature WHERE matrix_id IN
                    (SELECT m.id FROM core_omicmatrix m
                     JOIN core_omicdataset d ON d.id = m.dataset_id
                     WHERE d.accession = $1)",
                &[&test_accession],
            )
            .await;
        let _ = client
            .execute(
                "DELETE FROM core_omicmatrix WHERE dataset_id IN
                    (SELECT id FROM core_omicdataset WHERE accession = $1)",
                &[&test_accession],
            )
            .await;
        let _ = client
            .execute("DELETE FROM core_omicdataset WHERE accession = $1", &[&test_accession])
            .await;

        // Criar dataset descartável (todas as colunas NOT NULL sem default
        // de DB explícitas para não depender de nenhum default implícito).
        let dataset_row = client
            .query_one(
                "INSERT INTO core_omicdataset (
                    accession, source_db, bioproject_id, title, summary,
                    omic_type, omic_subcategory, organism, tax_id, n_samples,
                    platform, extra_metadata, omics_count, omics_layers,
                    is_single_cell, has_control_group, disease_axis, data_format,
                    access_type, sample_join_key, contract_confidence,
                    is_active, ingested_at, updated_at
                ) VALUES (
                    $1, 'cptac', '', 'fixture de teste', '',
                    '', '', '', NULL, NULL,
                    '', '{}'::jsonb, NULL, '{}'::varchar[],
                    'unknown', 'unknown', 'indeterminate', 'unknown',
                    'unknown', '{}'::varchar[], '{}'::jsonb,
                    true, NOW(), NOW()
                ) RETURNING id",
                &[&test_accession],
            )
            .await
            .expect("INSERT core_omicdataset fixture");
        let dataset_id: i64 = dataset_row.get(0);

        let matrix_row = client
            .query_one(
                "INSERT INTO core_omicmatrix (
                    dataset_id, omics_layer, data_format_level, feature_axis,
                    source_pointer, storage_key, n_features, n_samples,
                    checksum_md5, loader_version, loaded_at
                ) VALUES (
                    $1, 'proteomic', 'intensities', 'protein',
                    '', '', NULL, NULL,
                    '', 'test-fixture', NOW()
                ) RETURNING id",
                &[&dataset_id],
            )
            .await
            .expect("INSERT core_omicmatrix fixture");
        let matrix_id: i64 = matrix_row.get(0);

        // Parquet sintético local (tmpdir), imitando o eixo de linhas real.
        let dir = TempDir::new().unwrap();
        let parquet_path = write_feature_only_parquet(
            dir.path(),
            "fixture.parquet",
            &["TP53", "EGFR", "BRCA1", "MYC", "KRAS"],
        );
        let parquet_path_str = parquet_path.to_str().unwrap();

        // Primeira carga.
        let manifest_1 = catalog_matrix_features_async(parquet_path_str, matrix_id, &db_url)
            .await
            .expect("primeira chamada não deve falhar");
        assert_eq!(manifest_1.n_features, 5);
        assert_eq!(manifest_1.n_inserted, 5);
        assert_eq!(manifest_1.n_updated, 0);

        let count_after_first: i64 = client
            .query_one(
                "SELECT COUNT(*) FROM core_omicmatrixfeature WHERE matrix_id = $1",
                &[&matrix_id],
            )
            .await
            .unwrap()
            .get(0);
        assert_eq!(count_after_first, 5);

        // Segunda carga — mesmo Parquet: idempotente, sem duplicar.
        let manifest_2 = catalog_matrix_features_async(parquet_path_str, matrix_id, &db_url)
            .await
            .expect("segunda chamada (idempotência) não deve falhar");
        assert_eq!(manifest_2.n_features, 5);
        assert_eq!(manifest_2.n_inserted, 5); // DELETE limpou antes; tudo reinsere
        assert_eq!(manifest_2.n_updated, 0);

        let count_after_second: i64 = client
            .query_one(
                "SELECT COUNT(*) FROM core_omicmatrixfeature WHERE matrix_id = $1",
                &[&matrix_id],
            )
            .await
            .unwrap()
            .get(0);
        assert_eq!(count_after_second, 5, "segunda carga não deve duplicar linhas");

        // Verificar row_index correto para uma feature específica.
        let tp53_row_index: i32 = client
            .query_one(
                "SELECT row_index FROM core_omicmatrixfeature \
                 WHERE matrix_id = $1 AND feature_key = 'TP53'",
                &[&matrix_id],
            )
            .await
            .unwrap()
            .get(0);
        assert_eq!(tp53_row_index, 0);

        // Limpeza.
        let _ = client
            .execute("DELETE FROM core_omicmatrixfeature WHERE matrix_id = $1", &[&matrix_id])
            .await;
        let _ = client
            .execute("DELETE FROM core_omicmatrix WHERE id = $1", &[&matrix_id])
            .await;
        let _ = client
            .execute("DELETE FROM core_omicdataset WHERE id = $1", &[&dataset_id])
            .await;
    }
}
