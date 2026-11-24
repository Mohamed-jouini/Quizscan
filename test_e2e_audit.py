"""Audit du 24 novembre 2026 (demande de l'utilisateur : « chercher à tout les erreurs et
corrige et améliore la sécurité ») : chaque défaut trouvé, puis corrigé, reste corrigé.

  - nombres : « nan » et « inf » refusés comme note, barème, pénalité ou note minimale (ils
    rendaient toutes les pages de l'épreuve en erreur 500) ; une note minimale au-dessus du
    maximum refusée ; un paramètre d'adresse « ² » ne fait plus d'erreur 500 ;
  - écran de vérification : une note déjà lue, renvoyée telle quelle, n'est pas « saisie » ;
  - fichiers importés : repère de bonne réponse sans temps quadratique, classeur Excel trop
    gros une fois décompressé refusé, intervalle géant de la banque de questions refusé ;
  - registre : importé en entier (le plafond de 5 000 coupait sans rien dire), sans
    doublon quand deux ajouts arrivent en même temps ;
  - lecture des copies : une page entre en base d'un seul tenant ; un lot supprimé
    n'arrête pas la file des suivants ;
  - administration : un corrigé changé recalcule les copies ;
  - tableau de bord : une page scannée deux fois ne compte qu'une fois ;
  - étape suivante d'un concours : chiffres de l'étape d'avant gardés tant que rien ne
    change ; l'année ne touche pas l'étape d'un collègue ;
  - exports Excel : aucune formule active, liste des admis comprise ; coefficients au journal ;
  - sécurité : freins de connexion (100 échecs par adresse, adresse connue, réseau IPv6 /64),
    frein des recherches publiques à fenêtre fixe, envoi anonyme de plus d'1 Mo refusé avant
    lecture, en-têtes de langue sur les pages seulement, page 500 dans la bonne langue.
"""
import io as io_mod
import os
import sys
import threading
import time
import types

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

from django.contrib import admin as admin_mod                   # noqa: E402
from django.contrib.auth import get_user_model                  # noqa: E402
from django.core.cache import cache                             # noqa: E402
from django.db import connection                                # noqa: E402
from django.http import HttpResponse                            # noqa: E402
from django.template.loader import render_to_string             # noqa: E402
from django.test import Client, RequestFactory                  # noqa: E402
from django.urls import reverse                                 # noqa: E402
from django.utils import translation                            # noqa: E402
from openpyxl import Workbook, load_workbook                    # noqa: E402

from grader import importers, inscriptions, securite, services, views_public  # noqa: E402
from grader.admin import QuestionAdmin                          # noqa: E402
from grader.export import results_workbook                      # noqa: E402
from grader.forms import QuestionBankForm                       # noqa: E402
from grader.langue import Langue                                # noqa: E402
from grader.models import (Answer, ClassGroup, Inscrit, Modification,  # noqa: E402
                           Question, Quiz, ScanBatch, SheetScan, Student)
from grader.views import _int_or_none, _my_quizzes, _nombre_fini  # noqa: E402
from test_commun import enseignant_complet                      # noqa: E402

User = get_user_model()
BON = "Bon-Mot-De-Passe-Aud-1"


def _nettoyer():
    for quiz in Quiz.objects.filter(owner__username__startswith="aud_").order_by("-pk"):
        quiz.delete()
    ClassGroup.objects.filter(name__startswith="AUD ").delete()
    Inscrit.objects.filter(numero__startswith="AUD-REG-").delete()
    User.objects.filter(username__startswith="aud_").delete()


