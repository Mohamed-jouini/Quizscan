"""Sauvegarde complète : base de données et fichiers (copies scannées…).

    python manage.py sauvegarde
    python manage.py sauvegarde --garder 14 --dossier /mnt/nas/quizscan
    docker compose exec -T quizscan python manage.py sauvegarde

Lancée chaque nuit par deploy/quizscan-sauvegarde.timer. Voir
grader/sauvegarde.py pour le contenu d'une archive.
"""
from django.core.management.base import BaseCommand, CommandError

from grader import sauvegarde


def _taille(octets):
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024 or unite == "Go":
            return f"{octets:.0f} {unite}" if unite == "o" else \
                f"{octets:.1f} {unite}".replace(".", ",")
        octets /= 1024


class Command(BaseCommand):
    help = ("Écrit une archive datée de la base et des fichiers "
            "(copies scannées, images de contrôle, corrigés scannés).")

    def add_arguments(self, parser):
        parser.add_argument(
            "--dossier", default=None,
            help="Dossier des archives (par défaut : data/sauvegardes).")
        parser.add_argument(
            "--garder", type=int, default=sauvegarde.GARDER_PAR_DEFAUT,
            help="Nombre d'archives conservées (défaut : %(default)s). "
                 "0 : ne rien effacer.")
        parser.add_argument(
            "--sans-medias", action="store_true",
            help="Base seule, sans les fichiers : rapide, pour une mise à "
                 "jour. Ne remplace pas une sauvegarde complète.")

    def handle(self, *args, dossier=None, garder=7, sans_medias=False, **options):
        if garder < 0:
            raise CommandError("--garder doit être positif ou nul.")
        archive = sauvegarde.creer(dossier, avec_medias=not sans_medias)
        manifeste = sauvegarde.lire_manifeste(archive)   # relecture de contrôle
        effectifs = manifeste["effectifs"]
        self.stdout.write(self.style.SUCCESS(
            f"Sauvegarde écrite : {archive} ({_taille(archive.stat().st_size)})"))
        self.stdout.write(
            f"  {effectifs['épreuves']} épreuve(s), {effectifs['copies']} copie(s), "
            f"{manifeste['fichiers_medias']} fichier(s) scanné(s)"
            + ("" if manifeste["medias"] else " — base seule, sans les fichiers"))
        if garder:
            effacees = sauvegarde.rotation(dossier, garder)
            if effacees:
                self.stdout.write(f"  {len(effacees)} ancienne(s) archive(s) "
                                  f"effacée(s), {garder} conservée(s).")
