"""Protections transverses du site (audit de sécurité du 1er novembre 2026).

  - adresse_client : l'adresse du visiteur, sans croire un X-Forwarded-For
    inventé par le client ;
  - FreinConnexion : limite les essais de mot de passe (aucune limite avant :
    on pouvait deviner celui d'un administrateur sans fin, et chaque essai
    coûte un calcul de hachage lourd au serveur) ;
  - EntetesSecurite : politique de contenu (CSP), pages du personnel jamais
    gardées par le navigateur, session renouvelée sans écriture à chaque
    requête.
"""
import hashlib
import ipaddress
import logging
import time

from django import forms
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

ESSAIS_PAR_ADRESSE = 5          # par compte et par adresse…
ESSAIS_PAR_COMPTE = 20          # …et par compte, toutes adresses confondues
FENETRE_CONNEXION_S = 15 * 60


def _est_ip(texte):
    try:
        ipaddress.ip_address(texte)
    except ValueError:
        return False
    return True


def adresse_client(request):
    """Adresse du visiteur. X-Forwarded-For n'est cru que si la requête vient
    d'une adresse privée (Caddy, sur le serveur ou le réseau Docker), et on le
    lit en partant de la fin : chaque proxy y ajoute l'adresse qu'il a vue, le
    début vient du client, qui peut l'inventer à chaque requête.

    settings.PROXYS_DE_CONFIANCE donne le nombre de proxies devant
    l'application. Mal réglé derrière deux Caddy, tous les visiteurs auraient
    l'adresse du premier et se partageraient les mêmes limites."""
    distante = request.META.get("REMOTE_ADDR", "")
    proxys = settings.PROXYS_DE_CONFIANCE
    try:
        derriere_proxy = proxys > 0 and ipaddress.ip_address(distante).is_private
    except ValueError:
        derriere_proxy = False
    if derriere_proxy:
        transmises = [x.strip() for x in
                      request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if x.strip()]
        if transmises:
            adresse = transmises[-min(proxys, len(transmises))]
            if _est_ip(adresse):
                return adresse
    return distante


class FreinConnexion:
    """À placer avant AuthenticationForm dans les bases d'un formulaire de
    connexion. Après 5 échecs pour un compte depuis une adresse (ou 20 pour
    un compte, toutes adresses), les essais sont refusés 15 minutes — sans
    calculer le hachage. Le seuil par adresse évite qu'un tiers bloque un
    enseignant depuis chez lui ; celui par compte freine un essai distribué."""

    def clean(self):
        # L'identifiant nettoyé par le champ, celui qui sert à la connexion : il
        # est normalisé (NFKC), et « ｄemo » pleine chasse ouvre le compte
        # « demo ». Le texte brut donnerait un compteur neuf à chaque variante.
        compte = (self.cleaned_data.get("username") or "").strip().lower()[:150]
        adresse = adresse_client(self.request) if self.request else ""
        empreinte = hashlib.sha256(compte.encode()).hexdigest()[:32]
        par_adresse, par_compte = f"qs-cnx:{empreinte}:{adresse}", f"qs-cnx:{empreinte}"
        if (cache.get(par_adresse, 0) >= ESSAIS_PAR_ADRESSE
                or cache.get(par_compte, 0) >= ESSAIS_PAR_COMPTE):
            self.frein = True              # la page affiche ce message-ci
            # %r : un identifiant ne doit pas pouvoir fabriquer de fausses
            # lignes de journal. Seulement au blocage : l'identifiant d'un
            # échec isolé est parfois un mot de passe tapé au mauvais endroit.
            log.warning("Connexion freinée : compte %r, adresse %s", compte[:40], adresse)
            raise forms.ValidationError(
                "Trop d'essais de connexion pour ce compte. Réessayez dans "
                "15 minutes, ou demandez à l'administrateur de vérifier votre "
                "mot de passe.", code="frein")
        try:
            donnees = super().clean()
        except forms.ValidationError:
            for cle in (par_adresse, par_compte):
                cache.set(cle, cache.get(cle, 0) + 1, FENETRE_CONNEXION_S)
            raise
        # Seule une vraie connexion remet à zéro : un envoi sans mot de passe
        # arrive ici sans que rien n'ait été vérifié, et effacerait le compteur.
        if self.get_user() is not None:
            cache.delete(par_adresse)
            log.info("Connexion de %r depuis %s", self.get_user().get_username(), adresse)
        return donnees


# Tout vient du site lui-même : aucune ressource d'un autre domaine, donc pas
# de script tiers ni d'envoi de données ailleurs. Et aucun script en ligne : un
# nom piégé qui échapperait à l'échappement du gabarit ne s'exécuterait pas.
# Les comportements des pages sont dans grader/static/grader/pages.js. Pour les
# styles, « unsafe-inline » reste : les attributs style n'exécutent rien.
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
SESSION_RENOUVELLEE_S = 15 * 60


def _renouveler_session(request):
    """Délai d'inactivité glissant, une écriture au plus toutes les 15 minutes.
    SESSION_SAVE_EVERY_REQUEST écrirait à chaque image chargée, et des requêtes
    parallèles s'écraseraient : la dernière remettrait une session périmée,
    sans le rapport de vérification qu'une autre venait d'y déposer."""
    session = getattr(request, "session", None)
    if session is None or "_auth_user_id" not in session:
        return
    maintenant = int(time.time())
    if maintenant - session.get("_renouvelee", 0) >= SESSION_RENOUVELLEE_S:
        session["_renouvelee"] = maintenant


class EntetesSecurite:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        type_contenu = response.get("Content-Type", "")
        # Pages seulement : sur un PDF ouvert dans un onglet, elle gênerait
        # le lecteur intégré du navigateur sans rien protéger.
        if type_contenu.startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS)
        if settings.SESSION_COOKIE_NAME in request.COOKIES:
            _renouveler_session(request)
            # Poste partagé de salle des professeurs : notes, listes et sujets
            # ne restent pas dans le cache du navigateur après la déconnexion.
            if ("Cache-Control" not in response.headers
                    and not request.path.startswith(settings.STATIC_URL)):
                response.headers["Cache-Control"] = (
                    "private" if type_contenu.startswith("image/") else "private, no-store")
        return response
