"""Pièces jointes des communiqués : images, vidéos et documents publiés sur
l'espace public avec le communiqué.
"""
import io
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

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse
from PIL import Image

from grader import pieces
from grader.models import Communique, PieceJointe
from test_commun import enseignant_complet


def png(nom="affiche.png"):
    buf = io.BytesIO()
    Image.new("RGB", (64, 40), (30, 120, 60)).save(buf, "PNG")
    return SimpleUploadedFile(nom, buf.getvalue(), content_type="image/png")


VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40     # 10 252 octets


def fichiers():
    return [png(),
            SimpleUploadedFile("présentation.mp4", VIDEO, content_type="video/mp4"),
            SimpleUploadedFile("Convocation salle B12.pdf", b"%PDF-1.4\n%%EOF\n",
                               content_type="application/pdf"),
            SimpleUploadedFile("liste.docx", b"PK\x03\x04docx", content_type="application/octet-stream")]


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Communique.objects.filter(titre__startswith="PJ ").delete()
    User.objects.filter(username__in=["prof_pj", "prof_pj2"]).delete()
    prof = enseignant_complet(User.objects.create_user("prof_pj", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    anonyme = Client()

    # ---- types refusés : HTML / SVG (script sur le site), exécutable, fausse image
    for mauvais, motif in ((SimpleUploadedFile("page.html", b"<script>alert(1)</script>"), "non accepté"),
                           (SimpleUploadedFile("logo.svg", b"<svg onload='x'/>"), "non accepté"),
                           (SimpleUploadedFile("outil.exe", b"MZ"), "non accepté"),
                           (SimpleUploadedFile("photo.jpg", b"pas une image"), "pas une image lisible")):
        r = c.post(reverse("communiques"), {"titre": "PJ refusé", "texte": "x", "publie": "on",
                                            "fichiers": [mauvais]})
        assert r.status_code == 200 and motif in r.content.decode(), mauvais.name
    assert not Communique.objects.filter(titre="PJ refusé").exists()
    # trop lourde : la limite par genre s'applique
    ancienne = pieces.TAILLE_MAX[pieces.IMAGE]
    pieces.TAILLE_MAX[pieces.IMAGE] = 100
    r = c.post(reverse("communiques"), {"titre": "PJ lourd", "texte": "x", "fichiers": [png()]})
    pieces.TAILLE_MAX[pieces.IMAGE] = ancienne
    assert "au-delà des" in r.content.decode()
    print("  refusés : HTML, SVG, exécutable, fausse image, fichier trop lourd OK")

    # ---- un communiqué avec une image, une vidéo et deux documents
    r = c.post(reverse("communiques"), {"titre": "PJ Convocation", "publie": "on",
                                        "texte": "Salle B12. Plan : https://exemple.tn/plan",
                                        "fichiers": fichiers()})
    assert r.status_code == 302, r.context["form"].errors
    avis = Communique.objects.get(titre="PJ Convocation")
    assert sorted(p.genre for p in avis.pieces.all()) == ["document", "document", "image", "video"]
    assert avis.resume_pieces == "1 image · 1 vidéo · 2 documents"
    page = c.get(reverse("communiques")).content.decode()
    assert 'enctype="multipart/form-data"' in page and "présentation.mp4" in page

    page = anonyme.get(reverse("public_communique", args=[avis.pk])).content.decode()
    image, video = avis.images[0], avis.videos[0]
    assert f'<img src="{image.url}"' in page
    assert f'<video controls preload="metadata" playsinline src="{video.url}">' in page
    assert "Convocation salle B12.pdf" in page and "Documents à télécharger" in page
    assert '<a href="https://exemple.tn/plan"' in page            # lien cliquable
    accueil = anonyme.get(reverse("public_accueil")).content.decode()
    assert "1 image · 1 vidéo · 2 documents" in accueil and 'class="pub-avis__image"' in accueil
    print("  publié : image affichée, vidéo lue dans la page, documents à télécharger OK")

    # ---- envoi des fichiers au public
    r = anonyme.get(image.url)
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    assert r["Content-Disposition"].startswith("inline") and r["X-Content-Type-Options"] == "nosniff"
    pdf = next(p for p in avis.documents if p.nom.endswith(".pdf"))
    assert anonyme.get(pdf.url)["Content-Disposition"].startswith("inline")
    docx = next(p for p in avis.documents if p.nom.endswith(".docx"))
    r = anonyme.get(docx.url)
    assert r["Content-Disposition"].startswith("attachment")
    assert r["Content-Type"].startswith("application/vnd.openxmlformats")   # pas celui du navigateur
    # vidéo : lecture par morceaux (Safari, avance rapide)
    r = anonyme.get(video.url, HTTP_RANGE="bytes=0-99")
    assert r.status_code == 206 and r["Content-Range"] == f"bytes 0-99/{len(VIDEO)}"
    assert b"".join(r.streaming_content) == VIDEO[:100]
    r = anonyme.get(video.url, HTTP_RANGE="bytes=-52")
    assert r.status_code == 206 and b"".join(r.streaming_content) == VIDEO[-52:]
    r = anonyme.get(video.url, HTTP_RANGE=f"bytes={len(VIDEO)}-")
    assert r.status_code == 416
    r = anonyme.get(video.url)
    assert r.status_code == 200 and r["Accept-Ranges"] == "bytes"
    assert b"".join(r.streaming_content) == VIDEO
    # jamais par /media/ (réservé au personnel, par épreuve)
    assert anonyme.get("/media/" + image.fichier.name).status_code == 302
    autre = enseignant_complet(User.objects.create_user("prof_pj2", password="x-pass-123"))
    c2 = Client()
    c2.force_login(autre)
    assert c2.get("/media/" + image.fichier.name).status_code == 404
    print("  fichiers servis : image et PDF affichés, Word téléchargé, vidéo par morceaux OK")

    # ---- brouillon : fichiers cachés au public, visibles du personnel
    avis.publie = False
    avis.save()
    assert anonyme.get(image.url).status_code == 404
    assert c.get(image.url).status_code == 200
    avis.publie = True
    avis.save()
    print("  brouillon : pièces jointes cachées au public OK")

    # ---- modifier : retirer une pièce (fichier effacé), en ajouter une
    page = c.get(reverse("communiques") + f"?modifier={avis.pk}").content.decode()
    assert f'name="retirer" value="{docx.pk}"' in page
    nom_docx = docx.fichier.name
    r = c.post(reverse("communiques"), {"modifier": avis.pk, "titre": avis.titre,
                                        "texte": avis.texte, "publie": "on",
                                        "retirer": [docx.pk], "fichiers": [png("plan.png")]})
    assert r.status_code == 302
    assert sorted(p.nom for p in avis.pieces.all()) == [
        "Convocation salle B12.pdf", "affiche.png", "plan.png", "présentation.mp4"]
    assert not default_storage.exists(nom_docx), "fichier retiré resté sur le disque"
    # au plus MAX_PAR_COMMUNIQUE pièces jointes
    r = c.post(reverse("communiques"), {"modifier": avis.pk, "titre": avis.titre,
                                        "texte": avis.texte, "publie": "on",
                                        "fichiers": [png(f"p{i}.png") for i in range(7)]})
    assert r.status_code == 200 and "pièces jointes au plus" in r.content.decode()
    print("  modification : pièce retirée (fichier effacé), nouvelle ajoutée, plafond OK")

    # ---- supprimer le communiqué : ses fichiers partent avec lui
    noms = [p.fichier.name for p in avis.pieces.all()]
    c.post(reverse("communiques"), {"supprimer": avis.pk})
    assert not PieceJointe.objects.filter(communique_id=avis.pk).exists()
    assert not any(default_storage.exists(n) for n in noms), "fichiers restés sur le disque"
    print("  suppression du communiqué : fichiers effacés OK")
    print("\n✅ TEST DES PIÈCES JOINTES DES COMMUNIQUÉS RÉUSSI")


if __name__ == "__main__":
    main()
