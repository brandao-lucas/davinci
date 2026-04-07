# DaVinci — Prompt de Correção: SRA Parser + DatasetPaperLinks + NER Expansion

**Contexto:** O DaVinci está 95% funcional. 57 testes passando, 0 falhas. PubMed, GEO, BioProject e GWAS ingerem corretamente. Restam três problemas concretos para resolver nesta rodada.

**Estrutura do projeto Rust:** O código Rust está em `rust_src/` (foi renomeado de `rust_engine/`). O módulo compilado pelo maturin continua se chamando `rust_engine` (nome do crate no `Cargo.toml`). O import Python é `import rust_engine`.

---

## PROBLEMA 1 — SRA Parser retorna 0 datasets

### Contexto

No diagnóstico, a ingestão SRA retornou `datasets_processed: 0, datasets_inserted: 0` sem erro. Todas as outras fontes funcionaram. A API do NCBI confirma que existem milhares de resultados para "cardiovascular disease" no banco SRA.

### Causa provável

O SRA usa um formato de resposta peculiar no `esummary`. Diferente do GEO (que retorna JSON limpo) e do BioProject (XML limpo), o SRA retorna JSON onde os dados reais estão **embutidos como strings XML** dentro dos campos `expxml` e `runs`. Se o parser tenta ler os campos diretamente do JSON sem parsear o XML interno, não encontra nada.

### Investigação — Executar ANTES de corrigir

```bash
# PASSO 1: Confirmar que o esearch SRA retorna IDs
python3 -c "
import requests, json

r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
    'db': 'sra', 'term': 'cardiovascular disease', 'retmax': 5, 'retmode': 'json'
}, timeout=30)
data = r.json()
ids = data['esearchresult']['idlist']
count = data['esearchresult']['count']
print(f'esearch SRA: count={count}, ids={ids}')

if not ids:
    print('PROBLEMA: esearch não retornou IDs. Verificar se o termo está correto.')
    exit(1)

# PASSO 2: Ver a estrutura REAL do esummary SRA
r2 = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi', params={
    'db': 'sra', 'id': ','.join(ids[:2]), 'retmode': 'json'
}, timeout=30)
summary = r2.json()

# Imprimir estrutura completa do primeiro resultado
for uid in ids[:1]:
    item = summary.get('result', {}).get(uid, {})
    print(f'\n=== UID {uid} — Campos de primeiro nível ===')
    for k, v in item.items():
        val_str = str(v)
        if len(val_str) > 300:
            val_str = val_str[:300] + '...'
        print(f'  {k} ({type(v).__name__}): {val_str}')

    # Mostrar o XML embutido em expxml
    expxml = item.get('expxml', '')
    if expxml:
        print(f'\n=== expxml (XML embutido) ===')
        # Adicionar tag root porque o NCBI retorna fragmento
        print(f'<root>{expxml}</root>')

    runs = item.get('runs', '')
    if runs:
        print(f'\n=== runs (XML embutido) ===')
        print(f'<root>{runs}</root>')
"
```

**Anotar a saída.** Ela vai mostrar exatamente qual estrutura o parser precisa consumir.

### PASSO 3: Examinar o parser atual

```bash
# Ver o código completo do sra_parser.rs
cat rust_src/src/omics/sra_parser.rs

# Verificar como o SRA é chamado no lib.rs
grep -A 20 "sra" rust_src/src/lib.rs

# Verificar se o mod.rs exporta o sra_parser
cat rust_src/src/omics/mod.rs
```

### Correção do SRA Parser

Baseado na estrutura real da API do NCBI, o `sra_parser.rs` deve seguir este fluxo:

```
1. esearch(db=sra, term=query, retmax=max_per_source, retmode=json)
   → Vec<String> de UIDs numéricos (ex: ["18547231", "18547230", ...])

2. esummary(db=sra, id=uids.join(","), retmode=json)
   → JSON com result.<uid>.expxml contendo XML embutido como string

3. Para cada UID no resultado:
   a. Extrair campo "expxml" (é uma String contendo fragmento XML)
   b. Wrappear em <root>...</root> para tornar XML válido
   c. Parsear com quick-xml
   d. Extrair de dentro do XML:
      - <Study acc="SRP..." name="..." />         → accession, title
      - <Organism taxid="9606" ScientificName="Homo sapiens" />  → organism, tax_id
      - <Summary><Title>...</Title></Summary>      → title (fallback)
      - <Platform instrument_model="..." />        → platform
      - <Statistics total_runs="N" total_spots="M" total_bases="K" />  → n_samples
      - <Library_descriptor>
          <LIBRARY_STRATEGY>RNA-Seq</LIBRARY_STRATEGY>  → omic_subcategory
        </Library_descriptor>
      - <Bioproject>PRJNA...</Bioproject>           → bioproject_id (extra_metadata)
   e. Classificar omic_type via type_classifier usando título + strategy
   f. Construir OmicDatasetData {
        accession: study_acc (SRP...),
        source_db: "sra",
        title,
        summary: None (SRA não tem summary no esummary),
        omic_type,
        omic_subcategory: library_strategy,
        organism,
        tax_id,
        n_samples: total_runs como i32,
        platform: instrument_model,
        pub_date: createdate do campo JSON de primeiro nível,
        extra_metadata: { bioproject, total_bases, total_spots },
        linked_pmids: vec![],  // SRA não retorna PMIDs via esummary
      }

4. Se o campo "expxml" estiver vazio ou não existir:
   → Log warning e pular esse UID (não falhar silenciosamente retornando 0)
```

**Armadilhas comuns que causam 0 resultados:**

1. **O parser tenta `retmode=xml` no esummary do SRA** — A API retorna XML diferente do JSON. Usar `retmode=json` é mais previsível.

2. **O parser busca campos de primeiro nível como "title" ou "accession"** — No SRA, esses campos NÃO existem no primeiro nível do JSON. Estão DENTRO do `expxml`.

3. **O parser não trata o fragmento XML** — O `expxml` não é XML completo (não tem declaração nem root element). Precisa wrappear em `<root>...</root>` antes de parsear.

4. **O accession vem como Study acc (SRP...)** — Não confundir com Experiment (SRX) ou Run (SRR). Para o DaVinci, queremos o nível Study.

5. **Deduplicação por Study** — Múltiplos UIDs do esearch podem apontar para o mesmo SRP Study. Deduplicar pelo accession antes do COPY.

**Implementação sugerida em Rust:**

