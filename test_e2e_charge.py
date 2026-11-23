"""Le jour des résultats : des milliers de visiteurs sur l'espace public, sans le faire tomber.

Mesure du 23 novembre 2026, concours de 10 000 candidats (QCM de 50 questions) :
  - la liste publique des admis se recalculait pour chaque visiteur qui trouvait le cache vide
    (5 s et plus de 100 Mo chacun) : huit à la fois ont épuisé la mémoire d'un poste de 6 Go et
    fait tomber PostgreSQL. Une seule requête la calcule maintenant ; les autres attendent
    celle-ci, ou servent la précédente pendant qu'elle se refait ;
  - le cache en fichiers relisait tout son dossier à chaque écriture, et le frein aux
    recherches y garde un fichier par adresse : à 10 000 fichiers, une recherche passait de 67
    à 467 ms. Le ménage se fait au plus une fois par minute, périmés d'abord.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
import shutil
import tempfile
import threading
import time
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth import get_user_model          # noqa: E402
from django.core.cache import cache, caches             # noqa: E402
from django.db import connection, transaction           # noqa: E402
from django.test import Client                          # noqa: E402
from django.urls import reverse                         # noqa: E402
from django.utils import timezone                       # noqa: E402

from grader import cache as cache_qs                    # noqa: E402
from grader import views_public                         # noqa: E402
from grader.models import (Answer, ClassGroup, Question, Quiz,  # noqa: E402
                           ScanBatch, SheetScan, Student)


def _nettoyer():
    for quiz in Quiz.objects.filter(class_group__name__startswith="CH ").order_by("-pk"):
        quiz.delete()
    ClassGroup.objects.filter(name__startswith="CH ").delete()
    get_user_model().objects.filter(username__startswith="ch_").delete()


def _concours():
    """QCM de 5 questions publié, liste des admis comprise (3 bonnes réponses au moins) :
    CANDIDAT00 à CANDIDAT11, n bonnes réponses pour n % 6."""
    prof = get_user_model().objects.create_user("ch_prof", password="x")
    groupe = ClassGroup.objects.create(name="CH concours", owner=prof)
    quiz = Quiz.objects.create(title="CH concours", class_group=groupe, owner=prof,
                               auto_enroll=True, annee="1", note_admission=3,
                               resultats_publies=True, admis_publies=True,
                               publie_le=timezone.now())
    questions = [Question.objects.create(quiz=quiz, order=i, qtype="qcm", correct_choice=0)
                 for i in range(1, 6)]
    lot = ScanBatch.objects.create(quiz=quiz, status="done", total_pages=12)
    for n in range(12):
        etudiant = Student.objects.create(class_group=groupe, last_name=f"CANDIDAT{n:02d}",
                                          first_name="P", student_number=str(7700 + n))
        copie = SheetScan.objects.create(batch=lot, status="ok", student=etudiant,
                                         source_name="c.jpg")
        for k, question in enumerate(questions):
            juste = k < n % 6
            Answer.objects.create(sheet=copie, question=question,
                                  detected_choice=0 if juste else 1,
                                  points_awarded=1.0 if juste else 0.0)
    return quiz


def verifier_liste_des_admis():
    quiz = _concours()
    url = reverse("public_epreuve", args=[quiz.pk])
    appels = []
    vrai_calcul = views_public._calculer_admis

    def calcul_lent(q):
        appels.append(q.pk)
        time.sleep(1.0)             # le temps que les autres visiteurs arrivent
        return vrai_calcul(q)

    # ---- à la publication (rien en cache) : six visiteurs à la fois, un seul calcul
    cache.clear()
    pages, erreurs = [], []
    depart = threading.Barrier(6)

    def visiteur():
        try:
            client = Client()
            depart.wait(10)
            reponse = client.get(url)
            pages.append((reponse.status_code, reponse.content.decode()))
        except Exception as exc:  # noqa: BLE001
            erreurs.append(repr(exc))
        finally:
            connection.close()

    with mock.patch.object(views_public, "_calculer_admis", calcul_lent):
        fils = [threading.Thread(target=visiteur) for _ in range(6)]
        for fil in fils:
            fil.start()
        for fil in fils:
            fil.join(60)
    assert not erreurs, erreurs
    assert len(appels) == 1, f"liste calculée {len(appels)} fois pour 6 visiteurs à la fois"
    assert len(pages) == 6 and all(
        code == 200 and "CANDIDAT03" in page and "CANDIDAT11" in page
        and "CANDIDAT02" not in page for code, page in pages), "liste des admis fausse"
    print("  6 visiteurs à la fois, cache vide : la liste n'est calculée qu'une fois OK")

    # ---- liste à refaire (plus de 5 minutes) pendant qu'une autre requête la refait : la
    # précédente est servie tout de suite, sans second calcul
    sana = Student.objects.get(class_group=quiz.class_group, last_name="CANDIDAT02")
    Answer.objects.filter(sheet__student=sana).update(detected_choice=0, points_awarded=1.0)
    tenu, liberer = threading.Event(), threading.Event()

    def autre_requete():
        try:
            with transaction.atomic(), connection.cursor() as curseur:
                curseur.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)",
                                [views_public.VERROU_ADMIS, quiz.pk])
                tenu.set()
                liberer.wait(30)
        finally:
            connection.close()

    autre = threading.Thread(target=autre_requete)
    autre.start()
    assert tenu.wait(10)
    try:
        with mock.patch.object(views_public, "ADMIS_FRAICHEUR_S", 0), \
                mock.patch.object(views_public, "_calculer_admis", calcul_lent):
            debut = time.monotonic()
            page = Client().get(url).content.decode()
            assert time.monotonic() - debut < 0.9, "le visiteur a attendu un calcul"
            assert "CANDIDAT03" in page and "CANDIDAT02" not in page and len(appels) == 1, \
                "pendant le calcul d'un autre, la liste précédente devait être servie"
            liberer.set()
            autre.join(10)
            page = Client().get(url).content.decode()
            assert "CANDIDAT02" in page and len(appels) == 2, "liste pas refaite après 5 minutes"
    finally:
        liberer.set()
        autre.join(10)
    print("  liste à refaire : la précédente servie pendant le calcul d'un autre, "
          "puis la nouvelle OK")


def verifier_cache_en_fichiers():
    assert isinstance(caches["default"], cache_qs.CacheFichiers), caches["default"]
    dossier = tempfile.mkdtemp(prefix="qs-cache-test-")
    try:
        fichiers = cache_qs.CacheFichiers(dossier, {"OPTIONS": {"MAX_ENTRIES": 50}})
        lectures = []
        vraie_liste = cache_qs.CacheFichiers._list_cache_files

        def liste_comptee(self):
            lectures.append(1)
            return vraie_liste(self)

        cache_qs._prochain_menage.pop(dossier, None)
        with mock.patch.object(cache_qs.CacheFichiers, "_list_cache_files", liste_comptee):
            for i in range(40):
                fichiers.set(f"adresse-{i}", i, 600)
        assert len(lectures) <= 2, f"dossier relu {len(lectures)} fois pour 40 écritures"
        assert fichiers.get("adresse-7") == 7
        print(f"  cache : 40 écritures, dossier relu {len(lectures)} fois (une par écriture "
              "avant) OK")

        for i in range(10):
            fichiers.set(f"perimee-{i}", i, 1)
        time.sleep(1.2)
        assert fichiers.effacer_perimees() == 10
        assert len(os.listdir(dossier)) == 40 and fichiers.get("adresse-3") == 3
        print("  cache : les entrées périmées sont effacées au ménage, les autres gardées OK")

        for i in range(20):
            fichiers.set(f"autre-{i}", i, 600)
        avant = len(os.listdir(dossier))
        cache_qs._prochain_menage.pop(dossier, None)        # une minute plus tard
        fichiers.set("suivante", 1, 600)
        apres = len(os.listdir(dossier))
        assert avant == 60 and apres < 50, (avant, apres)
        print(f"  cache : au-delà de MAX_ENTRIES (50), le ménage de Django en retire ({avant} -> "
              f"{apres}) OK")
    finally:
        cache_qs._prochain_menage.pop(dossier, None)
        shutil.rmtree(dossier, ignore_errors=True)


def main():
    logging.disable(logging.ERROR)
    _nettoyer()
    try:
        verifier_liste_des_admis()
        verifier_cache_en_fichiers()
    finally:
        cache.clear()
        _nettoyer()
    print("\n✅ TEST DE CHARGE DE L'ESPACE PUBLIC RÉUSSI")


if __name__ == "__main__":
    main()
