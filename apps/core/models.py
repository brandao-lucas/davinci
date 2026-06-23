"""
DaVinci Models — PlatOmics Integration
=======================================

Arquitetura de dados para ingestão de literatura científica (PubMed/PMC)
e metadados ômicos (GEO/SRA/BioProject), com separação entre dados
compartilhados (globais) e dados por projeto/usuário.

Princípios:
-----------
1. Tabelas de DADOS são compartilhadas (um paper/dataset existe uma vez no banco).
2. Tabelas de RELAÇÃO conectam dados a projetos de usuários.
3. FTS (Full-Text Search) via SearchVectorField para busca instantânea.
4. UUIDs para PKs de projetos; IDs naturais (PMID, accession) para dados externos.
5. Ingestão via COPY (Rust) — sem ORM, sem Signals.
6. Django apenas lê e expõe via DRF + Views Materializadas.

Diagrama Simplificado:
----------------------
    User
      │
      └── Project (DaVinciProject)
            │
            ├──── ProjectPaper ──── Paper (compartilhado)
            │         │                 ├── PaperAuthor
            │         │                 ├── PaperKeyword
            │         │                 ├── PaperMeSHTerm
            │         │                 ├── PaperGene
            │         │                 ├── PaperDrug        ← NOVO
            │         │                 ├── PaperVariant
            │         │                 └── EntityContext     ← NOVO
            │         ├── curation_status / excluded / notes
            │         ├── clinical_categories (M2M via score) ← NOVO
            │         └── user_categories (M2M)               ← NOVO
            │
            ├──── ProjectDataset ── OmicDataset (compartilhado)
            │         │                 └── DatasetPaperLink
            │         └── curation_status / relevance_score
            │
            └──── ProjectPaperDataset (ponte literatura ↔ ômicas)

    Configuração Global:
        ├── ClinicalCategory (diagnosis, treatment, ...)  ← NOVO
        ├── OmicCategory (genomic, transcriptomic, ...)
        └── UserCategory (por projeto)                    ← NOVO
"""

import uuid
from django.db import models
from django.contrib.auth.models import User
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.fields import ArrayField


# =============================================================================
# SEÇÃO 1: PROJETO DO USUÁRIO (Escopo por usuário)
# =============================================================================

class DaVinciProject(models.Model):
    """
    Projeto de investigação do usuário.
    Equivale ao antigo Platomics, mas com UUID como PK
    e o identification legível como campo único.

    Cada projeto define uma query de busca e acumula papers + datasets
    curados pelo pesquisador.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='davinci_projects'
    )

    # Identificação legível (gerada automaticamente)
    slug = models.SlugField(
        'Identificador',
        max_length=255,
        unique=True,
        help_text='Gerado automaticamente: {titulo}_{user}_davinci'
    )
    title = models.CharField('Título do Projeto', max_length=255)
    description = models.TextField('Descrição', blank=True, default='')

    # Parâmetros de busca
    query_term = models.CharField(
        'Termo de Busca Principal',
        max_length=500,
        help_text='Ex: "hidradenitis AND cancer", "cardiovascular disease"'
    )
    query_synonyms = models.JSONField(
        'Sinônimos de Busca',
        default=list,
        blank=True,
        help_text='Termos alternativos para normalização entre bases (PubMed, GEO, SRA)'
    )
    date_from = models.PositiveSmallIntegerField('Ano Inicial', blank=True, null=True)
    date_to = models.PositiveSmallIntegerField('Ano Final', blank=True, null=True)
    target_organisms = models.JSONField(
        'Organismos Alvo',
        default=list,
        blank=True,
        help_text='Ex: ["Homo sapiens", "Mus musculus"]'
    )
    target_tissues = models.JSONField(
        'Tecidos Alvo',
        default=list,
        blank=True,
        help_text='Ex: ["blood", "skin", "liver"]'
    )

    # Pesquisa avançada premium (MeSH + painel de magnitude)
    advanced_search_enabled = models.BooleanField(
        'Pesquisa Avançada Habilitada',
        default=False
    )
    selected_mesh = models.JSONField(
        'Descritores MeSH Selecionados',
        default=list,
        blank=True,
        help_text='[{"descriptor": str, "ui": str, "qualifiers": [str], "mode": "and"|"or", "major_only": bool}]'
    )
    mesh_default_mode = models.CharField(
        'Modo Padrão MeSH',
        max_length=3,
        default='and',
        help_text="'and' (precisão) ou 'or' (recall)"
    )
    magnitude_snapshot = models.JSONField(
        'Snapshot de Magnitude',
        default=dict,
        blank=True,
        help_text='Último preview de magnitude calculado'
    )

    # Status do pipeline
    class PipelineStatus(models.TextChoices):
        DRAFT = 'draft', 'Rascunho'
        SEARCHING = 'searching', 'Buscando'
        CURATING = 'curating', 'Em Curadoria'
        ANALYZING = 'analyzing', 'Analisando'
        COMPLETE = 'complete', 'Completo'

    status = models.CharField(
        max_length=20,
        choices=PipelineStatus.choices,
        default=PipelineStatus.DRAFT
    )

    # Metadados
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['query_term']),
        ]

    def __str__(self):
        return f"{self.title} ({self.user.username})"


# =============================================================================
# SEÇÃO 2: LITERATURA CIENTÍFICA (Dados compartilhados entre projetos)
# =============================================================================

class Paper(models.Model):
    """
    Paper do PubMed/PMC — TABELA COMPARTILHADA.

    Um paper existe uma única vez no banco, independente de quantos
    projetos o referenciam. O Rust faz upsert via COPY com
    ON CONFLICT (pmid) DO UPDATE.

    O campo search_vector é mantido por trigger no Postgres
    (não pelo Django) para que o COPY do Rust o atualize automaticamente.
    """
    pmid = models.BigIntegerField(
        'PubMed ID',
        unique=True,
        db_index=True,
        help_text='Identificador único no PubMed'
    )
    pmc_id = models.CharField(
        'PMC ID',
        max_length=20,
        blank=True,
        default='',
        db_index=True
    )
    doi = models.CharField('DOI', max_length=255, blank=True, default='')

    # Conteúdo principal
    title = models.TextField('Título')
    abstract = models.TextField('Resumo', blank=True, default='')

    # Publicação
    journal = models.CharField('Periódico (ISO)', max_length=255, blank=True, default='')
    pub_year = models.PositiveSmallIntegerField('Ano de Publicação', blank=True, null=True)
    pub_month = models.PositiveSmallIntegerField('Mês de Publicação', blank=True, null=True)
    pub_type = models.CharField(
        'Tipo de Publicação',
        max_length=100,
        blank=True,
        default='',
        db_index=True,
        help_text='Tipo primário do PubMed (Review, Systematic Review, RCT, etc.)'
    )

    # Full-Text Search (Postgres tsvector)
    # O trigger no Postgres atualiza automaticamente:
    # CREATE TRIGGER paper_search_update BEFORE INSERT OR UPDATE
    #   ON davinci_paper FOR EACH ROW EXECUTE FUNCTION
    #   tsvector_update_trigger(search_vector, 'pg_catalog.english', title, abstract);
    search_vector = SearchVectorField(null=True)

    # Controle de ingestão
    raw_xml_hash = models.CharField(
        'Hash do XML original',
        max_length=64,
        blank=True,
        default='',
        help_text='SHA-256 do XML do PubMed para detectar atualizações'
    )
    ingested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
            models.Index(fields=['pub_year']),
            models.Index(fields=['journal']),
        ]

    def __str__(self):
        return f"PMID:{self.pmid} — {self.title[:80]}"


class PaperAuthor(models.Model):
    """
    Autores de um paper. Tabela separada porque um paper tem N autores
    e precisamos buscar por afiliação e nome.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='authors')
    position = models.PositiveSmallIntegerField(
        'Posição na Lista',
        help_text='1 = primeiro autor, último = senior author'
    )
    last_name = models.CharField('Sobrenome', max_length=255)
    initials = models.CharField('Iniciais', max_length=20, blank=True, default='')
    affiliation = models.TextField('Afiliação', blank=True, default='')
    country = models.CharField(
        'País',
        max_length=100,
        blank=True,
        default='',
        help_text='Extraído da afiliação via regex no Rust'
    )

    class Meta:
        ordering = ['paper', 'position']
        unique_together = ['paper', 'position']
        indexes = [
            models.Index(fields=['last_name']),
            models.Index(fields=['country']),
        ]

    def __str__(self):
        return f"{self.initials} {self.last_name} (pos {self.position})"