```rust
// rust_src/src/omics/sra_parser.rs

use quick_xml::Reader;
use quick_xml::events::Event;
use serde_json::Value;
use crate::ncbi::client::NcbiClient;
use crate::omics::models::OmicDatasetData;

pub async fn fetch_sra_datasets(
    client: &NcbiClient,
    query: &str,
    max: i64,
) -> Result<Vec<OmicDatasetData>, Box<dyn std::error::Error>> {
    // 1. esearch
    let ids = client.esearch("sra", query, max as usize).await?;
    if ids.is_empty() {
        return Ok(vec![]);
    }

    // 2. esummary em batches de 200
    let mut datasets = Vec::new();
    let mut seen_accessions = std::collections::HashSet::new();

    for chunk in ids.chunks(200) {
        let summary_json = client.esummary_json("sra", chunk).await?;
        let result = summary_json.get("result").unwrap_or(&Value::Null);

        for uid in chunk {
            let item = match result.get(uid) {
                Some(v) if v.is_object() => v,
                _ => continue,
            };

            // 3. Extrair expxml
            let expxml = match item.get("expxml").and_then(|v| v.as_str()) {
                Some(xml) if !xml.is_empty() => xml,
                _ => {
                    log::warn!("SRA UID {} has no expxml, skipping", uid);
                    continue;
                }
            };

            // 4. Parsear XML embutido
            let wrapped = format!("<root>{}</root>", expxml);
            match parse_sra_expxml(&wrapped) {
                Ok(mut dataset_data) => {
                    // Extrair createdate do JSON de primeiro nível
                    if let Some(date_str) = item.get("createdate").and_then(|v| v.as_str()) {
                        dataset_data.pub_date = parse_ncbi_date(date_str);
                    }

                    // Deduplicar por accession
                    if !dataset_data.accession.is_empty()
                        && seen_accessions.insert(dataset_data.accession.clone())
                    {
                        datasets.push(dataset_data);
                    }
                }
                Err(e) => {
                    log::warn!("Failed to parse SRA expxml for UID {}: {}", uid, e);
                    continue;
                }
            }
        }
    }

    Ok(datasets)
}

fn parse_sra_expxml(xml: &str) -> Result<OmicDatasetData, Box<dyn std::error::Error>> {
    let mut reader = Reader::from_str(xml);
    reader.config_mut().trim_text(true);

    let mut accession = String::new();
    let mut title = String::new();
    let mut organism = None;
    let mut tax_id = None;
    let mut platform = None;
    let mut n_samples = None;
    let mut library_strategy = None;
    let mut bioproject = None;
    let mut in_title = false;
    let mut in_bioproject = false;
    let mut in_library_strategy = false;

    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(ref e)) | Ok(Event::Empty(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "Study" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "acc" { accession = val; }
                            if key == "name" && title.is_empty() { title = val; }
                        }
                    }
                    "Organism" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "ScientificName" { organism = Some(val); }
                            if key == "taxid" { tax_id = Some(val); }
                        }
                    }
                    "Platform" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "instrument_model" { platform = Some(val); }
                        }
                    }
                    "Statistics" => {
                        for attr in e.attributes().flatten() {
                            let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
                            let val = String::from_utf8_lossy(&attr.value).to_string();
                            if key == "total_runs" {
                                n_samples = val.parse::<i32>().ok();
                            }
                        }
                    }
                    "Title" => { in_title = true; }
                    "Bioproject" => { in_bioproject = true; }
                    "LIBRARY_STRATEGY" => { in_library_strategy = true; }
                    _ => {}
                }
            }
            Ok(Event::Text(ref e)) => {
                let text = e.unescape().unwrap_or_default().to_string();
                if in_title && title.is_empty() {
                    title = text;
                    in_title = false;
                }
                if in_bioproject {
                    bioproject = Some(text);
                    in_bioproject = false;
                }
                if in_library_strategy {
                    library_strategy = Some(text);
                    in_library_strategy = false;
                }
            }
            Ok(Event::End(ref e)) => {
                let name = String::from_utf8_lossy(e.name().as_ref()).to_string();
                match name.as_str() {
                    "Title" => in_title = false,
                    "Bioproject" => in_bioproject = false,
                    "LIBRARY_STRATEGY" => in_library_strategy = false,
                    _ => {}
                }
            }
            Ok(Event::Eof) => break,
            Err(e) => return Err(Box::new(e)),
            _ => {}
        }
        buf.clear();
    }

    if accession.is_empty() {
        return Err("No Study accession found in expxml".into());
    }

    // Classificar omic_type baseado na library_strategy
    let (omic_type, omic_subcategory) = classify_from_strategy(
        library_strategy.as_deref(), &title
    );

    let mut extra_metadata = serde_json::Map::new();
    if let Some(bp) = &bioproject {
        extra_metadata.insert("bioproject".into(), Value::String(bp.clone()));
    }

    Ok(OmicDatasetData {
        accession,
        source_db: "sra".to_string(),
        title,
        summary: None, // SRA esummary não tem summary
        omic_type,
        omic_subcategory,
        organism,
        tax_id,
        n_samples,
        platform,
        pub_date: None, // Preenchido no caller
        extra_metadata: Value::Object(extra_metadata),
        linked_pmids: vec![],
    })
}

fn classify_from_strategy(strategy: Option<&str>, title: &str) -> (String, Option<String>) {
    match strategy {
        Some(s) => {
            let s_upper = s.to_uppercase();
            let omic_type = if s_upper.contains("RNA") {
                "transcriptomic"
            } else if s_upper.contains("WGS") || s_upper.contains("WXS")
                   || s_upper.contains("WHOLE GENOME") || s_upper.contains("EXOME") {
                "genomic"
            } else if s_upper.contains("CHIP") || s_upper.contains("ATAC")
                   || s_upper.contains("BISULFITE") || s_upper.contains("METHYLAT") {
                "epigenomic"
            } else if s_upper.contains("AMPLICON") || s_upper.contains("16S")
                   || s_upper.contains("METAGENOM") {
                "microbiome"
            } else {
                "other"
            };
            (omic_type.to_string(), Some(s.to_string()))
        }
        None => {
            // Fallback: usar type_classifier padrão no título
            ("other".to_string(), None)
        }
    }
}
```

### Teste do SRA Parser

Criar fixture e teste unitário:

```bash
# 1. Salvar um exemplo real da API como fixture
python3 -c "
import requests, json

r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
    'db': 'sra', 'term': 'cardiovascular disease', 'retmax': 3, 'retmode': 'json'
}, timeout=30)
ids = r.json()['esearchresult']['idlist']

r2 = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi', params={
    'db': 'sra', 'id': ','.join(ids), 'retmode': 'json'
}, timeout=30)

with open('rust_src/tests/fixtures/sample_sra_esummary.json', 'w') as f:
    json.dump(r2.json(), f, indent=2)
print(f'Fixture salva com {len(ids)} UIDs')
"
```

