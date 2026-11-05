"""Fichiers statiques servis par l'application en production (DEBUG=0).

Sans en-tête de cache, le navigateur redemandait la feuille de style et les
scripts à chaque page (« ont-ils changé ? ») : un aller-retour de plus par
fichier, sensible sur une connexion mobile lente. Une adresse à empreinte
(« quizscan.css?v=3f2a91c0d4 », templatetags/qs_statique) désigne une version
précise : elle reste un an dans le navigateur, sans redemande ; une nouvelle
version a une autre adresse. Les autres fichiers (polices, images,
administration de Django) se gardent une heure.
"""
import os

from django.conf import settings
from django.views.static import serve

from .templatetags.qs_statique import _empreinte

UN_AN = 365 * 24 * 3600
UNE_HEURE = 3600


def fichier_statique(request, path):
    reponse = serve(request, path, document_root=settings.STATIC_ROOT)
    empreinte = request.GET.get("v", "")
    duree = UNE_HEURE
    if empreinte:
        # l'empreinte demandée doit être celle du fichier servi : sinon (page
        # restée ouverte pendant une mise à jour…), il ne se garde qu'une heure
        chemin = os.path.join(settings.STATIC_ROOT, path)
        try:
            if _empreinte(chemin, os.path.getmtime(chemin)) == empreinte:
                duree = UN_AN
        except OSError:
            pass
    reponse["Cache-Control"] = (f"public, max-age={duree}, immutable" if duree == UN_AN
                                else f"public, max-age={duree}")
    return reponse
