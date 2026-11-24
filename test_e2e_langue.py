"""Le logiciel en arabe (demande de l'utilisateur, 24 novembre 2026 : « ajout une langue
arabe … pour changer la langue du logiciel arabe ou française », puis « fait tout le
logiciel ») : l'espace public d'abord (son choix), puis tout le reste.

  - français par défaut ; le bouton العربية (cookie de langue, vue set_language) passe tout
    le logiciel en arabe, de droite à gauche ; retour par le bouton Français ;
  - sans choix, l'espace public suit la langue du navigateur ; le reste reste en français ;
  - en arabe : <html lang="ar" dir="rtl"> partout : espace public (étapes, verdicts,
    résultat final, liste des admis et sa pagination), pages du personnel, administration
    (avec le rtl.css de Django), page de connexion ;
  - chaque texte à traduire, des gabarits comme des fichiers Python, a sa traduction arabe
    (celle de QuizScan ou, pour les textes de Django, celle de Django) ;
  - le catalogue compilé (.mo, versionné) suit le .po.
"""
import ast
import io
import os
import re
import sys
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

from django.contrib.auth import get_user_model          # noqa: E402
from django.core.cache import cache                     # noqa: E402
from django.test import Client                          # noqa: E402
from django.urls import reverse                         # noqa: E402
from django.utils import timezone, translation          # noqa: E402
from django.utils.translation.template import templatize  # noqa: E402
from openpyxl import load_workbook                      # noqa: E402

