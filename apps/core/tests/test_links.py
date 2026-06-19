"""
test_links.py — Testes E2E do fluxo de materialização de vínculos paper↔dataset.

Cobre o passo A5 do plano .claude/plans/2026-06-12-auditoria-roadmap-e-links-paper-dataset.md:

  1. Materialização: DatasetPaperLink global + ambas as pontas no projeto → ProjectPaperDataset criado.
  2. Idempotência: rodar a materialização duas vezes não duplica (ON CONFLICT DO NOTHING).
  3. Nível 1 estrito: só uma ponta curada → sem linha no bridge.
  4. Isolamento cross-user/cross-project: user B não vê links de user A.
  5. Contrato do serializer: GET /links/ retorna paper_pmid/dataset_accession/omic_type/confidence.
  6. Detalhes: GET /papers/{pk}/ traz linked_datasets; GET /datasets/{pk}/ traz linked_papers.
  7. Backfill: management command popula projetos legados.

Convenção: herda o estilo de test_api.py — helpers de factory no topo,
APITestCase por área temática, sem fixtures externas.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DatasetPaperLink,
    DaVinciProject,
    OmicDataset,
    Paper,
    ProjectDataset,
    ProjectPaper,
    ProjectPaperDataset,
)
from apps.core.services.link_service import (
    materialize_all_projects_links,
    materialize_project_links,
    suggest_orphan_links,
)


# =============================================================================
# Helpers de factory (mesmo estilo de test_api.py)
# =============================================================================

def make_user(username='testlinkuser', password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Link Test Project', query_term='cancer'):
    slug = f'{title.lower().replace(" ", "-")}-{user.username}-davinci'
    return DaVinciProject.objects.create(
        user=user, title=title, slug=slug, query_term=query_term
    )


def make_paper(pmid=9001, title='Link Paper', journal='Nature', pub_year=2024):
    return Paper.objects.create(pmid=pmid, title=title, journal=journal, pub_year=pub_year)


def make_dataset(accession='GSE900', omic_type='transcriptomic', organism='Homo sapiens'):
    return OmicDataset.objects.create(
        accession=accession,
        source_db='geo',
        title=f'Dataset {accession}',
        omic_type=omic_type,
        organism=organism,
    )


def make_project_paper(project, paper, curation_status='pending'):
    return ProjectPaper.objects.create(
        project=project, paper=paper, curation_status=curation_status
    )


def make_project_dataset(project, dataset, curation_status='pending'):
    return ProjectDataset.objects.create(
        project=project, dataset=dataset, curation_status=curation_status
    )


def make_global_link(paper, dataset, link_source='elink'):
    return DatasetPaperLink.objects.create(
        paper=paper, dataset=dataset, link_source=link_source
    )


# =============================================================================
# 1 — Materialização: link global + ambas as pontas → bridge criado
# =============================================================================

class LinkMaterializationTests(APITestCase):
    """
    Garante que a função materialize_project_links() cria ProjectPaperDataset
    quando existe um DatasetPaperLink global e as duas pontas estão no projeto.
    """

    def setUp(self):
        self.user = make_user('mat_user')
        self.project = make_project(self.user, 'Mat Project')
        self.paper = make_paper(pmid=9001)
        self.dataset = make_dataset(accession='GSE901')

        # Ambas as pontas no projeto
        self.pp = make_project_paper(self.project, self.paper)
        self.pd = make_project_dataset(self.project, self.dataset)

        # Link global criado pelo Rust (DatasetPaperLink)
        self.dpl = make_global_link(self.paper, self.dataset)

    def test_materialize_creates_bridge_record(self):
        """Materialização cria exatamente uma linha no bridge com confidence='auto'."""
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 1)
        bridge = ProjectPaperDataset.objects.get(project=self.project)
        self.assertEqual(bridge.project_paper, self.pp)
        self.assertEqual(bridge.project_dataset, self.pd)
        self.assertEqual(bridge.confidence, 'auto')

    def test_materialize_returns_inserted_count(self):
        """Retorno da função é o número de linhas efetivamente inseridas."""
        count = materialize_project_links(self.project.id)
        self.assertEqual(count, 1)

    def test_multiple_global_links_creates_multiple_bridge_records(self):
        """Dois links globais distintos geram duas linhas no bridge."""
        paper2 = make_paper(pmid=9002, title='Paper 2')
        dataset2 = make_dataset(accession='GSE902')
        make_project_paper(self.project, paper2)
        make_project_dataset(self.project, dataset2)
        make_global_link(paper2, dataset2)

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 2)
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 2)


# =============================================================================
# 2 — Idempotência: rodar duas vezes não duplica
# =============================================================================

class LinkMaterializationIdempotencyTests(APITestCase):
    """
    Garante que a materialização é idempotente:
    ON CONFLICT DO NOTHING no banco, 0 linhas inseridas na segunda execução.
    """

    def setUp(self):
        self.user = make_user('idem_user')
        self.project = make_project(self.user, 'Idem Project')
        self.paper = make_paper(pmid=9010)
        self.dataset = make_dataset(accession='GSE910')
        self.pp = make_project_paper(self.project, self.paper)
        self.pd = make_project_dataset(self.project, self.dataset)
        make_global_link(self.paper, self.dataset)

    def test_second_run_inserts_zero(self):
        """Segunda execução não insere nenhuma linha nova."""
        first = materialize_project_links(self.project.id)
        second = materialize_project_links(self.project.id)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_no_duplicate_records_after_double_run(self):
        """O banco não tem duplicatas após duas execuções."""
        materialize_project_links(self.project.id)
        materialize_project_links(self.project.id)

        count = ProjectPaperDataset.objects.filter(project=self.project).count()
        self.assertEqual(count, 1)

    def test_materialize_all_also_idempotent(self):
        """materialize_all_projects_links() também é idempotente."""
        first = materialize_all_projects_links()
        second = materialize_all_projects_links()

        self.assertGreaterEqual(first, 1)
        self.assertEqual(second, 0)


# =============================================================================
# 3 — Nível 1 estrito: órfão (só uma ponta no projeto) → sem linha no bridge
# =============================================================================

class LinkMaterializationLevel1StrictTests(APITestCase):
    """
    Regra de Nível 1: ProjectPaperDataset SÓ é criado quando AMBAS as pontas
    (ProjectPaper E ProjectDataset) existem no mesmo projeto. Casos órfãos
    (uma ponta ausente) não devem gerar linha no bridge.
    """

    def setUp(self):
        self.user = make_user('orphan_user')
        self.project = make_project(self.user, 'Orphan Project')
        self.paper = make_paper(pmid=9020)
        self.dataset = make_dataset(accession='GSE920')

    def test_orphan_paper_only_no_bridge(self):
        """Link global existe, mas dataset não está no projeto → sem bridge."""
        make_project_paper(self.project, self.paper)
        # dataset não tem ProjectDataset neste projeto
        make_global_link(self.paper, self.dataset)

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 0)
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

    def test_orphan_dataset_only_no_bridge(self):
        """Link global existe, mas paper não está no projeto → sem bridge."""
        make_project_dataset(self.project, self.dataset)
        # paper não tem ProjectPaper neste projeto
        make_global_link(self.paper, self.dataset)

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 0)
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

    def test_no_global_link_no_bridge(self):
        """Ambas as pontas no projeto, mas sem DatasetPaperLink global → sem bridge."""
        make_project_paper(self.project, self.paper)
        make_project_dataset(self.project, self.dataset)
        # sem DatasetPaperLink

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 0)
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

    def test_pontas_em_projetos_diferentes_nao_cruzam(self):
        """
        Paper em projeto A e dataset em projeto B, mesmo com link global:
        materializar projeto A não cria bridge (o filtro WHERE pp.project_id = pd.project_id
        bloqueia o cruzamento entre projetos).
        """
        user_b = make_user('orphan_userb')
        project_b = make_project(user_b, 'Project B', 'biomarker')

        make_project_paper(self.project, self.paper)       # paper só em projeto A
        make_project_dataset(project_b, self.dataset)       # dataset só em projeto B
        make_global_link(self.paper, self.dataset)

        inserted_a = materialize_project_links(self.project.id)
        inserted_b = materialize_project_links(project_b.id)

        self.assertEqual(inserted_a, 0)
        self.assertEqual(inserted_b, 0)


# =============================================================================
# 4 — Isolamento cross-user/cross-project (Regra #3 / firebase-auth-guard)
# =============================================================================

class LinkCrossUserIsolationTests(APITestCase):
    """
    Garante que user B não vê links de user A via GET /projects/{id}/links/.
    Cobre a Regra #3 (isolamento por request.user).
    """

    def setUp(self):
        # User A com projeto e link materializado
        self.user_a = make_user('link_usera')
        self.project_a = make_project(self.user_a, 'Project A')
        paper_a = make_paper(pmid=9030)
        dataset_a = make_dataset(accession='GSE930')
        pp_a = make_project_paper(self.project_a, paper_a)
        pd_a = make_project_dataset(self.project_a, dataset_a)
        make_global_link(paper_a, dataset_a)
        # Cria o bridge diretamente (como se a materialização já tivesse rodado)
        ProjectPaperDataset.objects.create(
            project=self.project_a,
            project_paper=pp_a,
            project_dataset=pd_a,
            confidence='auto',
        )

        # User B com projeto próprio e sem links
        self.user_b = make_user('link_userb')
        self.project_b = make_project(self.user_b, 'Project B')

        self.client_a = APIClient()
        self.client_a.force_authenticate(user=self.user_a)

        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)

    def test_user_b_cannot_access_project_a_links(self):
        """User B recebe 404 ao tentar listar links do projeto de user A."""
        url = f'/api/v1/projects/{self.project_a.id}/links/'
        response = self.client_b.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_a_sees_own_links(self):
        """User A vê seus próprios links normalmente."""
        url = f'/api/v1/projects/{self.project_a.id}/links/'
        response = self.client_a.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 1)

    def test_user_b_project_links_is_empty(self):
        """User B lista links do próprio projeto (vazio) sem ver links de user A."""
        url = f'/api/v1/projects/{self.project_b.id}/links/'
        response = self.client_b.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 0)

    def test_unauthenticated_gets_403(self):
        """Sem autenticação, retorna 403 (não 500)."""
        client_anon = APIClient()
        url = f'/api/v1/projects/{self.project_a.id}/links/'
        response = client_anon.get(url)
        self.assertIn(response.status_code, [
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        ])


# =============================================================================
# 5 — Contrato do serializer: paper_pmid/dataset_accession/omic_type/confidence
# =============================================================================

class LinkSerializerContractTests(APITestCase):
    """
    Garante que GET /projects/{id}/links/ retorna o contrato canônico:
    paper_pmid, dataset_accession, paper_title, dataset_title, omic_type, confidence.

    Antes do A2 o serializer retornava 'pmid'/'accession' (campo renomeado).
    Este teste documenta o contrato correto (Handoff atelier).
    """

    def setUp(self):
        self.user = make_user('contract_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'Contract Project')

        self.paper = make_paper(pmid=9040, title='Contract Paper', journal='Cell', pub_year=2024)
        self.dataset = make_dataset(accession='GSE940', omic_type='genomic')

        pp = make_project_paper(self.project, self.paper)
        pd = make_project_dataset(self.project, self.dataset)

        self.link = ProjectPaperDataset.objects.create(
            project=self.project,
            project_paper=pp,
            project_dataset=pd,
            confidence='auto',
        )
        self.url = f'/api/v1/projects/{self.project.id}/links/'

    def test_response_has_paper_pmid_not_pmid(self):
        """Campo deve ser 'paper_pmid', não 'pmid' (contrato canônico)."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.data['results'][0]
        self.assertIn('paper_pmid', result, "campo 'paper_pmid' ausente no response")
        self.assertNotIn('pmid', result, "campo 'pmid' legado presente — deve ser 'paper_pmid'")

    def test_response_has_dataset_accession_not_accession(self):
        """Campo deve ser 'dataset_accession', não 'accession'."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertIn('dataset_accession', result, "campo 'dataset_accession' ausente")
        self.assertNotIn('accession', result, "campo 'accession' legado presente")

    def test_paper_pmid_value_is_correct(self):
        """Valor de paper_pmid deve ser o PMID real do paper."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertEqual(result['paper_pmid'], 9040)

    def test_dataset_accession_value_is_correct(self):
        """Valor de dataset_accession deve ser o accession real do dataset."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertEqual(result['dataset_accession'], 'GSE940')

    def test_omic_type_present_with_correct_value(self):
        """Campo omic_type presente e correto."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertIn('omic_type', result)
        self.assertEqual(result['omic_type'], 'genomic')

    def test_confidence_present_with_correct_value(self):
        """Campo confidence presente e com valor 'auto' (padrão de materialização)."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertIn('confidence', result)
        self.assertEqual(result['confidence'], 'auto')

    def test_paper_title_and_dataset_title_present(self):
        """Campos paper_title e dataset_title presentes e não nulos."""
        response = self.client.get(self.url)
        result = response.data['results'][0]
        self.assertIn('paper_title', result)
        self.assertIn('dataset_title', result)
        self.assertEqual(result['paper_title'], 'Contract Paper')
        self.assertEqual(result['dataset_title'], 'Dataset GSE940')

    def test_all_canonical_fields_present(self):
        """Todos os campos canônicos do contrato estão presentes no response."""
        canonical = ['id', 'paper_pmid', 'paper_title', 'dataset_accession',
                     'dataset_title', 'omic_type', 'confidence', 'created_at']
        response = self.client.get(self.url)
        result = response.data['results'][0]
        for field in canonical:
            self.assertIn(field, result, f"campo canônico '{field}' ausente")


# =============================================================================
# 6 — Detalhes: paper detalhe traz linked_datasets; dataset detalhe traz linked_papers
# =============================================================================

class LinkedDetailTests(APITestCase):
    """
    Garante que os endpoints de detalhe de paper e dataset expõem
    os vínculos project-scoped corretos.
    """

    def setUp(self):
        self.user = make_user('detail_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'Detail Project')

        self.paper = make_paper(pmid=9050, title='Detail Paper')
        self.dataset = make_dataset(accession='GSE950', omic_type='proteomic')

        self.pp = make_project_paper(self.project, self.paper)
        self.pd = make_project_dataset(self.project, self.dataset)

        self.link = ProjectPaperDataset.objects.create(
            project=self.project,
            project_paper=self.pp,
            project_dataset=self.pd,
            confidence='auto',
        )

    def test_paper_detail_contains_linked_datasets(self):
        """GET /papers/{pk}/ traz 'linked_datasets' com o dataset vinculado."""
        url = f'/api/v1/projects/{self.project.id}/papers/{self.pp.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('linked_datasets', response.data)
        self.assertEqual(len(response.data['linked_datasets']), 1)

    def test_paper_detail_linked_datasets_has_correct_accession(self):
        """linked_datasets no paper detalhe expõe dataset_accession correto."""
        url = f'/api/v1/projects/{self.project.id}/papers/{self.pp.id}/'
        response = self.client.get(url)
        ld = response.data['linked_datasets'][0]
        self.assertEqual(ld['dataset_accession'], 'GSE950')

    def test_paper_detail_linked_datasets_has_omic_type(self):
        """linked_datasets no paper detalhe expõe omic_type."""
        url = f'/api/v1/projects/{self.project.id}/papers/{self.pp.id}/'
        response = self.client.get(url)
        ld = response.data['linked_datasets'][0]
        self.assertIn('omic_type', ld)
        self.assertEqual(ld['omic_type'], 'proteomic')

    def test_dataset_detail_contains_linked_papers(self):
        """GET /datasets/{pk}/ traz 'linked_papers' com o paper vinculado."""
        url = f'/api/v1/projects/{self.project.id}/datasets/{self.pd.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('linked_papers', response.data)
        self.assertEqual(len(response.data['linked_papers']), 1)

    def test_dataset_detail_linked_papers_has_correct_pmid(self):
        """linked_papers no dataset detalhe expõe paper_pmid correto."""
        url = f'/api/v1/projects/{self.project.id}/datasets/{self.pd.id}/'
        response = self.client.get(url)
        lp = response.data['linked_papers'][0]
        self.assertEqual(lp['paper_pmid'], 9050)

    def test_paper_detail_no_cross_project_leak_in_linked_datasets(self):
        """
        linked_datasets no detalhe de paper não vaza vínculos de outro projeto.

        Cria um segundo projeto (mesmo usuário) com o mesmo paper mas um dataset diferente.
        O detalhe do paper no projeto original deve mostrar apenas o dataset do projeto original.
        """
        # Segundo projeto com o mesmo paper
        project2 = make_project(self.user, 'Detail Project 2', 'query2')
        dataset2 = make_dataset(accession='GSE951', omic_type='genomic')
        pp2 = make_project_paper(project2, self.paper)
        pd2 = make_project_dataset(project2, dataset2)
        ProjectPaperDataset.objects.create(
            project=project2,
            project_paper=pp2,
            project_dataset=pd2,
            confidence='auto',
        )

        # Detalhe do paper no projeto ORIGINAL deve mostrar só GSE950
        url = f'/api/v1/projects/{self.project.id}/papers/{self.pp.id}/'
        response = self.client.get(url)
        linked = response.data['linked_datasets']
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]['dataset_accession'], 'GSE950')

    def test_dataset_detail_no_cross_project_leak_in_linked_papers(self):
        """
        linked_papers no detalhe de dataset não vaza vínculos de outro projeto.
        """
        # Segundo projeto com o mesmo dataset
        project2 = make_project(self.user, 'Detail Project 3', 'query3')
        paper2 = make_paper(pmid=9051, title='Paper 2')
        pp3 = make_project_paper(project2, paper2)
        pd3 = make_project_dataset(project2, self.dataset)
        ProjectPaperDataset.objects.create(
            project=project2,
            project_paper=pp3,
            project_dataset=pd3,
            confidence='auto',
        )

        # Detalhe do dataset no projeto ORIGINAL deve mostrar só PMID 9050
        url = f'/api/v1/projects/{self.project.id}/datasets/{self.pd.id}/'
        response = self.client.get(url)
        linked = response.data['linked_papers']
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]['paper_pmid'], 9050)

    def test_paper_without_links_returns_empty_linked_datasets(self):
        """Paper sem vínculos retorna linked_datasets vazio (não null)."""
        paper_solo = make_paper(pmid=9052, title='Solo Paper')
        pp_solo = make_project_paper(self.project, paper_solo)

        url = f'/api/v1/projects/{self.project.id}/papers/{pp_solo.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('linked_datasets', response.data)
        self.assertIsInstance(response.data['linked_datasets'], list)
        self.assertEqual(len(response.data['linked_datasets']), 0)

    def test_dataset_without_links_returns_empty_linked_papers(self):
        """Dataset sem vínculos retorna linked_papers vazio (não null)."""
        dataset_solo = make_dataset(accession='GSE952', omic_type='metabolomic')
        pd_solo = make_project_dataset(self.project, dataset_solo)

        url = f'/api/v1/projects/{self.project.id}/datasets/{pd_solo.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('linked_papers', response.data)
        self.assertIsInstance(response.data['linked_papers'], list)
        self.assertEqual(len(response.data['linked_papers']), 0)


# =============================================================================
# 7 — Backfill: management command popula projetos legados
# =============================================================================

class BackfillCommandTests(APITestCase):
    """
    Garante que o management command 'backfill_project_links' popula
    ProjectPaperDataset para projetos que já existem (legados).
    """

    def setUp(self):
        self.user = make_user('backfill_user')
        self.project = make_project(self.user, 'Backfill Project')

        self.paper = make_paper(pmid=9060)
        self.dataset = make_dataset(accession='GSE960')

        self.pp = make_project_paper(self.project, self.paper)
        self.pd = make_project_dataset(self.project, self.dataset)
        make_global_link(self.paper, self.dataset)

    def test_backfill_all_projects_creates_bridge(self):
        """Comando sem --project popula todos os projetos."""
        self.assertEqual(ProjectPaperDataset.objects.count(), 0)

        out = StringIO()
        call_command('backfill_project_links', stdout=out)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 1)

    def test_backfill_specific_project_creates_bridge(self):
        """Comando com --project <uuid> popula só o projeto especificado."""
        out = StringIO()
        call_command('backfill_project_links', project_id=str(self.project.id), stdout=out)

        bridge = ProjectPaperDataset.objects.get(project=self.project)
        self.assertEqual(bridge.confidence, 'auto')

    def test_backfill_specific_project_does_not_affect_others(self):
        """Backfill com --project não toca outros projetos."""
        user2 = make_user('backfill_user2')
        project2 = make_project(user2, 'Other Backfill Project', 'query2')
        paper2 = make_paper(pmid=9061)
        dataset2 = make_dataset(accession='GSE961')
        make_project_paper(project2, paper2)
        make_project_dataset(project2, dataset2)
        make_global_link(paper2, dataset2)

        out = StringIO()
        call_command('backfill_project_links', project_id=str(self.project.id), stdout=out)

        # project1 tem bridge
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 1)
        # project2 ainda não tem
        self.assertEqual(ProjectPaperDataset.objects.filter(project=project2).count(), 0)

    def test_backfill_idempotent_on_rerun(self):
        """Rodar o backfill duas vezes não duplica registros."""
        out = StringIO()
        call_command('backfill_project_links', stdout=out)
        call_command('backfill_project_links', stdout=out)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 1)

    def test_backfill_invalid_uuid_raises_command_error(self):
        """UUID inválido no --project dispara CommandError com mensagem clara."""
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('backfill_project_links', project_id='not-a-uuid')

    def test_backfill_output_mentions_inserted_count(self):
        """Saída do comando menciona quantas linhas foram inseridas."""
        out = StringIO()
        call_command('backfill_project_links', project_id=str(self.project.id), stdout=out)
        output = out.getvalue()
        self.assertIn('1', output, "output deve mencionar o total de vínculos inseridos")


# =============================================================================
# 8 — Regressão: materialização não cria bridge para confidence='rejected'
#     (vínculos rejeitados pelo pesquisador não são sobrescritos)
# =============================================================================

class LinkMaterializationDoesNotOverwriteRejectedTests(APITestCase):
    """
    ON CONFLICT DO NOTHING garante que vínculos já existentes (inclusive
    'rejected') não são sobrescritos por uma re-materialização.
    """

    def setUp(self):
        self.user = make_user('reject_user')
        self.project = make_project(self.user, 'Reject Project')
        self.paper = make_paper(pmid=9070)
        self.dataset = make_dataset(accession='GSE970')
        self.pp = make_project_paper(self.project, self.paper)
        self.pd = make_project_dataset(self.project, self.dataset)
        make_global_link(self.paper, self.dataset)

        # Pesquisador já rejeitou este link manualmente
        self.rejected_link = ProjectPaperDataset.objects.create(
            project=self.project,
            project_paper=self.pp,
            project_dataset=self.pd,
            confidence='rejected',
        )

    def test_materialize_does_not_overwrite_rejected(self):
        """Re-materialização não altera link 'rejected' existente."""
        inserted = materialize_project_links(self.project.id)

        # ON CONFLICT DO NOTHING → 0 inseridos
        self.assertEqual(inserted, 0)
        # Confidence mantida como 'rejected'
        self.rejected_link.refresh_from_db()
        self.assertEqual(self.rejected_link.confidence, 'rejected')

    def test_materialize_does_not_overwrite_confirmed(self):
        """Re-materialização não altera link 'confirmed' existente."""
        self.rejected_link.confidence = 'confirmed'
        self.rejected_link.save()

        inserted = materialize_project_links(self.project.id)

        self.assertEqual(inserted, 0)
        self.rejected_link.refresh_from_db()
        self.assertEqual(self.rejected_link.confidence, 'confirmed')


# =============================================================================
# 9 — B1: suggest_orphan_links() — lógica de serviço (read-only)
# =============================================================================

class OrphanLinkServiceTests(APITestCase):
    """
    Testa a função suggest_orphan_links() diretamente.

    Cobre os dois casos de órfão e a ausência de gravação no bridge.
    """

    def setUp(self):
        self.user = make_user('orphsvc_user')
        self.project = make_project(self.user, 'OrphanSvc Project')
        self.paper = make_paper(pmid=9080, title='Orphan Paper')
        self.dataset = make_dataset(accession='GSE980', omic_type='proteomic')

    def test_caso_a_dataset_missing(self):
        """
        Caso A: paper no projeto, dataset NÃO está → suggestion_type == 'dataset_missing'.
        """
        make_project_paper(self.project, self.paper)
        # dataset não tem ProjectDataset neste projeto
        make_global_link(self.paper, self.dataset)

        results = suggest_orphan_links(self.project.id)

        self.assertEqual(len(results), 1)
        s = results[0]
        self.assertEqual(s['suggestion_type'], 'dataset_missing')
        self.assertEqual(s['paper_pmid'], 9080)
        self.assertEqual(s['dataset_accession'], 'GSE980')
        self.assertIsNotNone(s['project_paper_id'])
        self.assertIsNone(s['project_dataset_id'])

    def test_caso_b_paper_missing(self):
        """
        Caso B: dataset no projeto, paper NÃO está → suggestion_type == 'paper_missing'.
        """
        make_project_dataset(self.project, self.dataset)
        # paper não tem ProjectPaper neste projeto
        make_global_link(self.paper, self.dataset)

        results = suggest_orphan_links(self.project.id)

        self.assertEqual(len(results), 1)
        s = results[0]
        self.assertEqual(s['suggestion_type'], 'paper_missing')
        self.assertEqual(s['paper_pmid'], 9080)
        self.assertEqual(s['dataset_accession'], 'GSE980')
        self.assertIsNone(s['project_paper_id'])
        self.assertIsNotNone(s['project_dataset_id'])

    def test_ambas_pontas_no_projeto_nao_retorna_sugestao(self):
        """
        Ambas as pontas já no projeto → Nível 1, não aparece como órfão.
        """
        make_project_paper(self.project, self.paper)
        make_project_dataset(self.project, self.dataset)
        make_global_link(self.paper, self.dataset)

        results = suggest_orphan_links(self.project.id)

        self.assertEqual(results, [])

    def test_sem_link_global_nao_retorna_sugestao(self):
        """Sem DatasetPaperLink global, nenhuma sugestão é retornada."""
        make_project_paper(self.project, self.paper)
        make_project_dataset(self.project, self.dataset)
        # sem DatasetPaperLink

        results = suggest_orphan_links(self.project.id)

        self.assertEqual(results, [])

    def test_nao_grava_no_bridge(self):
        """suggest_orphan_links() é READ-ONLY: bridge permanece vazio."""
        make_project_paper(self.project, self.paper)
        make_global_link(self.paper, self.dataset)

        suggest_orphan_links(self.project.id)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

    def test_campos_completos_na_resposta(self):
        """Todos os campos do contrato estão presentes no dict retornado."""
        make_project_paper(self.project, self.paper)
        make_global_link(self.paper, self.dataset)

        results = suggest_orphan_links(self.project.id)
        self.assertEqual(len(results), 1)
        s = results[0]

        campos_obrigatorios = [
            'suggestion_type', 'global_link_id', 'link_source',
            'project_paper_id', 'paper_pmid', 'paper_title',
            'project_dataset_id', 'dataset_id', 'dataset_accession',
            'dataset_title', 'omic_type',
        ]
        for campo in campos_obrigatorios:
            self.assertIn(campo, s, f"campo '{campo}' ausente no retorno de suggest_orphan_links()")

    def test_omic_type_e_link_source_corretos(self):
        """omic_type e link_source são preenchidos corretamente."""
        make_project_paper(self.project, self.paper)
        make_global_link(self.paper, self.dataset, link_source='geo_xml')

        results = suggest_orphan_links(self.project.id)

        s = results[0]
        self.assertEqual(s['omic_type'], 'proteomic')
        self.assertEqual(s['link_source'], 'geo_xml')

    def test_nao_vaza_sugestoes_de_outro_projeto(self):
        """
        Sugestões de um projeto não aparecem ao consultar outro projeto.
        Regra #3 — isolamento por project_id.
        """
        user_b = make_user('orphsvc_userb')
        project_b = make_project(user_b, 'OrphanSvc Project B', 'query_b')

        # paper no projeto A com link global para dataset fora do projeto A
        make_project_paper(self.project, self.paper)
        make_global_link(self.paper, self.dataset)

        # Projeto B não tem o paper nem o dataset
        results_b = suggest_orphan_links(project_b.id)

        self.assertEqual(results_b, [])

    def test_multiplas_sugestoes(self):
        """Dois links globais independentes geram duas sugestões."""
        paper2 = make_paper(pmid=9081, title='Orphan Paper 2')
        dataset2 = make_dataset(accession='GSE981', omic_type='genomic')

        # Caso A para dois pares distintos
        make_project_paper(self.project, self.paper)
        make_project_paper(self.project, paper2)
        make_global_link(self.paper, self.dataset)
        make_global_link(paper2, dataset2)

        results = suggest_orphan_links(self.project.id)

        self.assertEqual(len(results), 2)
        tipos = {s['suggestion_type'] for s in results}
        self.assertEqual(tipos, {'dataset_missing'})


# =============================================================================
# 10 — B1: endpoint GET /projects/{id}/links/suggestions/ (API)
# =============================================================================

class OrphanLinkEndpointTests(APITestCase):
    """
    Testa o endpoint GET /projects/{project_pk}/links/suggestions/.

    Cobre: contrato JSON, filtro ?type=, paginação, isolamento cross-user.
    """

    def setUp(self):
        self.user = make_user('orphapi_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'OrphanAPI Project')

        self.paper = make_paper(pmid=9090, title='API Orphan Paper')
        self.dataset = make_dataset(accession='GSE990', omic_type='transcriptomic')

        # Caso A: paper no projeto, dataset não
        self.pp = make_project_paper(self.project, self.paper)
        self.dpl = make_global_link(self.paper, self.dataset, link_source='elink')

        self.url = f'/api/v1/projects/{self.project.id}/links/suggestions/'

    def test_endpoint_retorna_200(self):
        """GET /links/suggestions/ retorna 200 para owner do projeto."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_response_paginado(self):
        """Resposta segue formato paginado (count, results)."""
        response = self.client.get(self.url)
        self.assertIn('count', response.data)
        self.assertIn('results', response.data)

    def test_retorna_caso_a(self):
        """Caso A (dataset_missing) aparece nos resultados."""
        response = self.client.get(self.url)
        self.assertEqual(response.data['count'], 1)
        s = response.data['results'][0]
        self.assertEqual(s['suggestion_type'], 'dataset_missing')

    def test_contrato_json_completo_caso_a(self):
        """Todos os campos canônicos do contrato estão na resposta (Caso A)."""
        response = self.client.get(self.url)
        s = response.data['results'][0]

        campos = [
            'suggestion_type', 'global_link_id', 'link_source',
            'project_paper_id', 'paper_pmid', 'paper_title',
            'project_dataset_id', 'dataset_id', 'dataset_accession',
            'dataset_title', 'omic_type',
        ]
        for campo in campos:
            self.assertIn(campo, s, f"campo '{campo}' ausente no response JSON")

        self.assertEqual(s['paper_pmid'], 9090)
        self.assertEqual(s['dataset_accession'], 'GSE990')
        self.assertEqual(s['omic_type'], 'transcriptomic')
        self.assertEqual(s['link_source'], 'elink')
        self.assertIsNotNone(s['project_paper_id'])
        self.assertIsNone(s['project_dataset_id'])
        self.assertEqual(s['project_paper_id'], self.pp.id)

    def test_contrato_json_completo_caso_b(self):
        """Todos os campos canônicos do contrato estão na resposta (Caso B)."""
        # Adicionar Caso B: dataset no projeto, paper não
        paper_b = make_paper(pmid=9091, title='API Orphan Paper B')
        dataset_b = make_dataset(accession='GSE991', omic_type='genomic')
        pd_b = make_project_dataset(self.project, dataset_b)
        make_global_link(paper_b, dataset_b, link_source='geo_xml')

        response = self.client.get(self.url)
        resultados = response.data['results']

        caso_b = next((s for s in resultados if s['suggestion_type'] == 'paper_missing'), None)
        self.assertIsNotNone(caso_b, "Caso B (paper_missing) ausente nos resultados")

        self.assertEqual(caso_b['paper_pmid'], 9091)
        self.assertEqual(caso_b['dataset_accession'], 'GSE991')
        self.assertIsNone(caso_b['project_paper_id'])
        self.assertIsNotNone(caso_b['project_dataset_id'])
        self.assertEqual(caso_b['project_dataset_id'], pd_b.id)

    def test_filtro_type_dataset_missing(self):
        """?type=dataset_missing retorna só sugestões do Caso A."""
        # Adicionar também um Caso B
        paper_b = make_paper(pmid=9092, title='Filter Paper B')
        dataset_b = make_dataset(accession='GSE992', omic_type='genomic')
        make_project_dataset(self.project, dataset_b)
        make_global_link(paper_b, dataset_b)

        response = self.client.get(self.url + '?type=dataset_missing')
        self.assertEqual(response.status_code, 200)
        for s in response.data['results']:
            self.assertEqual(s['suggestion_type'], 'dataset_missing')

    def test_filtro_type_paper_missing(self):
        """?type=paper_missing retorna só sugestões do Caso B."""
        paper_b = make_paper(pmid=9093, title='Filter Paper C')
        dataset_b = make_dataset(accession='GSE993', omic_type='metabolomic')
        make_project_dataset(self.project, dataset_b)
        make_global_link(paper_b, dataset_b)

        response = self.client.get(self.url + '?type=paper_missing')
        self.assertEqual(response.status_code, 200)
        for s in response.data['results']:
            self.assertEqual(s['suggestion_type'], 'paper_missing')

    def test_user_alheio_recebe_404(self):
        """User B não pode acessar sugestões do projeto de user A."""
        user_b = make_user('orphapi_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)

        response = client_b.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_nao_autenticado_recebe_403(self):
        """Sem autenticação, retorna 403 (não 500)."""
        client_anon = APIClient()
        response = client_anon.get(self.url)
        self.assertIn(response.status_code, [401, 403])

    def test_endpoint_nao_grava_no_bridge(self):
        """Chamar o endpoint não cria nenhum ProjectPaperDataset."""
        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

        self.client.get(self.url)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

    def test_sem_orfaos_retorna_lista_vazia(self):
        """Projeto sem órfãos retorna results=[] e count=0."""
        # Adicionar dataset ao projeto para que o link deixe de ser órfão (Nível 1)
        make_project_dataset(self.project, self.dataset)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['results'], [])


