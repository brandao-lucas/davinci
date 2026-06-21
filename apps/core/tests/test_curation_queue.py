"""
Testes de QA — CurationQueueViewSet (Fase 2, OmnisPathway).

Cobre:
  1. Contrato: GET retorna lista direta (sem paginação), JSON array raiz.
  2. Cross-user (Regra #3): usuário B não vê a fila do usuário A.
  3. Resolução manual:
     - Grava curated_at e notes no ProjectDataset (sem delete).
     - Marca origem manual em contract_confidence (score=1.0, is_manual=True).
     - Valor has_control_group atualizado no OmicDataset.
  4. Autenticação: endpoint protegido (401 sem autenticação).
  5. Tri-estado: itens com score >= 0.5 NÃO aparecem na fila.
  6. Fila vazia para projetos sem itens indeterminados.
  7. Resolução protegida: não é possível resolver item de projeto alheio.
  8. Acumulação de notes: nota nova concatena, não substitui.
  9. Resolução sem notes: curated_at é gravado mesmo sem nota.
 10. Resolução só aceita 'yes' ou 'no' (não 'unknown').

Padrão: APITestCase DRF (sem pytest).
Requer Postgres real (JSONB, has_key).
"""

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from apps.core.models import (
    DaVinciProject,
    OmicDataset,
    ProjectDataset,
)
from apps.core.services.contract_classifier_service import classify_has_control_group


# =============================================================================
# Helpers
# =============================================================================

def make_user(username, password='pw'):
    return User.objects.create_user(username=username, password=password)


def make_project(user, title='Test Project'):
    return DaVinciProject.objects.create(
        user=user,
        title=title,
        slug=f'{title.lower().replace(" ", "-")}-{user.username}-cq',
        query_term='test',
    )


def make_dataset(accession, source_db='geo', title='Dataset',
                 summary='', has_control_group='unknown',
                 contract_confidence=None, **kwargs):
    return OmicDataset.objects.create(
        accession=accession,
        source_db=source_db,
        title=title,
        summary=summary,
        has_control_group=has_control_group,
        contract_confidence=contract_confidence or {},
        **kwargs,
    )


def make_project_dataset(project, dataset, curation_status='pending', notes=''):
    return ProjectDataset.objects.create(
        project=project,
        dataset=dataset,
        curation_status=curation_status,
        notes=notes,
    )


def queue_url(project_id):
    """URL da lista da fila de curadoria."""
    return f'/api/v1/projects/{project_id}/curation-queue/'


def resolve_url(project_id, pd_pk):
    """URL da ação de resolução."""
    return f'/api/v1/projects/{project_id}/curation-queue/{pd_pk}/resolve/'


# =============================================================================
# 1. Contrato de resposta — lista direta sem paginação
# =============================================================================

