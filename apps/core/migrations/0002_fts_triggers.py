# Generated manually

from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        # 1. Trigger de FTS para Paper
        migrations.RunSQL('''
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
                ON core_paper
                FOR EACH ROW
                EXECUTE FUNCTION update_paper_search_vector();
        ''', reverse_sql='''
            DROP TRIGGER IF EXISTS paper_search_trigger ON core_paper;
            DROP FUNCTION IF EXISTS update_paper_search_vector();
        '''),

        # 2. Trigger de FTS para OmicDataset
        migrations.RunSQL('''
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
                ON core_omicdataset
                FOR EACH ROW
                EXECUTE FUNCTION update_dataset_search_vector();
        ''', reverse_sql='''
            DROP TRIGGER IF EXISTS dataset_search_trigger ON core_omicdataset;
            DROP FUNCTION IF EXISTS update_dataset_search_vector();
        '''),

        # 3. View Materializada para stats
        migrations.RunSQL('''
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
            FROM core_projectpaper pp
            JOIN core_paper p ON pp.paper_id = p.id
            LEFT JOIN LATERAL (
                SELECT p.pub_year, COUNT(*) AS year_count
                FROM core_projectpaper pp2
                JOIN core_paper p2 ON pp2.paper_id = p2.id
                WHERE pp2.project_id = pp.project_id
                GROUP BY p2.pub_year
            ) yearly ON TRUE
            GROUP BY pp.project_id;

            CREATE UNIQUE INDEX ON mv_project_paper_stats (project_id);
        ''', reverse_sql='''
            DROP MATERIALIZED VIEW IF EXISTS mv_project_paper_stats;
        '''),

        # 4. Índice para busca por gene across projects
        migrations.RunSQL('''
            CREATE INDEX idx_papergene_symbol_lower
                ON core_papergene (LOWER(gene_symbol));
        ''', reverse_sql='''
            DROP INDEX IF EXISTS idx_papergene_symbol_lower;
        ''')
    ]
