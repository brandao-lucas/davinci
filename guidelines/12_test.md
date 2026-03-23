## 12. TDD — Fluxo de Teste para Cada Feature

Cada nova feature segue este ciclo:

1. **Mock:** Script Python define os inputs esperados (lista de PMIDs, query, etc.).
2. **Fixture:** Arquivo XML de exemplo do PubMed em `rust_engine/tests/fixtures/`.
3. **Rust Test:** Verifica que o parser extrai todos os campos corretamente do XML fixture.
4. **Integration Test:** Verifica que o COPY injeta os dados no Postgres e que o schema Django reconhece os registros.
5. **API Test:** Verifica que o endpoint DRF retorna os dados corretos com filtros e FTS.

```python
# apps/core/tests/test_ingestion.py

from django.test import TestCase
from apps.core.models import Paper, PaperAuthor, PaperMeSHTerm

class PubMedIngestionTest(TestCase):
    """
    Teste end-to-end: verifica que o Rust engine ingeriu corretamente.
    Precisa do Rust compilado (maturin develop) e Postgres rodando.
    """

    def test_search_and_ingest(self):
        import rust_engine
        result = rust_engine.search_and_ingest_pubmed(
            job_id='test-job-001',
            query='hidradenitis AND cancer',
            date_from=2024,
            date_to=2025,
            db_url='postgresql://davinci:davinci_dev@localhost/davinci_test',
            ncbi_api_key=None,
        )
        self.assertGreater(result.records_processed, 0)
        self.assertGreater(result.records_inserted, 0)

        # Verifica que o ORM do Django consegue ler os dados inseridos pelo Rust
        papers = Paper.objects.all()
        self.assertGreater(papers.count(), 0)

        # Verifica que autores foram inseridos
        first_paper = papers.first()
        self.assertGreater(first_paper.authors.count(), 0)

        # Verifica FTS
        from django.contrib.postgres.search import SearchQuery
        results = Paper.objects.filter(
            search_vector=SearchQuery('hidradenitis')
        )
        self.assertGreater(results.count(), 0)
```