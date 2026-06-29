"""
Testes de regressão — lógica de n-gram do DiseaseAxisClassifierService.

Cobre os invariantes da lógica de extração e uso de n-grams de título
introduzida para corrigir falsos positivos/negativos na classificação de
disease_axis (OmnisPathway, Fase 3 — endurecimento do classificador).

Invariantes cobertos:
  1. N-gram exact destrava título longo: 'Glaucoma' (unigrama exato) classifica
     título longo como multifatorial, caso que o fuzzy do título-inteiro não pegava.
  2. Sub-span deconflict (FP#1): "cystic fibrosis" é monogênico; 'fibrosis' (sub-span
     multi) tem score zerado → resultado final: monogenic, não mixed.
  3. Exact-only para n-gram (FP#2): n-gram que SÓ casaria por fuzzy com termo
     não-relacionado NÃO é promovido via title_ngram — fuzzy é bloqueado para
     essa fonte; resultado: indeterminate (sem hit legítimo).
  4. Blocklist: termos genéricos ('disease', 'expression', 'stress', 'analysis')
     presentes em NGRAM_BLOCKLIST não viram candidatos.
  5. Unigrama curto: unigrama com < NGRAM_UNIGRAM_MIN_CHARS (6) é descartado
     antes de qualquer match.
  6. Mixed genuíno: título com sinal mono (cystic fibrosis) E multi (glaucoma)
     reais e não-sobrepostos → axis='mixed'.
  7. Indeterminate: título genérico sem sinal de doença → axis='indeterminate'.

Padrão: APITestCase DRF (sem pytest). Sem chamadas externas.
Requer Postgres real (índices B-tree em DiseaseAxisReference).

Fixtures injetadas diretamente no banco de teste (banco temporário do Django —
não afeta produção). As refs são criadas com source e name_normalized
determinísticos para garantir exact match controlado.
"""

from rest_framework.test import APITestCase

from apps.core.models import DiseaseAxisReference, OmicDataset
from apps.core.services.disease_axis_classifier_service import (
    NGRAM_BLOCKLIST,
    NGRAM_UNIGRAM_MIN_CHARS,
    _extract_ngram_candidates,
    _match_term_exact_only,
    classify_disease_axis_for_dataset,
)
from apps.core.services.disease_axis_service import normalize_trait_name


# =============================================================================
# Helpers
# =============================================================================

def make_dataset(accession, title='', summary='', **kwargs):
    """Cria OmicDataset mínimo com título e summary controlados."""
    return OmicDataset.objects.create(
        accession=accession,
        source_db='geo',
        title=title,
        summary=summary,
        omic_type='transcriptomic',
        **kwargs,
    )


def make_ref(name, axis, source='gwas_catalog'):
    """
    Cria DiseaseAxisReference com normalização automática via normalize_trait_name.

    Para exact match funcionar em title_ngram, name_normalized deve ser idêntico
    ao normalize_trait_name(ngram) — por isso usamos a mesma função aqui.
    """
    return DiseaseAxisReference.objects.create(
        name=name,
        name_normalized=normalize_trait_name(name),
        axis=axis,
        source=source,
        source_version='ngram-test-v1',
    )


# =============================================================================
# 1. N-gram exact destrava título longo (FP de recall histórico)
# =============================================================================