```rust
// rust_src/tests/test_sra_parser.rs

#[test]
fn test_parse_sra_expxml_basic() {
    let xml = r#"<root>
        <Study acc="SRP123456" name="Cardiovascular RNA-Seq Study"/>
        <Organism taxid="9606" ScientificName="Homo sapiens"/>
        <Summary>
            <Title>RNA-Seq analysis of heart tissue</Title>
            <Platform instrument_model="Illumina HiSeq 2500"/>
            <Statistics total_runs="24" total_spots="500000000" total_bases="75000000000"/>
        </Summary>
        <Library_descriptor>
            <LIBRARY_STRATEGY>RNA-Seq</LIBRARY_STRATEGY>
        </Library_descriptor>
        <Bioproject>PRJNA123456</Bioproject>
    </root>"#;

    let result = parse_sra_expxml(xml).unwrap();
    assert_eq!(result.accession, "SRP123456");
    assert_eq!(result.source_db, "sra");
    assert_eq!(result.organism, Some("Homo sapiens".to_string()));
    assert_eq!(result.tax_id, Some("9606".to_string()));
    assert_eq!(result.omic_type, "transcriptomic");
    assert_eq!(result.omic_subcategory, Some("RNA-Seq".to_string()));
    assert_eq!(result.n_samples, Some(24));
}

#[test]
fn test_parse_sra_expxml_empty_returns_error() {
    let xml = "<root></root>";
    assert!(parse_sra_expxml(xml).is_err());
}

#[test]
fn test_parse_sra_expxml_wgs() {
    let xml = r#"<root>
        <Study acc="SRP999999" name="WGS of cardiac patients"/>
        <Organism taxid="9606" ScientificName="Homo sapiens"/>
        <Library_descriptor>
            <LIBRARY_STRATEGY>WGS</LIBRARY_STRATEGY>
        </Library_descriptor>
    </root>"#;

    let result = parse_sra_expxml(xml).unwrap();
    assert_eq!(result.omic_type, "genomic");
    assert_eq!(result.omic_subcategory, Some("WGS".to_string()));
}
```

### Validação após correção

```bash
cd rust_src
cargo test sra -- --nocapture
maturin develop --release
cd ..

python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *

db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})

result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id), query='cardiovascular disease',
    db_url=db_url, project_id=str(project.id),
    sources=['sra'], max_per_source=20,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None), synonyms=[],
)
print(f'SRA resultado: {result}')

count = OmicDataset.objects.filter(source_db='sra').count()
print(f'SRA datasets no banco: {count}')
assert count > 0, 'FALHA: SRA ainda retorna 0 datasets'
print('✅ SRA OK')
"
```

---

## PROBLEMA 2 — DatasetPaperLinks sempre 0

### Contexto

Nenhum link dataset↔paper foi criado para nenhuma fonte (GEO, SRA, BioProject, GWAS). O diagnóstico mostrou `DatasetPaperLinks: 0` com 703 datasets e 7132 papers no banco. As relações existem (papers citam datasets e vice-versa) mas não estão sendo descobertas ou persistidas.

### Investigação — Executar ANTES de corrigir

```bash
# PASSO 1: Verificar se o elink está implementado
echo "=== Verificar elink no código Rust ==="
grep -rn "elink" rust_src/src/ --include="*.rs"

# PASSO 2: Verificar se é chamado no fluxo de ingestão
echo -e "\n=== Chamada de elink no fluxo ômico (lib.rs) ==="
grep -B5 -A15 "elink\|discover_links\|dataset_paper_link\|copy_dataset_paper" rust_src/src/lib.rs

# PASSO 3: Verificar se copy_dataset_paper_links existe
echo -e "\n=== copy_dataset_paper_links no copy_writer ==="
grep -B5 -A30 "dataset_paper_link\|DatasetPaperLink" rust_src/src/db/copy_writer.rs

# PASSO 4: Verificar a struct de link
echo -e "\n=== DatasetPaperLinkData struct ==="
grep -B3 -A10 "DatasetPaperLink" rust_src/src/omics/models.rs rust_src/src/ncbi/models.rs 2>/dev/null

# PASSO 5: Testar elink manualmente
python3 -c "
import requests

# Pegar IDs de GEO datasets que sabemos que existem
r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi', params={
    'db': 'gds', 'term': 'cardiovascular disease', 'retmax': 5, 'retmode': 'json'
}, timeout=30)
gds_ids = r.json()['esearchresult']['idlist']
print(f'GDS IDs para testar: {gds_ids}')

if gds_ids:
    # elink: gds → pubmed
    r2 = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi', params={
        'dbfrom': 'gds',
        'db': 'pubmed',
        'id': gds_ids[0],
        'retmode': 'json',
    }, timeout=30)
    data = r2.json()
    print(f'elink response para UID {gds_ids[0]}:')

    linksets = data.get('linksets', [])
    for ls in linksets:
        for lsdb in ls.get('linksetdbs', []):
            links = lsdb.get('links', [])
            print(f'  {lsdb.get(\"linkname\", \"?\")} → {len(links)} PMIDs: {links[:5]}')

    if not linksets or not any(ls.get('linksetdbs', []) for ls in linksets):
        print('  NENHUM link encontrado via elink')
        print('  → elink pode não retornar links para todos os GDS IDs')
        print('  → Alternativa: extrair PMIDs do esummary do GEO (campo PubMedIds)')
"
```

### Causas prováveis (avaliar após investigação)

**Causa A — elink não está sendo chamado no fluxo:**
O `search_and_ingest_omics` no `lib.rs` pode fazer `copy_omic_datasets` mas pular a chamada de elink/`copy_dataset_paper_links`. Verificar se a função de elink está conectada ao pipeline.

**Causa B — elink é chamado mas FK resolution falha:**
O `copy_dataset_paper_links` precisa resolver `accession → dataset.id` e `pmid → paper.id`. Se os PMIDs retornados pelo elink não existem na tabela `core_paper`, o JOIN falha silenciosamente e 0 links são criados.

**Causa C — GDS IDs vs GSE accessions:**
O esearch do GEO retorna GDS IDs numéricos (ex: "200012345"). O elink precisa do GDS ID, não do GSE accession. Mas o `copy_dataset_paper_links` precisa do `dataset.id` do Postgres, que é resolvido via `accession` (GSE). O mapeamento GDS → GSE pode estar faltando.

**Causa D — O campo linked_pmids do OmicDatasetData não está sendo preenchido:**
Cada parser ômico pode ter um campo `linked_pmids: Vec<String>` no `OmicDatasetData`. Se esse campo nunca é populado, os links nunca são gerados.

### Correção

Existem duas abordagens para criar links, e ambas devem ser implementadas:

#### Abordagem 1 — Extrair PMIDs do esummary do GEO (mais confiável)

O GEO esummary já contém um campo `PubMedIds` com a lista de PMIDs associados. Não precisa de elink separado.

```bash
# Verificar se o geo_parser já extrai PubMedIds
grep -n "PubMedIds\|pubmed\|pmid\|linked" rust_src/src/omics/geo_parser.rs
```

