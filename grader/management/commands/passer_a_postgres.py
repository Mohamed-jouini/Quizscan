"""Copie la base SQLite dans PostgreSQL, une fois, au moment de changer de base.

    docker compose run --rm quizscan python manage.py passer_a_postgres

L'application doit être arrêtée : ce qui serait écrit pendant la copie serait
perdu. Le fichier SQLite n'est jamais modifié (le travail se fait sur une copie) :
en retirant les réglages POSTGRES_* de .env, on revient à la situation d'avant.

Après la copie, chaque table est recomptée et la somme des notes comparée : au
moindre écart, la commande échoue. Sans ce message de succès, ne basculez pas.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections, models
from django.db.migrations.executor import MigrationExecutor

SOURCE = "source_sqlite"
# Recréés par « migrate » (types, droits), ou sans intérêt : chacun se reconnecte.
EXCLUS = ["contenttypes", "auth.permission", "sessions"]


def _brancher_source(chemin):
    settings.DATABASES[SOURCE] = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(chemin),
                                  "OPTIONS": {"timeout": 30}}
    connections.settings = connections.configure_settings(settings.DATABASES)


def nettoyer_ligne(ligne, signaler):
    """PostgreSQL refuse ce que SQLite laisse passer : un texte plus long que son
    champ, ou contenant un caractère nul. Chaque correction est signalée."""
    objet = json.loads(ligne)
    modele = apps.get_model(objet["model"])
    for champ in modele._meta.concrete_fields:
        valeur = objet["fields"].get(champ.name)
        if not isinstance(valeur, str) or not isinstance(champ, (models.CharField, models.TextField)):
            continue
        propre = valeur.replace("\x00", "")
        if champ.max_length and len(propre) > champ.max_length:
            propre = propre[:champ.max_length]
        if propre != valeur:
            signaler(f"{objet['model']} n° {objet.get('pk', '?')}, {champ.name} : "
                     f"{len(valeur)} caractères -> {len(propre)}")
            objet["fields"][champ.name] = propre
    return json.dumps(objet, ensure_ascii=False)


def _modeles_copies():
    return [m for m in apps.get_models() if not m._meta.proxy
            and m._meta.app_label not in ("contenttypes", "sessions")
            and m._meta.label_lower != "auth.permission"]


def _controler(cible, ecrire):
    """Mêmes effectifs partout, et même somme des notes."""
    ecarts = []
    for modele in _modeles_copies():
        avant = modele.objects.using(SOURCE).count()
        apres = modele.objects.using(cible).count()
        if avant != apres:
            ecarts.append(f"{modele._meta.label} : {avant} dans SQLite, {apres} copiés")
        elif avant:
            ecrire(f"  {modele._meta.label} : {apres}")
    Answer = apps.get_model("grader", "Answer")
    somme = {alias: Answer.objects.using(alias).aggregate(s=models.Sum("points_awarded"))["s"] or 0
             for alias in (SOURCE, cible)}
    if abs(somme[SOURCE] - somme[cible]) > 1e-6:
        ecarts.append(f"Somme des notes : {somme[SOURCE]} dans SQLite, {somme[cible]} copiés")
    else:
        ecrire(f"  somme des notes : {somme[cible]:g}")
    return ecarts


def transferer(source, cible="default", ecraser=False, ecrire=print):
    """Copie `source` (fichier SQLite) dans la base `cible`. Lève CommandError au
    moindre écart ; renvoie la liste des textes corrigés."""
    if connections[cible].vendor == "sqlite" and Path(connections[cible].settings_dict["NAME"]) == Path(source):
        raise CommandError("La base source et la base cible sont le même fichier.")
    corrections = []
    with tempfile.TemporaryDirectory() as dossier:
        copie = Path(dossier) / "source.sqlite3"
        originale = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination = sqlite3.connect(copie)
        originale.backup(destination)
        destination.close()
        originale.close()
        _brancher_source(copie)
        try:
            # Pas de « migrate » sur la source : d'anciennes migrations de données
            # et un signal écrivent toujours dans la base par défaut, c'est-à-dire
            # ici la cible. Les deux doivent seulement être au même niveau.
            executeur = MigrationExecutor(connections[SOURCE])
            if executeur.migration_plan(executeur.loader.graph.leaf_nodes()):
                raise CommandError(
                    "La base SQLite n'est pas à jour. Déployez d'abord cette version sur "
                    "SQLite (sans les réglages POSTGRES_*), puis recommencez.")
            ecrire("Préparation de la base cible…")
            call_command("migrate", database=cible, interactive=False, verbosity=0)
            User = get_user_model()
            if not ecraser and (User.objects.using(cible).exists()
                                or apps.get_model("grader", "Quiz").objects.using(cible).exists()):
                raise CommandError(
                    "La base cible contient déjà des données : rien n'a été copié. "
                    "--ecraser les remplace, définitivement.")
            if ecraser:
                call_command("flush", database=cible, interactive=False, verbosity=0)
            ecrire("Lecture de SQLite…")
            brut, propre = Path(dossier) / "brut.jsonl", Path(dossier) / "donnees.jsonl"
            # stdout et non output= : dumpdata ouvrirait le fichier dans l'encodage du
            # système, pas forcément UTF-8
            with open(brut, "w", encoding="utf-8") as sortie:
                call_command("dumpdata", database=SOURCE, natural_foreign=True,
                             natural_primary=True, exclude=EXCLUS, format="jsonl",
                             stdout=sortie, verbosity=0)
            with open(brut, encoding="utf-8") as lecture, open(propre, "w", encoding="utf-8") as sortie:
                for ligne in lecture:
                    if ligne.strip():
                        sortie.write(nettoyer_ligne(ligne, corrections.append) + "\n")
            ecrire("Écriture dans la base cible…")
            call_command("loaddata", str(propre), database=cible, verbosity=0)
            ecrire("Contrôle :")
            ecarts = _controler(cible, ecrire)
            if ecarts:
                raise CommandError("La copie n'est pas fidèle, ne basculez pas :\n  "
                                   + "\n  ".join(ecarts))
        finally:
            connections[SOURCE].close()
            del connections[SOURCE]
            settings.DATABASES.pop(SOURCE, None)
            connections.settings = connections.configure_settings(settings.DATABASES)
    return corrections


class Command(BaseCommand):
    help = "Copie la base SQLite dans PostgreSQL (voir le README). SQLite n'est pas modifié."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=None,
                            help="Fichier SQLite (par défaut : data/db.sqlite3).")
        parser.add_argument("--ecraser", action="store_true",
                            help="Remplace les données déjà présentes dans PostgreSQL.")

    def handle(self, *args, source=None, ecraser=False, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                "PostgreSQL n'est pas configuré : renseignez POSTGRES_DB, POSTGRES_USER, "
                "POSTGRES_PASSWORD et POSTGRES_HOST dans .env.")
        source = Path(source or settings.DATA_DIR / "db.sqlite3")
        if not source.is_file():
            raise CommandError(f"Fichier SQLite introuvable : {source}")
        corrections = transferer(source, ecraser=ecraser, ecrire=self.stdout.write)
        if corrections:
            self.stdout.write(self.style.WARNING(
                f"{len(corrections)} texte(s) raccourci(s) ou nettoyé(s), à vérifier :"))
            for ligne in corrections[:50]:
                self.stdout.write(f"  {ligne}")
        self.stdout.write(self.style.SUCCESS(
            "Copie fidèle. Relancez l'application : docker compose up -d --build"))