class NgramExactDestravasTituloLongoTests(APITestCase):
    """
    Invariante 1: título longo como "Glaucoma is a neurodegenerative disease..."
    não daria hit de title_summary via fuzzy (ratio ~0.22 contra 'glaucoma'),
    mas o unigrama 'Glaucoma' extraído dá exact hit.

    Setup: ref multifatorial para 'glaucoma' (GWAS Catalog). Nenhuma ref mono.
    Esperado: axis='multifactorial', método 'exact' via title_ngram.
    """

    def setUp(self):
        # Ref multifatorial: glaucoma (gwas_catalog — como no banco real)
        self.ref_glaucoma = make_ref('Glaucoma', 'multifactorial', source='gwas_catalog')

    def test_titulo_longo_glaucoma_classifica_multifactorial(self):
        """
        Título longo com 'Glaucoma' no início → multifactorial via exact n-gram.
        O título-inteiro como candidato único (title_summary) falharia no fuzzy
        porque o ratio contra 'glaucoma' (~7 chars vs. ~60 chars) cai < 0.22.
        """
        ds = make_dataset(
            'NGRAM_REG_01',
            title='Glaucoma is a neurodegenerative disease characterized by '
                  'progressive loss of retinal ganglion cells and optic nerve damage',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)

        self.assertEqual(result['axis'], 'multifactorial',
                         f'Esperado multifactorial, obtido {result["axis"]}. '
                         f'multi_matched={result["multi_matched"]}')
        self.assertEqual(result['multi_matched'], 'Glaucoma',
                         'O nome matched deve ser o registro canonical "Glaucoma"')

    def test_ngram_glaucoma_exactonly_hit(self):
        """
        Teste unitário direto: _match_term_exact_only('glaucoma') retorna hit
        multifatorial quando a ref existe.
        """
        hits = _match_term_exact_only('glaucoma')
        self.assertGreater(hits.multifactorial_score, 0.0,
                           'Exact hit de "glaucoma" deve ser > 0')
        self.assertEqual(hits.multi_method, 'exact')
        self.assertEqual(hits.multi_matched, 'Glaucoma')

    def test_glaucoma_no_inicio_do_titulo(self):
        """'Glaucoma genetics reveal novel loci' — unigrama no início → multifactorial."""
        ds = make_dataset(
            'NGRAM_REG_02',
            title='Glaucoma genetics reveal novel loci in population-based GWAS cohort',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'multifactorial')

    def test_glaucoma_no_meio_do_titulo(self):
        """'Transcriptomic profiling in Glaucoma patients' → multifactorial."""
        ds = make_dataset(
            'NGRAM_REG_03',
            title='Transcriptomic profiling in Glaucoma patients reveals pathway dysregulation',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'multifactorial')


# =============================================================================
# 2. Sub-span deconflict (FP#1) — "cystic fibrosis" vs "fibrosis"
# =============================================================================