Se não extrai, adicionar ao `geo_parser.rs`:
```rust
// No parsing do esummary do GEO, extrair:
// result.<uid>.PubMedIds → Vec<String> de PMIDs
// Colocar em dataset_data.linked_pmids = pmids;
```

#### Abordagem 2 — elink batch (complementar)

Para datasets que não têm PMIDs no esummary, usar elink como fallback:

```rust
// Após coletar todos os datasets de uma fonte:
// 1. Filtrar datasets que têm linked_pmids vazio
// 2. Coletar os IDs numéricos (do esearch, não accessions)
// 3. Chamar elink em batches de 100:
//    elink.fcgi?dbfrom=gds&db=pubmed&id=ID1,ID2,...&retmode=json
// 4. Parsear os links do JSON:
//    linksets[i].linksetdbs[j].links → Vec<String> de PMIDs
// 5. Mapear de volta para o dataset correto via UID

// CUIDADO: O elink retorna GDS UID → PMID
// O copy_dataset_paper_links precisa de dataset_id → paper_id
// A resolução é:
//   GDS UID → accession (manter mapa UID→accession durante o parsing)
//   accession → dataset.id (query no Postgres)
//   PMID → paper.id (query no Postgres, PMIDs já devem existir)
```

#### Persistência dos links (copy_writer)

Verificar que `copy_dataset_paper_links` faz a resolução de FK corretamente:

```rust
// rust_src/src/db/copy_writer.rs

pub async fn copy_dataset_paper_links(
    links: &[(String, String, String)], // (accession, pmid, link_source)
    conn: &Client,
) -> Result<usize> {
    if links.is_empty() {
        return Ok(0);
    }

    // Resolver accessions → dataset IDs
    let accessions: Vec<&str> = links.iter().map(|l| l.0.as_str()).collect();
    let dataset_map: HashMap<String, i64> = conn
        .query(
            "SELECT accession, id FROM core_omicdataset WHERE accession = ANY($1)",
            &[&accessions],
        )
        .await?
        .iter()
        .map(|row| (row.get::<_, String>(0), row.get::<_, i64>(1)))
        .collect();

    // Resolver PMIDs → paper IDs
    let pmids: Vec<&str> = links.iter().map(|l| l.1.as_str()).collect();
    let paper_map: HashMap<String, i64> = conn
        .query(
            "SELECT pmid, id FROM core_paper WHERE pmid = ANY($1)",
            &[&pmids],
        )
        .await?
        .iter()
        .map(|row| (row.get::<_, String>(0), row.get::<_, i64>(1)))
        .collect();

    // Inserir links que têm ambos os FKs resolvidos
    let mut count = 0;
    for (accession, pmid, source) in links {
        if let (Some(&did), Some(&pid)) = (dataset_map.get(accession), paper_map.get(pmid)) {
            let result = conn
                .execute(
                    "INSERT INTO core_datasetpaperlink (dataset_id, paper_id, link_source)
                     VALUES ($1, $2, $3)
                     ON CONFLICT (dataset_id, paper_id) DO NOTHING",
                    &[&did, &pid, &source],
                )
                .await?;
            count += result as usize;
        }
    }

    Ok(count)
}
```

#### Conectar no pipeline (lib.rs)

Verificar que `search_and_ingest_omics` chama `copy_dataset_paper_links` APÓS tanto datasets quanto papers existirem no banco:

```rust
// Em search_and_ingest_omics(), APÓS inserir todos os datasets de todas as fontes:

// Coletar todos os linked_pmids de todos os datasets
let mut all_links: Vec<(String, String, String)> = Vec::new();
for dataset in &all_datasets {
    let source = if !dataset.linked_pmids.is_empty() {
        "geo_xml" // ou "esummary"
    } else {
        continue;
    };
    for pmid in &dataset.linked_pmids {
        all_links.push((
            dataset.accession.clone(),
            pmid.clone(),
            source.to_string(),
        ));
    }
}

// Persistir
let links_inserted = copy_dataset_paper_links(&all_links, &conn).await?;
log::info!("DatasetPaperLinks inserted: {}", links_inserted);
```

### Validação dos links

```bash
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *

db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})

# Rodar GEO com max baixo para teste rápido
result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id), query='breast cancer',  # Query com muitos links conhecidos
    db_url=db_url, project_id=str(project.id),
    sources=['geo'], max_per_source=20,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None), synonyms=[],
)
print(f'GEO resultado: {result}')

links = DatasetPaperLink.objects.count()
print(f'DatasetPaperLinks total: {links}')
assert links > 0, 'FALHA: DatasetPaperLinks ainda é 0'

# Mostrar exemplos
for link in DatasetPaperLink.objects.select_related('dataset', 'paper')[:5]:
    print(f'  {link.dataset.accession} ↔ {link.paper.pmid} (via {link.link_source})')
print('✅ DatasetPaperLinks OK')
"
```

---

## PROBLEMA 3 — NER com dicionários hardcoded (6 genes, 6 drogas)

### Contexto

O NER atual tem apenas 6 genes (BRCA1, TP53, EGFR, TNF, IL6, BRAF) e 6 drogas hardcoded. Com 7132 papers no banco, apenas 52 `PaperGene` foram criados. Isso faz o DaVinci parecer vazio em análises genômicas. Para ser útil em revisões sistemáticas, precisa de dicionários com milhares de entidades.

### Abordagem: Arquivos de dicionário embutidos no binário Rust

Usar `include_str!()` para compilar os dicionários diretamente no binário — sem dependência de arquivos externos em runtime.

### PASSO 1 — Baixar dicionários de referência