def _epreuve(prof):
    """Une QCM (corrigé A) et une réponse manuscrite sur 10, une copie déjà lue."""
    groupe = ClassGroup.objects.create(name="AUD classe", owner=prof)
    quiz = Quiz.objects.create(title="AUD épreuve", class_group=groupe, owner=prof)
    qcm = Question.objects.create(quiz=quiz, order=1, qtype="qcm", correct_choice=0)
    ouverte = Question.objects.create(quiz=quiz, order=2, qtype="open", points=10)
    lue = Question.objects.create(quiz=quiz, order=3, qtype="open", points=10)
    etudiant = Student.objects.create(class_group=groupe, last_name="AMRI", first_name="Sana",
                                      student_number="AUD1")
    lot = ScanBatch.objects.create(quiz=quiz, status="done")
    copie = SheetScan.objects.create(batch=lot, status="ok", student=etudiant, source_name="a.jpg")
    reponses = {
        "qcm": Answer.objects.create(sheet=copie, question=qcm, detected_choice=0,
                                     points_awarded=1.0),
        "ouverte": Answer.objects.create(sheet=copie, question=ouverte, points_awarded=None),
        "lue": Answer.objects.create(sheet=copie, question=lue, points_awarded=6.0),
    }
    return quiz, groupe, lot, copie, etudiant, qcm, ouverte, reponses


def test_nombres(c, quiz, copie, ouverte, reponses):
    assert _int_or_none("²") is None and _int_or_none("12") == 12 and _int_or_none(" -3 ") == -3
    assert _int_or_none("9" * 5000) is None and _int_or_none("--5") is None
    for mauvais in ("nan", "inf", "-inf", "NaN", "1e400"):
        try:
            _nombre_fini(mauvais)
        except ValueError:
            continue
        raise AssertionError(f"« {mauvais} » accepté comme nombre")
    assert _nombre_fini("12,5") == 12.5 and _nombre_fini("١٢٫٥") == 12.5
    assert Client().get(reverse("public_communiques") + "?page=²").status_code == 200
    try:
        importers._points("nan")
    except ValueError:
        pass
    else:
        raise AssertionError("barème « nan » accepté à l'import")

    revue = reverse("sheet_review", args=[copie.pk])
    a = reponses["ouverte"]
    for mauvais in ("nan", "inf"):
        page = c.post(revue, {f"points_{a.pk}": mauvais}, follow=True).content.decode()
        a.refresh_from_db()
        assert "Note invalide" in page and a.points_awarded is None, (mauvais, a.points_awarded)
    c.post(revue, {f"points_{a.pk}": "7,5"})
    a.refresh_from_db()
    assert a.points_awarded == 7.5 and a.manually_set
    traces = Modification.objects.filter(copie=copie, action="note").count()
    lue = reponses["lue"]
    c.post(revue, {f"points_{a.pk}": "7,5", f"points_{lue.pk}": "6"})   # renvoyées telles quelles
    lue.refresh_from_db()
    assert not lue.manually_set, "note lue devenue « saisie » sans changement"
    assert Modification.objects.filter(copie=copie, action="note").count() == traces

    page = c.post(reverse("quiz_detail", args=[quiz.pk]),
                  {"edit_question": ouverte.pk, "points": "nan"}, follow=True).content.decode()
    ouverte.refresh_from_db()
    assert "Barème invalide" in page and ouverte.points == 10
    page = c.post(reverse("quiz_detail", args=[quiz.pk]),
                  {"update_settings": "1", "wrong_penalty": "inf"}, follow=True).content.decode()
    quiz.refresh_from_db()
    assert "invalides" in page and quiz.wrong_penalty == 0, quiz.wrong_penalty
    seuil = reverse("quiz_note_admission", args=[quiz.pk])
    page = c.post(seuil, {"note_admission": "inf"}, follow=True).content.decode()
    quiz.refresh_from_db()
    assert "Note minimale invalide" in page and quiz.note_admission is None
    page = c.post(seuil, {"note_admission": "50"}, follow=True).content.decode()   # sur 21
    quiz.refresh_from_db()
    assert "de 0 à 21" in page and quiz.note_admission is None, page[-600:]
    c.post(seuil, {"note_admission": "5"})
    quiz.refresh_from_db()
    assert quiz.note_admission == 5
    print("  « nan », « inf », « ² » et note minimale hors barème refusés ; note lue renvoyée "
          "telle quelle non « saisie » OK")


