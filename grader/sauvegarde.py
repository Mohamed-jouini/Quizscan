"""Sauvegarde et restauration complètes de QuizScan.

Ce qui faisait défaut avant ce module (deploy/update.sh copiait seulement
data/db.sqlite3, au moment d'une mise à jour) :

  - **les fichiers** : media/ contient les copies scannées, les images de
    contrôle et les corrigés scannés. Ce sont les pièces qui font foi quand
    un candidat conteste sa note ; elles n'étaient jamais sauvegardées ;
  - **la cohérence** : copier une base SQLite en service avec ``cp`` peut
    produire un fichier corrompu si une correction écrit pendant la copie.
    On passe par l'API de sauvegarde de SQLite, faite pour cela ;
  - **la régularité** : une sauvegarde par nuit, pas seulement lors d'une
    mise à jour (voir deploy/quizscan-sauvegarde.timer) ;
  - **le second support** : une archive restée sur le disque qu'elle protège
    disparaît avec lui (voir deploy/sauvegarde.sh et SAUVEGARDE_COPIE).

Une archive est un fichier ZIP ordinaire, lisible sans QuizScan :

    manifeste.json      date, version, moteur, effectifs
    base.sqlite3        base SQLite (copie cohérente)        — ou —
    base.json           données exportées (PostgreSQL)
    medias/...          arborescence complète de media/

Les commandes ``sauvegarde`` et ``restaurer`` (grader/management/commands)
ne sont que des façades sur ce module.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management import call_command
from django.db import connection, connections
from django.utils import timezone

PREFIXE = "quizscan-"
PREFIXE_FILET = "avant-restauration-"
GARDER_PAR_DEFAUT = 7
# Âge au-delà duquel verifier_deploiement s'inquiète : une nuit manquée
# passe, deux nuits de suite signalent un minuteur arrêté.
AGE_MAXIMAL = timedelta(hours=48)

# Images et PDF sont déjà compressés : les recompresser coûte du temps pour
# un gain nul. Le reste (base, JSON) se compresse très bien.
_DEJA_COMPRESSES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".zip"}


class ArchiveInvalide(Exception):
    """Archive illisible, incomplète ou qui n'est pas une sauvegarde."""


def dossier_par_defaut():
    return Path(settings.DATA_DIR) / "sauvegardes"


def _sqlite():
    return connection.vendor == "sqlite"


def _version():
    """Commit en service, s'il est connu (sans dépendre de git)."""
    tete = Path(settings.BASE_DIR) / ".git" / "HEAD"
    try:
        contenu = tete.read_text().strip()
        if contenu.startswith("ref:"):
            ref = Path(settings.BASE_DIR) / ".git" / contenu[5:]
            contenu = ref.read_text().strip()
        return contenu[:10]
    except OSError:
        return ""


def _effectifs():
    from .models import Answer, Modification, Quiz, SheetScan, Student
    return {"épreuves": Quiz.objects.count(),
            "candidats": Student.objects.count(),
            "copies": SheetScan.objects.count(),
            "réponses": Answer.objects.count(),
            "journal": Modification.objects.count()}


# ------------------------------------------------------------------ création