```bash
# Criar diretório de dados
mkdir -p rust_src/data

# ========================================
# GENES — HGNC (Hugo Gene Nomenclature Committee)
# ========================================
# Fonte oficial: ~43.000 gene symbols humanos aprovados
curl -sL "https://ftp.ebi.ac.uk/pub/databases/genenames/hgnc/tsv/hgnc_complete_set.txt" \
    -o /tmp/hgnc_complete.txt

# Extrair apenas os symbols aprovados (coluna 2)
# Filtrar: mínimo 2 caracteres, apenas letras/números/hifens
awk -F'\t' 'NR>1 && length($2)>=2 {print $2}' /tmp/hgnc_complete.txt \
    | grep -E '^[A-Za-z][A-Za-z0-9-]+$' \
    | sort -u > rust_src/data/gene_symbols.txt

echo "Genes: $(wc -l < rust_src/data/gene_symbols.txt) symbols"

# Adicionar aliases comuns (Previous Symbols, coluna 11)
awk -F'\t' 'NR>1 && $11!="" {
    n=split($11, a, "|");
    for(i=1;i<=n;i++) {
        gsub(/^ +| +$/, "", a[i]);
        if(length(a[i])>=2) print a[i]
    }
}' /tmp/hgnc_complete.txt \
    | grep -E '^[A-Za-z][A-Za-z0-9-]+$' \
    | sort -u >> rust_src/data/gene_symbols.txt

# Deduplicar após merge
sort -u -o rust_src/data/gene_symbols.txt rust_src/data/gene_symbols.txt
echo "Genes com aliases: $(wc -l < rust_src/data/gene_symbols.txt) symbols"

# ========================================
# DROGAS — DrugBank Vocabulary (open access)
# ========================================
# DrugBank open vocabulary: ~16.000 nomes de drogas
# Alternativa gratuita sem necessidade de conta:
python3 -c "
import requests, csv, io

# Usar o vocabulário do ChEMBL (gratuito, sem login)
# Alternativa: WHO INN list, RxNorm, ou simplesmente um dicionário curado
# Aqui uso uma lista curada das drogas mais relevantes em biomedicina

# Baixar do DrugBank open vocabularies (CC BY-NC 4.0)
url = 'https://go.drugbank.com/releases/latest/downloads/all-drug-links'

# Como DrugBank requer login, vamos usar uma abordagem alternativa:
# MeSH Pharmacological Actions + MeSH Supplementary Concept Records
print('Gerando lista de drogas a partir de fontes abertas...')

# Approach: usar nomes de drogas do PubChem (domínio público)
# Via PubChem REST: top 15000 compounds com nomes farmacêuticos
# Para simplificar, vamos baixar uma lista curada

# Lista curada de ~3000 drogas comuns em literatura biomédica
# Fonte: WHO Essential Medicines List + FDA Approved Drugs
drugs = set()

# FDA Drugs@FDA - lista pública
r = requests.get('https://api.fda.gov/drug/drugsfda.json?limit=1000', timeout=60)
if r.status_code == 200:
    for result in r.json().get('results', []):
        for product in result.get('products', []):
            name = product.get('brand_name', '')
            if name and len(name) >= 3:
                drugs.add(name.lower())
            active = product.get('active_ingredients', [])
            for ai in active:
                name = ai.get('name', '')
                if name and len(name) >= 3:
                    drugs.add(name.lower())
    print(f'  FDA: {len(drugs)} drogas')

# Adicionar drogas de oncologia, cardiologia, etc. comuns
common_drugs = '''
metformin,atorvastatin,lisinopril,amlodipine,metoprolol,omeprazole,
losartan,simvastatin,levothyroxine,gabapentin,sertraline,aspirin,
acetaminophen,ibuprofen,amoxicillin,azithromycin,prednisone,
albuterol,montelukast,fluticasone,insulin,warfarin,clopidogrel,
rosuvastatin,tamsulosin,pantoprazole,escitalopram,duloxetine,
venlafaxine,trazodone,tramadol,oxycodone,hydrocodone,morphine,
ciprofloxacin,doxycycline,clindamycin,vancomycin,meropenem,
pembrolizumab,nivolumab,atezolizumab,ipilimumab,trastuzumab,
bevacizumab,rituximab,cetuximab,panitumumab,erlotinib,gefitinib,
osimertinib,crizotinib,alectinib,imatinib,dasatinib,nilotinib,
sorafenib,sunitinib,pazopanib,lenvatinib,cabozantinib,axitinib,
everolimus,temsirolimus,olaparib,rucaparib,niraparib,talazoparib,
venetoclax,ibrutinib,acalabrutinib,idelalisib,doxorubicin,
paclitaxel,docetaxel,cisplatin,carboplatin,oxaliplatin,
gemcitabine,capecitabine,fluorouracil,temozolomide,cyclophosphamide,
methotrexate,azathioprine,mycophenolate,tacrolimus,cyclosporine,
sirolimus,adalimumab,infliximab,etanercept,tocilizumab,baricitinib,
tofacitinib,upadacitinib,secukinumab,ustekinumab,dupilumab,
enoxaparin,heparin,rivaroxaban,apixaban,dabigatran,edoxaban,
digoxin,amiodarone,flecainide,sotalol,diltiazem,verapamil,
furosemide,hydrochlorothiazide,spironolactone,eplerenone,
sacubitril,empagliflozin,dapagliflozin,canagliflozin,
semaglutide,liraglutide,dulaglutide,exenatide,sitagliptin,
pioglitazone,glimepiride,glyburide,acarbose,repaglinide
'''.strip().replace('\n', '')

for d in common_drugs.split(','):
    d = d.strip()
    if d:
        drugs.add(d.lower())

print(f'  Total: {len(drugs)} drogas')

with open('rust_src/data/drug_names.txt', 'w') as f:
    for d in sorted(drugs):
        f.write(d + '\n')
print(f'  Salvo em rust_src/data/drug_names.txt')
" 2>&1
echo "Drogas: $(wc -l < rust_src/data/drug_names.txt) nomes"
```

### PASSO 2 — Implementar Gene NER expandido

