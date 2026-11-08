"""Sécurité : failles trouvées à l'audit du 1er novembre 2026, et leur
correction.

  - /media/ : un « .. » dans le chemin faisait servir le document d'une autre
    épreuve (sujet avant l'examen, copies, corrigé) ;
  - supprimer un étudiant ayant des copies les rendait réaffectables à un
    autre, avec leur note de QCM ;
  - le registre des inscriptions renommait les candidats d'un collègue ;
  - le frein aux recherches publiques par n° se contournait en inventant un
    en-tête X-Forwarded-For ;
  - un compte « lecture seule » créait des classes, modifiait des listes,
    publiait des communiqués sur le site public ;
  - derrière deux Caddy, tous les visiteurs partageaient la même adresse ;
  - des scripts en ligne, que la CSP devait donc autoriser ;
  - renommer un étudiant noté, changer la note minimale ou publier ne
    laissaient aucune trace.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import glob
import logging
import re

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client, override_settings
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader.models import (AnswerKeySheet, ClassGroup, Communique, Inscrit, Quiz,
                           ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet


def _client(compte):
    c = Client()
    c.force_login(compte)
    return c


def main():
    logging.disable(logging.CRITICAL)
    setup_test_environment()
    Quiz.objects.filter(owner__username__startswith="sec_").delete()
    ClassGroup.objects.filter(name__startswith="SEC ").delete()
    Inscrit.objects.filter(numero__in=["990001", "990002"]).delete()
    User.objects.filter(username__startswith="sec_").delete()
    a = enseignant_complet(User.objects.create_user("sec_a", password="x-pass-123"))
    b = enseignant_complet(User.objects.create_user("sec_b", password="x-pass-123"))
    lecteur = User.objects.create_user("sec_lecteur", password="x-pass-123")   # aucun droit
    ca, cb = _client(a), _client(b)

    # ---- /media/ : pas de « .. » pour passer d'un lot permis à un autre document
    ga = ClassGroup.objects.create(name="SEC A", owner=a)
    gb = ClassGroup.objects.create(name="SEC B", owner=b)
    qa = Quiz.objects.create(title="SEC quiz A", class_group=ga, owner=a)
    qb = Quiz.objects.create(title="SEC quiz B", class_group=gb, owner=b)
    sujet_b = default_storage.save(f"sheets/sec_sujet_{qb.pk}.pdf", ContentFile(b"%PDF secret"))
    qb.sheet_pdf.name = sujet_b
    qb.save(update_fields=["sheet_pdf"])
    lot = ScanBatch.objects.create(quiz=qa, status="done")
    scan = default_storage.save(f"uploads/lot_{lot.pk}/abcd1234_scan.pdf", ContentFile(b"%PDF a"))
    lot.source_files = [{"name": "scan.pdf", "path": scan}]
    lot.save(update_fields=["source_files"])
    assert ca.get("/media/" + scan).status_code == 200
    assert ca.get("/media/" + sujet_b).status_code == 404
    for piege in (f"uploads/lot_{lot.pk}/../../{sujet_b}",
                  f"uploads/lot_{lot.pk}/x/../../../{sujet_b}",
                  f"uploads/lot_{lot.pk}/./../../{sujet_b}",
                  f"uploads/lot_{lot.pk}//../../{sujet_b}",
                  f"uploads/lot_{lot.pk}/autre_fichier.pdf"):
        r = ca.get("/media/" + piege)
        assert r.status_code == 404, (piege, r.status_code)
    # le corrigé scanné est visible de ceux qui ont accès à l'épreuve (le
    # bouton « Voir » donnait 404 à tout autre que l'administrateur)
    corrige = AnswerKeySheet(quiz=qa, source_name="corrige.jpg")
    corrige.image.save("sec_corrige.jpg", ContentFile(b"\xff\xd8jpg"), save=True)
    assert ca.get("/media/" + corrige.image.name).status_code == 200
    assert cb.get("/media/" + corrige.image.name).status_code == 404
    print("  /media/ : chemin avec « .. » refusé, fichier hors du lot refusé, corrigé visible OK")

    # ---- supprimer un étudiant qui a des copies : refusé
    note = Student.objects.create(class_group=ga, last_name="NOTE", first_name="A")
    vide = Student.objects.create(class_group=ga, last_name="VIDE", first_name="B")
    SheetScan.objects.create(batch=lot, status="ok", student=note, source_name="p1.jpg")
    r = ca.post(reverse("class_detail", args=[ga.pk]), {"delete_student": note.pk}, follow=True)
    assert Student.objects.filter(pk=note.pk).exists()
    assert "a des copies corrigées" in r.content.decode()
    ca.post(reverse("class_detail", args=[ga.pk]), {"delete_student": vide.pk})
    assert not Student.objects.filter(pk=vide.pk).exists()
    print("  étudiant ayant des copies : suppression refusée (sa note n'ira à personne d'autre) OK")

    # ---- registre : inscrire un n° ne relie pas, ni ne renomme, les candidats d'un collègue
    cand_a = Student.objects.create(class_group=ga, last_name="AMRI", first_name="Sami",
                                    student_number="990001")
    fiche = {"numero": "990001", "nom": "INTRUS", "prenom": "Faux", "cin": "SEC990001",
             "cin_lieu": "Tunis", "cin_date": "2023-02-01", "date_naissance": "2005-01-02",
             "etablissement": "Lycée X", "email": "intrus@exemple.tn", "inscrire": "1"}
    cb.post(reverse("inscriptions"), fiche)
    inscrit = Inscrit.objects.get(numero="990001")
    cand_a.refresh_from_db()
    assert cand_a.inscrit_id is None, "candidat d'un collègue relié au registre"
    cb.post(reverse("inscriptions"), dict(fiche, modifier=inscrit.pk, numero="990002",
                                          nom="RENOMME"))
    cand_a.refresh_from_db()
    assert (cand_a.last_name, cand_a.student_number) == ("AMRI", "990001")
    print("  registre : les candidats d'un collègue ne sont ni reliés ni renommés OK")

    # ---- frein aux recherches publiques : X-Forwarded-For inventé sans effet
    cache.clear()
    anonyme = Client()
    url = reverse("public_resultat") + "?numero=123456"
    for k in range(30):
        anonyme.get(url, REMOTE_ADDR="41.226.1.7", HTTP_X_FORWARDED_FOR=f"10.9.{k}.1")
    r = anonyme.get(url, REMOTE_ADDR="41.226.1.7", HTTP_X_FORWARDED_FOR="10.9.99.1")
    assert "Trop de recherches" in r.content.decode(), "frein contourné par X-Forwarded-For"
    # derrière Caddy (adresse locale) : la dernière valeur, ajoutée par Caddy, fait foi
    cache.clear()
    for k in range(30):
        anonyme.get(url, REMOTE_ADDR="172.17.0.1",
                    HTTP_X_FORWARDED_FOR=f"1.1.{k}.1, 198.51.100.4")
    r = anonyme.get(url, REMOTE_ADDR="172.17.0.1", HTTP_X_FORWARDED_FOR="9.9.9.9, 198.51.100.4")
    assert "Trop de recherches" in r.content.decode()
    r = anonyme.get(url, REMOTE_ADDR="172.17.0.1", HTTP_X_FORWARDED_FOR="198.51.100.5")
    assert "Trop de recherches" not in r.content.decode(), "autre visiteur bloqué"
    cache.clear()
    # nombre de proxies : l'adresse du visiteur, ni celle du proxy ni une inventée
    from django.test import RequestFactory
    from grader.securite import adresse_client
    def vue(xff, distante="172.17.0.1"):
        return adresse_client(RequestFactory().get("/", REMOTE_ADDR=distante,
                                                   HTTP_X_FORWARDED_FOR=xff))
    with override_settings(PROXYS_DE_CONFIANCE=2):
        assert vue("6.6.6.6, 41.226.1.20, 10.0.0.5") == "41.226.1.20"   # 6.6.6.6 inventée
        assert vue("10.0.0.5") == "10.0.0.5"                            # rien de mieux
    with override_settings(PROXYS_DE_CONFIANCE=1):
        assert vue("6.6.6.6, 41.226.1.20") == "41.226.1.20"
        assert vue("pas-une-adresse") == "172.17.0.1"                   # jamais de texte libre
        assert vue("6.6.6.6", distante="41.226.1.30") == "41.226.1.30"  # sans proxy
    with override_settings(PROXYS_DE_CONFIANCE=0):                      # accès direct
        assert vue("6.6.6.6", distante="192.168.1.10") == "192.168.1.10"
    print("  recherches publiques : frein non contournable par X-Forwarded-For OK")

    # ---- compte « lecture seule » : ne crée ni ne publie rien
    cl = _client(lecteur)
    ga.enseignants.add(lecteur)
    refus = [
        cl.post(reverse("class_list"), {"name": "SEC lecteur"}),
        cl.post(reverse("class_detail", args=[ga.pk]), {"import": "1", "text": "NOUVEAU Jean 1"}),
        cl.post(reverse("class_detail", args=[ga.pk]), {"delete_student": cand_a.pk}),
        cl.post(reverse("class_detail", args=[ga.pk]), {"supprimer_choix": "1", "tous": "1"}),
        cl.post(reverse("communiques"), {"titre": "SEC faux", "texte": "x", "publie": "on"}),
        cl.post(reverse("inscriptions"), dict(fiche, numero="990003", cin="SEC990003")),
        cl.post(reverse("quiz_results", args=[qa.pk]), {"masquer_non_identifiees": "1"}),
    ]
    assert [r.status_code for r in refus] == [403] * len(refus), [r.status_code for r in refus]
    assert not ClassGroup.objects.filter(name="SEC lecteur").exists()
    assert not Communique.objects.filter(titre="SEC faux").exists()
    assert Student.objects.filter(pk=cand_a.pk).exists()
    page = cl.get(reverse("class_detail", args=[ga.pk]))
    assert page.status_code == 200                                    # il consulte
    assert 'name="delete_student"' not in page.content.decode()       # sans bouton inutile
    assert 'name="supprimer_choix"' not in page.content.decode()      # ni cases à cocher
    assert 'name="choix"' not in page.content.decode()
    print("  compte lecture seule : classe, liste, registre, communiqué, alerte refusés OK")

    # ---- nom piégé : un simple texte dans un attribut, plus du JavaScript
    Student.objects.create(class_group=ga, last_name="X');alert(1);//", first_name="Y")
    page = ca.get(reverse("class_detail", args=[ga.pk])).content.decode()
    assert 'data-confirmer="Supprimer Y X&#x27;);alert(1);// ?"' in page
    print("  nom d'étudiant piégé : texte dans un attribut, jamais du JavaScript OK")

    # ---- journal : ce qui change à qui une note appartient, ou qui est admis
    from grader.models import Modification
    note.student_number = "990009"
    note.save(update_fields=["student_number"])
    ca.post(reverse("class_detail", args=[ga.pk]), {"import": "1", "text": "AUTRE;Personne;990009"})
    note.refresh_from_db()
    assert note.last_name == "AUTRE"
    entree = Modification.objects.get(action="etudiant", quiz=qa)
    assert "NOTE" in entree.avant and "AUTRE" in entree.apres and entree.auteur_id == a.pk
    # par le registre, et sans entrée pour un étudiant sans copie (aucune note en jeu)
    lot_b = ScanBatch.objects.create(quiz=qb, status="done")
    lie = Student.objects.create(class_group=gb, last_name="RENOMME", first_name="Faux",
                                 student_number="990002", inscrit=inscrit)
    SheetScan.objects.create(batch=lot_b, status="ok", student=lie, source_name="p.jpg")
    cb.post(reverse("inscriptions"), dict(fiche, modifier=inscrit.pk, numero="990002", nom="ENCORE"))
    lie.refresh_from_db()
    assert lie.last_name == "ENCORE"
    assert Modification.objects.filter(action="etudiant", quiz=qb, apres__contains="ENCORE").exists()
    ca.post(reverse("class_detail", args=[ga.pk]), {"import": "1", "text": "SANSCOPIE;Un;990010"})
    ca.post(reverse("class_detail", args=[ga.pk]), {"import": "1", "text": "SANSCOPIE;Deux;990010"})
    assert Modification.objects.filter(action="etudiant", quiz=qa).count() == 1
    ca.post(reverse("quiz_note_admission", args=[qa.pk]), {"note_admission": "10"})
    assert Modification.objects.filter(action="note_admission", quiz=qa, apres="10").exists()
    ca.post(reverse("quiz_publication", args=[qa.pk]), {"resultats_publies": "1"})
    ca.post(reverse("quiz_publication", args=[qa.pk]), {})
    assert list(Modification.objects.filter(action="publication", quiz=qa).order_by("pk")
                .values_list("avant", "apres")) == [("non publié", "résultats"),
                                                    ("résultats", "non publié")]
    print("  journal : identité d'un étudiant noté, note minimale, publication OK")

    # ---- fichiers piégés
    from django.core.files.uploadedfile import SimpleUploadedFile
    from grader import omr, services
    from grader.export import results_workbook
    from grader.forms import UploadForm
    for nom, valide in (("lot.pdf", True), ("page.JPG", True), ("faux.html", False),
                        ("logo.svg", False)):
        f = UploadForm(data={}, files={"files": SimpleUploadedFile(nom, b"x")})
        assert f.is_valid() is valide, (nom, f.errors)
    # fichier d'origine d'un lot : téléchargé, jamais affiché dans le site
    r = ca.get("/media/" + scan)
    assert r["Content-Disposition"] == "attachment"
    assert r["Content-Type"] == "application/octet-stream"
    import pymupdf
    geant = pymupdf.open()
    geant.new_page(width=14400, height=14400)          # 5 m de côté
    with services._open_pages("geant.pdf", geant.tobytes()) as pages:
        try:
            pages[0][1]()
            raise AssertionError("page géante rendue")
        except ValueError as exc:
            assert "démesurée" in str(exc)
    import io as io_mod
    from PIL import Image
    tampon = io_mod.BytesIO()
    Image.new("1", (9000, 9000)).save(tampon, "PNG")    # 81 Mpx, quelques Ko
    with services._open_pages("bombe.png", tampon.getvalue()) as pages:
        try:
            pages[0][1]()
            raise AssertionError("image démesurée décodée")
        except ValueError as exc:
            assert "démesurée" in str(exc)
    # le format se lit dans le contenu : un autre format renommé en .png
    # n'atteint pas les décodeurs d'OpenCV ni de Pillow (FlashPix, EPS…)
    from grader import pieces
    tampon = io_mod.BytesIO()
    Image.new("1", (8, 8)).save(tampon, "XBM")
    with services._open_pages("deguise.png", tampon.getvalue()) as pages:
        try:
            pages[0][1]()
            raise AssertionError("format inattendu décodé")
        except ValueError as exc:
            assert "non reconnu" in str(exc)
    assert "pas une image lisible" in pieces.controler(
        SimpleUploadedFile("deguise.png", tampon.getvalue()))
    tampon = io_mod.BytesIO()
    Image.new("RGB", (8, 8)).save(tampon, "PNG")
    assert pieces.controler(SimpleUploadedFile("vraie.png", tampon.getvalue())) is None
    # corrigé : lu pendant la requête, nombre de pages borné
    doc = pymupdf.open()
    for _ in range(4):
        doc.new_page()
    mise_en_page, qa.layout_json = qa.layout_json, {"pages": [{}]}
    qa.save(update_fields=["layout_json"])
    plafond, services.MAX_PAGES_CORRIGE = services.MAX_PAGES_CORRIGE, 2
    feuilles, _, _ = services.read_answer_key(qa, [SimpleUploadedFile("c.pdf", doc.tobytes()),
                                                   SimpleUploadedFile("d.pdf", doc.tobytes())])
    services.MAX_PAGES_CORRIGE = plafond
    qa.layout_json = mise_en_page
    qa.save(update_fields=["layout_json"])
    assert len(feuilles) == 3 and "limité à 2 pages" in feuilles[-1].error_message
    print("  fichiers : types actifs refusés, original téléchargé, page / image géantes refusées OK")

    # ---- QR fabriqué et formule dans l'export Excel
    class Faux:
        def __init__(self, texte):
            self.texte = texte

        def detectAndDecode(self, img):
            return self.texte, None, None
    assert omr._decode_qr(Faux("QSI|104523|BEN|Ali"), None) == ("QSI", None, "104523")
    assert omr._decode_qr(Faux('QSI|=HYPERLINK("http://x")|A|B'), None) is None
    piege = Student.objects.create(class_group=ga, last_name='=HYPERLINK("http://x")',
                                   first_name="+1", student_number="-2")
    SheetScan.objects.create(batch=lot, status="ok", student=piege, source_name="p2.jpg")
    from openpyxl import load_workbook
    ws = load_workbook(io_mod.BytesIO(results_workbook(qa, services.compute_results(qa)))).active
    valeurs = [c.value for ligne in ws.iter_rows() for c in ligne if isinstance(c.value, str)]
    assert not any(v.startswith("=") for v in valeurs), "formule active dans l'export"
    assert "'=HYPERLINK(\"http://x\")" in valeurs and "-2" in valeurs   # nombre gardé tel quel
    print("  QR fabriqué ignoré ; nom « =… » écrit comme texte dans l'export OK")

    # ---- connexion : essais limités
    cache.clear()
    a.set_password("Bon-Mot-De-Passe-42")
    a.save()
    pirate = Client()
    for _ in range(5):
        r = pirate.post(reverse("login"), {"username": "sec_a", "password": "faux"},
                        REMOTE_ADDR="41.226.1.8")
        assert r.status_code == 200 and "_auth_user_id" not in pirate.session
    r = pirate.post(reverse("login"), {"username": "sec_a", "password": "Bon-Mot-De-Passe-42"},
                    REMOTE_ADDR="41.226.1.8")
    assert "Trop d&#x27;essais" in r.content.decode() and "_auth_user_id" not in pirate.session
    # un envoi sans mot de passe ne vérifie rien : il n'efface pas le compteur
    cache.clear()
    for _ in range(4):
        pirate.post(reverse("login"), {"username": "sec_a", "password": "faux"},
                    REMOTE_ADDR="41.226.1.8")
    pirate.post(reverse("login"), {"username": "sec_a", "password": ""}, REMOTE_ADDR="41.226.1.8")
    pirate.post(reverse("login"), {"username": "sec_a", "password": "faux"},
                REMOTE_ADDR="41.226.1.8")
    r = pirate.post(reverse("login"), {"username": "sec_a", "password": "Bon-Mot-De-Passe-42"},
                    REMOTE_ADDR="41.226.1.8")
    assert "_auth_user_id" not in pirate.session, "compteur effacé par un envoi sans mot de passe"
    # une variante de l'identifiant (pleine chasse, majuscules) : même compteur
    cache.clear()
    for variante in ("sec_a", "ｓec_a", "SEC_A", "ｓｅc_a", "sec_a"):
        pirate.post(reverse("login"), {"username": variante, "password": "faux"},
                    REMOTE_ADDR="41.226.1.8")
    r = pirate.post(reverse("login"), {"username": "ｓec_a", "password": "Bon-Mot-De-Passe-42"},
                    REMOTE_ADDR="41.226.1.8")
    assert "_auth_user_id" not in pirate.session, "frein contourné par une variante de l'identifiant"
    assert "Trop d&#x27;essais" in r.content.decode()
    # l'enseignant, depuis son poste, n'est pas bloqué par le pirate
    vrai = Client()
    r = vrai.post(reverse("login"), {"username": "sec_a", "password": "Bon-Mot-De-Passe-42"},
                  REMOTE_ADDR="41.226.1.9")
    assert r.status_code == 302 and vrai.session.get("_auth_user_id") == str(a.pk)
    # même frein sur la connexion de l'administration
    for _ in range(6):
        r = pirate.post(reverse("admin_login"), {"username": "sec_b", "password": "faux"},
                        REMOTE_ADDR="41.226.1.8")
    assert "Trop d&#x27;essais" in r.content.decode()
    cache.clear()
    # pas de réinitialisation par courriel (aucun envoi configuré)
    assert Client().get("/comptes/password_reset/").status_code == 404
    print("  connexion : 5 essais par compte et par adresse, puis refus 15 min OK")

    # ---- en-têtes et session
    r = Client().get(reverse("public_accueil"))
    csp = r["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
    assert "script-src 'self';" in csp                 # ni script ni « onclick » en ligne
    # aucun script en ligne : ils seraient bloqués, la page ne marcherait plus
    en_ligne = re.compile(r"""<script(?![^>]*\b(?:src=|type="application/json"))[^>]*>"""
                          r"""|\son[a-z]+\s*=\s*["']""", re.I)
    from django.conf import settings
    for gabarit in glob.glob(os.path.join(settings.BASE_DIR, "templates", "**", "*.html"),
                             recursive=True):
        with open(gabarit, encoding="utf-8") as f:
            trouve = en_ligne.search(f.read())
        assert not trouve, (gabarit, trouve and trouve.group(0))
    # …ni dans les pages de l'administration, qui viennent aussi de Django
    admin = User.objects.create_superuser("sec_admin", password="x-pass-123")
    cz = _client(admin)
    for url in ["/admin/", "/admin/password_change/", "/admin/auth/user/", "/admin/auth/user/add/",
                f"/admin/auth/user/{a.pk}/change/", f"/admin/auth/user/{a.pk}/password/",
                "/admin/grader/quiz/", f"/admin/grader/quiz/{qa.pk}/change/",
                "/admin/grader/student/", f"/admin/grader/student/{cand_a.pk}/change/",
                "/admin/grader/classgroup/", "/admin/grader/modification/",
                "/admin/grader/communique/add/", "/admin/grader/inscrit/",
                reverse("quiz_detail", args=[qa.pk]), reverse("quiz_create"),
                reverse("class_detail", args=[ga.pk]), reverse("inscriptions"),
                reverse("communiques"), reverse("quiz_results", args=[qa.pk]),
                reverse("batch_detail", args=[lot.pk]), reverse("password_change")]:
        reponse = cz.get(url)
        assert reponse.status_code == 200, (url, reponse.status_code)
        trouve = en_ligne.search(reponse.content.decode())
        assert not trouve, (url, trouve and trouve.group(0))
    assert "camera=()" in r["Permissions-Policy"]
    assert "no-store" not in r.get("Cache-Control", "")         # page publique, visiteur
    # page d'un enseignant connecté : pas gardée par le navigateur (poste partagé)
    ca.force_login(a)                  # déconnecté par son changement de mot de passe
    assert "no-store" in ca.get(reverse("class_list"))["Cache-Control"]
    # un document n'a pas de CSP : elle gênerait le lecteur de PDF du navigateur
    r = ca.get("/media/" + scan)
    assert "Content-Security-Policy" not in r and "no-store" in r["Cache-Control"]
    # QR, images de contrôle : réutilisables par le navigateur, jamais partagées
    r = ca.get(reverse("student_qr", args=[cand_a.pk]))
    assert r["Cache-Control"] == "private"
    assert settings.SESSION_COOKIE_AGE == 12 * 3600
    # mot de passe : 10 caractères au moins
    from django.contrib.auth import password_validation
    from django.core.exceptions import ValidationError
    try:
        password_validation.validate_password("Xk7-qz-9w")
        raise AssertionError("mot de passe de 9 caractères accepté")
    except ValidationError:
        pass
    password_validation.validate_password("Xk7-qz-9w-L")
    # session : renouvelée, mais pas écrite à chaque requête (les requêtes
    # parallèles d'une même page s'écraseraient)
    assert not settings.SESSION_SAVE_EVERY_REQUEST
    from grader import securite
    ca.get(reverse("class_list"))
    assert ca.session.get("_renouvelee")
    r2 = ca.get(reverse("class_list"))
    assert settings.SESSION_COOKIE_NAME not in r2.cookies, "session réécrite à chaque requête"
    session = ca.session
    session["_renouvelee"] -= securite.SESSION_RENOUVELLEE_S + 1
    session.save()
    r3 = ca.get(reverse("class_list"))
    assert settings.SESSION_COOKIE_NAME in r3.cookies, "session non renouvelée"
    print("  en-têtes : CSP, pages du personnel non conservées ; session de 12 h sans activité OK")

    # ---- sauvegarde : sans les sessions ouvertes
    import tempfile
    import zipfile
    from django.contrib.sessions.models import Session
    from grader import sauvegarde
    assert Session.objects.exists(), "aucune session à exclure : contrôle sans valeur"
    with tempfile.TemporaryDirectory() as dossier:
        archive = sauvegarde.creer(dossier, avec_medias=False)
        with zipfile.ZipFile(archive) as z:
            assert b"sessions.session" not in z.read("base.json"), "sessions dans l'archive"
    # comptes de démonstration : un mot de passe faible est refusé
    from django.core.management import CommandError
    try:
        call_command("comptes_demo", mot_de_passe="1234", verbosity=0)
        raise AssertionError("mot de passe faible accepté")
    except CommandError as exc:
        assert "refusé" in str(exc)
    print("  sauvegarde sans sessions ; mot de passe de démonstration faible refusé OK")

    Communique.objects.filter(titre__startswith="SEC").delete()
    print("\n✅ TEST DE SÉCURITÉ RÉUSSI")


if __name__ == "__main__":
    main()