def test_imports(quiz):
    corps = "a" + " " * 200_000 + "b"
    debut = time.perf_counter()
    importers._MARK.search(corps)
    assert time.perf_counter() - debut < 1, "repère de bonne réponse : temps quadratique"
    classeur = Workbook()
    classeur.active.append(["n°", "nom", "prénom"])
    tampon = io_mod.BytesIO()
    classeur.save(tampon)
    seuil = importers.MAX_XLSX_DECOMPRESSE
    importers.MAX_XLSX_DECOMPRESSE = 100
    try:
        importers._xlsx_rows(tampon.getvalue())
    except importers.ImportError_ as exc:
        assert "trop volumineux" in str(exc), exc
    else:
        raise AssertionError("classeur trop gros une fois décompressé accepté")
    finally:
        importers.MAX_XLSX_DECOMPRESSE = seuil
    assert list(importers._xlsx_rows(tampon.getvalue()))[0][:3] == ["n°", "nom", "prénom"]
    form = QuestionBankForm(data={"source": quiz.pk, "orders": "1-100000"},
                            quizzes=Quiz.objects.filter(pk=quiz.pk))
    assert not form.is_valid() and "intervalle trop grand" in str(form.errors), form.errors
    print("  imports : repère de bonne réponse linéaire, classeur décompressé borné, "
          "intervalle de la banque borné OK")