class SubSpanDeconflictTests(APITestCase):
    """
    Invariante 2: "cystic fibrosis" é monogênico. 'Fibrosis' isolado aparece
    como sub-span multi na ref. Sem deconflito, ambos dariam hit e o resultado
    seria 'mixed' (FP#1). Com deconflito, 'fibrosis' (sub-span do nome mono
    'cystic fibrosis') tem score zerado → resultado: monogenic.

    Setup:
      - Ref monogênica: 'Cystic fibrosis' (orphanet)
      - Ref multifatorial: 'Fibrosis' (gwas_catalog) — o sub-span problemático

    O deconflito age em classify_disease_axis_for_dataset: ao detectar que
    normalize_trait_name('Fibrosis') está contido em normalize_trait_name('Cystic fibrosis'),
    o multi hit (fibrosis) tem score zerado.
    """

    def setUp(self):
        self.ref_cf = make_ref('Cystic fibrosis', 'monogenic', source='orphanet')
        # Fibrosis como sub-span multi — trigger do FP#1 histórico
        self.ref_fibrosis = make_ref('Fibrosis', 'multifactorial', source='gwas_catalog')

    def test_cystic_fibrosis_nao_e_mixed(self):
        """
        Dataset "...cystic fibrosis..." → monogenic, NÃO mixed.
        O hit multi 'fibrosis' deve ser zerado por ser substring de 'cystic fibrosis'.
        """
        ds = make_dataset(
            'NGRAM_SUBSPAN_01',
            title='RNA-seq analysis of cystic fibrosis airway epithelial cells',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)

        self.assertEqual(result['axis'], 'monogenic',
                         f'Esperado monogenic (não mixed). '
                         f'axis={result["axis"]} mono={result["mono_matched"]} '
                         f'multi={result["multi_matched"]}')
        self.assertEqual(result['mono_matched'], 'Cystic fibrosis',
                         'Matched mono deve ser "Cystic fibrosis"')

    def test_fibrosis_sub_span_score_zerado(self):
        """
        O score do hit 'fibrosis' (multi) deve ser 0 após deconflito,
        pois 'fibrosis' é substring de 'cystic fibrosis' (o matched mono).
        Verificação indireta: axis=monogenic (e não mixed) prova que multi=0.
        """
        ds = make_dataset(
            'NGRAM_SUBSPAN_02',
            title='Gene expression in cystic fibrosis bronchial organoids',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        # Se multi_matched estiver preenchido E axis for monogenic, significa
        # que o score do multi foi zerado — confirmamos pelo axis.
        self.assertIn(result['axis'], ('monogenic',),
                      'Deconflito deve zerar o hit multi (fibrosis sub-span) '
                      f'— axis obtido: {result["axis"]}')

    def test_cystic_fibrosis_como_unigrama_nao_causa_fp(self):
        """
        'fibrosis' isolado no título sem 'cystic' → deve ainda ser multi,
        mas quando 'cystic fibrosis' também está presente, o deconflito age.
        Este teste verifica que 'fibrosis' SOZINHO (sem 'cystic') dá multi.
        """
        ds = make_dataset(
            'NGRAM_SUBSPAN_03',
            title='Pulmonary fibrosis treatment response study',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        # 'fibrosis' sozinho (sem 'cystic') deve dar hit multi
        self.assertEqual(result['axis'], 'multifactorial',
                         '"fibrosis" sozinho (sem "cystic") deve ser multifactorial')

    def test_cystic_fibrosis_completo_e_prioritario(self):
        """
        'Cystic fibrosis in adults with severe lung disease' →
        ngram 'cystic fibrosis' (2 tokens) capturado e dá mono hit.
        """
        ds = make_dataset(
            'NGRAM_SUBSPAN_04',
            title='Cystic fibrosis in adults with severe lung disease and exacerbations',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'monogenic')


# =============================================================================
# 3. Exact-only para n-gram (FP#2) — fuzzy bloqueado em title_ngram
# =============================================================================

class ExactOnlyNgramTests(APITestCase):
    """
    Invariante 3: candidatos de fonte 'title_ngram' passam SOMENTE por
    _match_term_exact_only, nunca por fuzzy. Um n-gram que existiria apenas
    via fuzzy (sem exact hit) não deve gerar classificação.

    Cenário concreto (FP#2 histórico):
      - Ref multifatorial: 'brain development' (gwas_catalog)
        → name_normalized = 'brain development'
      - Título: 'Transcriptomic profiling of cardiac tissue response'
        Não contém 'brain development' como ngram — portanto sem exact hit.
      - Fuzzy sobre 'cardiac tissue' ou 'tissue response' contra 'brain development'
        poderia dar ratio 0.70–0.75, mas NÃO deve acontecer via title_ngram.
      - Resultado esperado: indeterminate (sem nenhum hit legítimo).

    Nota: como o título não contém nenhum token que leve a 'brain development'
    como n-gram, simplesmente confirmar que não há hit multi é suficiente.
    Adicionalmente testamos _match_term_exact_only diretamente para confirmar
    que um termo existente SÓ via fuzzy não é promovido.
    """

    def setUp(self):
        # Ref multi que só existiria via fuzzy para termos relacionados
        self.ref_brain_dev = make_ref('Brain development', 'multifactorial',
                                      source='gwas_catalog')
        # Ref mono que nunca dará hit no título cardíaco
        self.ref_hcm = make_ref('Hypertrophic cardiomyopathy', 'monogenic',
                                 source='orphanet')

    def test_ngram_sem_exact_hit_nao_promove_via_fuzzy(self):
        """
        Título cardíaco SEM 'brain development' como ngram exato →
        indeterminate (sem nenhum hit via title_ngram).

        Sem a restrição exact-only, 'brain development' poderia ser atingido
        por fuzzy de algum bigrama cardíaco com ratio > 0.70 (FP#2).
        """
        ds = make_dataset(
            'NGRAM_EXACTONLY_01',
            title='Transcriptomic profiling of cardiac tissue in heart failure patients',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate',
                         f'Título cardíaco não deve cair em multifactorial via fuzzy n-gram. '
                         f'axis={result["axis"]} multi_matched={result["multi_matched"]}')

    def test_match_term_exact_only_nao_usa_fuzzy(self):
        """
        _match_term_exact_only('brain') não deve dar hit em 'brain development'
        (termo incompleto — sem exact match).
        """
        hits = _match_term_exact_only('brain')
        self.assertEqual(hits.multifactorial_score, 0.0,
                         '"brain" (sem "development") não deve dar exact hit em "brain development"')

    def test_match_term_exact_only_retorna_hit_quando_exato(self):
        """
        _match_term_exact_only('brain development') DEVE dar hit exato
        quando a ref existe com esse name_normalized.
        """
        hits = _match_term_exact_only('brain development')
        self.assertGreater(hits.multifactorial_score, 0.0,
                           '"brain development" deve dar exact hit quando ref existe')
        self.assertEqual(hits.multi_method, 'exact')

    def test_titulo_com_palavras_separadas_nao_vira_hit_fuzzy(self):
        """
        Título com 'brain' e 'development' em posições não-contíguas
        não gera n-gram 'brain development' e portanto não dá hit.
        """
        ds = make_dataset(
            'NGRAM_EXACTONLY_02',
            # 'brain' e 'development' aparecem mas separados por muitas palavras
            title='A study of neural connectivity in the brain and fetal development markers',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        # 'brain development' NÃO é n-gram contíguo de até 4 tokens → sem exact hit
        # (pode ter outros hits se 'brain development' aparecer como ngram de outros tokens,
        # mas o título acima não tem os dois tokens contíguos)
        # Verificação: multi_matched não deve ser 'Brain development'
        self.assertNotEqual(result.get('multi_matched'), 'Brain development',
                            '"Brain development" não deve aparecer como matched quando '
                            'os tokens não são contíguos no título')


# =============================================================================
# 4. Blocklist — termos genéricos não viram candidatos
# =============================================================================

class BlocklistTests(APITestCase):
    """
    Invariante 4: termos presentes em NGRAM_BLOCKLIST são descartados por
    _extract_ngram_candidates ANTES de qualquer match contra DiseaseAxisReference.

    Não depende de banco — testa a função de extração diretamente.
    """

    def test_disease_na_blocklist(self):
        """'disease' está em NGRAM_BLOCKLIST."""
        self.assertIn('disease', NGRAM_BLOCKLIST,
                      '"disease" deve estar em NGRAM_BLOCKLIST')

    def test_expression_na_blocklist(self):
        """'expression' está em NGRAM_BLOCKLIST."""
        self.assertIn('expression', NGRAM_BLOCKLIST,
                      '"expression" deve estar em NGRAM_BLOCKLIST')

    def test_stress_na_blocklist(self):
        """'stress' está em NGRAM_BLOCKLIST."""
        self.assertIn('stress', NGRAM_BLOCKLIST,
                      '"stress" deve estar em NGRAM_BLOCKLIST')

    def test_analysis_na_blocklist(self):
        """'analysis' está em NGRAM_BLOCKLIST."""
        self.assertIn('analysis', NGRAM_BLOCKLIST,
                      '"analysis" deve estar em NGRAM_BLOCKLIST')

    def test_disease_nao_vira_candidato(self):
        """
        Unigrama 'disease' presente em NGRAM_BLOCKLIST é descartado por
        _extract_ngram_candidates — não aparece na lista de candidatos.
        """
        texto = 'Heart disease patients enrolled in this study'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        # Extrair todos os ngrams normalizados que foram retornados
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('disease', norms,
                         '"disease" bloqueado deve estar ausente dos candidatos')

    def test_expression_nao_vira_candidato(self):
        """
        'expression' presente em NGRAM_BLOCKLIST é descartado.
        'gene expression' seria descartado pelo filtro 3 (todos os tokens bloqueados)?
        Depende de 'gene' também estar na blocklist — verificamos ambos.
        """
        texto = 'Analysis of gene expression in cancer cells'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('expression', norms,
                         '"expression" bloqueado deve estar ausente dos candidatos')

    def test_stress_isolado_nao_vira_candidato(self):
        """
        'stress' isolado é bloqueado mesmo que apareça no título.
        """
        texto = 'Oxidative stress response in neuronal cells'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('stress', norms,
                         '"stress" bloqueado deve estar ausente dos candidatos')

    def test_ngram_com_termo_nao_bloqueado_passa(self):
        """
        N-gram que contém ao menos um token fora da blocklist não é bloqueado
        pelo filtro 3 (n-gram inteiramente bloqueado).
        'cancer cells' → 'cells' está na blocklist mas 'cancer' não — o bigrama
        não é "inteiramente bloqueado" por definição do filtro 3 (veja: filtro 3
        remove apenas n-grams onde TODOS os tokens estão na blocklist).

        Nota: o resultado depende de 'cancer' ter >= 6 chars (6 chars → passa filtro unigrama).
        """
        texto = 'Cancer prognosis study'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertIn('cancer', norms,
                      '"cancer" (6 chars, fora da blocklist) deve aparecer nos candidatos')

    def test_todos_bloqueados_bigrama_descartado(self):
        """
        Bigrama cujos dois tokens estão ambos na blocklist é descartado pelo filtro 3.
        Exemplo: 'cell death' → 'cell' (blocklist: 'cells') e 'death' (blocklist: 'death').

        Nota: 'cell' NÃO está diretamente na blocklist — 'cells' está.
        Verificar com um bigrama em que ambos os tokens aparecem na blocklist:
        'gene expression' → normalize('gene') = 'gene' (blocklist) e
        normalize('expression') = 'expression' (blocklist) → ambos bloqueados → descartado.
        """
        self.assertIn('gene', NGRAM_BLOCKLIST, '"gene" deve estar na blocklist')
        self.assertIn('expression', NGRAM_BLOCKLIST, '"expression" deve estar na blocklist')

        texto = 'Gene expression profiling study'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('gene expression', norms,
                         '"gene expression" (ambos tokens bloqueados) deve ser descartado')


# =============================================================================
# 5. Unigrama curto — < NGRAM_UNIGRAM_MIN_CHARS descartado
# =============================================================================

class UnigramasCurtosTests(APITestCase):
    """
    Invariante 5: unigramas com < NGRAM_UNIGRAM_MIN_CHARS (6) caracteres
    são descartados em _extract_ngram_candidates antes de qualquer match.

    Exemplos de unigramas que devem ser descartados: 'pain' (4), 'age' (3),
    'fat' (3), 'bmi' (3), 'flu' (3).

    Unigramas que DEVEM passar: 'autism' (6), 'cancer' (6), 'asthma' (6).
    """

    def test_ngram_unigram_min_chars_valor(self):
        """NGRAM_UNIGRAM_MIN_CHARS deve ser 6 (invariante travada)."""
        self.assertEqual(NGRAM_UNIGRAM_MIN_CHARS, 6,
                         'NGRAM_UNIGRAM_MIN_CHARS deve ser 6')

    def test_pain_4chars_descartado(self):
        """'pain' (4 chars) é descartado como unigrama."""
        texto = 'Chronic pain management in elderly patients'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('pain', norms,
                         '"pain" (4 chars) deve ser descartado como unigrama curto')

    def test_age_3chars_descartado(self):
        """'age' (3 chars) é descartado."""
        texto = 'Age of onset in neurodegenerative conditions'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('age', norms,
                         '"age" (3 chars) deve ser descartado como unigrama curto')

    def test_fat_3chars_descartado(self):
        """'fat' (3 chars) é descartado."""
        texto = 'Adipose fat tissue transcriptomics'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('fat', norms,
                         '"fat" (3 chars) deve ser descartado como unigrama curto')

    def test_autism_6chars_passa(self):
        """'autism' (6 chars) NÃO é descartado — está no limiar exato."""
        texto = 'Autism spectrum disorder genomics study'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertIn('autism', norms,
                      '"autism" (6 chars = limiar) deve passar no filtro de unigrama')

    def test_cancer_6chars_passa(self):
        """'cancer' (6 chars) passa no limiar."""
        texto = 'Cancer biomarker discovery in breast tissue'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertIn('cancer', norms,
                      '"cancer" (6 chars) deve passar no filtro de unigrama')

    def test_asthma_6chars_passa(self):
        """'asthma' (6 chars) passa no limiar."""
        texto = 'Asthma exacerbation study in pediatric cohort'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertIn('asthma', norms,
                      '"asthma" (6 chars) deve passar no filtro de unigrama')

    def test_bmi_3chars_descartado(self):
        """'bmi' (3 chars) é descartado."""
        texto = 'BMI genetic determinants in European populations'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        # 'bmi' em uppercase é tokenizado como 'BMI' → normalize → 'bmi' (3 chars)
        norms = [normalize_trait_name(raw) for raw, _ in candidates]
        self.assertNotIn('bmi', norms,
                         '"bmi" (3 chars) deve ser descartado como unigrama curto')


# =============================================================================
# 6. Mixed genuíno — sinal mono E multi reais e não-sobrepostos
# =============================================================================

class MixedGenuinoTests(APITestCase):
    """
    Invariante 6: dataset com âncora monogênica E multifatorial reais,
    sem sobreposição de sub-span, deve resultar em axis='mixed'.

    Setup: título com 'cystic fibrosis' (mono) E 'glaucoma' (multi).
    'cystic fibrosis' tem 2 tokens; 'fibrosis' é sub-span de 'cystic fibrosis'
    mas como o eixo oposto do matched é 'glaucoma' (não 'fibrosis'),
    o deconflito NÃO age — deconflito só age quando o matched de um eixo
    é substring do matched do outro.

    normalize_trait_name('Glaucoma') = 'glaucoma'
    normalize_trait_name('Cystic fibrosis') = 'cystic fibrosis'
    'glaucoma' NOT IN 'cystic fibrosis' e 'cystic fibrosis' NOT IN 'glaucoma'
    → deconflito não age → ambos os eixos mantêm score > 0 → mixed.
    """

    def setUp(self):
        self.ref_cf = make_ref('Cystic fibrosis', 'monogenic', source='orphanet')
        self.ref_glaucoma = make_ref('Glaucoma', 'multifactorial', source='gwas_catalog')

    def test_titulo_com_mono_e_multi_distintos_e_mixed(self):
        """
        Título com 'cystic fibrosis' (mono) E 'glaucoma' (multi) sem sobreposição
        → axis='mixed'.
        """
        ds = make_dataset(
            'NGRAM_MIXED_01',
            title='Cystic fibrosis and glaucoma co-occurrence in adult patients',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'mixed',
                         f'Esperado mixed (dois hits distintos). '
                         f'axis={result["axis"]} mono={result["mono_matched"]} '
                         f'multi={result["multi_matched"]}')
        self.assertEqual(result['mono_matched'], 'Cystic fibrosis')
        self.assertEqual(result['multi_matched'], 'Glaucoma')

    def test_deconflito_nao_age_quando_hits_distintos(self):
        """
        'glaucoma' não está contido em 'cystic fibrosis' e vice-versa
        → deconflito não age → ambos os scores permanecem > 0 → mixed.

        Prova indireta: se o deconflito agisse indevidamente, um dos
        scores seria zerado e o resultado seria mono ou multi, não mixed.
        """
        ds = make_dataset(
            'NGRAM_MIXED_02',
            title='Genetic overlap between glaucoma and cystic fibrosis: a multi-trait study',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'mixed',
                         'Deconflito NÃO deve agir quando os hits são distintos '
                         f'(glaucoma ≠ substring de cystic fibrosis). '
                         f'axis obtido: {result["axis"]}')


# =============================================================================
# 7. Indeterminate — título genérico sem sinal de doença
# =============================================================================

class IndeterminateTests(APITestCase):
    """
    Invariante 7: datasets com títulos genéricos (sequenciamento de metagenoma,
    método-foco, sem doença no título ou summary) → axis='indeterminate'.

    Não requer nenhuma ref no banco — simplesmente nenhum n-gram vai dar hit.
    """

    def setUp(self):
        # Cria ref "isca" para garantir que se houver hit indevido via fuzzy
        # em title_ngram, o teste pegará o erro.
        # Qualquer ref monogênica ou multi que não deveria ser atingida.
        make_ref('Autism spectrum disorder', 'multifactorial', source='gwas_catalog')
        make_ref('Cystic fibrosis', 'monogenic', source='orphanet')

    def test_metagenome_raw_sequence_indeterminate(self):
        """
        'gut metagenome Raw sequence reads' → indeterminate.
        Nenhum n-gram deve dar hit em refs de doença.
        """
        ds = make_dataset(
            'NGRAM_INDET_01',
            title='gut metagenome Raw sequence reads',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate',
                         f'Título de metagenoma genérico deve ser indeterminate. '
                         f'axis={result["axis"]} mono={result["mono_matched"]} '
                         f'multi={result["multi_matched"]}')

    def test_titulo_metodo_sem_doenca_indeterminate(self):
        """
        'RNA-seq transcriptomic profiling pipeline benchmark study' →
        indeterminate (nenhum n-gram é nome de doença).
        """
        ds = make_dataset(
            'NGRAM_INDET_02',
            title='RNA-seq transcriptomic profiling pipeline benchmark study',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate')

    def test_titulo_animal_sem_doenca_indeterminate(self):
        """
        'Mouse liver transcriptome in response to high fat diet' →
        indeterminate ('fat' é curto e descartado; os demais não casam).
        """
        ds = make_dataset(
            'NGRAM_INDET_03',
            title='Mouse liver transcriptome in response to high fat diet',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate')

    def test_titulo_e_summary_vazios_indeterminate(self):
        """Dataset sem título nem summary → sem candidatos → indeterminate."""
        ds = make_dataset(
            'NGRAM_INDET_04',
            title='',
            summary='',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate')

    def test_dataset_sem_refs_no_banco_indeterminate(self):
        """
        Sem refs no banco (banco de teste vazio neste setUp diferente),
        qualquer título resulta em indeterminate.

        Nota: este subcase usa tearDown implícito por transação — as refs
        criadas no setUp deste TestCase NÃO aparecem aqui porque cada
        método de teste tem isolamento de transação. As refs de outros
        TestCase também não aparecem (TestCase usa rollback).
        """
        # Deletar as refs criadas no setUp desta classe para confirmar banco vazio
        DiseaseAxisReference.objects.all().delete()
        ds = make_dataset(
            'NGRAM_INDET_05',
            title='Cystic fibrosis lung disease progression',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate',
                         'Sem refs no banco, qualquer título → indeterminate')


# =============================================================================
# 8. Regressão consolidada — deconflito + exact-only juntos
# =============================================================================

class RegressaoConsolidadaTests(APITestCase):
    """
    Testes de regressão que combinam múltiplos invariantes em cenários
    realistas, garantindo que nenhuma mudança futura rompa o comportamento
    conjunto.
    """

    def setUp(self):
        # Refs monogênicas
        make_ref('Cystic fibrosis', 'monogenic', source='orphanet')
        make_ref('Huntington disease', 'monogenic', source='orphanet')
        # Refs multifatoriais
        make_ref('Glaucoma', 'multifactorial', source='gwas_catalog')
        make_ref('Fibrosis', 'multifactorial', source='gwas_catalog')
        make_ref('Asthma', 'multifactorial', source='gwas_catalog')

    def test_huntington_longo_classifica_monogenic(self):
        """
        'Transcriptomic analysis of Huntington disease striatum in post-mortem tissue'
        → 'huntington disease' (2-gram) dá exact hit mono.
        """
        ds = make_dataset(
            'NGRAM_REG_CONSOL_01',
            title='Transcriptomic analysis of Huntington disease striatum in post-mortem tissue',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'monogenic',
                         f'Esperado monogenic. axis={result["axis"]}')

    def test_asthma_classifica_multifactorial(self):
        """
        'Asthma exacerbation genomics in pediatric cohort' →
        'asthma' (6 chars, fora da blocklist) dá exact hit multi.
        """
        ds = make_dataset(
            'NGRAM_REG_CONSOL_02',
            title='Asthma exacerbation genomics in pediatric cohort',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'multifactorial',
                         f'Esperado multifactorial. axis={result["axis"]}')

    def test_titulo_sem_doenca_com_refs_no_banco_indeterminate(self):
        """
        Mesmo com refs no banco, título que não contém nenhum n-gram
        de doença → indeterminate.
        """
        ds = make_dataset(
            'NGRAM_REG_CONSOL_03',
            title='Normalization methods for RNA-seq count data benchmark',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'indeterminate')

    def test_ngram_max_4_tokens(self):
        """
        N-gram de 4 tokens é extraído e pode dar hit quando existe ref exata.
        Exemplo: 'type 2 diabetes mellitus' (4 tokens) → ref multi.
        """
        make_ref('Type 2 diabetes mellitus', 'multifactorial', source='gwas_catalog')
        ds = make_dataset(
            'NGRAM_REG_CONSOL_04',
            title='Type 2 diabetes mellitus genetic architecture in diverse populations',
        )
        result = classify_disease_axis_for_dataset(ds, dry_run=True)
        self.assertEqual(result['axis'], 'multifactorial',
                         'N-gram de 4 tokens "type 2 diabetes mellitus" deve dar hit multi')

    def test_ngram_5_tokens_nao_extraido(self):
        """
        N-gram de 5 tokens NÃO é extraído por _extract_ngram_candidates (NGRAM_MAX_N = 4).

        Verificação direta na função de extração: o maior n-gram produzido tem
        no máximo 4 tokens. Texto com 10 tokens não gera nenhum n-gram de 5+.
        """
        texto = 'type 2 diabetes mellitus onset study in european cohort adults'
        candidates = _extract_ngram_candidates(texto, 'title_ngram')
        # Nenhum candidato deve ter 5+ tokens
        for raw, _ in candidates:
            token_count = len(raw.split())
            self.assertLessEqual(
                token_count,
                4,
                f'N-gram "{raw}" tem {token_count} tokens — maior que NGRAM_MAX_N=4',
            )
