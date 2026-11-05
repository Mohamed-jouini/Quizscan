"""Nombre de lots en cours de lecture, de ceux qui avancent encore.

La mise à jour automatique (deploy/update.sh) attend qu'il soit nul avant de
redémarrer le serveur : un redémarrage arrête la lecture d'un lot, qui ne
reprend qu'à la main, cinq minutes plus tard (« Relancer »). À 10 000 copies,
la lecture dure des heures ; chaque envoi sur GitHub l'interrompait.
Un lot resté « en cours » sans progresser (services.is_stalled) ne compte pas :
il est déjà arrêté, attendre ne servirait à rien.

N'écrit que le nombre, pour être lu par un script.
"""
from django.core.management.base import BaseCommand

from grader import services
from grader.models import ScanBatch


class Command(BaseCommand):
    help = "Nombre de lots en cours de lecture (ceux qui avancent encore)."

    def handle(self, *args, **options):
        lots = ScanBatch.objects.filter(status="processing")
        self.stdout.write(str(sum(1 for lot in lots if not services.is_stalled(lot))))