from grader import services, traductions                # noqa: E402
from grader.models import (Answer, ClassGroup, Communique, Question, Quiz,  # noqa: E402
                           ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet              # noqa: E402

User = get_user_model()
RACINE = Path(__file__).resolve().parent
GABARITS = sorted((RACINE / "templates").rglob("*.html"))
LITTERAL = r"""(u?(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'))"""
# templatize : {% translate %} et {% blocktranslate %} donnent gettext(…), _("…") dans les
# arguments d'une balise reste _(…), {% blocktranslate count %} donne ngettext(…, …, count)
APPEL = re.compile(r"(?:\bgettext|(?<![\w.])_)\(" + LITTERAL + r"\)")
PLURIEL = re.compile(r"\bngettext\(" + LITTERAL + r",\s*" + LITTERAL)
# rien à traduire : nom du logiciel, sigle, marqueurs, entités, ponctuation ; exemples
# d'identifiant et nom de fichier, les mêmes dans les deux langues
INVARIABLE = re.compile(r"%\(\w+\)s|%%|&\w+;|QuizScan|QR|[\W\d_]")
EXEMPLES = {"prenom.nom", "admin", "modele_questions.xlsx"}


def a_traduire(msgid):
    return msgid not in EXEMPLES and bool(INVARIABLE.sub("", msgid))


def textes_python():
    """(fichier, contexte, texte) de chaque gettext / gettext_lazy / pgettext_lazy à texte fixe
    du code de l'application."""
    for chemin in sorted((RACINE / "grader").rglob("*.py")):
        if "migrations" in chemin.parts:
            continue
        for noeud in ast.walk(ast.parse(chemin.read_text(encoding="utf-8"))):
            if not isinstance(noeud, ast.Call):
                continue
            nom = getattr(noeud.func, "id", None) or getattr(noeud.func, "attr", None)
            fixes = [a.value for a in noeud.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if nom in ("gettext", "gettext_lazy") and len(noeud.args) == len(fixes) == 1:
                yield chemin.name, None, fixes[0]
            elif nom in ("pgettext", "pgettext_lazy") and len(noeud.args) == len(fixes) == 2:
                yield chemin.name, fixes[0], fixes[1]


def _nettoyer():
    for quiz in Quiz.objects.filter(owner__username__startswith="lg_").order_by("-pk"):
        quiz.delete()
    ClassGroup.objects.filter(owner__username__startswith="lg_").delete()
    User.objects.filter(username__startswith="lg_").delete()
    Communique.objects.filter(titre__startswith="LG ").delete()


def _etape(prof, titre, type_epreuve, precedente, numeros, seuil):
    groupe = ClassGroup.objects.create(name=f"LG {titre}", owner=prof)
    quiz = Quiz.objects.create(title=f"LG concours{' — ' + titre if precedente else ''}",
                               class_group=groupe, owner=prof, auto_enroll=True, annee="1",
                               type_epreuve=type_epreuve, epreuve_precedente=precedente,
                               note_admission=seuil, resultats_publies=True,
                               admis_publies=True, publie_le=timezone.now())
    etudiants = [Student.objects.create(class_group=groupe, last_name=f"NOM{n}", first_name="P",
                                        student_number=str(n)) for n in numeros]
    return quiz, etudiants


def _concours(prof):
    """QCM d'une question (105 admis : la liste a deux pages), puis écrit et oral pour 7001,
    admis partout."""
    qcm, etudiants = _etape(prof, "QCM", "qcm", None, range(7001, 7106), 1)
    question = Question.objects.create(quiz=qcm, order=1, qtype="qcm", correct_choice=0)
    lot = ScanBatch.objects.create(quiz=qcm, status="done", total_pages=len(etudiants))
    for s in etudiants:
        copie = SheetScan.objects.create(batch=lot, status="ok", student=s, source_name="q.jpg")
        Answer.objects.create(sheet=copie, question=question, detected_choice=0,
                              points_awarded=1.0)
    precedente = qcm
    for titre, type_epreuve, note in (("écrit", "ecrit", 14), ("oral", "oral", 12)):
        quiz, (candidat,) = _etape(prof, titre, type_epreuve, precedente, [7001], 10)
        lot = ScanBatch.objects.create(quiz=quiz, status="done", total_pages=1)
        copie = SheetScan.objects.create(batch=lot, status="ok", student=candidat,
                                         source_name="e.jpg")
        Answer.objects.create(sheet=copie, question=services.question_note(quiz),
                              points_awarded=note, manually_set=True)
        precedente = quiz
    return qcm


def verifier_catalogue():
    for po, mo in traductions.catalogues():
        assert mo.exists() and mo.read_bytes() == traductions.compiler_po(po), \
            f"{mo} ne suit plus {po} : python manage.py traductions"
        # un fragment ajouté à un message (msg += gettext(" …")) garde son espace de tête
        collees = [msgid for _, msgid, _, msgstrs in traductions.lire_po(
                       po.read_text(encoding="utf-8"))
                   if msgid and msgid.startswith(" ") and any(msgstrs)
                   and not all(s.startswith(" ") for s in msgstrs)]
        assert not collees, f"traduction sans l'espace de tête : {collees}"
    manquants, n_gabarits, n_python = [], 0, 0
    with translation.override("ar"):
        for gabarit in GABARITS:
            source = templatize(gabarit.read_text(encoding="utf-8"))
            nom = gabarit.relative_to(RACINE / "templates").as_posix()
            for litteral in APPEL.findall(source):
                msgid = ast.literal_eval(litteral)
                n_gabarits += 1
                if translation.gettext(msgid) == msgid and a_traduire(msgid):
                    manquants.append(f"{nom} : {msgid}")
            for un, plusieurs in PLURIEL.findall(source):
                un, plusieurs = ast.literal_eval(un), ast.literal_eval(plusieurs)
                n_gabarits += 1
                if translation.ngettext(un, plusieurs, 1) == un:
                    manquants.append(f"{nom} : {un} / {plusieurs}")
        for fichier, contexte, msgid in textes_python():
            n_python += 1
            traduit = (translation.pgettext(contexte, msgid) if contexte
                       else translation.gettext(msgid))
            if traduit == msgid and a_traduire(msgid):
                manquants.append(f"{fichier} : {msgid}")
        # textes choisis à l'exécution : gettext(variable)
        from grader.templatetags.qs_langue import MOTS_OUI_NON
        for msgid in ("QCM", "Écrit", "Oral", "%(n)s image", "%(n)s images", "%(n)s vidéo",
                      "%(n)s vidéos", "%(n)s document", "%(n)s documents", *MOTS_OUI_NON):
            if translation.gettext(msgid) == msgid:
                manquants.append(f"(choisi à l'exécution) : {msgid}")
        assert translation.gettext("%(n)s images") % {"n": 3} == "صور: 3"
    assert not manquants, "sans traduction arabe :\n" + "\n".join(manquants)
    assert n_gabarits > 1000 and n_python > 500, (n_gabarits, n_python)   # le relevé marche
    print(f"  catalogue : .mo à jour, {n_gabarits} textes des gabarits et {n_python} du code, "
          "tous traduits en arabe OK")


def main():
    logging.disable(logging.ERROR)
    _nettoyer()
    cache.clear()
    print("Le logiciel en arabe :")
    verifier_catalogue()
    prof = enseignant_complet(User.objects.create_user("lg_prof", password="x-pass-123"))
    qcm = _concours(prof)
    Communique.objects.create(titre="LG Calendrier des épreuves", texte="Le 2 décembre.",
                              publie=True, auteur=prof)
    try:
        # ---- français par défaut
        c = Client()
        page = c.get(reverse("public_accueil")).content.decode()
        assert '<html lang="fr" dir="ltr">' in page and "Résultats et communiqués" in page
        assert 'value="ar" lang="ar"' in page and "العربية" in page, "bouton العربية absent"
        assert "النتائج" not in page
        print("  français par défaut, bouton العربية dans la barre OK")

        # ---- le bouton : arabe, de droite à gauche, gardé dans le cookie de langue
        r = c.post(reverse("set_language"), {"language": "ar", "next": reverse("public_accueil")})
        assert r.status_code == 302 and r["Location"] == reverse("public_accueil"), r.status_code
        assert c.cookies["django_language"].value == "ar"
        reponse = c.get(reverse("public_accueil"))
        page = reponse.content.decode()
        assert '<html lang="ar" dir="rtl">' in page, page[:300]
        for texte in ("النتائج والبلاغات", "آخر البلاغات", "النتائج المنشورة", "اختيار من متعدد",
                      "عرض نتائجي", "مناظرة · نُشرت في", 'value="fr" lang="fr"'):
            assert texte in page, texte
        for francais in ("Résultats et communiqués", "Derniers communiqués", "Voir mes résultats",
                         "publié le"):
            assert francais not in page, francais
        assert reponse["Content-Language"] == "ar"
        assert "Accept-Language" in reponse["Vary"] and "Cookie" in reponse["Vary"]
        print("  bouton العربية : accueil en arabe, de droite à gauche, choix gardé OK")

        page = c.post(reverse("public_resultat"), {"numero": "7001"}).content.decode()
        for texte in ("نتائجي", "رقم التسجيل 7001", "عدد المراحل: 3", "اختيار من متعدد", "كتابي",
                      "شفاهي", '<p class="pub-verdict pub-verdict--admis">مقبول</p>',
                      "النتيجة النهائية للمناظرة", "مقبول في المناظرة"):
            assert texte in page, texte
        assert "Admis" not in page and "Mes résultats" not in page
        page = c.post(reverse("public_resultat"), {"numero": "999999"}).content.decode()
        assert "لا توجد أيّ نتيجة منشورة للرقم <b>999999</b>" in page
        print("  « Mes résultats » en arabe : étapes, verdicts, résultat final du concours OK")

        page = c.get(reverse("public_epreuve", args=[qcm.pk])).content.decode()
        for texte in ("قائمة المقبولين، حسب رقم التسجيل", "عدد المقبولين: 105", "<th>رقم التسجيل</th>",
                      "<th>الاسم</th>", "<th>اللقب</th>", "<th>النتيجة</th>",
                      "الأسطر 1 إلى 100 من 105 — الصفحة 1 / 2", "التالية ◀", "الأخيرة ⏮"):
            assert texte in page, texte
        page2 = c.get(reverse("public_epreuve", args=[qcm.pk]) + "?page=2").content.decode()
        assert "▶ السابقة" in page2 and "⏭ الأولى" in page2
        page = c.get(reverse("public_communiques")).content.decode()
        assert "LG Calendrier des épreuves" in page and "<h1>البلاغات</h1>" in page
        print("  liste des admis (pagination comprise) et communiqués en arabe OK")

        # ---- tout le logiciel suit le choix : pages du personnel en arabe (24 novembre 2026)
        page_connexion = c.get(reverse("login")).content.decode()
        assert '<html lang="ar" dir="rtl">' in page_connexion and "تسجيل الدخول" in page_connexion
        assert 'value="fr" lang="fr"' in page_connexion and "Connexion" not in page_connexion
        c.force_login(prof)
        reponse = c.get(reverse("dashboard"))
        page = reponse.content.decode()
        assert '<html lang="ar" dir="rtl">' in page and reponse["Content-Language"] == "ar"
        for texte in ("لوحة القيادة", "الأقسام", "المناظرات", "النتائج", "تسجيل الخروج",
                      'value="fr" lang="fr"'):
            assert texte in page, texte
        for francais in ("Tableau de bord", "Déconnexion", "Nouveau quiz"):
            assert francais not in page, francais
        page = c.get(reverse("quiz_results", args=[qcm.pk])).content.decode()
        assert "مقبول" in page and "Note minimale" not in page and "Exporter" not in page
        page = c.get(reverse("quiz_create") + "?concours=1").content.decode()
        assert '<html lang="ar" dir="rtl">' in page and "مناظرة جديدة" in page
        with translation.override("fr"):
            assert qcm.type_court == "QCM"
            assert services._nom_liste_concours("LG X") == "Concours — LG X"
        with translation.override("ar"):
            assert qcm.type_court == "اختيار من متعدد"
            assert services._nom_liste_concours("LG X") == "مناظرة — LG X"
            assert services._nom_liste_concours("مناظرة LG") == "مناظرة LG"
        # les documents restent en français, même demandés depuis l'interface en arabe
        page = c.get(reverse("quiz_parcours", args=[qcm.pk])).content.decode()
        assert "مقبول في المناظرة" in page, "page du parcours non traduite"
        r = c.get(reverse("quiz_parcours", args=[qcm.pk]) + "?format=xlsx")
        assert r.status_code == 200, r.status_code
        cellules = [str(v) for ligne in load_workbook(io.BytesIO(r.content)).active
                    .iter_rows(values_only=True) for v in ligne if v is not None]
        assert "Admis au concours" in cellules, cellules[:40]
        assert not [v for v in cellules if re.search("[؀-ۿ]", v)], \
            "document mi-français, mi-arabe"
        r = c.get(reverse("quiz_parcours", args=[qcm.pk]) + "?format=pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF"), r.status_code
        c.logout()
        print("  avec le choix de l'arabe : connexion et pages du personnel en arabe, "
              "de droite à gauche ; documents (Excel, PDF) toujours en français OK")

        admin = User.objects.create_superuser("lg_admin", email="", password="x-pass-123")
        # logout() du client de test efface tous ses cookies ; un vrai navigateur, lui, garde
        # le cookie de langue après la déconnexion
        c.post(reverse("set_language"), {"language": "ar", "next": "/"})
        c.force_login(admin)
        page = c.get(reverse("admin:index")).content.decode()
        assert '<html lang="ar" dir="rtl">' in page and "admin/css/rtl.css" in page
        assert 'value="fr" lang="fr"' in page and "Recent actions" not in page
        page = c.get(reverse("admin:grader_student_changelist")).content.decode()
        assert 'dir="rtl"' in page and "اللقب" in page, "colonne « nom » non traduite"
        c.logout()
        print("  administration en arabe, avec la feuille de droite à gauche de Django OK")

        # ---- sans choix, le personnel reste en français, même avec un navigateur arabe
        navigateur_arabe = Client(HTTP_ACCEPT_LANGUAGE="ar-TN,ar;q=0.9,fr;q=0.5")
        navigateur_arabe.force_login(prof)
        reponse = navigateur_arabe.get(reverse("dashboard"))
        assert reponse["Content-Language"] == "fr"
        page = reponse.content.decode()
        assert '<html lang="fr" dir="ltr">' in page and "Tableau de bord" in page
        assert 'value="ar" lang="ar"' in page, "bouton العربية absent des pages du personnel"
        print("  sans choix : pages du personnel en français, bouton العربية dans la barre OK")

        # ---- retour au français par le bouton ; le navigateur choisit sans bouton
        c.post(reverse("set_language"), {"language": "fr", "next": reverse("public_resultat")})
        page = c.get(reverse("public_resultat")).content.decode()
        assert '<html lang="fr" dir="ltr">' in page and "Mes résultats" in page
        arabe = Client(HTTP_ACCEPT_LANGUAGE="ar-TN,ar;q=0.9,fr;q=0.5")
        assert '<html lang="ar" dir="rtl">' in arabe.get(reverse("public_accueil")).content.decode()
        francais = Client(HTTP_ACCEPT_LANGUAGE="fr-FR,fr;q=0.9")
        assert '<html lang="fr" dir="ltr">' in francais.get(reverse("public_accueil")).content.decode()
        r = arabe.post(reverse("set_language"), {"language": "fr",
                                                  "next": reverse("public_accueil")})
        assert '<html lang="fr"' in arabe.get(reverse("public_accueil")).content.decode(), \
            "le bouton doit l'emporter sur le navigateur"
        r = Client().post(reverse("set_language"), {"language": "ar",
                                                    "next": "https://ailleurs.example/"})
        assert r.status_code == 302 and "ailleurs" not in r["Location"], "redirection ouverte"
        print("  bouton Français ; sans choix, la langue du navigateur ; le choix l'emporte ; "
              "pas de redirection vers un autre site OK")
    finally:
        _nettoyer()
        cache.clear()
    print("\n✅ TEST DU LOGICIEL EN ARABE RÉUSSI")


if __name__ == "__main__":
    main()
