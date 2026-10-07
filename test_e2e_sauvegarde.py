"""Une sauvegarde qu'on n'a jamais restaurée n'est qu'une hypothèse.

On ne vérifie donc pas seulement qu'une archive est écrite : on détruit des
données après l'avoir faite — une épreuve supprimée, un scan effacé, un
fichier parasite ajouté — puis on restaure, et on contrôle que tout est
revenu à l'identique. On vérifie aussi les garde-fous : pas de restauration
sans confirmation, pas d'archive abîmée acceptée, un filet de l'état d'avant,
et une rotation qui ne chasse jamais la dernière sauvegarde complète.

Le test travaille dans un dossier temporaire, avec son propre media/ : le
media_test des autres tests pèse plusieurs centaines de mégaoctets et
n'a rien à faire dans ces archives.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth import get_user_model          # noqa: E402
from django.core.management.base import CommandError    # noqa: E402
from django.test import override_settings               # noqa: E402

from grader import sauvegarde                           # noqa: E402
from grader.models import ClassGroup, Quiz              # noqa: E402


def main():
    logging.disable(logging.ERROR)
    racine = Path(tempfile.mkdtemp(prefix="qs-sauvegarde-"))
    medias, archives = racine / "media", racine / "archives"
    medias.mkdir()
    try:
        with override_settings(MEDIA_ROOT=str(medias), DATA_DIR=racine):
            _scenario(medias, archives)
    finally:
        shutil.rmtree(racine, ignore_errors=True)
    _commandes_importables()
    print("\n✅ TEST DE LA SAUVEGARDE RÉUSSI")


def _commandes_importables():
    """Chaque commande de gestion doit au moins se charger.

    « manage.py check » n'importe pas les commandes : une erreur de syntaxe
    dans verifier_deploiement est passée une fois inaperçue, et ne se
    serait révélée qu'en lançant la commande sur le serveur.
    """
    import importlib
    import pkgutil

    import grader.management.commands as paquet
    noms = [m.name for m in pkgutil.iter_modules(paquet.__path__)]
    for nom in noms:
        importlib.import_module(f"grader.management.commands.{nom}")
    print(f"  commandes      {len(noms)} commande(s) se chargent sans erreur OK")


def _scenario(medias, archives):
    User = get_user_model()
    Quiz.objects.filter(title="SV épreuve témoin").delete()
    ClassGroup.objects.filter(name="SV classe").delete()
    User.objects.filter(username="sv_prof").delete()
    prof = User.objects.create_user("sv_prof", password="x")
    groupe = ClassGroup.objects.create(name="SV classe", owner=prof)
    Quiz.objects.create(title="SV épreuve témoin", class_group=groupe, owner=prof)

    scan = medias / "scans" / "copie-témoin.jpg"
    scan.parent.mkdir(parents=True)
    scan.write_bytes(b"\xff\xd8 contenu d'une copie scannee")

    # ---------------------------------------------------------------- 1
    archive = sauvegarde.creer(archives)
    manifeste = sauvegarde.lire_manifeste(archive)
    with zipfile.ZipFile(archive) as zf:
        noms = set(zf.namelist())
    assert "base.sqlite3" in noms, f"la base manque : {sorted(noms)[:5]}"
    assert "medias/scans/copie-témoin.jpg" in noms, (
        "les copies scannées ne sont pas dans l'archive")
    assert manifeste["fichiers_medias"] == 1 and manifeste["medias"], manifeste
    assert not list(archives.glob("*.partiel")), "archive provisoire oubliée"
    print(f"  écriture       base + {manifeste['fichiers_medias']} fichier, "
          "manifeste relu OK")

    # ---------------------------------------------------------------- 2
    # On abîme l'état actuel.
    Quiz.objects.filter(title="SV épreuve témoin").delete()
    scan.unlink()
    parasite = medias / "ajouté-après.jpg"
    parasite.write_bytes(b"ne devrait pas survivre")

    # Sans --oui : rien ne bouge.
    try:
        call_command("restaurer", str(archive), stdout=open(os.devnull, "w"))
    except CommandError:
        pass
    else:
        raise AssertionError("restauration lancée sans confirmation")
    assert not Quiz.objects.filter(title="SV épreuve témoin").exists(), (
        "la restauration a écrit sans --oui")
    print("  confirmation   rien n'est touché sans --oui OK")

    call_command("restaurer", str(archive), oui=True,
                 stdout=open(os.devnull, "w"))
    assert Quiz.objects.filter(title="SV épreuve témoin").exists(), (
        "l'épreuve supprimée n'est pas revenue")
    assert scan.read_bytes() == b"\xff\xd8 contenu d'une copie scannee", (
        "le scan restauré n'est pas identique")
    assert not parasite.exists(), (
        "un fichier ajouté après la sauvegarde a survécu à la restauration")
    print("  restauration   épreuve, scan à l'octet près, parasite retiré OK")

    # ---------------------------------------------------------------- 3
    filets = sauvegarde.lister(None, sauvegarde.PREFIXE_FILET)
    assert filets, "aucun filet « avant-restauration » n'a été écrit"
    with zipfile.ZipFile(filets[0]) as zf:
        assert "medias/ajouté-après.jpg" in zf.namelist(), (
            "le filet ne contient pas l'état d'avant la restauration")
    print("  filet          l'état d'avant est conservé OK")

    # ---------------------------------------------------------------- 4
    abimee = archives / "quizscan-abimee.zip"
    donnees = bytearray(archive.read_bytes())
    milieu = len(donnees) // 2
    donnees[milieu:milieu + 64] = bytes(64)
    abimee.write_bytes(bytes(donnees))
    try:
        sauvegarde.lire_manifeste(abimee)
    except sauvegarde.ArchiveInvalide:
        pass
    else:
        raise AssertionError("une archive abîmée a été acceptée")
    abimee.unlink()
    print("  intégrité      une archive abîmée est refusée OK")

    # ---------------------------------------------------------------- 5
    # Rotation : les sauvegardes « base seule » ne chassent pas les complètes.
    for _ in range(3):
        sauvegarde.creer(archives)
    for _ in range(4):
        sauvegarde.creer(archives, avec_medias=False)
    sauvegarde.rotation(archives, garder=2)
    restantes = sauvegarde.lister(archives)
    completes = [a for a in restantes if not a.stem.endswith("-base")]
    rapides = [a for a in restantes if a.stem.endswith("-base")]
    assert len(completes) == 2 and len(rapides) == 2, (
        f"{len(completes)} complète(s), {len(rapides)} base(s) seule(s)")
    trouvee = sauvegarde.derniere(archives)
    assert trouvee and not trouvee[0].stem.endswith("-base"), (
        "la « dernière sauvegarde » retenue n'est pas une complète")
    print("  rotation       2 complètes + 2 rapides gardées, séparément OK")

    # ---------------------------------------------------------------- 6
    # La commande elle-même, telle que le minuteur la lance.
    call_command("sauvegarde", dossier=str(archives), garder=2,
                 stdout=open(os.devnull, "w"))
    assert len([a for a in sauvegarde.lister(archives)
                if not a.stem.endswith("-base")]) == 2
    print("  commande       manage.py sauvegarde OK")


if __name__ == "__main__":
    main()