def creer(dossier=None, *, avec_medias=True, prefixe=PREFIXE):
    """Écrit une archive complète et renvoie son chemin.

    L'archive est d'abord écrite sous un nom provisoire, puis renommée : une
    sauvegarde interrompue (disque plein, coupure) ne peut pas être prise
    pour la dernière sauvegarde valable.
    """
    dossier = Path(dossier or dossier_par_defaut())
    dossier.mkdir(parents=True, exist_ok=True)
    horodatage = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    suffixe = "" if avec_medias else "-base"
    final = dossier / f"{prefixe}{horodatage}{suffixe}.zip"
    n = 2
    while final.exists():           # deux sauvegardes dans la même seconde
        final = dossier / f"{prefixe}{horodatage}-{n}{suffixe}.zip"
        n += 1
    provisoire = final.with_suffix(".zip.partiel")

    medias = Path(settings.MEDIA_ROOT)
    n_fichiers = taille_medias = 0
    try:
        with zipfile.ZipFile(provisoire, "w", allowZip64=True) as zf, \
                tempfile.TemporaryDirectory() as tmp:
            if _sqlite():
                copie = Path(tmp) / "base.sqlite3"
                _copier_sqlite(copie)
                zf.write(copie, "base.sqlite3", zipfile.ZIP_DEFLATED)
            else:
                export = Path(tmp) / "base.json"
                call_command("dumpdata", natural_foreign=True,
                             natural_primary=True, indent=None,
                             exclude=["contenttypes", "auth.permission",
                                      "sessions", "admin.logentry"],
                             output=str(export), verbosity=0)
                zf.write(export, "base.json", zipfile.ZIP_DEFLATED)

            if avec_medias and medias.is_dir():
                for chemin in sorted(medias.rglob("*")):
                    if not chemin.is_file():
                        continue
                    relatif = chemin.relative_to(medias).as_posix()
                    methode = (zipfile.ZIP_STORED
                               if chemin.suffix.lower() in _DEJA_COMPRESSES
                               else zipfile.ZIP_DEFLATED)
                    zf.write(chemin, f"medias/{relatif}", methode)
                    n_fichiers += 1
                    taille_medias += chemin.stat().st_size

            manifeste = {
                "application": "QuizScan",
                "format": 1,
                "date": timezone.localtime().isoformat(timespec="seconds"),
                "version": _version(),
                "moteur": connection.vendor,
                "medias": avec_medias,
                "fichiers_medias": n_fichiers,
                "taille_medias": taille_medias,
                "effectifs": _effectifs(),
            }
            zf.writestr("manifeste.json",
                        json.dumps(manifeste, ensure_ascii=False, indent=2))
        os.replace(provisoire, final)
    except BaseException:
        provisoire.unlink(missing_ok=True)
        raise
    return final


def _copier_sqlite(destination):
    """Copie cohérente d'une base SQLite en service (API de sauvegarde)."""
    connection.ensure_connection()
    cible = sqlite3.connect(str(destination))
    try:
        connection.connection.backup(cible)
    finally:
        cible.close()


def rotation(dossier=None, garder=GARDER_PAR_DEFAUT, prefixe=PREFIXE):
    """Ne garde que les `garder` archives les plus récentes de ce préfixe.

    Les archives « avant-restauration » ont leur propre préfixe et ne sont
    donc jamais effacées par la rotation nocturne : elles se suppriment à la
    main, une fois la restauration validée.
    """
    # Complètes et « base seule » se comptent à part : dix sauvegardes
    # rapides avant mise à jour ne doivent pas chasser la dernière
    # sauvegarde complète, la seule qui contienne les copies scannées.
    effacees = []
    archives = lister(dossier, prefixe)
    for base_seule in (False, True):
        du_genre = [a for a in archives
                    if a.stem.endswith("-base") == base_seule]
        for vieille in du_genre[garder:]:
            vieille.unlink(missing_ok=True)
            effacees.append(vieille)
    return effacees


