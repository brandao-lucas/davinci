"""
test_storage_utils.py — Cobertura de apps/core/storage_utils.py

OmnisPathway Objetivo 2, Fase hardening M4.

Áreas cobertas:
  1. shared_omics_storage_key — path feliz: accession e filename válidos produzem
     chave no namespace omics/_shared/{accession}/{filename}.
  2. shared_omics_storage_key — validação de accession: '..' puro → ValueError;
     '.' puro → ValueError; '../secret' → ValueError; '/' → ValueError;
     string vazia → ValueError; e demais casos de path traversal.
  3. shared_omics_storage_key — validação de filename: mesmos casos para filename.
  4. Regressão positiva: 'file..name' e 'v1.2.3' são ACEITOS (pontos internos
     não são path traversal) e produzem a chave esperada.

Nota: omics_storage_key NÃO é testada com allowlist estrito pois foi revertida ao
comportamento original (sem validação de allowlist).  Testes que assumam rejeição de
caracteres especiais em omics_storage_key seriam incorretos e induziriam falsos
negativos em downloads legítimos do Obj 1.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.core.storage_utils import shared_omics_storage_key


# =============================================================================
# 1. shared_omics_storage_key — path feliz
# =============================================================================

class SharedOmicsStorageKeyValidCasesTests(SimpleTestCase):
    """
    Casos felizes: accession e filename válidos (apenas [A-Za-z0-9._-]).
    """

    def test_typical_cptac_accession_and_parquet_filename(self):
        """
        Accession e filename reais do CPTAC produzem chave no namespace _shared.
        """
        key = shared_omics_storage_key(
            'CPTAC-CCRCC-PROTEOME',
            'cptac_ccrcc_proteome.parquet',
        )
        self.assertEqual(
            key,
            'omics/_shared/CPTAC-CCRCC-PROTEOME/cptac_ccrcc_proteome.parquet',
        )

    def test_key_starts_with_shared_prefix(self):
        """Chave começa com omics/_shared/."""
        key = shared_omics_storage_key('DATASET-123', 'matrix.parquet')
        self.assertTrue(
            key.startswith('omics/_shared/'),
            f'Esperava início "omics/_shared/", obteve: {key!r}',
        )

    def test_key_contains_accession_and_filename_as_components(self):
        """Accession e filename aparecem como componentes distintos na chave."""
        accession = 'GSE99999'
        filename = 'counts.parquet'
        key = shared_omics_storage_key(accession, filename)
        self.assertIn(f'/{accession}/', key)
        self.assertTrue(key.endswith(f'/{filename}'))

    def test_underscore_hyphen_dot_digits_accepted_in_accession(self):
        """Underscores, hífens, pontos e dígitos são aceitos no accession."""
        key = shared_omics_storage_key('Study_v2.0-beta', 'file.parquet')
        self.assertIn('Study_v2.0-beta', key)

    def test_underscore_hyphen_dot_digits_accepted_in_filename(self):
        """Underscores, hífens, pontos e dígitos são aceitos no filename."""
        key = shared_omics_storage_key('DATASET', 'my_file-v1.0.parquet')
        self.assertIn('my_file-v1.0.parquet', key)

    def test_key_has_no_leading_slash(self):
        """Chave não começa com '/' (sem leading slash)."""
        key = shared_omics_storage_key('ACC', 'file.parquet')
        self.assertFalse(key.startswith('/'), f'Não deve ter leading slash: {key!r}')

    def test_key_has_no_trailing_slash(self):
        """Chave não termina com '/' (sem trailing slash)."""
        key = shared_omics_storage_key('ACC', 'file.parquet')
        self.assertFalse(key.endswith('/'), f'Não deve ter trailing slash: {key!r}')


# =============================================================================
# 2. shared_omics_storage_key — validação de accession (path traversal)
# =============================================================================

class SharedOmicsStorageKeyAccessionValidationTests(SimpleTestCase):
    """
    Rejeição de accessions que habilitariam path traversal ou colisão de chave
    no namespace _shared (compartilhado entre todos os projetos).
    """

    def test_accession_double_dot_pure_raises_value_error(self):
        """
        '..' puro como accession → ValueError (path traversal clássico).

        O fix pós-handoff adiciona verificação explícita de value in ('.', '..')
        após o allowlist regex.  Sem esse fix, '..' passaria o regex porque '.'
        é caractere aceito.  A chave resultante seria 'omics/_shared/../filename',
        que backends com normalização de path resolveriam como escalada de diretório.
        """
        with self.assertRaises(ValueError):
            shared_omics_storage_key('..', 'file.parquet')

    def test_accession_single_dot_pure_raises_value_error(self):
        """'.' puro como accession → ValueError (referência ao diretório corrente)."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('.', 'file.parquet')

    def test_accession_with_embedded_double_dot_and_slash_raises_value_error(self):
        """'../secret' como accession → ValueError ('../' contém '/' fora do allowlist)."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('../secret', 'file.parquet')

    def test_accession_with_slash_raises_value_error(self):
        """'/' no accession → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET/evil', 'file.parquet')

    def test_accession_with_backslash_raises_value_error(self):
        """Backslash no accession → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET\\evil', 'file.parquet')

    def test_accession_with_null_byte_raises_value_error(self):
        r"""'\0' no accession → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET\x00', 'file.parquet')

    def test_accession_with_space_raises_value_error(self):
        """Espaço no accession → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET evil', 'file.parquet')

    def test_empty_accession_raises_value_error(self):
        """Accession vazio → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('', 'file.parquet')

    def test_absolute_path_accession_raises_value_error(self):
        """/abs/path como accession → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('/etc/passwd', 'file.parquet')

    def test_accession_with_parentheses_raises_value_error(self):
        """Parênteses no accession → ValueError (fora do allowlist)."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET(1)', 'file.parquet')


