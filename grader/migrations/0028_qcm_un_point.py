"""Chaque QCM vaut un point (règle de l'établissement, 14 octobre 2026).

Les QCM saisies avec un autre barème passent à 1 point, et les réponses
justes déjà notées sont recalculées en conséquence (une réponse fausse ou
vide ne dépend pas du barème : 0, ou la pénalité de l'épreuve).
"""
from django.db import migrations, models
from django.db.models import F


def un_point(apps, schema_editor):
    Question = apps.get_model("grader", "Question")
    Answer = apps.get_model("grader", "Answer")
    Question.objects.filter(qtype="qcm").exclude(points=1.0).update(points=1.0)
    (Answer.objects
     .filter(question__qtype="qcm", manually_set=False, is_multiple=False,
             question__correct_choice__isnull=False,
             detected_choice=F("question__correct_choice"))
     .exclude(points_awarded=1.0)
     .update(points_awarded=1.0))


class Migration(migrations.Migration):

    dependencies = [("grader", "0027_inscriptions_public")]

    operations = [
        migrations.RunPython(un_point, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="question", name="points",
            field=models.FloatField(
                "barème (points)", default=1.0,
                help_text="Une QCM vaut toujours 1 point ; barème libre pour une "
                          "réponse manuscrite."),
        ),
    ]