def test_registre(c, prof):
    Inscrit.objects.bulk_create([Inscrit(numero=f"AUD-REG-{k:05d}", nom=f"NOM{k}", prenom="P")
                                 for k in range(5005)], batch_size=1000)
    liste = ClassGroup.objects.create(name="AUD registre", owner=prof)
    envoi = {"importer_base": "1", "source": "registre", "filtre": "AUD-REG"}
    c.post(reverse("class_detail", args=[liste.pk]), envoi)
    assert liste.students.count() == 5005, liste.students.count()
    c.post(reverse("class_detail", args=[liste.pk]), envoi)
    assert liste.students.count() == 5005, "registre ajouté deux fois"
    # deux ajouts en même temps (double clic) : chacun attend l'autre, aucun doublon
    course = ClassGroup.objects.create(name="AUD course", owner=prof)
    source = list(liste.students.all()[:300])
    barriere, erreurs = threading.Barrier(2), []

    def ajouter():
        try:
            barriere.wait()
            inscriptions.copier_etudiants(course, source)
        except Exception as exc:  # noqa: BLE001
            erreurs.append(exc)
        finally:
            connection.close()
    fils = [threading.Thread(target=ajouter) for _ in range(2)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert not erreurs and course.students.count() == 300, (erreurs, course.students.count())
    print("  registre de 5 005 inscrits importé en entier ; deux ajouts simultanés sans "
          "doublon OK")


def test_lecture(quiz, groupe, lot):
    eleves = list(groupe.students.all())
    n = len(eleves)
    vraie = services._enregistrer_page_d_un_tenant

    def coupee(sheet, lecture, students, questions, quiz):
        sheet.status = "ok"
        sheet.save()
        students.append(Student(class_group=groupe, last_name="X", first_name="Y"))
        raise KeyboardInterrupt          # le serveur s'arrête entre la fiche et les réponses
    services._enregistrer_page_d_un_tenant = coupee
    try:
        services._enregistrer_page(SheetScan(batch=lot, source_name="coupee.jpg"), None,
                                   eleves, {}, quiz)
    except KeyboardInterrupt:
        pass
    finally:
        services._enregistrer_page_d_un_tenant = vraie
    assert not SheetScan.objects.filter(batch=lot, source_name="coupee.jpg").exists(), \
        "copie « lue » enregistrée sans ses réponses"
    assert len(eleves) == n
    # file de lots : un lot supprimé pendant qu'il attendait n'arrête pas les suivants
    disparu = ScanBatch.objects.create(quiz=quiz, status="processing")
    numero_disparu = disparu.pk
    disparu.delete()
    vide = ScanBatch.objects.create(quiz=quiz, status="processing")
    services._run_in_thread([numero_disparu, vide.pk])
    vide.refresh_from_db()
    assert vide.status == "done", vide.status
    assert vide.pk not in services.lots_verrouilles()
    print("  page enregistrée d'un seul tenant ; file de lots continuée après un lot "
          "supprimé OK")


def test_administration(qcm, reponses):
    patron = User.objects.create_superuser("aud_admin", email="", password="x-pass-123-aud")
    a = reponses["qcm"]
    a.detected_choice, a.points_awarded = 1, 0.0        # B, compté faux (corrigé : A)
    a.save()
    qcm.correct_choice = 1                               # le corrigé passe à B
    requete = RequestFactory().post("/")
    requete.user = patron
    QuestionAdmin(Question, admin_mod.site).save_model(requete, qcm, None, True)
    a.refresh_from_db()
    assert a.points_awarded == 1.0, a.points_awarded
    print("  corrigé changé dans l'administration : copies recalculées OK")


def test_chiffres(prof, groupe, etudiant):
    double = Quiz.objects.create(title="AUD double", class_group=groupe, owner=prof)
    questions = [Question.objects.create(quiz=double, order=k, qtype="qcm", correct_choice=0)
                 for k in (1, 2)]
    lot = ScanBatch.objects.create(quiz=double, status="done")
    for nom in ("x1.jpg", "x2.jpg"):                     # la même page, scannée deux fois
        copie = SheetScan.objects.create(batch=lot, status="ok", student=etudiant,
                                         source_name=nom, page_index=0)
        for q in questions:
            Answer.objects.create(sheet=copie, question=q, detected_choice=0, points_awarded=1.0)
    assert services.compute_results(double)[0]["score"] == 2
    assert services.resume_epreuve(double)["meilleure"] == 2, services.resume_epreuve(double)
    assert services.bilan_admissions(Quiz.objects.filter(pk=double.pk))["moyenne_pct"] == 100
    # étape suivante : les chiffres de l'étape d'avant ne se recalculent que s'ils changent
    ecrit = Quiz.objects.create(title="AUD double — écrit", owner=prof, type_epreuve="ecrit",
                                class_group=ClassGroup.objects.create(name="AUD écrit",
                                                                      owner=prof),
                                epreuve_precedente=double)
    appels, vrai = [], services.admis_de
    services.admis_de = lambda q: appels.append(q.pk) or vrai(q)
    try:
        services.etat_etape(ecrit)
        services.etat_etape(ecrit)
        assert len(appels) == 1, appels
        SheetScan.objects.create(batch=lot, status="no_match", source_name="x3.jpg")
        services.etat_etape(ecrit)
        assert len(appels) == 2, appels
    finally:
        services.admis_de = vrai
    # l'année d'un concours ne touche pas l'étape d'un collègue qu'on ne voit pas
    collegue = enseignant_complet(User.objects.create_user("aud_prof2", password=BON))
    qcm_a = Quiz.objects.create(title="AUD année", owner=prof, annee="1",
                                class_group=ClassGroup.objects.create(name="AUD année A",
                                                                      owner=prof))
    oral_b = Quiz.objects.create(title="AUD année — oral", owner=collegue, annee="1",
                                 type_epreuve="oral", epreuve_precedente=qcm_a,
                                 class_group=ClassGroup.objects.create(name="AUD année B",
                                                                       owner=collegue))
    requete = RequestFactory().get("/")
    requete.user = collegue
    services.changer_annee(oral_b, "2", _my_quizzes(requete))
    qcm_a.refresh_from_db()
    oral_b.refresh_from_db()
    assert (qcm_a.annee, oral_b.annee) == ("1", "2"), (qcm_a.annee, oral_b.annee)
    print("  page scannée deux fois comptée une fois ; chiffres de l'étape d'avant gardés "
          "tant que rien ne change ; année limitée aux étapes visibles OK")


def test_exports(c, prof, quiz, lot, qcm, ouverte):
    piege = Student.objects.create(class_group=quiz.class_group, first_name="+1",
                                   last_name='=HYPERLINK("http://x")', student_number="AUD9")
    copie = SheetScan.objects.create(batch=lot, status="ok", student=piege, source_name="p.jpg")
    Answer.objects.create(sheet=copie, question=qcm, detected_choice=1, points_awarded=1.0)
    Answer.objects.create(sheet=copie, question=ouverte, points_awarded=10.0)
    rows = services.compute_results(quiz)
    admission = services.admissions(quiz, rows)
    assert any(r["student"].pk == piege.pk for r in admission["admis"])
    classeur = load_workbook(io_mod.BytesIO(results_workbook(
        quiz, rows, services.compute_stats(quiz, rows), admission)))
    assert "Admis" in classeur.sheetnames
    valeurs = [cellule.value for feuille in classeur.worksheets for ligne in feuille.iter_rows()
               for cellule in ligne if isinstance(cellule.value, str)]
    assert not [v for v in valeurs if v.startswith("=")], "formule active dans l'export"
    # coefficients de matière : au journal
    ecrit = Quiz.objects.create(title="AUD matières", owner=prof, type_epreuve="ecrit",
                                auto_enroll=True, annee="1",
                                class_group=ClassGroup.objects.create(name="AUD matières",
                                                                      owner=prof))
    services.creer_matieres(ecrit)
    matieres = ecrit.matieres
    envoi = {"coefficients": "1", **{f"coef_m_{m.pk}": f"{m.coefficient:g}" for m in matieres}}
    envoi[f"coef_m_{matieres[0].pk}"] = f"{matieres[0].coefficient + 1:g}"
    c.post(reverse("quiz_parcours", args=[ecrit.pk]), envoi)
    assert Modification.objects.filter(quiz=ecrit, objet__startswith="Coefficient de la matière"
                                       ).count() == 1
    print("  exports sans formule active, liste des admis comprise ; coefficient changé "
          "inscrit au journal OK")


def test_freins():
    url = reverse("login")
    cnx = User.objects.create_user("aud_cnx", password=BON)
    # 1. des échecs depuis une adresse, identifiants inventés : l'adresse est bloquée (seuil
    # abaissé pour le test : chaque essai coûte un hachage)
    cache.clear()
    seuil, securite.ECHECS_PAR_ADRESSE = securite.ECHECS_PAR_ADRESSE, 10
    try:
        pirate = Client()
        for k in range(10):
            pirate.post(url, {"username": f"inconnu{k}", "password": "faux"},
                        REMOTE_ADDR="41.226.2.1")
        r = pirate.post(url, {"username": "aud_cnx", "password": BON}, REMOTE_ADDR="41.226.2.1")
        assert "depuis cette adresse" in r.content.decode() and "_auth_user_id" not in pirate.session
    finally:
        securite.ECHECS_PAR_ADRESSE = seuil
    # 2. le compte fermé par des tiers (20 échecs) reste ouvert depuis le poste habituel
    cache.clear()
    assert Client().post(url, {"username": "aud_cnx", "password": BON},
                         REMOTE_ADDR="41.226.3.1").status_code == 302
    for k in range(4):
        for _ in range(5):
            Client().post(url, {"username": "aud_cnx", "password": "faux"},
                          REMOTE_ADDR=f"41.226.4.{k}")
    r = Client().post(url, {"username": "aud_cnx", "password": BON}, REMOTE_ADDR="41.226.5.1")
    assert "Trop d&#x27;essais" in r.content.decode(), "compte fermé : adresse inconnue admise"
    r = Client().post(url, {"username": "aud_cnx", "password": BON}, REMOTE_ADDR="41.226.3.1")
    assert r.status_code == 302, "poste habituel bloqué par les essais d'un tiers"
    # 3. IPv6 : tout le réseau /64 partage un compteur
    cache.clear()
    for k in range(5):
        Client().post(url, {"username": "aud_cnx", "password": "faux"},
                      REMOTE_ADDR=f"2001:db8:1:2::{k + 1}")
    r = Client().post(url, {"username": "aud_cnx", "password": BON},
                      REMOTE_ADDR="2001:db8:1:2::99")
    assert "Trop d&#x27;essais" in r.content.decode(), "frein contourné en changeant d'adresse IPv6"
    assert securite.cle_d_adresse("2001:db8:1:2::99") == "2001:db8:1:2::/64"
    assert securite.cle_d_adresse("::ffff:41.226.1.1") == "41.226.1.1"
    # 4. recherches publiques : vraie fenêtre de 10 minutes
    cache.clear()
    horloge = [1_000_000.0]
    vraie_horloge = views_public.time
    views_public.time = types.SimpleNamespace(time=lambda: horloge[0])
    try:
        recherche = reverse("public_resultat") + "?numero=AUD0"
        for _ in range(100):
            Client().get(recherche, REMOTE_ADDR="41.226.6.1")
        assert "Trop de recherches" in Client().get(recherche, REMOTE_ADDR="41.226.6.1"
                                                    ).content.decode()
        horloge[0] += 601
        assert "Trop de recherches" not in Client().get(recherche, REMOTE_ADDR="41.226.6.1"
                                                        ).content.decode()
        for _ in range(150):                    # trafic régulier et lent : jamais bloqué
            horloge[0] += 10
            page = Client().get(recherche, REMOTE_ADDR="41.226.6.2").content.decode()
            assert "Trop de recherches" not in page, "fenêtre glissante : trafic lent bloqué"
        for k in range(100):
            Client().get(recherche, REMOTE_ADDR=f"2001:db8:5:6::{k + 1:x}")
        assert "Trop de recherches" in Client().get(recherche, REMOTE_ADDR="2001:db8:5:6::ffff"
                                                    ).content.decode()
    finally:
        views_public.time = vraie_horloge
    cache.clear()
    print("  freins : 100 échecs par adresse, poste habituel jamais bloqué par un tiers, "
          "réseau IPv6 /64, recherches publiques à fenêtre fixe OK")


def test_envois_anonymes(c, quiz):
    anonyme = Client(enforce_csrf_checks=True)
    page = anonyme.post(reverse("quiz_upload", args=[quiz.pk]),
                        {"files": io_mod.BytesIO(b"x" * 1_100_000)})
    assert page.status_code == 403 and "Formulaire expiré" not in page.content.decode(), \
        "envoi anonyme lu jusqu'au contrôle CSRF"
    gros = anonyme.post(reverse("public_resultat"), data="numero=" + "1" * 2_000_000,
                        content_type="application/x-www-form-urlencoded")
    assert gros.status_code == 403
    assert Client().post(reverse("login"), {"username": "aud_cnx", "password": BON},
                         REMOTE_ADDR="41.226.7.1").status_code == 302
    connecte = c.post(reverse("quiz_upload", args=[quiz.pk]),
                      {"files": io_mod.BytesIO(b"x" * 1000)})
    assert connecte.status_code != 403, "envoi d'un enseignant connecté refusé"
    print("  envoi de plus d'1 Mo sans compte refusé avant lecture ; connexion et envois "
          "connectés intacts OK")


def test_langue():
    def reponse(type_contenu):
        requete = RequestFactory().get("/")
        requete.COOKIES = {}
        return Langue(lambda r: HttpResponse("x", content_type=type_contenu))(requete)
    assert "Cookie" not in reponse("text/css").get("Vary", ""), "Vary: Cookie sur un statique"
    assert "Cookie" in reponse("text/html; charset=utf-8")["Vary"]
    with translation.override("ar"):
        page = render_to_string("erreur.html")
    assert 'lang="ar" dir="rtl"' in page, page[:300]
    print("  langue : en-têtes sur les pages seulement ; page 500 en arabe de droite à "
          "gauche OK")


def main():
    logging.disable(logging.ERROR)
    _nettoyer()
    cache.clear()
    print("Audit du 24 novembre 2026 :")
    prof = enseignant_complet(User.objects.create_user("aud_prof", password=BON))
    c = Client()
    c.force_login(prof)
    try:
        quiz, groupe, lot, copie, etudiant, qcm, ouverte, reponses = _epreuve(prof)
        test_nombres(c, quiz, copie, ouverte, reponses)
        test_imports(quiz)
        test_registre(c, prof)
        test_lecture(quiz, groupe, lot)
        test_administration(qcm, reponses)
        test_chiffres(prof, groupe, etudiant)
        test_exports(c, prof, quiz, lot, qcm, ouverte)
        test_freins()
        test_envois_anonymes(c, quiz)
        test_langue()
    finally:
        _nettoyer()
        cache.clear()
    print("\n✅ TEST DE L'AUDIT DU 24 NOVEMBRE 2026 RÉUSSI")


if __name__ == "__main__":
    main()
