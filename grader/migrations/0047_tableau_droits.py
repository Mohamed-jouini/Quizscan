"""Le tableau des droits (26 novembre 2026, demande de l'utilisateur : « donne la main de
choisir Ajouter, Modifier, Supprimer, Vérifier ») : une permission par case (concours et quiz,
questions et corrigé, copies, candidats et inscriptions, communiqués × ajouter, modifier,
supprimer, vérifier). Les droits d'avant restent, comme paquets de cases : aucun compte ne
perd rien (grader/droits.py). Les permissions sont créées par post_migrate, leurs libellés
posés par grader/apps.py. Ni table changée, ni migration de données (passer_a_postgres).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('grader', '0046_droits_detailles'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='quiz',
            options={'ordering': ['-created_at'], 'permissions': [('creer_epreuve', 'Tout créer : concours et quiz, questions, candidats, communiqués'), ('corriger_copies', 'Tout corriger : copies, et lancer la correction'), ('saisir_questions', 'Questions et corrigé — tout'), ('epreuve_ajouter', 'Concours et quiz — ajouter'), ('epreuve_modifier', 'Concours et quiz — modifier'), ('question_ajouter', 'Questions et corrigé — ajouter'), ('question_modifier', 'Questions et corrigé — modifier'), ('question_supprimer', 'Questions et corrigé — supprimer'), ('televerser_copies', 'Copies — ajouter (téléverser les scans)'), ('copie_modifier', 'Copies — modifier (noter)'), ('copie_supprimer', 'Copies — supprimer'), ('copie_verifier', 'Copies — vérifier'), ('lancer_correction', 'Lancer la correction'), ('candidat_ajouter', 'Candidats et inscriptions — ajouter'), ('candidat_modifier', 'Candidats et inscriptions — modifier'), ('candidat_supprimer', 'Candidats et inscriptions — supprimer'), ('communique_ajouter', 'Communiqués — ajouter'), ('communique_modifier', 'Communiqués — modifier'), ('communique_supprimer', 'Communiqués — supprimer')], 'verbose_name': 'quiz', 'verbose_name_plural': 'quiz'},
        ),
    ]
