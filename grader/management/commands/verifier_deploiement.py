"""Contrôle la configuration d'un serveur avant ou après mise en service.

    python manage.py verifier_deploiement
    docker compose exec quizscan python manage.py verifier_deploiement

Chaque point est signalé OK, ATTENTION (ça marche mais c'est risqué) ou
ERREUR (ça ne marchera pas). La commande sort avec un code non nul s'il
reste une erreur, pour pouvoir l'enchaîner dans un script d'installation.
"""
import os
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

OK, ATTENTION, ERREUR = "OK", "ATTENTION", "ERREUR"


class Command(BaseCommand):
    help = "Vérifie la configuration du serveur (réglages, OCR, base, fichiers)"

    def handle(self, *args, **options):
        resultats = []

        def note(niveau, titre, detail=""):
            resultats.append((niveau, titre, detail))

        self._reglages(note)
        self._acces(note)
        self._ocr(note)
        self._base(note)
        self._fichiers(note)

        styles = {OK: self.style.SUCCESS, ATTENTION: self.style.WARNING,
                  ERREUR: self.style.ERROR}
        for niveau, titre, detail in resultats:
            self.stdout.write(f"{styles[niveau](f'[{niveau:9}]')} {titre}")
            for ligne in filter(None, detail.split("\n")):
                self.stdout.write(f"             {ligne}")

        erreurs = sum(1 for n, _, _ in resultats if n == ERREUR)
        alertes = sum(1 for n, _, _ in resultats if n == ATTENTION)
        self.stdout.write("")
        if erreurs:
            self.stdout.write(self.style.ERROR(
                f"{erreurs} erreur(s) et {alertes} avertissement(s)."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            f"Aucune erreur. {alertes} avertissement(s)."))

    # ------------------------------------------------------------ réglages
    def _reglages(self, note):
        if settings.DEBUG:
            note(ATTENTION, "DEBUG est actif",
                 "Sur un serveur, posez DEBUG=0 (docker-compose.yml le fait "
                 "déjà). En DEBUG, les pages d'erreur exposent la "
                 "configuration.")
        else:
            note(OK, "DEBUG désactivé")

        if settings.SECRET_KEY == getattr(settings, "INSECURE_DEV_SECRET_KEY", None):
            note(ERREUR, "SECRET_KEY est celle du dépôt",
                 "Renseignez SECRET_KEY dans .env.")
        else:
            note(OK, "SECRET_KEY propre au serveur")

        if settings.ALLOWED_HOSTS == ["*"]:
            note(ERREUR, "ALLOWED_HOSTS accepte n'importe quel hôte",
                 "Listez les noms d'hôte dans .env.")
        else:
            note(OK, "ALLOWED_HOSTS : " + ", ".join(settings.ALLOWED_HOSTS))

    # --------------------------------------------------------------- accès
    def _acces(self, note):
        prefixe = getattr(settings, "URL_PREFIX", "")
        if prefixe:
            note(ATTENTION, f"URL_PREFIX = {prefixe}",
                 "Toutes les URLs porteront ce préfixe. Le reverse proxy doit "
                 f"le retirer (« uri strip_prefix {prefixe} »). Pour un "
                 "domaine ou sous-domaine dédié, laissez URL_PREFIX vide.")
        else:
            note(OK, "URL_PREFIX vide (application servie à la racine)")

        securise = getattr(settings, "SESSION_COOKIE_SECURE", False)
        origines = getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        if settings.DEBUG:
            pass
        elif securise:
            note(OK, "Cookies marqués « Secure » (accès en https)",
                 "Si vous joignez l'application en http:// par son adresse IP, "
                 "la connexion bouclera : posez HTTPS=0 dans .env.")
        else:
            note(ATTENTION, "Cookies non marqués « Secure » (HTTPS=0)",
                 "Acceptable sur un réseau local de confiance uniquement : le "
                 "cookie de session circule en clair.")

        if not settings.DEBUG and securise and not origines:
            note(ATTENTION, "CSRF_TRUSTED_ORIGINS n'est pas renseignée",
                 "Indiquez l'origine https du site (ex. "
                 "CSRF_TRUSTED_ORIGINS=https://quiz.mon-domaine.tn), sinon "
                 "l'envoi des formulaires peut être refusé.")
        elif origines:
            self._coherence_origines(note, origines, securise)

    def _coherence_origines(self, note, origines, securise):
        """Une origine CSRF dont l'hôte n'est pas dans ALLOWED_HOSTS ne sert à
        rien (faute de frappe dans le nom, oubli d'un alias) : le formulaire
        est accepté par le contrôle CSRF puis rejeté en 400 par l'hôte — ou
        l'inverse. On compare les deux listes."""
        from urllib.parse import urlsplit

        hotes = {h.lstrip("*.").lower() for h in settings.ALLOWED_HOSTS}
        for origine in origines:
            decoupe = urlsplit(origine)
            hote = (decoupe.hostname or "").lower()
            if hote and hote not in hotes:
                note(ERREUR, f"Origine CSRF « {origine} » absente d'ALLOWED_HOSTS",
                     f"L'hôte « {hote} » n'est pas dans ALLOWED_HOSTS "
                     f"({', '.join(settings.ALLOWED_HOSTS)}). Vérifiez "
                     "l'orthographe du nom de domaine dans .env.")
                return
            if securise and decoupe.scheme != "https":
                note(ATTENTION, f"Origine CSRF « {origine} » en http",
                     "Les cookies sont marqués « Secure » (HTTPS=1) : "
                     "l'origine devrait être en https.")
                return
        note(OK, "CSRF_TRUSTED_ORIGINS : " + ", ".join(origines))

    # ----------------------------------------------------------------- OCR
    def _ocr(self, note):
        import pytesseract
        binaire = shutil.which("tesseract") or getattr(
            pytesseract.pytesseract, "tesseract_cmd", "")
        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001 — absent ou injoignable
            note(ATTENTION, "Tesseract introuvable : OCR du nom désactivé",
                 f"({type(exc).__name__}) L'identification par QR et par "
                 "grille de n° fonctionne ; seules les copies à identifier par "
                 "le nom manuscrit resteront à affecter à la main.\n"
                 "Installez : tesseract-ocr tesseract-ocr-fra tesseract-ocr-ara")
            return
        try:
            langues = set(pytesseract.get_languages(config=""))
        except Exception:  # noqa: BLE001
            langues = set()
        manquantes = {"fra", "ara"} - langues
        if manquantes:
            note(ATTENTION, f"Tesseract {version} — paquets de langue manquants",
                 f"Absents : {', '.join(sorted(manquantes))}. "
                 "Installez tesseract-ocr-fra et tesseract-ocr-ara, sinon "
                 "l'OCR du nom ne fonctionnera pas dans cette langue.")
        else:
            note(OK, f"Tesseract {version} avec les langues fra et ara ({binaire})")

    # ---------------------------------------------------------------- base
    def _base(self, note):
        moteur = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
        try:
            connection.ensure_connection()
        except Exception as exc:  # noqa: BLE001
            note(ERREUR, f"Base de données ({moteur}) injoignable", str(exc))
            return
        executor = MigrationExecutor(connection)
        en_retard = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if en_retard:
            note(ERREUR, f"{len(en_retard)} migration(s) non appliquée(s)",
                 "Lancez : python manage.py migrate")
        else:
            note(OK, f"Base {moteur} joignable, migrations à jour")

    # ------------------------------------------------------------ fichiers
    def _fichiers(self, note):
        for libelle, chemin in (("data/ (base, sauvegardes)", settings.DATA_DIR),
                                ("media/ (fiches, scans, bulletins)",
                                 settings.MEDIA_ROOT)):
            chemin = str(chemin)
            if not os.path.isdir(chemin):
                note(ERREUR, f"{libelle} n'existe pas", chemin)
            elif not os.access(chemin, os.W_OK):
                note(ERREUR, f"{libelle} n'est pas accessible en écriture", chemin)
            else:
                note(OK, f"{libelle} accessible en écriture")

        racine = getattr(settings, "STATIC_ROOT", None)
        if settings.DEBUG:
            return
        if not racine or not os.path.isdir(racine) or not os.listdir(racine):
            note(ERREUR, "Fichiers statiques non collectés",
                 "Lancez : python manage.py collectstatic --noinput "
                 "(le conteneur le fait au démarrage).")
        else:
            note(OK, "Fichiers statiques collectés")
