# Fix de regressão (Fase 0): as colunas de contrato do OmicDataset eram NOT NULL
# com default apenas no nível Django (app-level). O AddField do Django NÃO emite
# DEFAULT no Postgres, então o COPY writer do Rust (INSERT ... SELECT com subset
# de colunas) inseria NULL nessas colunas e violava o NOT NULL (SqlState 23502).
#
# Esta migration é puramente aditiva e reversível: aplica SET DEFAULT no nível do
# banco espelhando os defaults do Django. Não há mudança de estado de modelo
# (os defaults Django já existem em models.py), logo makemigrations --check
# continua limpo. omics_count é nullable e não precisa de default.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_alter_ingestionjob_job_type'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE core_omicdataset "
                "ALTER COLUMN contract_confidence SET DEFAULT '{}'::jsonb, "
                "ALTER COLUMN has_control_group SET DEFAULT 'unknown', "
                "ALTER COLUMN disease_axis SET DEFAULT 'indeterminate', "
                "ALTER COLUMN is_single_cell SET DEFAULT 'unknown', "
                "ALTER COLUMN data_format SET DEFAULT 'unknown', "
                "ALTER COLUMN access_type SET DEFAULT 'unknown', "
                "ALTER COLUMN omics_layers SET DEFAULT '{}'::varchar[], "
                "ALTER COLUMN sample_join_key SET DEFAULT '{}'::varchar[];"
            ),
            reverse_sql=(
                "ALTER TABLE core_omicdataset "
                "ALTER COLUMN contract_confidence DROP DEFAULT, "
                "ALTER COLUMN has_control_group DROP DEFAULT, "
                "ALTER COLUMN disease_axis DROP DEFAULT, "
                "ALTER COLUMN is_single_cell DROP DEFAULT, "
                "ALTER COLUMN data_format DROP DEFAULT, "
                "ALTER COLUMN access_type DROP DEFAULT, "
                "ALTER COLUMN omics_layers DROP DEFAULT, "
                "ALTER COLUMN sample_join_key DROP DEFAULT;"
            ),
            elidable=False,
        ),
    ]
