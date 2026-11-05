"""Copie d'une ancienne base SQLite dans PostgreSQL (manage.py passer_a_postgres).

Vide la base de test : à lancer comme les autres, seul.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.management.base import CommandError
from django.db import connection, connections

from grader.management.commands.passer_a_postgres import transferer
from grader.models import Answer, ClassGroup, Question, Quiz, ScanBatch, SheetScan, Student

FABRIQUE = "fabrique_sqlite"
NOM_LONG = "N" * 100              # SQLite l'accepte dans un champ de 80 caractères


def _fabriquer(chemin):
    """Une ancienne base SQLite, comme celle d'un serveur à migrer."""
    settings.DATABASES[FABRIQUE] = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(chemin)}
    connections.settings = connections.configure_settings(settings.DATABASES)
    call_command("migrate", database=FABRIQUE, interactive=False, verbosity=0)
    prof = User.objects.db_manager(FABRIQUE).create_user("ancien_prof", password="x-pass-123")
    prof.user_permissions.add(Permission.objects.using(FABRIQUE).get(codename="creer_epreuve"))
    groupe = ClassGroup.objects.using(FABRIQUE).create(name="Classe é", owner=prof)
    quiz = Quiz.objects.using(FABRIQUE).create(title="Épreuve à copier", class_group=groupe, owner=prof)
    question = Question.objects.using(FABRIQUE).create(quiz=quiz, order=1, qtype="qcm", correct_choice=0)
    etudiant = Student.objects.using(FABRIQUE).create(class_group=groupe, last_name=NOM_LONG,
                                                      first_name="Zoé", student_number="770001")
    lot = ScanBatch.objects.using(FABRIQUE).create(quiz=quiz, status="done", label="Lot")
    copie = SheetScan.objects.using(FABRIQUE).create(batch=lot, status="ok", student=etudiant,
                                                     source_name="p.jpg")
    Answer.objects.using(FABRIQUE).create(sheet=copie, question=question, detected_choice=0,
                                          points_awarded=1.5)
    connections[FABRIQUE].close()
    del connections[FABRIQUE]
    settings.DATABASES.pop(FABRIQUE)
    connections.settings = connections.configure_settings(settings.DATABASES)


def _empreinte(chemin):
    return hashlib.sha256(Path(chemin).read_bytes()).hexdigest()


def main():
    logging.disable(logging.CRITICAL)
    assert connection.vendor == "postgresql" and connection.settings_dict["NAME"].endswith("_test")
    dossier = tempfile.mkdtemp()
    try:
        source = Path(dossier) / "db.sqlite3"
        _fabriquer(source)
        avant = _empreinte(source)
        muet = lambda *_: None          # noqa: E731

        # ---- copie fidèle ; texte trop long pour PostgreSQL raccourci et signalé
        corrections = transferer(source, ecraser=True, ecrire=muet)
        assert _empreinte(source) == avant, "le fichier SQLite a été modifié"
        etudiant = Student.objects.get(student_number="770001")
        assert etudiant.last_name == "N" * 80 and etudiant.first_name == "Zoé"
        assert any("last_name" in c for c in corrections), corrections
        assert Quiz.objects.get().title == "Épreuve à copier"
        assert Answer.objects.get().points_awarded == 1.5
        prof = User.objects.get(username="ancien_prof")
        assert prof.check_password("x-pass-123"), "mot de passe perdu"
        assert prof.has_perm("grader.creer_epreuve"), "droit perdu"
        print("  copie : notes, comptes, droits, accents ; texte trop long signalé OK")

        # ---- les compteurs repartent après les données copiées
        nouveau = Student.objects.create(class_group=etudiant.class_group, last_name="NEUF", first_name="A")
        assert nouveau.pk > etudiant.pk
        nouveau.delete()
        print("  nouvel enregistrement après la copie : sans collision OK")

        # ---- jamais par-dessus des données déjà là
        try:
            transferer(source, ecrire=muet)
            raise AssertionError("base non vide écrasée")
        except CommandError as exc:
            assert "déjà des données" in str(exc)
        assert Student.objects.filter(student_number="770001").exists()
        print("  base déjà remplie : refus, rien n'est écrit OK")

        # ---- ancienne base en retard de quelques migrations : mise à niveau de la copie
        tardive = Path(dossier) / "tardive.sqlite3"
        shutil.copy(source, tardive)
        # (une migration qui change les tables s'ajoute ici : retirer ce qu'elle a créé)
        base = sqlite3.connect(tardive)
        base.execute("DROP TABLE grader_piecejointe")
        base.execute("ALTER TABLE grader_quiz DROP COLUMN format_impression")
        base.execute("ALTER TABLE grader_quiz DROP COLUMN date_epreuve")
        for colonne in ("cin_lieu", "cin_date", "centre", "salle"):
            base.execute(f"ALTER TABLE grader_inscrit DROP COLUMN {colonne}")
        base.execute("DELETE FROM django_migrations WHERE app='grader' AND name >= '0035'")
        base.commit()
        base.close()
        reference = _empreinte(tardive)
        transferer(tardive, ecraser=True, ecrire=muet)
        assert _empreinte(tardive) == reference, "le fichier en retard a été modifié"
        assert Answer.objects.get().points_awarded == 1.5
        assert Quiz.objects.get().format_impression == "a4" and Quiz.objects.get().date_epreuve is None
        print("  base en retard de plusieurs migrations : copie mise à niveau, original intact OK")

        # ---- trop ancienne : une migration de données irait dans la mauvaise base
        vieille = Path(dossier) / "vieille.sqlite3"
        shutil.copy(source, vieille)
        base = sqlite3.connect(vieille)
        base.execute("DELETE FROM django_migrations WHERE app='grader' AND name >= '0028'")
        base.commit()
        base.close()
        try:
            transferer(vieille, ecraser=True, ecrire=muet)
            raise AssertionError("base trop ancienne acceptée")
        except CommandError as exc:
            assert "trop ancienne" in str(exc)
        print("  base trop ancienne : refus clair OK")
    finally:
        call_command("flush", interactive=False, verbosity=0)
        shutil.rmtree(dossier, ignore_errors=True)
    print("\n✅ TEST DU PASSAGE À POSTGRESQL RÉUSSI")


if __name__ == "__main__":
    main()