class CurationQueueContractTests(APITestCase):
    """
    GET /projects/{id}/curation-queue/ deve retornar lista JSON direta.
    Sem paginação (atelier consome direto como array).
    """

    def setUp(self):
        self.user = make_user('cq_contract_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)

    def test_empty_queue_returns_empty_list(self):
        """Projeto sem itens indeterminados → resposta é lista vazia []."""
        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list,
                              'Resposta deve ser lista direta, não objeto paginado')
        self.assertEqual(len(response.data), 0)

    def test_response_is_direct_list_not_paginated(self):
        """
        Resposta NÃO é objeto paginado {count, results, ...}.
        Atelier consome como array diretamente.
        """
        # Cria um item na fila
        ds = make_dataset(
            'CQ_CONTRACT_DS',
            summary='Test summary.',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.3},
        )
        make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Deve ser uma lista JSON, não um dicionário com 'results'
        self.assertIsInstance(response.data, list,
                              'GET curation-queue deve retornar lista direta (sem paginação)')
        # Não deve ter chave 'results' (indicador de paginação DRF)
        if isinstance(response.data, dict):
            self.assertNotIn('results', response.data,
                             'Resposta paginada detectada — atelier espera lista direta')

    def test_list_contains_only_indeterminate_items(self):
        """
        A fila só contém itens com:
          - has_control_group='unknown'
          - chave 'has_control_group' presente em contract_confidence
          - score < 0.5
        """
        # Item na fila (indeterminado com score < 0.5)
        ds_indet = make_dataset(
            'CQ_CONTRACT_INDET',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.2},
        )
        pd_indet = make_project_dataset(self.project, ds_indet)

        # Item auto-classificado (score >= 0.5 → NÃO deve aparecer)
        ds_auto = make_dataset(
            'CQ_CONTRACT_AUTO',
            has_control_group='yes',
            contract_confidence={'has_control_group': 0.8},
        )
        make_project_dataset(self.project, ds_auto)

        # Item não-classificado (sem chave em contract_confidence → NÃO deve aparecer)
        ds_unclass = make_dataset(
            'CQ_CONTRACT_UNCLASS',
            has_control_group='unknown',
            contract_confidence={},
        )
        make_project_dataset(self.project, ds_unclass)

        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertIn(pd_indet.pk, pks, 'Item indeterminado deve aparecer na fila')
        self.assertNotIn(
            ds_auto.pk, pks,
            'Item auto-classificado (score >= 0.5) NÃO deve aparecer na fila'
        )

    def test_item_score_in_response(self):
        """
        Cada item da lista expõe has_control_group_score (score do classificador).
        """
        ds = make_dataset(
            'CQ_CONTRACT_SCORE',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.35},
        )
        make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(len(response.data), 1)
        item = response.data[0]
        self.assertIn('has_control_group_score', item)
        self.assertAlmostEqual(item['has_control_group_score'], 0.35, places=2)

    def test_item_no_extra_metadata_exposed(self):
        """
        extra_metadata NÃO é exposto no serializer (reduz superfície de dado sensível).
        """
        ds = make_dataset(
            'CQ_CONTRACT_NOMETA',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.1},
            extra_metadata={'sensitive_field': 'secret_value'},
        )
        make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(len(response.data), 1)
        item = response.data[0]
        self.assertNotIn('extra_metadata', item,
                         'extra_metadata NÃO deve ser exposto na fila de curadoria')


# =============================================================================
# 2. Cross-user (Regra #3)
# =============================================================================

