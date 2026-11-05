"""Relance la lecture des lots arrêtés en route : serveur redémarré, coupure de
courant, processus arrêté. Avant, un tel lot restait « interrompu » jusqu'à ce
qu'un enseignant clique sur « Relancer », au moins cinq minutes plus tard.

Un lot est arrêté s'il est « en cours » sans que personne ne le lise : aucun
processus ne tient son verrou dans PostgreSQL (services.verrou_du_lot), et il
n'a pas avancé depuis une minute. Les pages déjà lues ne sont pas relues.

--veiller : recommence toutes les deux minutes, sans fin. C'est ainsi que le
conteneur le lance à son démarrage (docker-entrypoint.sh) : la lecture reprend
d'elle-même après un redémarrage, et après l'arrêt d'un processus du site.
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection

from grader import services


class Command(BaseCommand):
    help = "Relance la lecture des lots arrêtés en route (--veiller : sans fin)."

    def add_arguments(self, parser):
        parser.add_argument("--veiller", action="store_true",
                            help="recommencer toutes les deux minutes, sans fin")
        parser.add_argument("--pause", type=int, default=120,
                            help="secondes entre deux vérifications (avec --veiller)")

    def handle(self, *args, veiller=False, pause=120, **options):
        while True:
            try:
                for lot in services.lots_a_reprendre():
                    self.stdout.write(f"Reprise du lot {lot.pk} : {lot.processed_pages} "
                                      f"page(s) déjà lue(s) sur {lot.total_pages}")
                    lot = services.run_batch(lot.pk)
                    self.stdout.write(f"Lot {lot.pk} : {lot.get_status_display()}")
            except Exception as exc:  # noqa: BLE001 — la veille ne s'arrête jamais
                self.stderr.write(f"Reprise des lots : {type(exc).__name__}: {exc}")
            if not veiller:
                return
            connection.close()          # pas de connexion gardée entre deux tours
            time.sleep(max(pause, 10))
