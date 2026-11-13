"""Année d'un concours (demande de l'utilisateur, 13 novembre 2026) : à sa création, on
choisit السنة الأولى ou السنة الثانية ; le code d'anonymat des copies à talon, au QCM comme
à l'écrit, commence alors toujours par X (1re année) ou par Y (2e année).

Vérifie : le choix obligatoire à la création d'un concours, absent d'une épreuve de classe ;
les codes en X sur le talon et sous le QR ; l'étape suivante reprend l'année ; l'année
changée vaut pour tout le concours, les copies sont à régénérer et leurs codes changent de
lettre ; un formulaire sans le champ la garde ; sans année, jamais X ni Y en tête.
"""
import os
import sys

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

import pymupdf
from django.contrib.auth.models import User
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import qr as qr_mod
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet

ETUDIANTS = [("AMRI", "Sana", "604501"), ("BAKIR", "Omar", "604502"),
             ("CHERIF", "Lina", "604503")]
CONCOURS = {"type_epreuve": "qcm", "language": "fr", "sheet_mode": "grid",
            "id_mode": "sticker", "id_digits": "6", "anonymat": "talon",
            "grading_mode": "immediate", "wrong_penalty": "0"}


def codes(quiz):
    quiz.refresh_from_db()
    return list((quiz.layout_json or {}).get("codes", {}).values())


