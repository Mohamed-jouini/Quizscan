from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('grader', '0044_quiz_coefficient_final'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='versions',
            field=models.PositiveSmallIntegerField(choices=[(1, 'Une seule version'), (2, 'Deux versions, A et B — questions et choix dans un autre ordre')], default=1, help_text='QCM : la version B mélange les questions et leurs choix ; chaque copie porte sa version et se corrige selon elle.', verbose_name='versions du sujet'),
        ),
    ]
