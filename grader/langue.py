"""Langue de l'interface : français ou arabe (demande de l'utilisateur, 24 novembre 2026 :
« ajout une langue arabe … pour changer la langue du logiciel arabe ou française », puis
« fait tout le logiciel »).

La langue est celle choisie par le bouton العربية / Français (cookie de langue de Django, vue
set_language), partout : espace public, pages du personnel, administration. Sans choix :
  - l'espace public suit la langue du navigateur (un téléphone réglé en arabe l'ouvre en
    arabe) ;
  - le reste est en français : le personnel n'est pas basculé en arabe par la seule
    préférence de son navigateur, sans l'avoir demandé.

En arabe, les pages se lisent de droite à gauche (LANGUAGE_BIDI dans les gabarits) ; les
textes sont dans grader/locale/ar/LC_MESSAGES/django.po, compilés par
« python manage.py traductions ».
"""
import functools

from django.conf import settings
from django.utils import translation
from django.utils.cache import patch_vary_headers

ESPACE_PUBLIC = ("/public/",)       # sans le préfixe URL_PREFIX


def en_francais(fonction):
    """Documents (PDF, Excel) : en français, comme leurs en-têtes, même demandés depuis
    l'interface en arabe. Les textes traduits qu'ils empruntent à l'application (type
    d'épreuve, « Lot du … », résultat final d'un concours) restent alors en français eux
    aussi : pas de document mi-français, mi-arabe (24 novembre 2026)."""
    @functools.wraps(fonction)
    def enveloppe(*args, **kwargs):
        with translation.override(settings.LANGUAGE_CODE):
            return fonction(*args, **kwargs)
    return enveloppe


def espace_public(chemin):
    return chemin.startswith(ESPACE_PUBLIC)


class Langue:
    """À placer après SessionMiddleware, avant CommonMiddleware (comme LocaleMiddleware)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public = espace_public(request.path_info)
        choix = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if choix in dict(settings.LANGUAGES):
            langue = choix
        elif public:
            langue = translation.get_language_from_request(request)
        else:
            langue = settings.LANGUAGE_CODE
        translation.activate(langue)
        request.LANGUAGE_CODE = translation.get_language()
        reponse = self.get_response(request)
        # la même adresse, en français ou en arabe : un cache ne doit pas les mêler
        # Pages seulement : la feuille de style et les scripts, gardés un an par le
        # navigateur, se seraient retéléchargés à chaque cookie changé (Vary: Cookie)
        if reponse.get("Content-Type", "").startswith("text/html"):
            patch_vary_headers(reponse, ("Accept-Language", "Cookie") if public else ("Cookie",))
            reponse.headers.setdefault("Content-Language", request.LANGUAGE_CODE)
        return reponse
