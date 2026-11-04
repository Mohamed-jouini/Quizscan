from django.db import migrations, models


class Migration(migrations.Migration):
    """Plus d'heure sur le sujet (demande de l'utilisateur) : la date seule, gardée
    telle quelle pour les épreuves déjà datées."""

    dependencies = [
        ('grader', '0039_date_heure'),
    ]

    operations = [
        migrations.RenameField(
            model_name='quiz',
            old_name='date_heure',
            new_name='date_epreuve',
        ),
        migrations.AlterField(
            model_name='quiz',
            name='date_epreuve',
            field=models.DateField(blank=True, help_text='Imprimée dans le cartouche du sujet, la durée dessous.', null=True, verbose_name="date de l'épreuve"),
        ),
    ]
