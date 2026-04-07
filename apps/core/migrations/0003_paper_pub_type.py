from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_fts_triggers'),
    ]

    operations = [
        migrations.AddField(
            model_name='paper',
            name='pub_type',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text='Tipo primário do PubMed (Review, Systematic Review, RCT, etc.)',
                max_length=100,
                verbose_name='Tipo de Publicação',
            ),
        ),
    ]
