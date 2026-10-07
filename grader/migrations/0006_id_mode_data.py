# Reprise des quiz existants : grille active si id_digits > 0
from django.db import migrations


def forward(apps, schema_editor):
    Quiz = apps.get_model("grader", "Quiz")
    Quiz.objects.filter(id_digits__gt=0).update(id_mode="grid")
    Quiz.objects.filter(id_digits=0).update(id_digits=6)


class Migration(migrations.Migration):
    dependencies = [("grader", "0005_quiz_id_mode_alter_quiz_id_digits")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
