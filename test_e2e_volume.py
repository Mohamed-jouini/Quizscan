"""Volume : 10 000 fichiers scannés en un seul envoi, le besoin de l'établissement.

Le gestionnaire de Django garde un fichier ouvert par fichier reçu : l'envoi
plantait en « Too many open files » (erreur 500, rien de reçu). Ce test passe par
le vrai découpage de la requête, avec des fichiers temporaires.
"""
import glob
import io
import os
import shutil
import sys
import tempfile

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
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse
from PIL import Image

from grader import pieces
from grader.envois import FichierRecu
from grader.models import ClassGroup, Communique, Question, Quiz, ScanBatch
from test_commun import enseignant_complet

N = settings.DATA_UPLOAD_MAX_NUMBER_FILES


def _fichiers(octets, n):
    return [SimpleUploadedFile(f"page_{i:05d}.jpg", octets, content_type="image/jpeg")
            for i in range(n)]


def main():
    logging.disable(logging.CRITICAL)
    setup_test_environment()
    assert N >= 10000, "l'établissement scanne 10 000 fichiers par envoi"
    Communique.objects.filter(titre="VOL communiqué").delete()
    Quiz.objects.filter(owner__username="prof_volume").delete()
    ClassGroup.objects.filter(name="VOLUME").delete()
    User.objects.filter(username="prof_volume").delete()
    prof = enseignant_complet(User.objects.create_user("prof_volume", password="x-pass-123"))
    groupe = ClassGroup.objects.create(name="VOLUME", owner=prof)
    # corrigé incomplet : le lot est reçu et mis en attente, sans correction
    quiz = Quiz.objects.create(title="Volume", class_group=groupe, owner=prof,
                               layout_json={"pages": [{"qcm": [], "open": []}]})
    Question.objects.create(quiz=quiz, order=1, qtype="qcm", correct_choice=None)
    c = Client(raise_request_exception=False)
    c.force_login(prof)
    url = reverse("quiz_upload", args=[quiz.pk])

    tampon = io.BytesIO()
    Image.new("L", (40, 40), 200).save(tampon, "JPEG")
    # plus de 5 Mo au total : le chemin des fichiers temporaires, pas la mémoire
    octets = tampon.getvalue() + b"\x00" * max(0, 1500 - len(tampon.getvalue()))
    avant = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.upload")))

    try:
        # ---- la limite elle-même
        r = c.post(url, {"files": _fichiers(octets, N), "label": "plein"})
        assert r.status_code == 302, r.status_code
        lot = ScanBatch.objects.get(quiz=quiz, label="plein")
        assert len(lot.source_files) == N and lot.total_pages == N, len(lot.source_files)
        chemin = os.path.join(settings.MEDIA_ROOT, lot.source_files[-1]["path"])
        with open(chemin, "rb") as f:
            assert f.read() == octets, "fichier abîmé à la réception"
        print(f"  {N} fichiers en un envoi : tous reçus, sans erreur de fichiers ouverts OK")

        r = c.post(url, {"files": _fichiers(octets, N + 1), "label": "trop"})
        assert r.status_code == 400 and not ScanBatch.objects.filter(label="trop").exists()
        page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
        assert f'data-max-fichiers="{N}"' in page
        print("  un fichier de plus : refusé, et la page le dit avant l'envoi OK")

        # ---- pièce jointe de communiqué au-delà de 5 Mo : même chemin
        video = b"\x00\x00\x00\x18ftypmp42" + b"v" * (6 * 1024 * 1024)
        r = c.post(reverse("communiques"), {
            "titre": "VOL communiqué", "texte": "x", "publie": "on",
            "fichiers": [SimpleUploadedFile("film.mp4", video, content_type="video/mp4"),
                         SimpleUploadedFile("affiche.png", _png(), content_type="image/png")]})
        assert r.status_code == 302, r.context["form"].errors
        avis = Communique.objects.get(titre="VOL communiqué")
        assert avis.videos[0].taille == len(video)
        with avis.videos[0].fichier.open("rb") as f:
            assert f.read() == video
        assert pieces.type_de("film.mp4")[0] == pieces.VIDEO
        print("  pièce jointe de 6 Mo : reçue intacte OK")

        # ---- rien ne traîne sur le disque ; lire puis refermer, puis relire
        restes = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.upload"))) - avant
        assert not restes, f"{len(restes)} fichier(s) temporaire(s) restés"
        descripteur, nom = tempfile.mkstemp(suffix=".upload")
        os.write(descripteur, b"contenu")
        os.close(descripteur)
        recu = FichierRecu(nom, "a.jpg", "image/jpeg", 7, None, {})
        assert recu.closed and recu.size == 7
        assert recu.read() == b"contenu" and not recu.closed
        recu.close()
        assert recu.closed and recu.read() == b"contenu"
        assert b"".join(recu.chunks()) == b"contenu"
        recu.effacer()
        assert not os.path.exists(nom), "fichier temporaire non effacé"
        print("  fichiers temporaires effacés, même pour un envoi refusé OK")
    finally:
        for lot in ScanBatch.objects.filter(quiz=quiz):
            shutil.rmtree(os.path.join(settings.MEDIA_ROOT, f"uploads/lot_{lot.pk}"),
                          ignore_errors=True)
        Communique.objects.filter(titre="VOL communiqué").delete()
        Quiz.objects.filter(pk=quiz.pk).delete()
        groupe.delete()
        prof.delete()
    print("\n✅ TEST DU VOLUME (10 000 FICHIERS) RÉUSSI")


def _png():
    tampon = io.BytesIO()
    Image.new("RGB", (8, 8)).save(tampon, "PNG")
    return tampon.getvalue()


if __name__ == "__main__":
    main()
