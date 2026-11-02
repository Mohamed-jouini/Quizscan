from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('grader', '0036_journal_identite'),
    ]

    operations = [
        migrations.AddField(
            model_name='quiz',
            name='format_impression',
            field=models.CharField(choices=[('a4', 'A4'), ('a3', 'A3 recto-verso, plié au milieu')], default='a4', help_text='Écrit : les copies. QCM : le sujet.', max_length=2, verbose_name="format d'impression"),
        ),
    ]