```rust
// rust_src/src/categorization/gene_ner.rs

use std::collections::{HashMap, HashSet};
use once_cell::sync::Lazy;
use regex::Regex;

/// Dicionário de gene symbols carregado em tempo de compilação
static GENE_SYMBOLS: Lazy<HashSet<String>> = Lazy::new(|| {
    include_str!("../../data/gene_symbols.txt")
        .lines()
        .filter(|l| !l.is_empty() && l.len() >= 2)
        .map(|l| l.trim().to_uppercase())
        .collect()
});

/// Palavras comuns em inglês que são homônimas de gene symbols
/// Ex: "NOT", "WAS", "CAN", "SET", "MAP", "GAP", "REST", "CAMP"
static FALSE_POSITIVES: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "NOT", "WAS", "CAN", "SET", "MAP", "GAP", "REST", "CAMP", "ACE",
        "SHE", "HER", "HIS", "ALL", "FOR", "ARE", "THE", "AND", "BUT",
        "HAD", "HAS", "LET", "MAY", "RAN", "SAT", "SAW", "PUT", "GOT",
        "DID", "TOP", "END", "AGE", "BAD", "BIG", "CUT", "FAT", "FIT",
        "HOT", "LOW", "MET", "NEW", "OLD", "RED", "RUN", "TEN", "TIE",
        "USE", "WIN", "AIM", "AIR", "ARM", "ART", "BAR", "BIT", "BOX",
        "BUS", "CAR", "COP", "DAD", "DAM", "DOT", "DRY", "DUE", "EAR",
        "EAT", "EGG", "ERA", "EVE", "EYE", "FAN", "FAR", "FEW", "FIG",
        "FIN", "FIX", "FLY", "FOG", "FOX", "FUN", "FUR", "GAS", "GEL",
        "GUM", "GUN", "GUT", "GYM", "HAM", "HAT", "HIT", "HOP", "HUB",
        "ICE", "ILL", "INK", "ION", "JAM", "JAR", "JAW", "JET", "JOB",
        "JOG", "JOY", "KEY", "KID", "KIT", "LAB", "LAP", "LAW", "LAY",
        "LED", "LEG", "LID", "LIP", "LOG", "LOT", "MAD", "MAN", "MAT",
        "MIX", "MOB", "MOM", "MUD", "MUG", "NAP", "NET", "NIT", "NOD",
        "NOR", "NUN", "NUT", "OAK", "OAR", "ODD", "OIL", "ONE", "OPT",
        "ORB", "ORE", "OWE", "OWL", "OWN", "PAD", "PAN", "PAT", "PAW",
        "PEA", "PEN", "PET", "PIE", "PIG", "PIN", "PIT", "PLY", "POD",
        "POP", "POT", "PUB", "PUP", "RAG", "RAM", "RAP", "RAT", "RAW",
        "RAY", "RIB", "RIG", "RIM", "RIP", "ROB", "ROD", "ROT", "ROW",
        "RUB", "RUG", "RUM", "SAD", "SAP", "SIR", "SIS", "SIT", "SIX",
        "SKI", "SKY", "SLY", "SOB", "SOD", "SON", "SOP", "SOW", "SOY",
        "SPA", "SPY", "STY", "SUB", "SUM", "SUN", "TAB", "TAG", "TAN",
        "TAP", "TAR", "TAX", "TEA", "TIN", "TIP", "TOE", "TON", "TOO",
        "TOW", "TOY", "TUB", "TUG", "TWO", "URN", "VAN", "VET", "VIA",
        "VOW", "WAR", "WAX", "WAY", "WEB", "WED", "WET", "WIG", "WIT",
        "WOE", "WOK", "WON", "WOO", "WOW", "YAM", "YAP", "YAW", "YES",
        "YET", "YEW", "ZAP", "ZEN", "ZIP", "ZIT", "ZOO",
        // Termos genéricos biomédicos que são gene symbols mas confusos:
        "CELL", "GENE", "DRUG", "DOSE", "RISK", "CARE", "CASE", "DATA",
        "DIET", "FAST", "FISH", "FOOD", "HAND", "HEAD", "HEAR", "HELP",
        "HIGH", "HOME", "HOPE", "HOST", "HOUR", "IRON", "LACK", "LAST",
        "LATE", "LEAD", "LEFT", "LESS", "LIFE", "LIKE", "LINE", "LINK",
        "LIST", "LIVE", "LONG", "LOOK", "LOOP", "LOSS", "LOST", "LUNG",
        "MADE", "MAIN", "MAKE", "MALE", "MANY", "MARK", "MASS", "MEAN",
        "MILD", "MILK", "MIND", "MINE", "MODE", "MORE", "MOST", "MUCH",
        "MUST", "NAME", "NEAR", "NEED", "NEXT", "NINE", "NODE", "NONE",
        "NORM", "NOSE", "NOTE", "ODDS", "ONCE", "ONLY", "OPEN", "ORAL",
        "OVER", "PACE", "PACK", "PAGE", "PAID", "PAIN", "PAIR", "PALE",
        "PALM", "PART", "PASS", "PAST", "PATH", "PEAK", "PEER", "PICK",
        "PILL", "PLAN", "PLAY", "PLOT", "PLUS", "POLL", "POOL", "POOR",
        "PORT", "POSE", "POST", "POUR", "PULL", "PUMP", "PURE", "PUSH",
        "RACE", "RANK", "RARE", "RATE", "READ", "REAL", "REAR", "RICE",
        "RICH", "RIDE", "RING", "RISE", "ROLE", "ROLL", "ROOT", "ROPE",
        "ROSE", "RULE", "RUSH", "SAFE", "SAID", "SAKE", "SALE", "SALT",
        "SAME", "SAND", "SAVE", "SCAN", "SEAL", "SEAT", "SEED", "SEEK",
        "SELF", "SEND", "SEPT", "SHIP", "SHOP", "SHOT", "SHOW", "SHUT",
        "SICK", "SIDE", "SIGN", "SILK", "SITE", "SIZE", "SKIN", "SLIP",
        "SLOT", "SLOW", "SNAP", "SNOW", "SOLE", "SOME", "SONG", "SOON",
        "SORT", "SOUL", "SPIN", "SPOT", "STAR", "STAY", "STEM", "STEP",
        "STOP", "SUCH", "SUIT", "SURE", "SWIM", "TAIL", "TAKE", "TALE",
        "TALK", "TALL", "TANK", "TAPE", "TASK", "TEAM", "TEAR", "TELL",
        "TERM", "TEST", "TEXT", "THAN", "THAT", "THEM", "THEN", "THEY",
        "THIN", "THIS", "THUS", "TIED", "TILL", "TIME", "TINY", "TIRE",
        "TOLD", "TOLL", "TONE", "TOOK", "TOOL", "TORN", "TOUR", "TOWN",
        "TRAP", "TREE", "TRIM", "TRIP", "TRUE", "TUBE", "TUCK", "TUNE",
        "TURN", "TWIN", "TYPE", "UPON", "USED", "USER", "VALE", "VARY",
        "VAST", "VERY", "VIEW", "VINE", "VOID", "VOTE", "WAGE", "WAIT",
        "WAKE", "WALK", "WALL", "WANT", "WARD", "WARM", "WARN", "WASH",
        "WAVE", "WEAK", "WEAR", "WEEK", "WELL", "WENT", "WERE", "WEST",
        "WHAT", "WHEN", "WHOM", "WIDE", "WIFE", "WILD", "WILL", "WIND",
        "WINE", "WING", "WIRE", "WISE", "WISH", "WITH", "WOKE", "WOLF",
        "WOOD", "WOOL", "WORD", "WORE", "WORK", "WORM", "WORN", "WRAP",
        "YARD", "YEAR", "YOUR", "ZERO", "ZONE",
    ].iter().cloned().collect()
});

/// Padrão para contexto: gene symbol aparece em contexto biomédico
static GENE_CONTEXT_REGEX: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)\b(gene|protein|express|mutat|variant|allele|locus|transcript|mRNA|encod|regulat|pathway|receptor|kinase|inhibit|activat|phosphoryl|knockout|knockdown|overexpress|silenc|polymorphism|SNP|amplif|delet|fusion|transloc|promoter|enhancer|exon|intron|domain|isoform)\b").unwrap()
});

#[derive(Debug, Clone)]
pub struct GeneData {
    pub gene_symbol: String,
    pub entrez_id: Option<String>,
    pub mention_count: i32,
}

pub fn extract_genes(abstract_text: &str) -> Vec<GeneData> {
    if abstract_text.is_empty() {
        return vec![];
    }

    let has_bio_context = GENE_CONTEXT_REGEX.is_match(abstract_text);

    // Tokenizar: separar por qualquer coisa que não seja alfanumérico ou hífen
    let mut gene_counts: HashMap<String, i32> = HashMap::new();

    for word in abstract_text.split(|c: char| !c.is_alphanumeric() && c != '-') {
        if word.len() < 2 || word.len() > 15 {
            continue;
        }

        let upper = word.to_uppercase();

        // Filtrar false positives
        if FALSE_POSITIVES.contains(upper.as_str()) {
            continue;
        }

        // Verificar se é um gene symbol conhecido
        if GENE_SYMBOLS.contains(&upper) {
            // Para symbols curtos (2-3 letras), exigir contexto biomédico
            if upper.len() <= 3 && !has_bio_context {
                continue;
            }

            // Para symbols de 2 letras, exigir que esteja em UPPERCASE no texto original
            if upper.len() == 2 && word != upper {
                continue;
            }

            *gene_counts.entry(upper).or_insert(0) += 1;
        }
    }

    gene_counts
        .into_iter()
        .map(|(symbol, count)| GeneData {
            gene_symbol: symbol,
            entrez_id: None,
            mention_count: count,
        })
        .collect()
}
```

