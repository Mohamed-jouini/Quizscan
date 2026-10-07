"""Crée (ou réinitialise) les deux comptes de démonstration.

Sert à essayer l'application sur un poste : un compte enseignant et un compte
administrateur. Le mot de passe est tiré au hasard et écrit dans un fichier
du dossier data/ — jamais affiché à l'écran ni envoyé ailleurs, pour qu'il ne
traîne pas dans un historique de terminal. data/ n'est pas versionné.

    python manage.py comptes_demo              # crée / réinitialise
    python manage.py comptes_demo --supprimer  # avant la mise en service

Pour un vrai compte d'administration, utilisez plutôt les commandes de
Django : « createsuperuser » puis « changepassword ».
"""
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

COMPTES = [
    ("demo_prof", False, "Enseignant — accède à ses classes et à ses épreuves"),
    ("demo_admin", True, "Administrateur — accède à /admin/ et voit tout"),
]
FICHIER = "comptes-demo.txt"


class Command(BaseCommand):
    help = ("Crée ou réinitialise les comptes de démonstration et écrit leurs "
            "identifiants dans data/" + FICHIER)

    def add_arguments(self, parser):
        parser.add_argument(
            "--mot-de-passe", dest="mot_de_passe", default=None,
            help="Mot de passe à poser (par défaut : tiré au hasard).")
        parser.add_argument(
            "--supprimer", action="store_true",
            help="Supprime les comptes de démonstration et le fichier.")

    def handle(self, *args, **options):
        User = get_user_model()
        chemin = settings.DATA_DIR / FICHIER

        if options["supprimer"]:
            noms = [n for n, _, _ in COMPTES]
            n_supprimes = User.objects.filter(username__in=noms).delete()[0]
            chemin.unlink(missing_ok=True)
            self.stdout.write(self.style.SUCCESS(
                f"Comptes de démonstration supprimés ({n_supprimes} objet(s)) "
                f"et {chemin} effacé."))
            return

        mot_de_passe = options["mot_de_passe"] or secrets.token_urlsafe(12)
        lignes = ["Comptes de démonstration QuizScan",
                  "Fichier local, jamais versionné. À supprimer avant la mise "
                  "en service :", "    python manage.py comptes_demo --supprimer",
                  ""]
        for nom, admin, role in COMPTES:
            compte, cree = User.objects.get_or_create(username=nom)
            compte.is_staff = compte.is_superuser = admin
            compte.set_password(mot_de_passe)
            compte.save()
            lignes.append(f"{nom:12} {mot_de_passe}    {role}")
            self.stdout.write(
                f"  {nom:12} {'créé' if cree else 'réinitialisé'} — {role}")

        lignes += ["",
                   "Connexion enseignant     : /comptes/login/",
                   "Connexion administration : /comptes/administration/"]
        chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(
            f"\nMot de passe écrit dans : {chemin}\n"
            "(il n'est pas affiché ici pour ne pas rester dans l'historique "
            "du terminal)"))
