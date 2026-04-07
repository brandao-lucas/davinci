from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_projectstats_omic_subcategory_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='DatasetPaperLinkPending',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('dataset_accession', models.CharField(max_length=50)),
                ('paper_pmid', models.BigIntegerField()),
                ('link_source', models.CharField(default='elink', max_length=50)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'core_datasetpaperlinkpending',
                'unique_together': {('dataset_accession', 'paper_pmid')},
            },
        ),
    ]