### PASSO 3 — Implementar Drug NER expandido

```rust
// rust_src/src/categorization/drug_ner.rs

use std::collections::{HashMap, HashSet};
use once_cell::sync::Lazy;

static DRUG_NAMES: Lazy<HashSet<String>> = Lazy::new(|| {
    include_str!("../../data/drug_names.txt")
        .lines()
        .filter(|l| !l.is_empty() && l.len() >= 3)
        .map(|l| l.trim().to_lowercase())
        .collect()
});

#[derive(Debug, Clone)]
pub struct DrugData {
    pub drug_name: String,
    pub drug_name_lower: String,
    pub mention_count: i32,
    pub drugbank_id: Option<String>,
}

pub fn extract_drugs(abstract_text: &str) -> Vec<DrugData> {
    if abstract_text.is_empty() {
        return vec![];
    }

    let text_lower = abstract_text.to_lowercase();
    let mut drug_counts: HashMap<String, (String, i32)> = HashMap::new();

    // Abordagem: buscar cada droga do dicionário no texto
    // Para dicionários grandes (>1000), usar Aho-Corasick seria mais eficiente
    // mas para MVP, iteração simples funciona

    // Tokenização por palavras para drogas de uma palavra
    let words: Vec<&str> = text_lower
        .split(|c: char| !c.is_alphanumeric() && c != '-')
        .filter(|w| w.len() >= 3)
        .collect();

    for word in &words {
        if DRUG_NAMES.contains(*word) {
            let entry = drug_counts
                .entry(word.to_string())
                .or_insert_with(|| (word.to_string(), 0));
            entry.1 += 1;
        }
    }

    // Busca de drogas com nome composto (ex: "vitamin d", "folic acid")
    // Apenas para nomes que contêm espaço no dicionário
    for drug_name in DRUG_NAMES.iter() {
        if drug_name.contains(' ') && text_lower.contains(drug_name.as_str()) {
            let count = text_lower.matches(drug_name.as_str()).count() as i32;
            drug_counts
                .entry(drug_name.clone())
                .or_insert_with(|| (drug_name.clone(), count));
        }
    }

    drug_counts
        .into_iter()
        .map(|(_, (name, count))| DrugData {
            drug_name: name.clone(),
            drug_name_lower: name.to_lowercase(),
            mention_count: count,
            drugbank_id: None,
        })
        .collect()
}
```

### PASSO 4 — Adicionar dependência `once_cell` ao Cargo.toml

```bash
# Verificar se once_cell já está no Cargo.toml
grep "once_cell" rust_src/Cargo.toml

# Se não estiver, adicionar:
# [dependencies]
# once_cell = "1"
```

Se já estiver usando Rust 1.80+, pode usar `std::sync::LazyLock` em vez de `once_cell::sync::Lazy` (estabilizado na std).

### PASSO 5 — Testes do NER

```rust
// rust_src/tests/test_gene_ner.rs

#[test]
fn test_extract_known_genes() {
    let text = "We found that BRCA1 and TP53 mutations were associated with \
                increased risk. EGFR expression was elevated in tumor samples. \
                The BRCA1 gene was also linked to DNA repair pathways.";
    let genes = extract_genes(text);
    let symbols: Vec<&str> = genes.iter().map(|g| g.gene_symbol.as_str()).collect();
    assert!(symbols.contains(&"BRCA1"));
    assert!(symbols.contains(&"TP53"));
    assert!(symbols.contains(&"EGFR"));
    // BRCA1 aparece 2x
    let brca1 = genes.iter().find(|g| g.gene_symbol == "BRCA1").unwrap();
    assert_eq!(brca1.mention_count, 2);
}

#[test]
fn test_no_false_positives_for_common_words() {
    let text = "The patient was not able to set a new goal for the race. \
                This case was very rare and the risk was high.";
    let genes = extract_genes(text);
    // NOT, SET, WAS, CASE, RARE, RISK, HIGH são false positives
    assert!(genes.is_empty(), "Found false positives: {:?}", genes);
}

#[test]
fn test_short_gene_needs_bio_context() {
    // IL6 (3 letras) em contexto biomédico → deve encontrar
    let text = "IL6 expression was upregulated in the inflammatory pathway.";
    let genes = extract_genes(text);
    assert!(genes.iter().any(|g| g.gene_symbol == "IL6"));

    // IL6 (3 letras) sem contexto → NÃO deve encontrar (mas esse cenário é raro)
    // Na prática, abstracts biomédicos sempre têm contexto
}

#[test]
fn test_extract_drugs() {
    let text = "Patients received metformin 500mg twice daily and atorvastatin 20mg. \
                The metformin group showed improved glucose control.";
    let drugs = extract_drugs(text);
    let names: Vec<&str> = drugs.iter().map(|d| d.drug_name.as_str()).collect();
    assert!(names.contains(&"metformin"));
    assert!(names.contains(&"atorvastatin"));
    let met = drugs.iter().find(|d| d.drug_name == "metformin").unwrap();
    assert_eq!(met.mention_count, 2);
}

#[test]
fn test_drug_names_loaded() {
    assert!(DRUG_NAMES.len() > 100, "Drug dictionary too small: {}", DRUG_NAMES.len());
}

#[test]
fn test_gene_symbols_loaded() {
    assert!(GENE_SYMBOLS.len() > 10000, "Gene dictionary too small: {}", GENE_SYMBOLS.len());
    assert!(GENE_SYMBOLS.contains("BRCA1"));
    assert!(GENE_SYMBOLS.contains("TP53"));
    assert!(GENE_SYMBOLS.contains("EGFR"));
}
```

### PASSO 6 — Recompilar e validar

