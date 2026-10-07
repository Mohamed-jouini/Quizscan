"""Restaure une sauvegarde : base de données et fichiers.

    python manage.py restaurer data/sauvegardes/quizscan-20261007-023000.zip
    python manage.py restaurer <archive> --oui

Sans --oui, la commande décrit l'archive et ce qui serait remplacé, puis
s'arrête sans rien toucher. Avec --oui, elle sauvegarde d'abord l'état
actuel (archive « avant-restauration-… ») puis restaure.

Dans le conteneur, arrêtez d'abord le service web pour qu'aucune correction
n'écrive pendant la restauration :

    docker compose stop quizscan
    docker compose run --rm quizscan python manage.py restaurer <archive> --oui
    docker compose start quizscan
"""
from django.core.management.base import BaseCommand, CommandError

from grader import sauvegarde


class Command(BaseCommand):
    help = "Remplace la base et les fichiers par le contenu d'une sauvegarde."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="Chemin de l'archive .zip")
        parser.add_argument(
            "--oui", action="store_true",
            help="Confirme le remplacement des données actuelles.")
        parser.add_argument(
            "--sans-medias", action="store_true",
            help="Ne restaurer que la base ; laisser media/ tel quel.")
        parser.add_argument(
            "--sans-filet", action="store_true",
            help="Ne pas sauvegarder l'état actuel avant de restaurer "
                 "(déconseillé).")

    def handle(self, *args, archive, oui=False, sans_medias=False,
               sans_filet=False, **options):
        try:
            manifeste = sauvegarde.lire_manifeste(archive)
        except sauvegarde.ArchiveInvalide as exc:
            raise CommandError(f"Archive refusée : {exc}") from exc

        effectifs = manifeste.get("effectifs", {})
        self.stdout.write(f"Archive du {manifeste.get('date', '?')}"
                          + (f", version {manifeste['version']}"
                             if manifeste.get("version") else ""))
        self.stdout.write(
            f"  {effectifs.get('épreuves', '?')} épreuve(s), "
            f"{effectifs.get('copies', '?')} copie(s), "
            f"{manifeste.get('fichiers_medias', 0)} fichier(s) scanné(s)")
        if not manifeste.get("medias"):
            self.stdout.write(self.style.WARNING(
                "  Archive sans les fichiers : seule la base sera restaurée."))

        if not oui:
            raise CommandError(
                "Rien n'a été modifié. La restauration REMPLACE la base"
                + ("" if sans_medias else " et le contenu de media/")
                + " par ceux de l'archive. Relancez avec --oui pour confirmer.")

        try:
            _, filet = sauvegarde.restaurer(archive, avec_medias=not sans_medias,
                                            filet=not sans_filet)
        except sauvegarde.ArchiveInvalide as exc:
            raise CommandError(f"Restauration interrompue : {exc}") from exc

        self.stdout.write(self.style.SUCCESS("Restauration terminée."))
        if filet:
            self.stdout.write(
                f"  L'état d'avant est conservé dans {filet} — restaurez-le "
                "pour annuler, supprimez-le une fois la restauration validée.")