# =============================================================================
# 11 — B2: endpoint POST /papers/add_from_suggestion/ e /datasets/add_from_suggestion/
# =============================================================================

class AddPaperFromSuggestionTests(APITestCase):
    """
    Testa POST /projects/{project_pk}/papers/add_from_suggestion/.

    Cobre: criação (201), idempotência (200), 404 para paper inexistente,
    404 para projeto alheio, promoção automática do vínculo (materialização),
    status inicial 'pending' e campos de auditoria.
    """

    def setUp(self):
        self.user = make_user('addpaper_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'AddPaper Project')

        self.paper = make_paper(pmid=9100, title='Suggestion Paper')
        self.dataset = make_dataset(accession='GSE100', omic_type='transcriptomic')

        self.url = f'/api/v1/projects/{self.project.id}/papers/add_from_suggestion/'

    def test_cria_project_paper_retorna_201(self):
        """POST com PMID válido cria ProjectPaper e retorna 201."""
        response = self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ProjectPaper.objects.filter(project=self.project, paper=self.paper).exists()
        )

    def test_status_inicial_e_pending(self):
        """ProjectPaper criado via sugestão começa com curation_status='pending'."""
        self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')
        pp = ProjectPaper.objects.get(project=self.project, paper=self.paper)
        self.assertEqual(pp.curation_status, ProjectPaper.CurationStatus.PENDING)

    def test_idempotencia_retorna_200(self):
        """Segunda chamada com mesmo PMID retorna 200 (existente) sem duplicar."""
        self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')
        response = self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ProjectPaper.objects.filter(project=self.project, paper=self.paper).count(), 1
        )

    def test_paper_inexistente_retorna_404(self):
        """PMID que não existe no banco retorna 404."""
        response = self.client.post(self.url, {'pmid': 99999999}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_pmid_ausente_retorna_400(self):
        """Body sem campo 'pmid' retorna 400 (validação do serializer)."""
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_pmid_invalido_retorna_400(self):
        """PMID não-inteiro retorna 400."""
        response = self.client.post(self.url, {'pmid': 'abc'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_projeto_alheio_retorna_404(self):
        """User B não consegue adicionar paper ao projeto de user A."""
        user_b = make_user('addpaper_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)
        response = client_b.post(self.url, {'pmid': self.paper.pmid}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_response_contem_campos_de_list_serializer(self):
        """Response tem os campos canônicos de ProjectPaperListSerializer."""
        response = self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')
        self.assertEqual(response.status_code, 201)
        # ProjectPaperListSerializer expõe ao menos estes campos
        for campo in ('id', 'curation_status'):
            self.assertIn(campo, response.data, f"campo '{campo}' ausente no response")

    def test_materializa_vinculo_ao_adicionar_paper(self):
        """
        Adicionar a ponta faltante (paper) deve promover o par órfão a
        ProjectPaperDataset(confidence='auto') via materialize_project_links.

        Cenário: dataset já está no projeto (órfão Caso B). Após adicionar o paper,
        o bridge deve ser criado automaticamente.
        """
        # Dataset já no projeto + link global (Caso B)
        make_project_dataset(self.project, self.dataset)
        make_global_link(self.paper, self.dataset)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

        self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')

        self.assertEqual(
            ProjectPaperDataset.objects.filter(project=self.project).count(), 1,
            "materialize_project_links deve criar o bridge após adicionar a ponta faltante",
        )
        bridge = ProjectPaperDataset.objects.get(project=self.project)
        self.assertEqual(bridge.confidence, 'auto')

    def test_nao_autenticado_retorna_403(self):
        """Sem autenticação, retorna 401/403."""
        client_anon = APIClient()
        response = client_anon.post(self.url, {'pmid': self.paper.pmid}, format='json')
        self.assertIn(response.status_code, [401, 403])

    def test_re_post_included_nao_rebaixa_curadoria(self):
        """
        Re-POST de paper já com status 'included' NÃO zera curated_at, notes,
        exclusion_reason e NÃO volta para 'pending'.

        Ponto crítico exigido pelo 007 (skill curation-audit-trail):
        add_from_suggestion usa get_or_create, portanto se o registro já existe
        ele é retornado intacto independentemente do status atual.
        """
        import datetime
        from django.utils import timezone

        # Pré-existência: paper já curado como 'included'
        curated_ts = timezone.now() - datetime.timedelta(days=1)
        pp = ProjectPaper.objects.create(
            project=self.project,
            paper=self.paper,
            curation_status=ProjectPaper.CurationStatus.INCLUDED,
            notes='nota preservada',
            curated_at=curated_ts,
        )

        response = self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')

        self.assertEqual(response.status_code, 200)
        pp.refresh_from_db()
        self.assertEqual(pp.curation_status, ProjectPaper.CurationStatus.INCLUDED,
                         "status não deve regredir para 'pending'")
        self.assertEqual(pp.notes, 'nota preservada',
                         "notes não deve ser zerado")
        self.assertIsNotNone(pp.curated_at,
                             "curated_at não deve ser apagado")
        # Garante que o timestamp não mudou (margem de 1 segundo)
        self.assertAlmostEqual(
            pp.curated_at.timestamp(), curated_ts.timestamp(), delta=1,
            msg="curated_at não deve ser alterado"
        )

    def test_re_post_excluded_nao_rebaixa_curadoria(self):
        """
        Re-POST de paper já com status 'excluded' NÃO zera exclusion_reason
        nem curated_at e NÃO volta para 'pending'.
        """
        import datetime
        from django.utils import timezone

        curated_ts = timezone.now() - datetime.timedelta(days=2)
        pp = ProjectPaper.objects.create(
            project=self.project,
            paper=self.paper,
            curation_status=ProjectPaper.CurationStatus.EXCLUDED,
            exclusion_reason='fora do escopo',
            curated_at=curated_ts,
        )

        response = self.client.post(self.url, {'pmid': self.paper.pmid}, format='json')

        self.assertEqual(response.status_code, 200)
        pp.refresh_from_db()
        self.assertEqual(pp.curation_status, ProjectPaper.CurationStatus.EXCLUDED,
                         "status não deve regredir para 'pending'")
        self.assertEqual(pp.exclusion_reason, 'fora do escopo',
                         "exclusion_reason não deve ser zerado")
        self.assertIsNotNone(pp.curated_at,
                             "curated_at não deve ser apagado")


class AddDatasetFromSuggestionTests(APITestCase):
    """
    Testa POST /projects/{project_pk}/datasets/add_from_suggestion/.

    Cobre: criação (201), idempotência (200), 404 para dataset inexistente,
    404 para projeto alheio, promoção automática do vínculo (materialização),
    status inicial 'pending' e campos de auditoria.
    """

    def setUp(self):
        self.user = make_user('adddataset_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user, 'AddDataset Project')

        self.paper = make_paper(pmid=9110, title='Suggestion Paper D')
        self.dataset = make_dataset(accession='GSE110', omic_type='genomic')

        self.url = f'/api/v1/projects/{self.project.id}/datasets/add_from_suggestion/'

    def test_cria_project_dataset_retorna_201(self):
        """POST com dataset_id válido cria ProjectDataset e retorna 201."""
        response = self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ProjectDataset.objects.filter(project=self.project, dataset=self.dataset).exists()
        )

    def test_status_inicial_e_pending(self):
        """ProjectDataset criado via sugestão começa com curation_status='pending'."""
        self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        pd = ProjectDataset.objects.get(project=self.project, dataset=self.dataset)
        self.assertEqual(pd.curation_status, ProjectDataset.CurationStatus.PENDING)

    def test_idempotencia_retorna_200(self):
        """Segunda chamada com mesmo dataset_id retorna 200 sem duplicar."""
        self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        response = self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            ProjectDataset.objects.filter(project=self.project, dataset=self.dataset).count(), 1
        )

    def test_dataset_inexistente_retorna_404(self):
        """dataset_id que não existe no banco retorna 404."""
        response = self.client.post(self.url, {'dataset_id': 99999999}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_dataset_id_ausente_retorna_400(self):
        """Body sem campo 'dataset_id' retorna 400."""
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_dataset_id_invalido_retorna_400(self):
        """dataset_id não-inteiro retorna 400."""
        response = self.client.post(self.url, {'dataset_id': 'xyz'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_projeto_alheio_retorna_404(self):
        """User B não consegue adicionar dataset ao projeto de user A."""
        user_b = make_user('adddataset_userb')
        client_b = APIClient()
        client_b.force_authenticate(user=user_b)
        response = client_b.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_response_contem_campos_de_list_serializer(self):
        """Response tem os campos canônicos de ProjectDatasetListSerializer."""
        response = self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        self.assertEqual(response.status_code, 201)
        for campo in ('id', 'curation_status', 'accession'):
            self.assertIn(campo, response.data, f"campo '{campo}' ausente no response")

    def test_materializa_vinculo_ao_adicionar_dataset(self):
        """
        Adicionar a ponta faltante (dataset) deve promover o par órfão a
        ProjectPaperDataset(confidence='auto') via materialize_project_links.

        Cenário: paper já está no projeto (órfão Caso A). Após adicionar o dataset,
        o bridge deve ser criado automaticamente.
        """
        # Paper já no projeto + link global (Caso A)
        make_project_paper(self.project, self.paper)
        make_global_link(self.paper, self.dataset)

        self.assertEqual(ProjectPaperDataset.objects.filter(project=self.project).count(), 0)

        self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')

        self.assertEqual(
            ProjectPaperDataset.objects.filter(project=self.project).count(), 1,
            "materialize_project_links deve criar o bridge após adicionar a ponta faltante",
        )
        bridge = ProjectPaperDataset.objects.get(project=self.project)
        self.assertEqual(bridge.confidence, 'auto')

    def test_nao_autenticado_retorna_403(self):
        """Sem autenticação, retorna 401/403."""
        client_anon = APIClient()
        response = client_anon.post(self.url, {'dataset_id': self.dataset.id}, format='json')
        self.assertIn(response.status_code, [401, 403])

    def test_re_post_included_nao_rebaixa_curadoria(self):
        """
        Re-POST de dataset já com status 'included' NÃO zera curated_at, notes,
        exclusion_reason e NÃO volta para 'pending'.

        Ponto crítico exigido pelo 007 (skill curation-audit-trail).
        """
        import datetime
        from django.utils import timezone

        curated_ts = timezone.now() - datetime.timedelta(days=1)
        pd = ProjectDataset.objects.create(
            project=self.project,
            dataset=self.dataset,
            curation_status=ProjectDataset.CurationStatus.INCLUDED,
            notes='nota dataset preservada',
            curated_at=curated_ts,
        )

        response = self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')

        self.assertEqual(response.status_code, 200)
        pd.refresh_from_db()
        self.assertEqual(pd.curation_status, ProjectDataset.CurationStatus.INCLUDED,
                         "status não deve regredir para 'pending'")
        self.assertEqual(pd.notes, 'nota dataset preservada',
                         "notes não deve ser zerado")
        self.assertIsNotNone(pd.curated_at,
                             "curated_at não deve ser apagado")
        self.assertAlmostEqual(
            pd.curated_at.timestamp(), curated_ts.timestamp(), delta=1,
            msg="curated_at não deve ser alterado"
        )

    def test_re_post_excluded_nao_rebaixa_curadoria(self):
        """
        Re-POST de dataset já com status 'excluded' NÃO zera exclusion_reason
        nem curated_at e NÃO volta para 'pending'.
        """
        import datetime
        from django.utils import timezone

        curated_ts = timezone.now() - datetime.timedelta(days=2)
        pd = ProjectDataset.objects.create(
            project=self.project,
            dataset=self.dataset,
            curation_status=ProjectDataset.CurationStatus.EXCLUDED,
            exclusion_reason='dataset irrelevante',
            curated_at=curated_ts,
        )

        response = self.client.post(self.url, {'dataset_id': self.dataset.id}, format='json')

        self.assertEqual(response.status_code, 200)
        pd.refresh_from_db()
        self.assertEqual(pd.curation_status, ProjectDataset.CurationStatus.EXCLUDED,
                         "status não deve regredir para 'pending'")
        self.assertEqual(pd.exclusion_reason, 'dataset irrelevante',
                         "exclusion_reason não deve ser zerado")
        self.assertIsNotNone(pd.curated_at,
                             "curated_at não deve ser apagado")