```bash
cd rust_src
cargo test -- --nocapture
maturin develop --release
cd ..

# Testar NER expandido em dados reais
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *

# Contar genes/drogas antes
genes_before = PaperGene.objects.count()
drugs_before = PaperDrug.objects.count()
print(f'Antes: {genes_before} genes, {drugs_before} drugs')

# Rodar ingestão com query pequena para testar NER
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='pubmed_search', parameters={})

result = rust_engine.search_and_ingest_pubmed(
    job_id=str(job.id), query='BRCA1 breast cancer treatment',
    db_url=db_url, project_id=str(project.id),
    date_from=2024, date_to=2025,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None),
)
print(f'PubMed: {result.records_inserted} papers')

genes_after = PaperGene.objects.count()
drugs_after = PaperDrug.objects.count()
print(f'Depois: {genes_after} genes (+{genes_after - genes_before}), {drugs_after} drugs (+{drugs_after - drugs_before})')

# Top genes
from django.db.models import Sum
top = PaperGene.objects.values('gene_symbol').annotate(
    total=Sum('mention_count')
).order_by('-total')[:20]
print(f'\nTop 20 genes:')
for g in top:
    print(f'  {g[\"gene_symbol\"]}: {g[\"total\"]} menções')

assert genes_after > genes_before, 'NER expandido não produziu novos genes'
print('\n✅ NER expandido OK')
"
```

---

## PASSO 7 — Re-processar papers existentes com NER expandido

Após expandir o NER, os 7132 papers já no banco não têm os novos genes/drogas extraídos. Para re-processar:

```bash
# Opção A: Se existe uma função Rust para NER standalone
python manage.py shell -c "
import rust_engine
# Se existir extract_genes_from_abstracts:
if hasattr(rust_engine, 'extract_genes_from_abstracts'):
    from apps.core.models import DaVinciProject, IngestionJob
    from django.conf import settings
    db = settings.DATABASES['default']
    db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
    project = DaVinciProject.objects.first()
    job = IngestionJob.objects.create(project=project, job_type='gene_ner', parameters={})
    result = rust_engine.extract_genes_from_abstracts(
        job_id=str(job.id), project_id=str(project.id), db_url=db_url,
    )
    print(f'Re-processamento NER: {result}')
else:
    print('Função extract_genes_from_abstracts não disponível')
    print('Opção: criar uma função Rust que leia abstracts do banco e re-execute NER')
"
```

Se a função standalone não existir, considerar criar uma Celery task que:
1. Busca papers que não têm `PaperGene` associados (ou todos)
2. Para cada batch de 500 papers, chama o NER do Rust
3. Insere os resultados via COPY

---

## VALIDAÇÃO FINAL COMPLETA

```bash
echo "=== VALIDAÇÃO FINAL — SRA + Links + NER ===" | tee diagnostics/VALIDATION_V2.log

echo -e "\n1. SRA retorna datasets" | tee -a diagnostics/VALIDATION_V2.log
python manage.py shell -c "
import rust_engine
from django.conf import settings
from apps.core.models import *
db = settings.DATABASES['default']
db_url = f\"postgresql://{db['USER']}:{db['PASSWORD']}@{db['HOST']}:{db['PORT']}/{db['NAME']}\"
project = DaVinciProject.objects.first()
job = IngestionJob.objects.create(project=project, job_type='geo_search', parameters={})
result = rust_engine.search_and_ingest_omics(
    job_id=str(job.id), query='breast cancer RNA-seq',
    db_url=db_url, project_id=str(project.id),
    sources=['sra'], max_per_source=15,
    ncbi_api_key=getattr(settings, 'NCBI_API_KEY', None), synonyms=[],
)
count = OmicDataset.objects.filter(source_db='sra').count()
print(f'  {\"✅\" if count > 0 else \"❌\"} SRA datasets: {count}')
" 2>&1 | tee -a diagnostics/VALIDATION_V2.log

echo -e "\n2. DatasetPaperLinks existem" | tee -a diagnostics/VALIDATION_V2.log
python manage.py shell -c "
from apps.core.models import DatasetPaperLink
count = DatasetPaperLink.objects.count()
print(f'  {\"✅\" if count > 0 else \"❌\"} DatasetPaperLinks: {count}')
if count > 0:
    for link in DatasetPaperLink.objects.select_related('dataset', 'paper')[:3]:
        print(f'    {link.dataset.accession} ↔ PMID:{link.paper.pmid} (via {link.link_source})')
" 2>&1 | tee -a diagnostics/VALIDATION_V2.log

echo -e "\n3. Gene NER expandido" | tee -a diagnostics/VALIDATION_V2.log
python manage.py shell -c "
from apps.core.models import PaperGene
from django.db.models import Sum
count = PaperGene.objects.count()
unique = PaperGene.objects.values('gene_symbol').distinct().count()
print(f'  {\"✅\" if count > 100 else \"⚠️\"} PaperGene: {count} registros, {unique} genes únicos')
top = PaperGene.objects.values('gene_symbol').annotate(t=Sum('mention_count')).order_by('-t')[:10]
for g in top:
    print(f'    {g[\"gene_symbol\"]}: {g[\"t\"]} menções')
" 2>&1 | tee -a diagnostics/VALIDATION_V2.log

echo -e "\n4. Drug NER expandido" | tee -a diagnostics/VALIDATION_V2.log
python manage.py shell -c "
from apps.core.models import PaperDrug
count = PaperDrug.objects.count()
unique = PaperDrug.objects.values('drug_name').distinct().count()
print(f'  {\"✅\" if count > 20 else \"⚠️\"} PaperDrug: {count} registros, {unique} drogas únicas')
" 2>&1 | tee -a diagnostics/VALIDATION_V2.log

echo -e "\n5. Testes Rust" | tee -a diagnostics/VALIDATION_V2.log
cd rust_src && cargo test 2>&1 | tail -5 | tee -a ../diagnostics/VALIDATION_V2.log
cd ..

echo -e "\n6. Testes Django" | tee -a diagnostics/VALIDATION_V2.log
python manage.py test apps/ -v 2 --no-input 2>&1 | tail -5 | tee -a diagnostics/VALIDATION_V2.log

echo -e "\n=== FIM ===" | tee -a diagnostics/VALIDATION_V2.log
```

---

## Resumo das Entregas Esperadas

Após executar este prompt, o DaVinci deve ter:

1. **SRA retornando datasets** (>0 para qualquer query relevante)
2. **DatasetPaperLinks sendo criados** (relações GEO/SRA → PubMed)
3. **Gene NER com ~43.000 symbols** (HGNC completo) em vez de 6
4. **Drug NER com ~3.000+ nomes** em vez de 6
5. **Todos os testes Rust passando** (incluindo novos testes de SRA e NER)
6. **57+ testes Django passando** (sem regressões)
7. **`diagnostics/VALIDATION_V2.log`** com resultados

Traga o `VALIDATION_V2.log` de volta para avaliarmos.
