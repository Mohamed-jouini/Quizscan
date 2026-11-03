from django.db import migrations


class Migration(migrations.Migration):
    """Un QCM ouvre un concours (QCM -> écrit -> oral) : il ne suit aucune épreuve.
    Le formulaire le permettait, et « demo-concours » s'est retrouvé relié au QCM
    « jhjh ». Seul le lien est retiré : candidats, copies et notes ne bougent pas.

    En SQL plutôt qu'en RunPython : `passer_a_postgres` refuse une ancienne base
    dont une migration RunPython est en attente."""

    dependencies = [
        ('grader', '0037_format_impression'),
    ]

    operations = [
        migrations.RunSQL(
            "UPDATE grader_quiz SET epreuve_precedente_id = NULL "
            "WHERE type_epreuve = 'qcm' AND epreuve_precedente_id IS NOT NULL",
            migrations.RunSQL.noop),
    ]
