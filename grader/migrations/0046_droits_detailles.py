"""Droits détaillés (26 novembre 2026, demande de l'utilisateur) : « téléverser les copies
scannées », « lancer la correction » et « saisir les questions » se donnent aussi seuls.

Rien ne change pour les comptes en place : « créer des épreuves » permet toujours de saisir
les questions, « corriger les copies » de téléverser et de lancer (grader/droits.py). Les
trois nouvelles permissions sont créées par le signal post_migrate de contrib.auth, comme
toute permission ; le nouveau libellé des deux anciennes est posé au même moment
(grader/apps.py), pas ici : une migration de données empêcherait le passage d'une base
SQLite à PostgreSQL (passer_a_postgres). Aucune table ne change.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('grader', '0045_quiz_versions'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='quiz',
            options={'ordering': ['-created_at'], 'permissions': [('creer_epreuve', 'Créer les concours et les quiz, et leurs questions'), ('corriger_copies', 'Corriger les copies : téléverser, lancer la correction, vérifier'), ('televerser_copies', 'Téléverser les copies scannées'), ('lancer_correction', 'Lancer la correction'), ('saisir_questions', 'Saisir les questions et le corrigé')], 'verbose_name': 'quiz', 'verbose_name_plural': 'quiz'},
        ),
    ]
