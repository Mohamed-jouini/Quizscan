"""Compile les catalogues de traduction (.po -> .mo), sans GNU gettext (grader/traductions.py).

    python manage.py traductions              # après chaque modification d'un .po
    python manage.py traductions --verifier   # code d'erreur si un .mo ne suit plus son .po
"""
from django.core.management.base import BaseCommand, CommandError

from grader import traductions


class Command(BaseCommand):
    help = "Compile grader/locale/*/LC_MESSAGES/django.po en django.mo (sans msgfmt)."

    def add_arguments(self, parser):
        parser.add_argument("--verifier", action="store_true",
                            help="ne rien écrire : échouer si un .mo est à refaire")

    def handle(self, *args, verifier=False, **options):
        en_retard = []
        for po, mo in traductions.catalogues():
            contenu = traductions.compiler_po(po)
            if mo.exists() and mo.read_bytes() == contenu:
                self.stdout.write(f"{mo.relative_to(traductions.DOSSIER)} : à jour")
                continue
            if verifier:
                en_retard.append(str(mo.relative_to(traductions.DOSSIER)))
                continue
            mo.write_bytes(contenu)
            self.stdout.write(self.style.SUCCESS(f"{mo.relative_to(traductions.DOSSIER)} : compilé"))
        if en_retard:
            raise CommandError("À recompiler (python manage.py traductions) : "
                               + ", ".join(en_retard))