class PaperKeyword(models.Model):
    """
    Keywords do paper (author keywords, não MeSH).
    Tabela separada para permitir buscas e agregações eficientes.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='keywords')
    keyword = models.CharField('Keyword', max_length=255)
    keyword_lower = models.CharField(
        'Keyword Normalizada',
        max_length=255,
        db_index=True,
        help_text='Lowercase para dedup e busca'
    )

    class Meta:
        unique_together = ['paper', 'keyword_lower']

    def __str__(self):
        return self.keyword


class PaperMeSHTerm(models.Model):
    """
    MeSH Terms associados ao paper.
    Fundamentais para a categorização heurística e correlação com ômicas.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='mesh_terms')
    descriptor = models.CharField(
        'MeSH Descriptor',
        max_length=255,
        db_index=True
    )
    qualifier = models.CharField(
        'MeSH Qualifier',
        max_length=255,
        blank=True,
        default=''
    )
    is_major_topic = models.BooleanField(
        'Major Topic',
        default=False,
        help_text='MeSH MajorTopicYN="Y"'
    )

    class Meta:
        unique_together = ['paper', 'descriptor', 'qualifier']
        indexes = [
            models.Index(fields=['descriptor', 'is_major_topic']),
        ]

    def __str__(self):
        return f"{self.descriptor}/{self.qualifier}" if self.qualifier else self.descriptor


class PaperGene(models.Model):
    """
    Genes mencionados no paper (extraídos do abstract via NER no Rust).
    Compartilhado entre projetos.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='genes')
    gene_symbol = models.CharField('Símbolo do Gene', max_length=50, db_index=True)
    entrez_id = models.BigIntegerField('Entrez Gene ID', blank=True, null=True)
    mention_count = models.PositiveIntegerField(
        'Menções no Abstract',
        default=1
    )

    class Meta:
        unique_together = ['paper', 'gene_symbol']
        indexes = [
            models.Index(fields=['entrez_id']),
        ]

    def __str__(self):
        return f"{self.gene_symbol} (x{self.mention_count}) — PMID:{self.paper.pmid}"


class PaperVariant(models.Model):
    """
    Variantes (rs numbers) mencionadas no paper.
    Compartilhado entre projetos.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='variants')
    rs_number = models.CharField('RS Number', max_length=20, db_index=True)
    mention_count = models.PositiveIntegerField('Menções no Abstract', default=1)

    class Meta:
        unique_together = ['paper', 'rs_number']

    def __str__(self):
        return f"{self.rs_number} — PMID:{self.paper.pmid}"


class PaperDrug(models.Model):
    """
    Drogas/fármacos mencionados no paper (extraídos do abstract via NER no Rust).
    Compartilhado entre projetos.

    Feature migrada do R: drugs() — identifica menções a compostos
    químicos e fármacos nos abstracts.
    """
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='drugs')
    drug_name = models.CharField('Nome da Droga', max_length=255, db_index=True)
    drug_name_lower = models.CharField(
        'Nome Normalizado',
        max_length=255,
        db_index=True,
        help_text='Lowercase para dedup e busca'
    )
    mention_count = models.PositiveIntegerField('Menções no Abstract', default=1)
    drugbank_id = models.CharField(
        'DrugBank ID',
        max_length=20,
        blank=True,
        default='',
        help_text='Referência cruzada opcional com DrugBank'
    )

    class Meta:
        unique_together = ['paper', 'drug_name_lower']
        indexes = [
            models.Index(fields=['drug_name_lower']),
        ]

    def __str__(self):
        return f"{self.drug_name} (x{self.mention_count}) — PMID:{self.paper.pmid}"


class VariantAnnotation(models.Model):
    """
    Anotações externas de variantes (dbSNP, ClinVar).
    Uma variante existe uma vez, compartilhada entre papers.

    Populada assincronamente após ingestão dos papers
    (batch lookup no dbSNP via Rust).
    """
    rs_number = models.CharField('RS Number', max_length=20, primary_key=True)
    gene_symbol = models.CharField('Gene', max_length=50, blank=True, default='')
    gene_name = models.TextField('Nome do Gene', blank=True, default='')
    entrez_id = models.BigIntegerField('Entrez ID', blank=True, null=True)
    chromosome = models.CharField('Cromossomo', max_length=10, blank=True, default='')
    position = models.BigIntegerField('Posição', blank=True, null=True)
    alleles = models.CharField('Alelos', max_length=200, blank=True, default='')
    maf = models.FloatField('Minor Allele Frequency', blank=True, null=True)
    clinical_significance = models.CharField(
        'Significância Clínica',
        max_length=255,
        blank=True,
        default=''
    )
    last_fetched = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.rs_number} ({self.gene_symbol})"


# =============================================================================
# SEÇÃO 3: METADADOS ÔMICOS (Dados compartilhados entre projetos)
# =============================================================================