class CurationQueueCrossUserTests(APITestCase):
    """
    Usuário B não vê a fila do usuário A.
    Todos os endpoints da fila são escopados por request.user via ProjectDataset.
    """

    def setUp(self):
        self.user_a = make_user('cq_cross_user_a')
        self.user_b = make_user('cq_cross_user_b')

        self.client_a = APIClient()
        self.client_b = APIClient()
        self.client_a.force_authenticate(user=self.user_a)
        self.client_b.force_authenticate(user=self.user_b)

        self.project_a = make_project(self.user_a, title='Project A')
        self.project_b = make_project(self.user_b, title='Project B')

        # Dataset indeterminado no projeto de A
        self.ds_a = make_dataset(
            'CQ_CROSS_DS_A',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.15},
        )
        self.pd_a = make_project_dataset(self.project_a, self.ds_a)

        # Dataset indeterminado no projeto de B
        self.ds_b = make_dataset(
            'CQ_CROSS_DS_B',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.25},
        )
        self.pd_b = make_project_dataset(self.project_b, self.ds_b)

    def test_user_b_cannot_list_queue_of_user_a(self):
        """
        User B tentando GET na fila do projeto A → 404 (projeto alheio não existe para B).
        """
        response = self.client_b.get(queue_url(self.project_a.id))
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao listar fila do projeto A '
            '(sem vazar que o projeto existe).'
        )

    def test_user_a_cannot_list_queue_of_user_b(self):
        """
        User A tentando GET na fila do projeto B → 404.
        """
        response = self.client_a.get(queue_url(self.project_b.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_a_sees_only_own_queue(self):
        """
        User A vê apenas seus itens (não os de B).
        """
        response = self.client_a.get(queue_url(self.project_a.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertIn(self.pd_a.pk, pks)
        self.assertNotIn(self.pd_b.pk, pks)

    def test_user_b_cannot_resolve_item_of_user_a(self):
        """
        User B tentando POST /resolve/ no item do projeto A → 404.
        O item do projeto A existe, mas é invisível para B.
        """
        response = self.client_b.post(
            resolve_url(self.project_a.id, self.pd_a.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'User B deve receber 404 ao tentar resolver item do projeto A.'
        )
        # Confirma que o dataset NÃO foi alterado
        self.ds_a.refresh_from_db()
        self.assertEqual(self.ds_a.has_control_group, 'unknown')

    def test_user_b_cannot_resolve_item_in_wrong_project(self):
        """
        User B tentando resolver pd_a via projeto_b → 404
        (pd_a não pertence a projeto_b, mesmo que B seja dono de B).
        """
        response = self.client_b.post(
            resolve_url(self.project_b.id, self.pd_a.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            'ProjectDataset de projeto alheio deve retornar 404.'
        )
        self.ds_a.refresh_from_db()
        self.assertEqual(self.ds_a.has_control_group, 'unknown')

    def test_unauthenticated_user_cannot_list_queue(self):
        """
        Requisição sem autenticação → 401 (Firebase guard ativo).
        """
        unauth_client = APIClient()
        response = unauth_client.get(queue_url(self.project_a.id))
        self.assertIn(
            response.status_code,
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN],
            'Fila de curadoria deve exigir autenticação.'
        )


# =============================================================================
# 3. Ação de resolução — preservação de auditoria
# =============================================================================

class CurationQueueResolveAuditTests(APITestCase):
    """
    Resolução manual: curated_at, notes, sem delete, origem manual marcada.
    Skill: curation-audit-trail.
    """

    def setUp(self):
        self.user = make_user('cq_audit_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)

        # Dataset na fila (indeterminado pelo classificador)
        self.ds = make_dataset(
            'CQ_AUDIT_DS',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.2},
        )
        self.pd = make_project_dataset(self.project, self.ds)

    def test_resolve_yes_sets_has_control_group(self):
        """POST resolve com 'yes' → OmicDataset.has_control_group='yes'."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ds.refresh_from_db()
        self.assertEqual(self.ds.has_control_group, 'yes')

    def test_resolve_no_sets_has_control_group(self):
        """POST resolve com 'no' → OmicDataset.has_control_group='no'."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.ds.refresh_from_db()
        self.assertEqual(self.ds.has_control_group, 'no')

    def test_resolve_sets_curated_at(self):
        """Após resolução, ProjectDataset.curated_at é preenchido."""
        self.assertIsNone(self.pd.curated_at)
        before = timezone.now()
        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.pd.refresh_from_db()
        self.assertIsNotNone(self.pd.curated_at)
        self.assertGreater(self.pd.curated_at, before)

    def test_resolve_with_notes_stores_notes(self):
        """Nota do curador é armazenada no ProjectDataset.notes."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes', 'notes': 'Texto claramente indica grupo controle'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pd.refresh_from_db()
        self.assertIn('Texto claramente indica grupo controle', self.pd.notes)

    def test_resolve_without_notes_still_sets_curated_at(self):
        """Resolução sem nota → curated_at é preenchido mesmo assim."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.pd.refresh_from_db()
        self.assertIsNotNone(self.pd.curated_at)
        # Notes pode estar vazio — curated_at é obrigatório
        self.assertIsNotNone(self.pd.curated_at)

    def test_resolve_marks_manual_in_contract_confidence(self):
        """
        Após resolução manual, contract_confidence contém:
          - 'has_control_group': 1.0 (score máximo)
          - 'has_control_group_manual': True (origem manual)
        """
        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes', 'notes': 'Manual curation'},
            format='json',
        )
        self.ds.refresh_from_db()
        conf = self.ds.contract_confidence
        self.assertIn('has_control_group', conf)
        self.assertEqual(conf['has_control_group'], 1.0,
                         'Score após curadoria manual deve ser 1.0')
        self.assertIn('has_control_group_manual', conf)
        self.assertTrue(conf['has_control_group_manual'],
                        'has_control_group_manual deve ser True após curadoria manual')

    def test_resolve_does_not_delete_project_dataset(self):
        """
        Resolução manual NÃO deleta o ProjectDataset (curation-audit-trail).
        O status de curadoria pode mudar, mas o registro persiste.
        """
        pd_pk = self.pd.pk
        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        # ProjectDataset ainda existe
        self.assertTrue(
            ProjectDataset.objects.filter(pk=pd_pk).exists(),
            'Resolução não deve deletar ProjectDataset (curation-audit-trail)'
        )

    def test_resolve_does_not_delete_omic_dataset(self):
        """
        OmicDataset (dado compartilhado) NÃO é deletado pela resolução.
        """
        ds_pk = self.ds.pk
        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertTrue(
            OmicDataset.objects.filter(pk=ds_pk).exists(),
            'Resolução não deve deletar OmicDataset'
        )

    def test_resolve_preserves_existing_notes(self):
        """
        Nota acumula — nova nota é concatenada à existente, não substitui.
        """
        self.pd.notes = 'nota original do sistema'
        self.pd.save(update_fields=['notes'])

        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes', 'notes': 'nota do curador'},
            format='json',
        )
        self.pd.refresh_from_db()
        self.assertIn('nota original do sistema', self.pd.notes,
                      'Nota original deve ser preservada')
        self.assertIn('nota do curador', self.pd.notes,
                      'Nova nota deve ser concatenada')

    def test_resolve_confidence_preserves_other_axes(self):
        """
        Resolução manual atualiza has_control_group em contract_confidence
        mas preserva outros eixos (ex.: is_single_cell).
        """
        self.ds.contract_confidence = {
            'has_control_group': 0.2,
            'is_single_cell': 0.9,
        }
        self.ds.save(update_fields=['contract_confidence'])

        self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.ds.refresh_from_db()
        self.assertIn('is_single_cell', self.ds.contract_confidence,
                      'is_single_cell deve ser preservado na contract_confidence')
        self.assertEqual(self.ds.contract_confidence['is_single_cell'], 0.9)

    def test_resolve_response_contains_expected_fields(self):
        """
        Resposta do resolve contém os campos esperados pelo frontend:
        id, dataset_id, accession, has_control_group, has_control_group_score,
        notes, curated_at.
        """
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes', 'notes': 'test'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in ['id', 'dataset_id', 'accession', 'has_control_group',
                      'has_control_group_score', 'notes', 'curated_at']:
            self.assertIn(field, response.data,
                          f"Campo '{field}' ausente na resposta do resolve")

    def test_resolve_response_has_control_group_reflects_decision(self):
        """
        Resposta reflete a decisão do curador no campo has_control_group.
        """
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['has_control_group'], 'no')

    def test_resolve_response_score_is_one(self):
        """
        Após resolução manual, has_control_group_score na resposta é 1.0.
        """
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(float(response.data['has_control_group_score']), 1.0)


# =============================================================================
# 4. Validação de input
# =============================================================================

class CurationQueueResolveValidationTests(APITestCase):
    """
    Resolução rejeita valores inválidos de has_control_group.
    """

    def setUp(self):
        self.user = make_user('cq_valid_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)
        self.ds = make_dataset(
            'CQ_VALID_DS',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.1},
        )
        self.pd = make_project_dataset(self.project, self.ds)

    def test_resolve_unknown_is_rejected(self):
        """
        has_control_group='unknown' é rejeitado (curador deve decidir yes/no).
        """
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'unknown'},
            format='json',
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
            "Curador não pode resolver como 'unknown' — deve decidir yes/no."
        )
        self.ds.refresh_from_db()
        self.assertEqual(self.ds.has_control_group, 'unknown')

    def test_resolve_invalid_value_is_rejected(self):
        """has_control_group='maybe' → 400."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'maybe'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resolve_missing_field_is_rejected(self):
        """Body sem has_control_group → 400."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'notes': 'sem campo obrigatório'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resolve_with_yes_is_accepted(self):
        """has_control_group='yes' → 200."""
        response = self.client.post(
            resolve_url(self.project.id, self.pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resolve_with_no_is_accepted(self):
        """has_control_group='no' → 200."""
        # Cria dataset separado para evitar conflito com test_resolve_with_yes
        ds2 = make_dataset(
            'CQ_VALID_DS2',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.1},
        )
        pd2 = make_project_dataset(self.project, ds2)
        response = self.client.post(
            resolve_url(self.project.id, pd2.pk),
            {'has_control_group': 'no'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


# =============================================================================
# 5. Tri-estado: score >= 0.5 não aparece na fila
# =============================================================================

class CurationQueueTriStateTests(APITestCase):
    """
    Verifica que o filtro de fila respeita o limiar 0.5 (D2 travado).
    """

    def setUp(self):
        self.user = make_user('cq_tristate_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)

    def test_score_below_threshold_appears_in_queue(self):
        """Score 0.49 → fila de curadoria (abaixo do limiar)."""
        ds = make_dataset(
            'CQ_TS_LOW',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.49},
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertIn(pd.pk, pks, 'Score 0.49 deve aparecer na fila')

    def test_score_at_threshold_does_not_appear(self):
        """
        Score 0.50 com has_control_group='yes' → não aparece na fila
        (foi auto-classificado, campo não é 'unknown').
        """
        ds = make_dataset(
            'CQ_TS_AT',
            has_control_group='yes',
            contract_confidence={'has_control_group': 0.5},
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks,
                         'Dataset auto-classificado (score >= 0.5) NÃO deve aparecer na fila')

    def test_score_above_threshold_does_not_appear(self):
        """Score 0.80 → não aparece na fila."""
        ds = make_dataset(
            'CQ_TS_HIGH',
            has_control_group='yes',
            contract_confidence={'has_control_group': 0.80},
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks)

    def test_unclassified_not_in_queue(self):
        """
        Dataset sem chave em contract_confidence (não-classificado) →
        NÃO aparece na fila (é diferente de classificado-indeterminado).
        """
        ds = make_dataset(
            'CQ_TS_UNCLASS',
            has_control_group='unknown',
            contract_confidence={},
        )
        pd = make_project_dataset(self.project, ds)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks,
                         'Dataset não-classificado (sem chave) NÃO deve aparecer na fila')

    def test_mixed_queue_only_indeterminate_items(self):
        """
        Projeto com mistura de estados: apenas indeterminados aparecem.
        """
        # Indeterminado (vai para a fila)
        ds_indet = make_dataset('CQ_TS_MIX_INDET', has_control_group='unknown',
                                contract_confidence={'has_control_group': 0.3})
        pd_indet = make_project_dataset(self.project, ds_indet)

        # Auto-classificado yes
        ds_yes = make_dataset('CQ_TS_MIX_YES', has_control_group='yes',
                              contract_confidence={'has_control_group': 0.8})
        make_project_dataset(self.project, ds_yes)

        # Auto-classificado no
        ds_no = make_dataset('CQ_TS_MIX_NO', has_control_group='no',
                             contract_confidence={'has_control_group': 0.7})
        make_project_dataset(self.project, ds_no)

        # Não-classificado
        ds_unc = make_dataset('CQ_TS_MIX_UNC', has_control_group='unknown',
                              contract_confidence={})
        make_project_dataset(self.project, ds_unc)

        response = self.client.get(queue_url(self.project.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pks = [item['id'] for item in response.data]
        self.assertEqual(len(pks), 1, 'Apenas 1 item deve aparecer na fila')
        self.assertIn(pd_indet.pk, pks)


# =============================================================================
# 6. Integração: classify_has_control_group → fila
# =============================================================================

class CurationQueueIntegrationTests(APITestCase):
    """
    Integração do fluxo completo:
    1. Dataset sem sinal → score=0 → vai para fila
    2. Dataset com sinal forte → score >= 0.5 → não vai para fila
    3. Resolve via endpoint → saiu da fila
    """

    def setUp(self):
        self.user = make_user('cq_integ_user')
        self.client.force_authenticate(user=self.user)
        self.project = make_project(self.user)

    def test_low_signal_dataset_appears_in_queue_after_classification(self):
        """Dataset sem keyword → classificador marca score 0 → aparece na fila."""
        ds = make_dataset(
            'CQ_INTEG_LOW',
            summary='Transcriptomic profiling of tumor samples.',
        )
        pd = make_project_dataset(self.project, ds)

        # Roda classificador
        classify_has_control_group(ds)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertIn(pd.pk, pks, 'Dataset com score 0 deve aparecer na fila')

    def test_high_signal_dataset_not_in_queue_after_classification(self):
        """Dataset com sinal forte → score >= 0.5 → NÃO aparece na fila."""
        ds = make_dataset(
            'CQ_INTEG_HIGH',
            summary='Case-control study with healthy control group and matched healthy donors.',
        )
        pd = make_project_dataset(self.project, ds)

        classify_has_control_group(ds)
        ds.refresh_from_db()
        # Confirma que foi auto-classificado
        self.assertGreaterEqual(ds.contract_confidence.get('has_control_group', 0), 0.5)

        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks,
                         'Dataset auto-classificado não deve aparecer na fila')

    def test_resolve_removes_item_from_queue(self):
        """
        Item resolvido manualmente não deve mais aparecer na fila
        (has_control_group já não é 'unknown').
        """
        ds = make_dataset(
            'CQ_INTEG_RESOLVE',
            has_control_group='unknown',
            contract_confidence={'has_control_group': 0.1},
        )
        pd = make_project_dataset(self.project, ds)

        # Confirma que está na fila
        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertIn(pd.pk, pks)

        # Resolve
        self.client.post(
            resolve_url(self.project.id, pd.pk),
            {'has_control_group': 'yes'},
            format='json',
        )

        # Confirma que saiu da fila
        response = self.client.get(queue_url(self.project.id))
        pks = [item['id'] for item in response.data]
        self.assertNotIn(pd.pk, pks,
                         'Item resolvido deve sair da fila (has_control_group não é mais unknown)')
