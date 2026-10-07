"""Toutes les pages sont-elles en français ?

On parcourt chaque page de l'application et de l'administration — listes,
formulaires d'ajout et de modification, historiques, confirmations de
suppression — puis les pages d'erreur (404, 403, formulaire expiré, 500), et
on refuse le moindre mot anglais dans le texte visible : contenu, titres,
infobulles, textes alternatifs, libellés de boutons.

Ce test existe parce que l'anglais revenait par des chemins qu'on ne voit
pas en relisant le code : un texte écrit en dur dans un gabarit de Django
(la loupe « Search »), des noms de permission créés en anglais à la
migration (« Can add log entry »), un modèle sans nom lisible (« Answer
object (5099) »), les pages d'erreur par défaut (« Not Found »).
"""
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
from html import unescape

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib import admin                         # noqa: E402
from django.contrib.auth import get_user_model           # noqa: E402
from django.contrib.auth.models import Permission        # noqa: E402
from django.template.loader import render_to_string      # noqa: E402
from django.test import Client, override_settings        # noqa: E402
from django.urls import reverse                          # noqa: E402

from grader.models import (Answer, ClassGroup, Modification,  # noqa: E402
                           Question, Quiz, ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet               # noqa: E402

# Mots anglais sans homographe français : leur présence est une faute.
# (« change », « page », « action », « note »… existent en français.)
ANGLAIS = re.compile(
    r"\b(the|and|you|your|this|that|with|from|search|delete|save|add|home|"
    r"log ?out|log ?in|select|choose|remove|available|chosen|today|yes|"
    r"history|welcome|password|username|forbidden|not found|server error|"
    r"show counts|clear all|please|cancel|submit|objects?|"
    r"can (add|change|delete|view)|requested|resource|go back)\b", re.I)

# Tournures de la traduction de Django, françaises mais fautives.
MALADROIT = re.compile(r"Sélectionnez l[’']objet|Modification de [A-Za-zÉé]|"
                       r"Ajout de [A-Za-zÉé]|\bobject \(\d+\)")


def visible(html):
    """Texte que l'utilisateur voit ou entend (lecteur d'écran compris)."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    attributs = re.findall(r'(?:title|placeholder|aria-label|alt)="([^"]*)"', html)
    boutons = re.findall(r'<input[^>]*type="(?:submit|button|reset)"[^>]*'
                         r'value="([^"]*)"', html)
    texte = re.sub(r"<[^>]+>", " ", html)
    # « l&#x27;administrateur » se lit « l'administrateur » à l'écran.
    return unescape(re.sub(r"\s+", " ", " ".join([texte, *attributs, *boutons])))


fautes = []


def controler(client, url, *, attendu=200, etiquette=""):
    reponse = client.get(url)
    if reponse.status_code != attendu:
        fautes.append(f"{url} -> {reponse.status_code} (attendu {attendu})")
        return ""
    texte = visible(reponse.content.decode("utf-8", "replace"))
    _verifier(texte, etiquette or url)
    return texte


def _verifier(texte, ou):
    for motif, quoi in ((ANGLAIS, "anglais"), (MALADROIT, "tournure fautive")):
        for m in motif.finditer(texte):
            i = m.start()
            fautes.append(f"{ou} — {quoi} « {m.group(0)} » : "
                          f"…{texte[max(i - 40, 0):i + 50]}…")


def _donnees():
    """Un exemplaire de chaque objet, pour que chaque fiche ait de quoi s'ouvrir."""
    User = get_user_model()
    Quiz.objects.filter(class_group__name="FR classe").delete()
    ClassGroup.objects.filter(name="FR classe").delete()
    User.objects.filter(username__startswith="fr_").delete()
    prof = enseignant_complet(User.objects.create_user("fr_prof", password="x"))
    sans_droit = User.objects.create_user("fr_lecteur", password="x")
    patron = User.objects.create_superuser("fr_admin", email="", password="x")
    groupe = ClassGroup.objects.create(name="FR classe", owner=prof)
    etudiant = Student.objects.create(class_group=groupe, last_name="DUPONT",
                                      first_name="Amel", student_number="7")
    quiz = Quiz.objects.create(title="FR épreuve", class_group=groupe, owner=prof)
    question = Question.objects.create(quiz=quiz, order=1, qtype="qcm",
                                       points=1, num_choices=4, correct_choice=0)
    lot = ScanBatch.objects.create(quiz=quiz, label="FR lot", status="done",
                                   total_pages=1)
    copie = SheetScan.objects.create(batch=lot, source_name="fr.jpg",
                                     status="ok", student=etudiant)
    Answer.objects.create(sheet=copie, question=question, detected_choice=0,
                          points_awarded=1.0)
    Modification.objects.create(auteur=prof, auteur_nom="fr_prof", action="note",
                                quiz=quiz, quiz_titre=quiz.title, copie=copie,
                                objet="fr.jpg — question 1", avant="0", apres="1")
    return prof, sans_droit, patron, quiz, lot, copie, groupe


def main():
    logging.disable(logging.CRITICAL)
    prof, sans_droit, patron, quiz, lot, copie, groupe = _donnees()

    # ---------------------------------------------------------------- 1
    enseignant = Client()
    enseignant.force_login(prof)
    pages = ["/", "/classes/", "/concours/", "/quiz/nouveau/",
             f"/classes/{groupe.pk}/", f"/quiz/{quiz.pk}/",
             f"/quiz/{quiz.pk}/resultats/", f"/lots/{lot.pk}/",
             f"/lots/{lot.pk}/supprimer/", f"/copies/{copie.pk}/",
             "/comptes/password_change/"]
    for url in pages:
        controler(enseignant, url)
    print(f"  application    {len(pages)} pages de l'enseignant")

    # ---------------------------------------------------------------- 2
    gerant = Client()
    gerant.force_login(patron)
    vues = ["/admin/", "/admin/password_change/"]
    for modele, ecran in admin.site._registry.items():
        meta = modele._meta
        base = f"admin:{meta.app_label}_{meta.model_name}"
        vues.append(reverse(f"{base}_changelist"))
        vues.append(reverse(f"{base}_changelist") + "?_popup=1")
        objet = modele.objects.order_by("-pk").first()
        requete = type("Requete", (), {"user": patron})()
        if ecran.has_add_permission(requete):
            vues.append(reverse(f"{base}_add"))
        if objet is not None:
            vues.append(reverse(f"{base}_change", args=[objet.pk]))
            vues.append(reverse(f"{base}_history", args=[objet.pk]))
            if ecran.has_delete_permission(requete, objet):
                vues.append(reverse(f"{base}_delete", args=[objet.pk]))
    for url in vues:
        controler(gerant, url)
    print(f"  administration {len(vues)} pages : listes, fiches, historiques, "
          "suppressions")

    # Majuscule en milieu de phrase : « 4 Pages scannées », « Par Enseignant ».
    # Django insère le nom des tables et des champs dans ses phrases ; écrits
    # avec une majuscule, ils la gardaient au milieu de la phrase.
    for modele in admin.site._registry:
        meta = modele._meta
        url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
        page = gerant.get(url).content.decode()
        decompte = re.search(r'<p class="paginator">(.*?)</p>', page, re.S)
        morceaux = [visible(decompte.group(1))] if decompte else []
        morceaux += [visible(t) for t in re.findall(
            r"<h3>(.*?)</h3>", page.split('id="changelist-filter"')[-1], re.S)]
        for morceau in morceaux:
            m = re.search(r"\b(\d+|Par) ([A-ZÀ-Ý][a-zà-ÿ]+)", morceau)
            if m and m.group(2) not in ("QCM", "OCR", "QR"):
                fautes.append(f"{url} — majuscule en milieu de phrase : "
                              f"« {morceau.strip()[:60]} »")
    print("  typographie    décomptes et filtres sans majuscule fautive")

    # ---------------------------------------------------------------- 3
    anonyme = Client()
    for url in ("/comptes/login/", "/comptes/administration/"):
        controler(anonyme, url)

    with override_settings(DEBUG=False):
        texte = controler(enseignant, "/cette-page-n-existe-pas/", attendu=404,
                          etiquette="page 404")
        assert "Page introuvable" in texte, "la 404 n'est pas la nôtre"

    lecteur = Client()
    lecteur.force_login(sans_droit)
    texte = controler(lecteur, "/quiz/nouveau/", attendu=403, etiquette="page 403")
    assert "Demandez-la à l'administrateur" in texte, (
        "la 403 n'affiche pas le message qui dit quoi faire")

    strict = Client(enforce_csrf_checks=True)
    reponse = strict.post("/comptes/login/", {"username": "x", "password": "y"})
    assert reponse.status_code == 403
    texte = visible(reponse.content.decode())
    _verifier(texte, "refus CSRF")
    assert "Formulaire expiré" in texte, "le refus CSRF n'est pas le nôtre"

    texte = visible(render_to_string("500.html"))
    _verifier(texte, "page 500")
    assert "Une erreur est survenue" in texte
    print("  erreurs        404, 403, formulaire expiré, 500")

    # ---------------------------------------------------------------- 4
    anglaises = list(Permission.objects.filter(name__startswith="Can ")
                     .values_list("name", flat=True)[:5])
    if anglaises:
        fautes.append(f"permissions restées en anglais : {anglaises}")
    print("  permissions    noms en français")

    if fautes:
        print("\n".join(f"  ✗ {f}" for f in fautes[:30]))
        raise AssertionError(f"{len(fautes)} défaut(s) de langue")
    print("\n✅ TEST DE LA LANGUE FRANÇAISE RÉUSSI")


if __name__ == "__main__":
    main()