class OmicDataset(models.Model):
    """
    Dataset ômico de repositório público — TABELA COMPARTILHADA.

    Unifica BioProject e GDS em um modelo normalizado.
    Campos específicos de cada fonte vão em JSONField (extra_metadata)
    ao invés de dezenas de colunas nullable.
    """

    class SourceDB(models.TextChoices):
        GEO = 'geo', 'GEO (Gene Expression Omnibus)'
        SRA = 'sra', 'Sequence Read Archive'
        ARRAYEXPRESS = 'arrayexpress', 'ArrayExpress'
        TCGA = 'tcga', 'TCGA'
        BIOPROJECT = 'bioproject', 'BioProject'
        GWAS_CATALOG = 'gwas_catalog', 'GWAS Catalog'

    class OmicType(models.TextChoices):
        GENOMIC = 'genomic', 'Genômica'
        TRANSCRIPTOMIC = 'transcriptomic', 'Transcriptômica'
        PROTEOMIC = 'proteomic', 'Proteômica'
        METABOLOMIC = 'metabolomic', 'Metabolômica'
        EPIGENOMIC = 'epigenomic', 'Epigenômica'
        METAGENOMIC = 'metagenomic', 'Metagenômica'
        MICROBIOME = 'microbiome', 'Microbiômica'
        MULTI_OMIC = 'multi_omic', 'Multi-ômica'
        OTHER = 'other', 'Outro'

    # --- Contrato de dados (OmnisPathway) — campos aditivos ---
    class SingleCell(models.TextChoices):
        SINGLE_CELL = 'single_cell', 'Single-cell'
        BULK = 'bulk', 'Bulk'
        UNKNOWN = 'unknown', 'Desconhecido'

    class ControlGroup(models.TextChoices):
        YES = 'yes', 'Sim'
        NO = 'no', 'Não'
        UNKNOWN = 'unknown', 'Desconhecido'

    class DiseaseAxis(models.TextChoices):
        MONOGENIC = 'monogenic', 'Monogênica'
        MULTIFACTORIAL = 'multifactorial', 'Multifatorial'
        MIXED = 'mixed', 'Mista'
        INDETERMINATE = 'indeterminate', 'Indeterminado'

    class DataFormat(models.TextChoices):
        RAW = 'raw', 'Bruto (raw)'
        PROCESSED = 'processed', 'Processado'
        UNKNOWN = 'unknown', 'Desconhecido'

    class AccessType(models.TextChoices):
        PUBLIC = 'public', 'Público'
        CONTROLLED = 'controlled', 'Controlado'
        UNKNOWN = 'unknown', 'Desconhecido'

    # Vocabulário canônico de camadas ômicas (minúsculas, alinhado ao OmicType)
    OMICS_LAYER_VOCAB = [
        'genomic', 'transcriptomic', 'proteomic', 'metabolomic',
        'epigenomic', 'metagenomic', 'microbiome',
    ]

    # Identificação — accession como chave natural
    accession = models.CharField(
        'Accession',
        max_length=50,
        unique=True,
        db_index=True,
        help_text='GSE, SRP, PRJNA, E-MTAB, etc.'
    )
    source_db = models.CharField(
        'Banco de Origem',
        max_length=20,
        choices=SourceDB.choices
    )
    bioproject_id = models.CharField(
        'BioProject ID',
        max_length=50,
        blank=True,
        default='',
        db_index=True
    )

    # Conteúdo
    title = models.TextField('Título')
    summary = models.TextField('Resumo/Descrição', blank=True, default='')

    # Classificação ômica
    # Armazena múltiplos valores separados por vírgula (ex: "transcriptomic,genomic")
    omic_type = models.CharField(
        'Tipo Ômico',
        max_length=200,
        choices=OmicType.choices,
        blank=True,
        default=''
    )
    omic_subcategory = models.CharField(
        'Subcategoria',
        max_length=500,
        blank=True,
        default='',
        help_text='Ex: RNA-Seq, WGS, ChIP-Seq, 16S'
    )

    # Metadados biológicos
    organism = models.CharField('Organismo', max_length=200, blank=True, default='')
    tax_id = models.PositiveIntegerField('Taxonomy ID', blank=True, null=True)
    n_samples = models.PositiveIntegerField('Número de Amostras', blank=True, null=True)
    platform = models.CharField(
        'Plataforma',
        max_length=200,
        blank=True,
        default='',
        help_text='GPL number ou nome da tecnologia'
    )

    # Metadados extras (campos específicos de cada fonte)
    # BioProject: target_scope, target_material, method_type, etc.
    # GDS: gds_type, gpl, gse, etc.
    extra_metadata = models.JSONField(
        'Metadados Extras',
        default=dict,
        blank=True,
        help_text='Campos específicos da fonte que não justificam colunas próprias'
    )

    # Link com literatura
    related_pmids = models.ManyToManyField(
        Paper,
        through='DatasetPaperLink',
        related_name='related_datasets',
        blank=True
    )

    # --- Contrato de dados (OmnisPathway) — campos aditivos ---
    # Populados parcialmente por backfill (Fase 0) e refinados nas Fases 2/3.
    omics_count = models.PositiveSmallIntegerField(
        'Nº de Camadas Ômicas',
        null=True,
        blank=True,
        help_text='Nº de camadas ômicas distintas. NULL = não avaliado'
    )
    omics_layers = ArrayField(
        models.CharField(max_length=40),
        default=list,
        blank=True,
        help_text='Camadas ômicas normalizadas (genomic, transcriptomic, ...)'
    )
    is_single_cell = models.CharField(
        'Single-cell?',
        max_length=20,  # folga p/ futuro 'spatial' (P1)
        choices=SingleCell.choices,
        default=SingleCell.UNKNOWN
    )
    has_control_group = models.CharField(
        'Tem grupo controle?',
        max_length=10,
        choices=ControlGroup.choices,
        default=ControlGroup.UNKNOWN
    )
    disease_axis = models.CharField(
        'Eixo da Doença',
        max_length=15,
        choices=DiseaseAxis.choices,
        default=DiseaseAxis.INDETERMINATE
    )
    data_format = models.CharField(
        'Formato dos Dados',
        max_length=15,  # folga (P1)
        choices=DataFormat.choices,
        default=DataFormat.UNKNOWN
    )
    access_type = models.CharField(
        'Tipo de Acesso',
        max_length=20,  # folga p/ futuro 'embargoed' (P1)
        choices=AccessType.choices,
        default=AccessType.UNKNOWN
    )
    sample_join_key = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        help_text='Chaves de junção entre datasets (N chaves). [] = ausente'
    )
    contract_confidence = models.JSONField(
        'Confiança do Contrato',
        default=dict,
        blank=True,
        help_text='{eixo: score 0..1}, populado nas Fases 2/3'
    )

    # FTS
    search_vector = SearchVectorField(null=True)

    # Controle
    is_active = models.BooleanField(default=True)
    ingested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            GinIndex(fields=['search_vector']),
            models.Index(fields=['omic_type']),
            models.Index(fields=['organism']),
            models.Index(fields=['source_db']),
            models.Index(fields=['n_samples']),
            # --- Contrato de dados (OmnisPathway) ---
            GinIndex(fields=['omics_layers'], name='omicdataset_layers_gin'),
            GinIndex(fields=['sample_join_key'], name='omicdataset_join_key_gin'),
            models.Index(fields=['is_single_cell'], name='omicdataset_single_cell_idx'),
            models.Index(fields=['has_control_group'], name='omicdataset_control_grp_idx'),
            models.Index(fields=['disease_axis'], name='omicdataset_disease_axis_idx'),
            models.Index(fields=['data_format'], name='omicdataset_data_format_idx'),
            models.Index(fields=['access_type'], name='omicdataset_access_type_idx'),
            models.Index(fields=['omics_count'], name='omicdataset_omics_count_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                name='omicdataset_is_single_cell_valid',
                condition=models.Q(is_single_cell__in=['single_cell', 'bulk', 'unknown']),
            ),
            models.CheckConstraint(
                name='omicdataset_has_control_group_valid',
                condition=models.Q(has_control_group__in=['yes', 'no', 'unknown']),
            ),
            models.CheckConstraint(
                name='omicdataset_disease_axis_valid',
                condition=models.Q(disease_axis__in=['monogenic', 'multifactorial', 'mixed', 'indeterminate']),
            ),
            models.CheckConstraint(
                name='omicdataset_data_format_valid',
                condition=models.Q(data_format__in=['raw', 'processed', 'unknown']),
            ),
            models.CheckConstraint(
                name='omicdataset_access_type_valid',
                condition=models.Q(access_type__in=['public', 'controlled', 'unknown']),
            ),
            models.CheckConstraint(
                name='omicdataset_omics_layers_valid',
                condition=models.Q(omics_layers__contained_by=[
                    'genomic', 'transcriptomic', 'proteomic', 'metabolomic',
                    'epigenomic', 'metagenomic', 'microbiome',
                ]),
            ),
        ]

    def __str__(self):
        return f"{self.accession} — {self.omic_type} ({self.organism})"


class OmicSample(models.Model):
    """
    Amostra (sample) de um dataset ômico — TABELA COMPARTILHADA.

    Espelha o padrão de OmicDataset: uma amostra existe uma única vez no banco,
    independente de quantos projetos referenciam o dataset pai. O Rust faz upsert
    via COPY com ON CONFLICT (accession) DO UPDATE.

    O `accession` é a chave natural estável da amostra (GSM*/SRR*/SRS*) e, por ser
    `unique` (mesma convenção de OmicDataset.accession), serve diretamente ao
    ON CONFLICT DO UPDATE do COPY writer do Rust.

    Ingestão sob demanda: as amostras são populadas quando o dataset pai entra em
    curadoria (espelha o ciclo de ProjectDataset → ProjectSample).
    """

    dataset = models.ForeignKey(
        OmicDataset,
        on_delete=models.CASCADE,
        related_name='samples'
    )

    # Identificação — accession como chave natural (igual a OmicDataset)
    accession = models.CharField(
        'Accession',
        max_length=50,
        unique=True,
        db_index=True,
        help_text='GSM, SRR, SRS, etc. — chave natural da amostra'
    )

    # Conteúdo
    title = models.TextField('Título', blank=True, default='')
    source_name = models.CharField(
        'Source Name',
        max_length=500,
        blank=True,
        default='',
        help_text='Tecido/fonte declarada da amostra (ex: "blood", "tumor biopsy")'
    )

    # Metadados biológicos
    organism = models.CharField('Organismo', max_length=200, blank=True, default='')
    tax_id = models.PositiveIntegerField('Taxonomy ID', blank=True, null=True)
    platform = models.CharField(
        'Plataforma',
        max_length=200,
        blank=True,
        default='',
        help_text='GPL number ou nome da tecnologia'
    )

    # Características key/value da amostra (characteristics_ch* no SOFT/MINiML)
    characteristics = models.JSONField(
        'Características',
        default=dict,
        blank=True,
        help_text='Metadados key/value da amostra (ex: {"age": "45", "sex": "F"})'
    )

    # Metadados extras (campos específicos de cada fonte)
    extra_metadata = models.JSONField(
        'Metadados Extras',
        default=dict,
        blank=True,
        help_text='Campos específicos da fonte que não justificam colunas próprias'
    )

    # Controle (mesma convenção de OmicDataset)
    ingested_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['dataset']),
            models.Index(fields=['accession']),
            models.Index(fields=['organism']),
        ]

    def __str__(self):
        return f"{self.accession} — {self.title[:60]}"