# =============================================================================
# 3. shared_omics_storage_key — validação de filename (path traversal)
# =============================================================================

class SharedOmicsStorageKeyFilenameValidationTests(SimpleTestCase):
    """
    Rejeição de filenames que habilitariam path traversal no namespace _shared.
    """

    def test_filename_with_space_raises_value_error(self):
        """Espaço no filename → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', 'my file.parquet')

    def test_filename_with_slash_raises_value_error(self):
        """'/' no filename (tentativa de criar subdiretório) → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', 'subdir/file.parquet')

    def test_filename_double_dot_pure_raises_value_error(self):
        """
        '..' puro como filename → ValueError.

        Espelha o mesmo fix do accession: '.' e '..' puros são explicitamente
        rejeitados após o allowlist, pois passariam o regex.
        """
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', '..')

    def test_filename_single_dot_pure_raises_value_error(self):
        """'.' puro como filename → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', '.')

    def test_filename_with_double_dot_raises_value_error(self):
        """'../evil.parquet' como filename → ValueError ('../' contém '/' fora do allowlist)."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', '../evil.parquet')

    def test_filename_with_backslash_raises_value_error(self):
        """Backslash no filename → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', 'evil\\file.parquet')

    def test_empty_filename_raises_value_error(self):
        """Filename vazio → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', '')

    def test_filename_with_null_byte_raises_value_error(self):
        r"""'\0' no filename → ValueError."""
        with self.assertRaises(ValueError):
            shared_omics_storage_key('DATASET', 'file\x00.parquet')


# =============================================================================
# 4. Regressão positiva — pontos internos NÃO são path traversal
# =============================================================================

class SharedOmicsStorageKeyInternalDotRegressionTests(SimpleTestCase):
    """
    Regressão: valores com pontos *internos* ('file..name', 'v1.2.3') devem
    ser ACEITOS — apenas os componentes que são *exatamente* '.' ou '..' são
    path traversal.

    Garante que o fix que rejeita '.' e '..' puros não quebrou acidentalmente
    accessions/filenames legítimos que contenham múltiplos pontos.
    """

    def test_accession_with_internal_double_dot_is_accepted(self):
        """
        'file..name' como accession é aceito: pontos internos não são traversal.
        A chave gerada contém o accession intacto.
        """
        key = shared_omics_storage_key('file..name', 'matrix.parquet')
        self.assertIn('file..name', key)
        self.assertEqual(key, 'omics/_shared/file..name/matrix.parquet')

    def test_accession_with_version_dots_is_accepted(self):
        """
        'v1.2.3' como accession é aceito (versão com pontos separadores).
        """
        key = shared_omics_storage_key('v1.2.3', 'data.parquet')
        self.assertIn('v1.2.3', key)
        self.assertEqual(key, 'omics/_shared/v1.2.3/data.parquet')

    def test_filename_with_internal_double_dot_is_accepted(self):
        """
        'file..name.parquet' como filename é aceito: pontos internos não são traversal.
        """
        key = shared_omics_storage_key('DATASET', 'file..name.parquet')
        self.assertIn('file..name.parquet', key)
        self.assertEqual(key, 'omics/_shared/DATASET/file..name.parquet')

    def test_filename_with_version_dots_is_accepted(self):
        """
        'cptac_v1.2.3.parquet' como filename é aceito.
        """
        key = shared_omics_storage_key('DATASET', 'cptac_v1.2.3.parquet')
        self.assertIn('cptac_v1.2.3.parquet', key)
        self.assertEqual(key, 'omics/_shared/DATASET/cptac_v1.2.3.parquet')