def inscrire(quiz, etudiants):
    for nom, prenom, num in etudiants:
        Student.objects.create(class_group=quiz.class_group, last_name=nom, first_name=prenom,
                               student_number=num)


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_annee").delete()
    ClassGroup.objects.filter(owner__username="prof_annee").delete()
    User.objects.filter(username="prof_annee").delete()
    prof = enseignant_complet(User.objects.create_user("prof_annee", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    creer = reverse("quiz_create") + "?concours=1"
    print("Année d'un concours :")

    # ---- création : l'année, à choisir pour un concours, absente d'une épreuve de classe
    page = c.get(creer).content.decode()
    assert 'name="annee"' in page and "السنة الأولى" in page and "السنة الثانية" in page
    r = c.post(creer, dict(CONCOURS, title="Concours 1A"))
    assert r.status_code == 200 and "Choisissez l&#x27;année du concours." in r.content.decode()
    r = c.post(creer, dict(CONCOURS, title="Concours 1A", annee="3"))
    assert r.status_code == 200 and "annee" in r.context["form"].errors
    assert not Quiz.objects.filter(title="Concours 1A").exists()
    r = c.post(creer, dict(CONCOURS, title="Concours 1A", annee="1"))
    assert r.status_code == 302, r.context["form"].errors
    qcm = Quiz.objects.get(title="Concours 1A")
    assert qcm.annee == "1" and qcm.auto_enroll and qcm.talon_nom
    assert 'name="annee"' not in c.get(reverse("quiz_create")).content.decode()
    print("  création : année obligatoire pour un concours (السنة الأولى ou السنة الثانية), "
          "absente d'une épreuve de classe OK")

    # ---- QCM de 1re année : chaque code commence par X, sur le talon et sous le QR
    Question.objects.create(quiz=qcm, order=1, qtype="qcm", correct_choice=0)
    inscrire(qcm, ETUDIANTS)
    c.post(reverse("quiz_detail", args=[qcm.pk]), {"generate_pdf": "1"})
    premiers = codes(qcm)
    assert len(premiers) == 3 and all(code[0] == "X" for code in premiers), premiers
    amri = qcm.class_group.students.get(last_name="AMRI")
    lisible = qr_mod.code_lisible(qcm.layout_json["codes"][str(amri.pk)])
    doc = pymupdf.open(stream=open(qcm.sheet_pdf.path, "rb").read(), filetype="pdf")
    assert "Sana AMRI" in doc[0].get_text() and len(doc[0].search_for(lisible)) == 2, lisible
    page = c.get(reverse("quiz_detail", args=[qcm.pk])).content.decode()
    assert '<span class="badge grey">السنة الأولى</span>' in page
    assert "السنة الأولى" in c.get(reverse("concours_list")).content.decode()
    print(f"  QCM de 1re année : codes en X ({lisible} sur le talon et sous le QR), année "
          "affichée sur la page et dans la liste des concours OK")

    # ---- l'écrit, étape suivante, reprend l'année du QCM ; ses codes aussi en X
    qcm.note_admission = 1
    qcm.save(update_fields=["note_admission"])
    suite = reverse("quiz_create") + f"?concours=1&precedente={qcm.pk}&type=ecrit"
    assert c.get(suite).context["form"].initial["annee"] == "1"
    r = c.post(suite, dict(CONCOURS, title="Concours 1A — écrit", type_epreuve="ecrit",
                           note_sur="20", epreuve_precedente=qcm.pk, annee="1"))
    assert r.status_code == 302, r.context["form"].errors
    ecrit = Quiz.objects.get(title="Concours 1A — écrit")
    assert ecrit.annee == "1" and ecrit.epreuve_precedente_id == qcm.pk and ecrit.talon_nom
    inscrire(ecrit, ETUDIANTS[:2])
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"generer": "1"})
    assert len(codes(ecrit)) == 2 and all(code[0] == "X" for code in codes(ecrit))
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert 'Concours · <bdi lang="ar">السنة الأولى</bdi>' in page
    assert 'name="annee"' in page, "réglages de l'écrit d'un concours : l'année"
    print("  écrit, étape suivante : année reprise du QCM, codes en X OK")

    # ---- l'année changée sur l'écrit vaut pour tout le concours ; copies à régénérer,
    # leurs codes passent en Y
    reglages = {"reglages": "1", "title": ecrit.title, "note_sur": "20", "id_mode": "sticker",
                "id_digits": "6", "anonymat": "talon", "pages_redaction": "1",
                "format_impression": "a4"}
    r = c.post(reverse("quiz_detail", args=[ecrit.pk]), dict(reglages, annee="2"))
    assert r.status_code == 302, r.context["form"].errors
    qcm.refresh_from_db()
    ecrit.refresh_from_db()
    assert (qcm.annee, ecrit.annee) == ("2", "2"), (qcm.annee, ecrit.annee)
    assert "régénérez les documents" in c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert "Régénérez la fiche" in c.get(reverse("quiz_detail", args=[qcm.pk])).content.decode()
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"generer": "1"})
    c.post(reverse("quiz_detail", args=[qcm.pk]), {"generate_pdf": "1"})
    assert all(code[0] == "Y" for code in codes(ecrit) + codes(qcm)), codes(qcm)
    assert len(codes(qcm)) == 3 and "régénérez les documents" not in \
        c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    # un formulaire sans le champ (d'avant) garde l'année
    r = c.post(reverse("quiz_detail", args=[ecrit.pk]), reglages)
    assert r.status_code == 302
    ecrit.refresh_from_db()
    assert ecrit.annee == "2"
    print("  année changée sur l'écrit : le QCM suit, les deux à régénérer, codes en Y ; "
          "formulaire sans le champ : année gardée OK")

    # ---- et depuis les réglages du QCM
    reglages_qcm = {"update_settings": "1", "title": qcm.title, "sheet_mode": "grid",
                    "id_mode": "qr", "id_digits": "6", "wrong_penalty": "0", "anonymat": "talon"}
    c.post(reverse("quiz_detail", args=[qcm.pk]), dict(reglages_qcm, annee="1"))
    qcm.refresh_from_db()
    ecrit.refresh_from_db()
    assert (qcm.annee, ecrit.annee) == ("1", "1"), (qcm.annee, ecrit.annee)
    c.post(reverse("quiz_detail", args=[qcm.pk]), reglages_qcm)
    qcm.refresh_from_db()
    assert qcm.annee == "1", "un formulaire sans le champ garde l'année"
    print("  année changée sur le QCM : l'écrit suit ; formulaire sans le champ : gardée OK")

    # ---- sans année (concours d'avant ce choix, épreuve de classe) : jamais X ni Y en tête,
    # pour qu'aucun code ne fasse croire à une année
    ancien = Quiz.objects.create(title="Ancien concours", owner=prof, auto_enroll=True,
                                 type_epreuve="ecrit", id_mode="qr", talon_nom=True,
                                 class_group=ClassGroup.objects.create(
                                     name="Ancien concours (annee)", owner=prof))
    inscrire(ancien, ETUDIANTS)
    c.post(reverse("quiz_detail", args=[ancien.pk]), {"generer": "1"})
    assert len(codes(ancien)) == 3 and not any(code[0] in "XY" for code in codes(ancien))
    tires = [qr_mod.nouveau_code(set()) for _ in range(2000)]
    assert {code[0] for code in tires} == set(qr_mod.ALPHABET_CODE) - {"X", "Y"}
    assert all(len(code) == qr_mod.LONGUEUR_CODE and set(code) <= set(qr_mod.ALPHABET_CODE)
               for code in tires)
    assert {qr_mod.nouveau_code(set(), "Y")[0] for _ in range(200)} == {"Y"}
    # réglages de cet ancien concours : sans choix, il reste sans année
    r = c.post(reverse("quiz_detail", args=[ancien.pk]), dict(reglages, title=ancien.title))
    assert r.status_code == 302
    ancien.refresh_from_db()
    assert ancien.annee == ""
    print("  sans année : jamais X ni Y en tête (toutes les autres lettres possibles) ; un "
          "ancien concours le reste tant qu'on ne choisit pas OK")

    print("\n✅ TEST DE L'ANNÉE DES CONCOURS RÉUSSI")


if __name__ == "__main__":
    main()
