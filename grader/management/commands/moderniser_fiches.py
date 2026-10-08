"""Met à jour les fiches « grille de n° » générées avant le cadre QR.

Ces fiches portaient des cases NOM/PRÉNOM, remplacées depuis par
l'emplacement de l'étiquette QR. Le PDF enregistré ne change pas tout seul :
il fallait cliquer sur « Régénérer les PDF », ce que rien ne signalait.

Lancée au démarrage du conteneur (docker-entrypoint.sh). Sans risque : une
fiche n'est remplacée que si tout ce que le scan lit reste à la même place
(voir services.moderniser_fiche_grille) — les copies déjà imprimées restent
lisibles. La page du quiz fait la même mise à jour à son ouverture.
"""
from django.core.management.base import BaseCommand

from grader import services
from grader.models import Quiz


class Command(BaseCommand):
    help = ("Remplace les cases NOM/PRÉNOM des fiches « grille de n° » déjà "
            "générées par l'emplacement de l'étiquette QR.")

    def handle(self, *args, **options):
        faites = 0
        for quiz in Quiz.objects.filter(id_mode="grid").exclude(sheet_pdf=""):
            try:
                faites += services.moderniser_fiche_grille(quiz)
            except Exception as exc:  # noqa: BLE001 — une fiche ne bloque pas les autres
                self.stderr.write(f"quiz {quiz.pk} : {type(exc).__name__}: {exc}")
        self.stdout.write(f"{faites} fiche(s) mise(s) à jour.")