class DatasetFile(models.Model):
    """
    Arquivo físico (bytes reais) associado a um dataset ou amostra ômica.

    Relação 1:N — um OmicDataset tem N arquivos (series matrix + várias
    supplementary + CEL) e um OmicSample pode ter N arquivos (FASTQ R1/R2,
    .sra). Por isso é tabela própria, não colunas em OmicDataset/OmicSample.

    Exatamente UM de (dataset, sample) é preenchido — garantido por
    CheckConstraint XOR. O `accession` é a chave natural estável do arquivo
    remoto (ex.: `GSExxx_supp_<nome>`, `SRRxxx_1`) e, por ser `unique`, serve
    diretamente ao ON CONFLICT (accession) DO UPDATE do COPY writer do Rust.

    `download_status` é o estado *fino* por arquivo; o estado *agregado* de
    curadoria do dataset continua em ProjectDataset.CurationStatus
    (queued/downloaded).
    """

    class FileType(models.TextChoices):
        SERIES_MATRIX = 'series_matrix', 'Series Matrix (GEO)'
        SUPPLEMENTARY = 'supplementary', 'Suplementar (GEO)'
        CEL = 'cel', 'CEL (Affymetrix)'
        FASTQ = 'fastq', 'FASTQ (reads brutos)'
        SRA = 'sra', 'SRA'

    class Source(models.TextChoices):
        GEO_FTP = 'geo_ftp', 'GEO FTP (NCBI)'
        ENA_FTP = 'ena_ftp', 'ENA FTP'
        SRA_TOOLS = 'sra_tools', 'sra-tools'

    class DownloadStatus(models.TextChoices):
        PENDING = 'pending', 'Pendente'
        QUEUED = 'queued', 'Na Fila'
        DOWNLOADING = 'downloading', 'Baixando'
        DOWNLOADED = 'downloaded', 'Baixado'
        FAILED = 'failed', 'Falhou'

    # Vínculo — exatamente um preenchido (CheckConstraint XOR abaixo)
    dataset = models.ForeignKey(
        OmicDataset,
        on_delete=models.CASCADE,
        related_name='files',
        null=True,
        blank=True
    )
    sample = models.ForeignKey(
        OmicSample,
        on_delete=models.CASCADE,
        related_name='files',
        null=True,
        blank=True
    )

    # Chave natural do arquivo remoto — serve ao ON CONFLICT do COPY do Rust
    accession = models.CharField(
        'Accession',
        max_length=255,
        unique=True,
        db_index=True,
        help_text='Chave natural estável (ex.: GSExxx_supp_<nome>, SRRxxx_1)'
    )

    # Classificação
    file_type = models.CharField(
        'Tipo de Arquivo',
        max_length=20,
        choices=FileType.choices
    )
    source = models.CharField(
        'Fonte',
        max_length=20,
        choices=Source.choices
    )

    # Localização
    remote_url = models.TextField('URL Remota')
    storage_key = models.TextField(
        'Storage Key',
        blank=True,
        default='',
        help_text='Path no object storage; vazio até o download concluir'
    )

    # Integridade
    size_bytes = models.BigIntegerField('Tamanho (bytes)', null=True, blank=True)
    checksum_md5 = models.CharField(
        'Checksum MD5',
        max_length=128,
        null=True,
        blank=True
    )
    checksum_algo = models.CharField(
        'Algoritmo de Checksum',
        max_length=20,
        default='md5'
    )

    # Estado do download (fino, por arquivo)
    download_status = models.CharField(
        'Status do Download',
        max_length=20,
        choices=DownloadStatus.choices,
        default=DownloadStatus.PENDING,
        db_index=True
    )
    bytes_downloaded = models.BigIntegerField(
        'Bytes Baixados',
        default=0,
        help_text='Progresso/retomada (Range request)'
    )
    error_message = models.TextField('Mensagem de Erro', blank=True, default='')

    # Controle
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    downloaded_at = models.DateTimeField('Baixado em', null=True, blank=True)

    class Meta:
        # dataset/sample já ganham índice automático por serem FK; download_status
        # ganha índice via db_index=True acima. Nenhum Index extra para não duplicar.
        constraints = [
            models.CheckConstraint(
                name='datasetfile_dataset_xor_sample',
                condition=(
                    models.Q(dataset__isnull=False, sample__isnull=True)
                    | models.Q(dataset__isnull=True, sample__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return f"{self.accession} ({self.file_type}/{self.download_status})"


class DatasetGene(models.Model):
    """
    Genes materializados no NÍVEL DO DATASET (não do paper).

    Duas fontes de procedência (campo `source`):
      - `paper_link`: genes herdados dos papers ligados ao dataset
        (DatasetPaperLink -> Paper -> PaperGene).
      - `dataset_metadata`: genes extraídos por NER sobre os campos próprios
        do OmicDataset (title, summary, extra_metadata, ex.: contract/disease_raw).

    Alimenta o sinal `monogenic_gene_hit` da Fase 3 do OmnisPathway
    (cruzamento gene-do-dataset x lista mendeliana): um gene visto pelas DUAS
    fontes tem confiança maior que um visto por apenas uma, por isso a
    unicidade inclui `source` — a mesma menção pode coexistir vinda de cada
    lado sem colidir, preservando a contagem por procedência.
    """

    class Source(models.TextChoices):
        PAPER_LINK = 'paper_link', 'Paper Vinculado'
        DATASET_METADATA = 'dataset_metadata', 'Metadados do Dataset'

    dataset = models.ForeignKey(
        OmicDataset,
        on_delete=models.CASCADE,
        related_name='genes'
    )
    gene_symbol = models.CharField('Símbolo do Gene', max_length=50, db_index=True)
    entrez_id = models.BigIntegerField('Entrez Gene ID', blank=True, null=True)
    mention_count = models.PositiveIntegerField('Menções', default=1)
    source = models.CharField(
        'Procedência',
        max_length=20,
        choices=Source.choices,
        db_index=True
    )

    class Meta:
        # source na unicidade: mesma menção pode vir de paper_link E
        # dataset_metadata sem colidir — é justamente o que dá força ao
        # monogenic_gene_hit (gene nos dois lados).
        unique_together = ['dataset', 'gene_symbol', 'source']
        indexes = [
            models.Index(fields=['entrez_id']),
            # composto p/ o cruzamento com a lista mendeliana por dataset
            models.Index(fields=['dataset', 'gene_symbol']),
        ]

    def __str__(self):
        return f"{self.gene_symbol} (x{self.mention_count}) — {self.dataset.accession} [{self.source}]"


class DatasetPaperLink(models.Model):
    """
    Relação entre um dataset ômico e papers que o referenciam.
    Descoberta automaticamente via elink ou PMIDs no XML do GEO.
    """
    dataset = models.ForeignKey(OmicDataset, on_delete=models.CASCADE)
    paper = models.ForeignKey(Paper, on_delete=models.CASCADE)
    link_source = models.CharField(
        'Origem do Link',
        max_length=50,
        default='elink',
        help_text='elink, geo_xml, manual'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['dataset', 'paper']

    def __str__(self):
        return f"{self.dataset.accession} ↔ PMID:{self.paper.pmid}"


class DatasetPaperLinkPending(models.Model):
    """
    Staging table for dataset-paper links awaiting FK resolution.
    Links are stored here during omics ingestion and resolved when
    both datasets and papers exist in the database.
    """
    dataset_accession = models.CharField(max_length=50)
    paper_pmid = models.BigIntegerField()
    link_source = models.CharField(max_length=50, default='elink')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['dataset_accession', 'paper_pmid']
        db_table = 'core_datasetpaperlinkpending'

    def __str__(self):
        return f"Pending: {self.dataset_accession} ↔ PMID:{self.paper_pmid}"


# =============================================================================
# SEÇÃO 4: CATEGORIZAÇÃO ÔMICA (Configuração global)
# =============================================================================

class OmicCategory(models.Model):
    """
    Definições de categorias ômicas com keywords para classificação
    automática pelo Rust engine.
    """
    omic_type = models.CharField(
        'Tipo Ômico',
        max_length=50,
        choices=OmicDataset.OmicType.choices,
        unique=True
    )
    keywords = models.JSONField(
        'Keywords de Classificação',
        default=list,
        help_text='Lista de termos usados pelo Rust para categorizar datasets'
    )
    priority = models.PositiveSmallIntegerField('Prioridade', default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['priority']

    def __str__(self):
        return f"{self.get_omic_type_display()} ({len(self.keywords)} keywords)"


class ClinicalCategory(models.Model):
    """
    Categorias clínicas/científicas para classificação de papers.

    Feature migrada do R: categories.davinci() — classifica papers
    em eixos clínicos. Os 5 eixos padrão são pré-populados, mas o
    sistema é extensível (o pesquisador pode adicionar novos).

    Eixos padrão:
    - diagnosis, treatment, epidemiology, mechanism, signs_symptoms

    O Rust usa os keywords para categorização heurística via regex
    compilados, da mesma forma que faz com OmicCategory.
    """
    slug = models.SlugField('Slug', max_length=50, unique=True)
    name = models.CharField('Nome', max_length=100)
    description = models.TextField('Descrição', blank=True, default='')
    keywords = models.JSONField(
        'Keywords de Classificação',
        default=list,
        help_text='Termos usados pelo Rust para detectar esta categoria no abstract'
    )
    is_default = models.BooleanField(
        'Categoria Padrão',
        default=False,
        help_text='True para os 5 eixos padrão (não deletável)'
    )
    priority = models.PositiveSmallIntegerField('Prioridade', default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['priority']
        verbose_name_plural = 'Clinical categories'

    def __str__(self):
        return self.name


class DiseaseAxisReference(models.Model):
    """
    Store de REFERÊNCIA versionado que ancora a classificação de
    `disease_axis` (OmnisPathway, Fase 3).

    Guarda nomes de trait/doença vindos de fontes externas curadas, cada um
    pré-atribuído a um eixo (`monogenic` | `multifactorial`). A normalização
    da Fase 3 é fuzzy name-matching leve de `disease_raw`/MeSH do dataset
    contra estes nomes; por isso guardamos o nome cru (`name`, como veio da
    fonte) E uma forma normalizada (`name_normalized`, lowercase/limpa) para
    o match.

    Âncoras e suas fontes:
      - GWAS Catalog (`gwas-efo-trait-mappings.tsv`)  -> multifactorial  [P4, esta etapa]
      - Orphanet / Gene2Phenotype (G2P)               -> monogenic       [etapa seguinte]
      - OMIM está FORA por restrição de licença.

    Apenas os dois eixos-ÂNCORA entram aqui: `monogenic` e `multifactorial`.
    Os valores `mixed`/`indeterminate` são RESULTADO da classificação, não
    âncora, e portanto não têm lugar neste store. Reaproveitamos os labels de
    `OmicDataset.DiseaseAxis` para os dois que valem.

    Carga (responsabilidade do vitruvio, NÃO deste model):
      - download/parse/UPSERT são feitos fora daqui.
      - É idempotente e versionada: `unique_together = ('source', 'name')`
        permite re-rodar a mesma release fazendo UPSERT em vez de duplicar,
        e `source_version` registra de qual release cada nome veio.

    NOTA — âncora monogênica de GENES:
        O sinal `monogenic_gene_hit` da Fase 3 cruza genes do dataset
        (ver `DatasetGene`) contra a lista de genes mendelianos de
        Orphanet/G2P. Isso é GENE, não nome de doença, e não pertence a
        este model: vive no model próprio `MendelianGene` (logo abaixo).
    """

    class Source(models.TextChoices):
        GWAS_CATALOG = 'gwas_catalog', 'GWAS Catalog'
        ORPHANET = 'orphanet', 'Orphanet'
        G2P = 'g2p', 'Gene2Phenotype'

    # Eixos-âncora válidos = subconjunto de OmicDataset.DiseaseAxis.
    # Não referenciamos o enum completo porque indeterminate/mixed não
    # são âncoras; espelhamos só os dois valores canônicos.
    AXIS_CHOICES = [
        (OmicDataset.DiseaseAxis.MONOGENIC.value,
         OmicDataset.DiseaseAxis.MONOGENIC.label),
        (OmicDataset.DiseaseAxis.MULTIFACTORIAL.value,
         OmicDataset.DiseaseAxis.MULTIFACTORIAL.label),
    ]

    name = models.CharField(
        'Nome da Doença/Trait',
        max_length=512,
        db_index=True,
        help_text='Nome cru, exatamente como veio da fonte.'
    )
    name_normalized = models.CharField(
        'Nome Normalizado',
        max_length=512,
        db_index=True,
        help_text='Lowercase/limpo, usado como alvo do fuzzy name-matching.'
    )
    axis = models.CharField(
        'Eixo Ancorado',
        max_length=20,
        choices=AXIS_CHOICES,
        db_index=True,
        help_text='monogenic ou multifactorial — qual eixo esta entrada ancora.'
    )
    source = models.CharField(
        'Fonte',
        max_length=20,
        choices=Source.choices,
        db_index=True
    )
    source_version = models.CharField(
        'Versão/Release da Fonte',
        max_length=100,
        blank=True,
        default='',
        help_text='Identificador do release (ex.: data do FTP do GWAS Catalog).'
    )
    loaded_at = models.DateTimeField('Carregado em', auto_now=True)

    class Meta:
        # Idempotência da carga: a MESMA (fonte, nome) é uma linha só.
        # Escolhemos `name` (não `name_normalized`) na chave porque o nome
        # cru é o dado autoritativo da fonte — duas grafias distintas que
        # colapsam na mesma forma normalizada (ex.: "Type 2 diabetes" e
        # "type-2 diabetes") devem coexistir como entradas próprias e ambas
        # virar candidatas do fuzzy; usar name_normalized na unicidade
        # descartaria silenciosamente sinônimos da fonte. `source` entra na
        # chave porque a mesma doença pode ser ancorada por fontes diferentes
        # (e até em eixos diferentes) sem colidir.
        unique_together = ['source', 'name']
        indexes = [
            # Cobre o caminho quente do fuzzy: filtrar candidatos por eixo
            # e bater por nome normalizado.
            models.Index(
                fields=['axis', 'name_normalized'],
                name='diseaseaxisref_axis_norm_idx',
            ),
        ]
        verbose_name = 'Disease axis reference'
        verbose_name_plural = 'Disease axis references'

    def __str__(self):
        return f"{self.name} [{self.axis}] ({self.source})"


class MendelianGene(models.Model):
    """
    Store de REFERÊNCIA versionado de GENES mendelianos (monogênicos).

    É a contraparte de GENE do `DiseaseAxisReference` (que ancora NOMES de
    doença). Aqui mora a lista contra a qual o sinal `monogenic_gene_hit` da
    Fase 3 do OmnisPathway cruza os genes do dataset: dado um
    `DatasetGene.gene_symbol`, este store responde "é mendeliano? de qual
    fonte? com que confiança? associado a qual doença?".

    O cross é IGUALDADE EXATA de símbolo uppercase — não fuzzy. Por isso,
    diferente do `DiseaseAxisReference`, não há `pg_trgm`/forma normalizada
    fuzzy: `gene_symbol` é guardado já em UPPERCASE (alinhado ao NER e ao
    `DatasetGene`, que materializam símbolos em uppercase), e o lookup do
    cross é um índice B-tree simples.

    Fontes (estrutura já validada nos dumps):
      - Orphanet / Orphadata product6 (XML, CC-BY-4.0): `<Gene><Symbol>`,
        xref Ensembl (ENSG...), e a associação carrega tipo + `SourceOfValidation`
        (PMID) — mapeado para `confidence` como assessed/not_assessed.
      - Gene2Phenotype / G2P (CSV, EBI): `gene symbol`, `hgnc id`,
        `disease name`, `confidence` (definitive/strong/moderate/limited).

    Reaproveita o enum `Source` do `DiseaseAxisReference` (orphanet, g2p) —
    a mesma família de fontes monogênicas.

    Carga (responsabilidade do vitruvio, NÃO deste model):
      - download/parse/UPSERT são feitos fora daqui.
      - É idempotente e versionada: `unique_together` por
        (source, gene_symbol, disease_name) permite re-rodar a mesma release
        com UPSERT em vez de duplicar; `source_version`/`loaded_at` registram
        de qual release veio cada associação.
    """

    # Mesma família de fontes monogênicas do DiseaseAxisReference.
    # Espelhamos apenas o subconjunto que produz GENES (orphanet, g2p);
    # GWAS Catalog não entra aqui (é multifatorial / nomes de trait).
    class Source(models.TextChoices):
        ORPHANET = DiseaseAxisReference.Source.ORPHANET.value, \
            DiseaseAxisReference.Source.ORPHANET.label
        G2P = DiseaseAxisReference.Source.G2P.value, \
            DiseaseAxisReference.Source.G2P.label

    class Confidence(models.TextChoices):
        """
        Escala ORDINAL normalizada que alimenta o peso do `monogenic_gene_hit`.

        G2P traz a escada nativa (definitive>strong>moderate>limited). Orphanet
        não tem essa escada: traz tipo de associação + presença/ausência de
        `SourceOfValidation` (PMID). Mapeamos a validação do Orphanet para
        dois degraus próprios (assessed/not_assessed) em vez de forçá-la na
        escada do G2P, porque são escalas semanticamente distintas — colapsá-las
        falsearia a confiança. A string crua da fonte fica em `confidence_raw`
        para rastreabilidade/curadoria; este campo é o eixo comparável.
        """
        DEFINITIVE = 'definitive', 'Definitive'        # G2P
        STRONG = 'strong', 'Strong'                    # G2P
        MODERATE = 'moderate', 'Moderate'              # G2P
        LIMITED = 'limited', 'Limited'                 # G2P
        ASSESSED = 'assessed', 'Assessed'              # Orphanet c/ SourceOfValidation
        NOT_ASSESSED = 'not_assessed', 'Not assessed'  # Orphanet s/ validação
        UNKNOWN = 'unknown', 'Unknown'                 # fallback

    gene_symbol = models.CharField(
        'Símbolo do Gene',
        max_length=50,
        db_index=True,
        help_text='UPPERCASE normalizado — casa por igualdade exata com '
                  'DatasetGene.gene_symbol no cross do monogenic_gene_hit.'
    )
    entrez_id = models.BigIntegerField(
        'Entrez Gene ID',
        blank=True,
        null=True,
        help_text='Xref estável quando disponível.'
    )
    hgnc_id = models.CharField(
        'HGNC ID',
        max_length=32,
        blank=True,
        default='',
        help_text='Ex.: "HGNC:6614". G2P fornece direto; Orphanet via xref.'
    )
    ensembl_id = models.CharField(
        'Ensembl Gene ID',
        max_length=32,
        blank=True,
        default='',
        help_text='Ex.: "ENSG00000166813". Orphanet fornece via ExternalReference.'
    )
    disease_name = models.CharField(
        'Doença Monogênica Associada',
        max_length=512,
        blank=True,
        default='',
        help_text='Doença associada ao gene na fonte — rastreabilidade da '
                  'fila de curadoria de alto valor.'
    )
    source = models.CharField(
        'Fonte',
        max_length=20,
        choices=Source.choices,
        db_index=True
    )
    confidence = models.CharField(
        'Confiança (normalizada)',
        max_length=20,
        choices=Confidence.choices,
        default=Confidence.UNKNOWN,
        db_index=True,
        help_text='Escala ordinal comparável que pondera o monogenic_gene_hit.'
    )
    confidence_raw = models.CharField(
        'Confiança (crua da fonte)',
        max_length=128,
        blank=True,
        default='',
        help_text='Valor original (G2P confidence ou tipo de associação '
                  'Orphanet) antes da normalização — auditoria.'
    )
    source_version = models.CharField(
        'Versão/Release da Fonte',
        max_length=100,
        blank=True,
        default='',
        help_text='Release: Orphanet date/version do <JDBOR>; G2P Last-Modified.'
    )
    loaded_at = models.DateTimeField('Carregado em', auto_now=True)

    class Meta:
        # Idempotência da carga: uma associação (fonte, gene, doença) é uma
        # linha só, fazendo UPSERT no re-load. Mantemos disease_name na chave
        # — e NÃO um esquema gene-distinto-com-doenças-em-JSON — porque a fila
        # de curadoria de alto valor precisa de cada par gene×doença×confiança
        # como linha própria (filtrável/joinable), e o custo do JSON-aggregate
        # não compra nada para o cross (que é só igualdade de gene_symbol).
        # `source` na chave: o mesmo gene/doença pode vir de Orphanet E G2P
        # sem colidir (e um gene nas duas fontes dá força ao sinal).
        unique_together = ['source', 'gene_symbol', 'disease_name']
        indexes = [
            # Caminho QUENTE do cross: lookup por símbolo (igualdade exata).
            models.Index(
                fields=['gene_symbol'],
                name='mendgene_symbol_idx',
            ),
            # Composto p/ "este gene é mendeliano POR esta fonte?" e p/ ordenar
            # candidatos por fonte sem tocar a heap.
            models.Index(
                fields=['gene_symbol', 'source'],
                name='mendgene_symbol_source_idx',
            ),
        ]
        verbose_name = 'Mendelian gene'
        verbose_name_plural = 'Mendelian genes'

    def __str__(self):
        disease = f" → {self.disease_name}" if self.disease_name else ''
        return f"{self.gene_symbol} [{self.confidence}] ({self.source}){disease}"


class UserCategory(models.Model):
    """
    Categorias customizadas criadas pelo pesquisador para um projeto.

    Feature migrada do R: categorização definida pelo usuário.
    Permite que o pesquisador insira seus próprios termos e categorias
    para uma classificação personalizada, no escopo de um projeto.
    """
    project = models.ForeignKey(
        'DaVinciProject',
        on_delete=models.CASCADE,
        related_name='custom_categories'
    )
    name = models.CharField('Nome da Categoria', max_length=100)
    keywords = models.JSONField(
        'Keywords',
        default=list,
        help_text='Termos para classificação automática ou manual'
    )
    color = models.CharField(
        'Cor (hex)',
        max_length=7,
        blank=True,
        default='',
        help_text='Para visualização no frontend'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['project', 'name']
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.project.title})"


class EntityContext(models.Model):
    """
    Contexto semântico extraído do abstract — sentenças ao redor
    de entidades mencionadas (genes, drogas, variantes).

    Feature migrada do R: contexts() — analisa as sentenças ao redor
    de termos específicos para entender a relação semântica.

    Persistido para servir como input à camada de IA generativa futura
    (RAG sobre contextos curados).
    """

    class EntityType(models.TextChoices):
        GENE = 'gene', 'Gene'
        DRUG = 'drug', 'Droga'
        VARIANT = 'variant', 'Variante'
        DISEASE = 'disease', 'Doença'
        PATHWAY = 'pathway', 'Pathway'
        MESH = 'mesh', 'MeSH Term'

    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name='contexts')
    entity_type = models.CharField(max_length=20, choices=EntityType.choices)
    entity_name = models.CharField('Nome da Entidade', max_length=255, db_index=True)
    sentence = models.TextField(
        'Sentença Contexto',
        help_text='Sentença do abstract contendo a entidade'
    )
    sentence_position = models.SmallIntegerField(
        'Posição no Abstract',
        default=0,
        help_text='Índice da sentença no abstract (0-based; -1 = sentinela "processado sem snippet")'
    )
    computed_at = models.DateTimeField(
        'Derivado em',
        null=True,
        blank=True,
        help_text=(
            'Momento em que o snippet foi derivado/materializado. '
            'Comparar com paper.updated_at para invalidar cache stale.'
        )
    )

    class Meta:
        unique_together = ['paper', 'entity_type', 'entity_name', 'sentence_position']
        indexes = [
            models.Index(fields=['entity_type', 'entity_name']),
            models.Index(fields=['paper', 'entity_type']),
            models.Index(fields=['paper', 'entity_type', 'entity_name']),
        ]

    def __str__(self):
        return f"{self.entity_type}:{self.entity_name} — PMID:{self.paper.pmid}"


# =============================================================================
# SEÇÃO 5: RELAÇÕES POR PROJETO (Curadoria do usuário)
# =============================================================================

class ProjectPaper(models.Model):
    """
    Relação Paper ↔ Projeto do Usuário.

    É aqui que a curadoria acontece. O mesmo Paper pode estar em N projetos,
    mas cada projeto tem seu próprio status de curadoria, notas e categorias.
    """

    class CurationStatus(models.TextChoices):
        PENDING = 'pending', 'Pendente'
        INCLUDED = 'included', 'Incluído'
        EXCLUDED = 'excluded', 'Excluído'
        MAYBE = 'maybe', 'Talvez'

    project = models.ForeignKey(
        DaVinciProject,
        on_delete=models.CASCADE,
        related_name='project_papers'
    )
    paper = models.ForeignKey(
        Paper,
        on_delete=models.CASCADE,
        related_name='in_projects'
    )
    ingestion_job = models.ForeignKey(
        'IngestionJob',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_papers',
        help_text='Job de ingestão que criou este vínculo (proveniência da busca)',
    )

    # Curadoria
    curation_status = models.CharField(
        max_length=20,
        choices=CurationStatus.choices,
        default=CurationStatus.PENDING
    )
    exclusion_reason = models.CharField(
        'Motivo da Exclusão',
        max_length=500,
        blank=True,
        default='',
        help_text='Auditável: por que o pesquisador excluiu este paper'
    )
    notes = models.TextField('Notas do Pesquisador', blank=True, default='')

    # Categorização clínica (estruturada — via ClinicalCategory)
    clinical_categories = models.ManyToManyField(
        ClinicalCategory,
        through='ProjectPaperClinicalCategory',
        blank=True,
        help_text='Categorias clínicas atribuídas ao paper (diagnosis, treatment, etc.)'
    )

    # Categorização customizada do usuário
    user_categories = models.ManyToManyField(
        UserCategory,
        blank=True,
        help_text='Categorias customizadas do pesquisador para este projeto'
    )

    # Relevância (score do Rust ou manual)
    relevance_score = models.FloatField(
        'Score de Relevância',
        blank=True,
        null=True,
        help_text='0.0 a 1.0 — calculado pelo Rust ou ajustado manualmente'
    )

    # Controle
    added_at = models.DateTimeField(auto_now_add=True)
    curated_at = models.DateTimeField('Curado em', blank=True, null=True)

    class Meta:
        unique_together = ['project', 'paper']
        indexes = [
            models.Index(fields=['project', 'curation_status']),
            models.Index(fields=['relevance_score']),
            models.Index(fields=['project', 'ingestion_job']),
        ]

    def __str__(self):
        return f"PMID:{self.paper.pmid} → {self.project.title} [{self.curation_status}]"


class ProjectPaperClinicalCategory(models.Model):
    """
    Relação Paper ↔ Categoria Clínica dentro de um projeto, com score.

    Feature migrada do R: categories.davinci() — a categorização pode
    ser automática (via Rust regex + scoring) ou manual (pelo pesquisador).
    O confidence_score permite ranking e filtragem.
    """
    project_paper = models.ForeignKey(
        ProjectPaper,
        on_delete=models.CASCADE,
        related_name='category_assignments'
    )
    category = models.ForeignKey(
        ClinicalCategory,
        on_delete=models.CASCADE
    )
    confidence_score = models.FloatField(
        'Score de Confiança',
        default=1.0,
        help_text='0.0 a 1.0 — automático (Rust) ou 1.0 se manual'
    )
    is_manual = models.BooleanField(
        'Classificação Manual',
        default=False,
        help_text='True se o pesquisador atribuiu manualmente'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['project_paper', 'category']

    def __str__(self):
        return f"{self.category.name} → PMID:{self.project_paper.paper.pmid}"


class ProjectDataset(models.Model):
    """
    Relação Dataset Ômico ↔ Projeto do Usuário.
    Mesma lógica de curadoria que ProjectPaper.
    """

    class CurationStatus(models.TextChoices):
        PENDING = 'pending', 'Pendente'
        INCLUDED = 'included', 'Incluído'
        EXCLUDED = 'excluded', 'Excluído'
        QUEUED_DOWNLOAD = 'queued', 'Na Fila de Download'
        DOWNLOADED = 'downloaded', 'Baixado'

    project = models.ForeignKey(
        DaVinciProject,
        on_delete=models.CASCADE,
        related_name='project_datasets'
    )
    dataset = models.ForeignKey(
        OmicDataset,
        on_delete=models.CASCADE,
        related_name='in_projects'
    )
    ingestion_job = models.ForeignKey(
        'IngestionJob',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_datasets',
        help_text='Job de ingestão que criou este vínculo (proveniência da busca)',
    )

    # Curadoria
    curation_status = models.CharField(
        max_length=20,
        choices=CurationStatus.choices,
        default=CurationStatus.PENDING
    )
    exclusion_reason = models.CharField(max_length=500, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    relevance_score = models.FloatField(blank=True, null=True)

    # Controle
    added_at = models.DateTimeField(auto_now_add=True)
    curated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ['project', 'dataset']
        indexes = [
            models.Index(fields=['project', 'curation_status']),
            models.Index(fields=['project', 'ingestion_job']),
        ]

    def __str__(self):
        return f"{self.dataset.accession} → {self.project.title} [{self.curation_status}]"


class ProjectSample(models.Model):
    """
    Relação Amostra Ômica ↔ Projeto do Usuário.

    Espelha ProjectDataset: mesma lógica de curadoria auditável por projeto.
    A mesma OmicSample pode estar em N projetos, mas cada projeto mantém seu
    próprio status de curadoria, motivo de exclusão e notas.

    Ingestão sob demanda: ProjectSample é criado quando o dataset pai é curado
    como `included` (espelha o ciclo de ProjectDataset).

    As choices de curadoria seguem ProjectPaper (pending/included/excluded/maybe),
    pois amostras são apenas metadados — não têm ciclo de download como datasets.
    """

    class CurationStatus(models.TextChoices):
        PENDING = 'pending', 'Pendente'
        INCLUDED = 'included', 'Incluído'
        EXCLUDED = 'excluded', 'Excluído'
        MAYBE = 'maybe', 'Talvez'

    project = models.ForeignKey(
        DaVinciProject,
        on_delete=models.CASCADE,
        related_name='project_samples'
    )
    sample = models.ForeignKey(
        OmicSample,
        on_delete=models.CASCADE,
        related_name='in_projects'
    )

    # Curadoria (idêntico a ProjectDataset)
    curation_status = models.CharField(
        max_length=20,
        choices=CurationStatus.choices,
        default=CurationStatus.PENDING
    )
    exclusion_reason = models.CharField(max_length=500, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    relevance_score = models.FloatField(blank=True, null=True)

    # Controle (idêntico a ProjectDataset)
    added_at = models.DateTimeField(auto_now_add=True)
    curated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        unique_together = ['project', 'sample']
        indexes = [
            models.Index(fields=['project', 'curation_status']),
        ]

    def __str__(self):
        return f"{self.sample.accession} → {self.project.title} [{self.curation_status}]"


class ProjectPaperDataset(models.Model):
    """
    PONTE entre literatura e ômicas dentro de um projeto.

    Esta é a tabela que permite a análise integrada:
    "Este paper (PMID:12345) está relacionado a este dataset (GSE67890)
    dentro do projeto X, e o pesquisador confirmou a relação."
    """

    class LinkConfidence(models.TextChoices):
        AUTO = 'auto', 'Automático (elink/co-ocorrência)'
        CONFIRMED = 'confirmed', 'Confirmado pelo Pesquisador'
        REJECTED = 'rejected', 'Rejeitado pelo Pesquisador'

    project = models.ForeignKey(DaVinciProject, on_delete=models.CASCADE)
    project_paper = models.ForeignKey(ProjectPaper, on_delete=models.CASCADE)
    project_dataset = models.ForeignKey(ProjectDataset, on_delete=models.CASCADE)
    confidence = models.CharField(
        max_length=20,
        choices=LinkConfidence.choices,
        default=LinkConfidence.AUTO
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['project', 'project_paper', 'project_dataset']

    def __str__(self):
        return (
            f"PMID:{self.project_paper.paper.pmid} ↔ "
            f"{self.project_dataset.dataset.accession} [{self.confidence}]"
        )


# =============================================================================
# SEÇÃO 6: ESTATÍSTICAS E AGREGAÇÕES POR PROJETO
# =============================================================================

class ProjectStats(models.Model):
    """
    Cache de estatísticas do projeto.

    Atualizado pelo Rust após cada ingestão ou pelo Django via
    View Materializada (REFRESH MATERIALIZED VIEW CONCURRENTLY).

    Evita queries pesadas de COUNT/GROUP BY em tempo real.
    """
    project = models.OneToOneField(
        DaVinciProject,
        on_delete=models.CASCADE,
        related_name='stats'
    )

    # Literatura
    total_papers = models.PositiveIntegerField(default=0)
    included_papers = models.PositiveIntegerField(default=0)
    excluded_papers = models.PositiveIntegerField(default=0)
    pending_papers = models.PositiveIntegerField(default=0)

    # Ômicas
    total_datasets = models.PositiveIntegerField(default=0)
    included_datasets = models.PositiveIntegerField(default=0)
    total_samples = models.PositiveIntegerField(default=0)
    included_samples = models.PositiveIntegerField(default=0)

    # Agregações (populadas como JSON para flexibilidade)
    papers_by_year = models.JSONField(default=dict, blank=True)
    papers_by_journal = models.JSONField(default=dict, blank=True)
    papers_by_country = models.JSONField(
        default=dict,
        blank=True,
        help_text='Distribuição geográfica (extraída de PaperAuthor.country)'
    )
    papers_by_clinical_category = models.JSONField(
        default=dict,
        blank=True,
        help_text='Contagem por categoria clínica (diagnosis, treatment, etc.)'
    )
    datasets_by_omic_type = models.CharField(max_length=200, blank=True)
    omic_subcategory = models.CharField(max_length=500, blank=True)
    datasets_by_omic_type = models.JSONField(default=dict, blank=True)
    datasets_by_organism = models.JSONField(default=dict, blank=True)
    top_genes = models.JSONField(
        default=list,
        blank=True,
        help_text='Top N genes mencionados na literatura do projeto'
    )
    top_drugs = models.JSONField(
        default=list,
        blank=True,
        help_text='Top N drogas/fármacos mencionados na literatura do projeto'
    )
    top_mesh_terms = models.JSONField(
        default=list,
        blank=True,
        help_text='Top N MeSH terms na literatura do projeto'
    )
    top_variants = models.JSONField(default=list, blank=True)

    # Controle
    last_computed = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Stats: {self.project.title}"


# =============================================================================
# SEÇÃO 7: CONTROLE DE INGESTÃO (Monitoramento Rust ↔ Django)
# =============================================================================

class IngestionJob(models.Model):
    """
    Registro de cada job de ingestão disparado pelo Django para o Rust.

    O Django cria o job, o Rust atualiza o status via UPDATE direto
    na tabela (sem ORM), e o Django monitora via polling ou webhook.
    """

    class JobType(models.TextChoices):
        PUBMED_SEARCH = 'pubmed_search', 'Busca PubMed'
        PUBMED_FETCH = 'pubmed_fetch', 'Fetch PubMed XML'
        GEO_SEARCH = 'geo_search', 'Busca GEO'
        SRA_SEARCH = 'sra_search', 'Busca SRA'
        GWAS_SEARCH = 'gwas_search', 'Busca GWAS Catalog'
        PRIDE_SEARCH = 'pride_search', 'Busca PRIDE'
        SAMPLE_FETCH = 'sample_fetch', 'Fetch de Amostras'
        VARIANT_ANNOTATION = 'variant_annotation', 'Anotação de Variantes'
        GENE_NER = 'gene_ner', 'Extração de Genes'
        DRUG_NER = 'drug_ner', 'Extração de Drogas'
        CONTEXT_EXTRACTION = 'context_extraction', 'Extração de Contextos'
        GEO_SUPPLEMENTARY_DOWNLOAD = 'geo_supplementary_download', 'Download Suplementar GEO'
        FASTQ_DOWNLOAD = 'fastq_download', 'Download FASTQ'

    class JobStatus(models.TextChoices):
        PENDING = 'pending', 'Pendente'
        RUNNING = 'running', 'Executando'
        COMPLETED = 'completed', 'Completo'
        FAILED = 'failed', 'Falhou'
        CANCELLED = 'cancelled', 'Cancelado'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        DaVinciProject,
        on_delete=models.CASCADE,
        related_name='ingestion_jobs'
    )
    job_type = models.CharField(max_length=30, choices=JobType.choices)
    status = models.CharField(
        max_length=20,
        choices=JobStatus.choices,
        default=JobStatus.PENDING
    )

    # Parâmetros enviados ao Rust
    parameters = models.JSONField(
        default=dict,
        help_text='Query, date range, PMIDs, etc.'
    )

    # Resultado
    records_processed = models.PositiveIntegerField(default=0)
    records_inserted = models.PositiveIntegerField(default=0)
    records_updated = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default='')

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', 'status']),
            models.Index(fields=['job_type', 'status']),
        ]

    def __str__(self):
        return f"{self.job_type} — {self.project.title} [{self.status}]"


# =============================================================================
# SQL AUXILIAR (Executar manualmente ou via migration RunSQL)
# =============================================================================

"""
-- 1. Trigger de FTS para Paper (dispara no INSERT/UPDATE do COPY do Rust)
CREATE OR REPLACE FUNCTION update_paper_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.abstract, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER paper_search_trigger
    BEFORE INSERT OR UPDATE OF title, abstract
    ON davinci_paper
    FOR EACH ROW
    EXECUTE FUNCTION update_paper_search_vector();


-- 2. Trigger de FTS para OmicDataset
CREATE OR REPLACE FUNCTION update_dataset_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.summary, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dataset_search_trigger
    BEFORE INSERT OR UPDATE OF title, summary
    ON davinci_omicdataset
    FOR EACH ROW
    EXECUTE FUNCTION update_dataset_search_vector();


-- 3. View Materializada para stats (refresh periódico via cron ou Celery beat)
CREATE MATERIALIZED VIEW mv_project_paper_stats AS
SELECT
    pp.project_id,
    COUNT(*) AS total_papers,
    COUNT(*) FILTER (WHERE pp.curation_status = 'included') AS included_papers,
    COUNT(*) FILTER (WHERE pp.curation_status = 'excluded') AS excluded_papers,
    COUNT(*) FILTER (WHERE pp.curation_status = 'pending') AS pending_papers,
    jsonb_object_agg(
        COALESCE(p.pub_year::text, 'unknown'),
        year_count
    ) AS papers_by_year
FROM davinci_projectpaper pp
JOIN davinci_paper p ON pp.paper_id = p.id
LEFT JOIN LATERAL (
    SELECT p.pub_year, COUNT(*) AS year_count
    FROM davinci_projectpaper pp2
    JOIN davinci_paper p2 ON pp2.paper_id = p2.id
    WHERE pp2.project_id = pp.project_id
    GROUP BY p2.pub_year
) yearly ON TRUE
GROUP BY pp.project_id;

CREATE UNIQUE INDEX ON mv_project_paper_stats (project_id);


-- 4. Índice para busca por gene across projects
CREATE INDEX idx_papergene_symbol_lower
    ON davinci_papergene (LOWER(gene_symbol));
"""