from django.db.models import Count, Sum, Q

from apps.core.models import (
    DaVinciProject, ProjectStats, ProjectPaper, ProjectDataset, ProjectSample,
    PaperAuthor, PaperGene, PaperDrug, PaperMeSHTerm, PaperVariant,
    ProjectPaperClinicalCategory,
)


class StatsService:
    """
    Computes and persists aggregated statistics for a DaVinci project.

    All queries run against the Django ORM on-demand. Results are cached
    in ProjectStats to avoid repeated heavy aggregations.
    """

    @staticmethod
    def compute_and_save(project: DaVinciProject) -> ProjectStats:
        included_pp = ProjectPaper.objects.filter(
            project=project,
            curation_status=ProjectPaper.CurationStatus.INCLUDED,
        )
        included_paper_ids = included_pp.values_list('paper_id', flat=True)

        # ── Literature counts ──────────────────────────────────────────
        status_counts = (
            ProjectPaper.objects.filter(project=project)
            .values('curation_status')
            .annotate(n=Count('id'))
        )
        counts_map = {row['curation_status']: row['n'] for row in status_counts}
        total_papers = ProjectPaper.objects.filter(project=project).count()

        # ── Dataset counts ────────────────────────────────────────────
        total_datasets = ProjectDataset.objects.filter(project=project).count()
        included_datasets = ProjectDataset.objects.filter(
            project=project,
            curation_status=ProjectDataset.CurationStatus.INCLUDED,
        ).count()

        # total_samples: usa ProjectSample reais quando disponíveis; caso contrário
        # cai de volta na soma de OmicDataset.n_samples (estimativa do esummary).
        # Isso garante que o número não regrida enquanto samples ainda não foram ingeridos.
        real_sample_count = ProjectSample.objects.filter(project=project).count()
        if real_sample_count > 0:
            total_samples = real_sample_count
        else:
            total_samples = (
                ProjectDataset.objects.filter(project=project)
                .aggregate(s=Sum('dataset__n_samples'))['s'] or 0
            )

        included_samples = ProjectSample.objects.filter(
            project=project,
            curation_status=ProjectSample.CurationStatus.INCLUDED,
        ).count()

        # ── Papers by year ────────────────────────────────────────────
        papers_by_year = {}
        for row in (
            included_pp.values('paper__pub_year')
            .annotate(n=Count('id'))
            .order_by('paper__pub_year')
        ):
            if row['paper__pub_year']:
                papers_by_year[str(row['paper__pub_year'])] = row['n']

        # ── Papers by journal ─────────────────────────────────────────
        papers_by_journal = {}
        for row in (
            included_pp.values('paper__journal')
            .annotate(n=Count('id'))
            .order_by('-n')[:20]
        ):
            if row['paper__journal']:
                papers_by_journal[row['paper__journal']] = row['n']

        # ── Papers by country (first author) ─────────────────────────
        papers_by_country = {}
        for row in (
            PaperAuthor.objects.filter(
                paper_id__in=included_paper_ids,
                position=1,
            )
            .exclude(country='')
            .values('country')
            .annotate(n=Count('id'))
            .order_by('-n')[:30]
        ):
            papers_by_country[row['country']] = row['n']

        # ── Papers by clinical category ───────────────────────────────
        papers_by_clinical_category = {}
        for row in (
            ProjectPaperClinicalCategory.objects.filter(
                project_paper__project=project,
                project_paper__curation_status=ProjectPaper.CurationStatus.INCLUDED,
            )
            .values('category__name')
            .annotate(n=Count('id'))
            .order_by('-n')
        ):
            papers_by_clinical_category[row['category__name']] = row['n']

        # ── Datasets by omic_type ─────────────────────────────────────
        datasets_by_omic_type = {}
        for row in (
            ProjectDataset.objects.filter(project=project)
            .values('dataset__omic_type')
            .annotate(n=Count('id'))
            .order_by('-n')
        ):
            if row['dataset__omic_type']:
                datasets_by_omic_type[row['dataset__omic_type']] = row['n']

        # ── Datasets by organism ──────────────────────────────────────
        datasets_by_organism = {}
        for row in (
            ProjectDataset.objects.filter(project=project)
            .values('dataset__organism')
            .annotate(n=Count('id'))
            .order_by('-n')[:20]
        ):
            if row['dataset__organism']:
                datasets_by_organism[row['dataset__organism']] = row['n']

        # ── Top genes ────────────────────────────────────────────────
        top_genes = [
            {'gene': row['gene_symbol'], 'count': row['total']}
            for row in PaperGene.objects.filter(paper_id__in=included_paper_ids)
            .values('gene_symbol')
            .annotate(total=Sum('mention_count'))
            .order_by('-total')[:20]
        ]

        # ── Top drugs ────────────────────────────────────────────────
        top_drugs = [
            {'drug': row['drug_name'], 'count': row['total']}
            for row in PaperDrug.objects.filter(paper_id__in=included_paper_ids)
            .values('drug_name')
            .annotate(total=Sum('mention_count'))
            .order_by('-total')[:20]
        ]

        # ── Top MeSH terms ───────────────────────────────────────────
        top_mesh_terms = [
            {'term': row['descriptor'], 'count': row['n']}
            for row in PaperMeSHTerm.objects.filter(
                paper_id__in=included_paper_ids,
                is_major_topic=True,
            )
            .values('descriptor')
            .annotate(n=Count('id'))
            .order_by('-n')[:20]
        ]

        # ── Top variants ─────────────────────────────────────────────
        top_variants = [
            {'rs_number': row['rs_number'], 'count': row['total']}
            for row in PaperVariant.objects.filter(paper_id__in=included_paper_ids)
            .values('rs_number')
            .annotate(total=Sum('mention_count'))
            .order_by('-total')[:20]
        ]

        stats, _ = ProjectStats.objects.update_or_create(
            project=project,
            defaults={
                'total_papers': total_papers,
                'included_papers': counts_map.get(ProjectPaper.CurationStatus.INCLUDED, 0),
                'excluded_papers': counts_map.get(ProjectPaper.CurationStatus.EXCLUDED, 0),
                'pending_papers': counts_map.get(ProjectPaper.CurationStatus.PENDING, 0),
                'total_datasets': total_datasets,
                'included_datasets': included_datasets,
                'total_samples': total_samples,
                'included_samples': included_samples,
                'papers_by_year': papers_by_year,
                'papers_by_journal': papers_by_journal,
                'papers_by_country': papers_by_country,
                'papers_by_clinical_category': papers_by_clinical_category,
                'datasets_by_omic_type': datasets_by_omic_type,
                'datasets_by_organism': datasets_by_organism,
                'top_genes': top_genes,
                'top_drugs': top_drugs,
                'top_mesh_terms': top_mesh_terms,
                'top_variants': top_variants,
            },
        )
        return stats