def lister(dossier=None, prefixe=PREFIXE):
    """Archives terminées, de la plus récente à la plus ancienne."""
    dossier = Path(dossier or dossier_par_defaut())
    if not dossier.is_dir():
        return []
    return sorted(dossier.glob(f"{prefixe}*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def derniere(dossier=None):
    """(chemin, âge) de la sauvegarde complète la plus récente, ou None."""
    for archive in lister(dossier):
        if archive.stem.endswith("-base"):
            continue                 # sauvegarde sans médias : incomplète
        age = datetime.now() - datetime.fromtimestamp(archive.stat().st_mtime)
        return archive, age
    return None


# ------------------------------------------------------------- restauration

def lire_manifeste(archive):
    """Ouvre et contrôle une archive ; renvoie son manifeste.

    Contrôle le CRC de chaque fichier (``testzip``) : une archive abîmée sur
    le disque ou pendant une copie est refusée avant toute écriture.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            abime = zf.testzip()
            if abime:
                raise ArchiveInvalide(f"fichier abîmé dans l'archive : {abime}")
            noms = set(zf.namelist())
            if "manifeste.json" not in noms:
                raise ArchiveInvalide("ce n'est pas une sauvegarde QuizScan "
                                      "(manifeste.json absent)")
            manifeste = json.loads(zf.read("manifeste.json"))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ArchiveInvalide(f"archive illisible : {exc}") from exc
    if manifeste.get("application") != "QuizScan":
        raise ArchiveInvalide("ce n'est pas une sauvegarde QuizScan")
    if not ({"base.sqlite3", "base.json"} & noms):
        raise ArchiveInvalide("la base de données manque dans l'archive")
    return manifeste


def restaurer(archive, *, avec_medias=True, filet=True):
    """Remplace la base (et media/) par le contenu d'une archive.

    Avant toute écriture, une sauvegarde de l'état actuel est faite (le
    « filet ») : une restauration lancée par erreur se défait en restaurant
    cette archive-là. Renvoie (manifeste, chemin du filet ou None).
    """
    archive = Path(archive)
    manifeste = lire_manifeste(archive)
    filet_chemin = (creer(prefixe=PREFIXE_FILET, avec_medias=avec_medias)
                    if filet else None)

    with zipfile.ZipFile(archive) as zf, tempfile.TemporaryDirectory() as tmp:
        noms = zf.namelist()
        if "base.sqlite3" in noms:
            if not _sqlite():
                raise ArchiveInvalide("archive SQLite, serveur sur "
                                      f"{connection.vendor} : restauration "
                                      "impossible telle quelle")
            source = Path(tmp) / "base.sqlite3"
            source.write_bytes(zf.read("base.sqlite3"))
            _restaurer_sqlite(source)
        else:
            export = Path(tmp) / "base.json"
            export.write_bytes(zf.read("base.json"))
            call_command("flush", interactive=False, verbosity=0)
            call_command("loaddata", str(export), verbosity=0)

        if avec_medias and manifeste.get("medias"):
            _restaurer_medias(zf, Path(settings.MEDIA_ROOT))
    return manifeste, filet_chemin


def _restaurer_sqlite(source):
    """Recopie une base SQLite DANS la base en service, par l'API de
    sauvegarde : pas de fichier remplacé sous les pieds d'une connexion
    ouverte (ce que Windows refuse d'ailleurs)."""
    connection.ensure_connection()
    origine = sqlite3.connect(str(source))
    try:
        origine.backup(connection.connection)
    finally:
        origine.close()
    connections.close_all()          # les connexions repartiront de zéro


def _restaurer_medias(zf, racine):
    """Vide media/ puis y extrait les fichiers de l'archive.

    On vide plutôt que d'écraser : un scan ajouté après la sauvegarde ne
    doit pas survivre à côté de données qui ne le référencent plus. (Il est
    dans le filet.) Le contenu est vidé, pas le dossier lui-même, qui est un
    point de montage dans le conteneur.
    """
    racine.mkdir(parents=True, exist_ok=True)
    for enfant in racine.iterdir():
        if enfant.is_dir() and not enfant.is_symlink():
            shutil.rmtree(enfant)
        else:
            enfant.unlink()
    base = racine.resolve()
    for info in zf.infolist():
        if not info.filename.startswith("medias/") or info.is_dir():
            continue
        relatif = PurePosixPath(info.filename).relative_to("medias")
        cible = (racine / Path(*relatif.parts)).resolve()
        # Une archive fabriquée avec « ../ » ne doit rien écrire hors de media/.
        if base not in cible.parents:
            raise ArchiveInvalide(f"chemin refusé dans l'archive : {info.filename}")
        cible.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(cible, "wb") as dst:
            shutil.copyfileobj(src, dst)
