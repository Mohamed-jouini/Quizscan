"""Adresse d'un fichier statique suivie de son empreinte : « …/quizscan.css?v=3f2a91c0d4 ».

L'adresse de la feuille de style ne changeait jamais : après une mise à jour,
les navigateurs gardaient l'ancienne en cache (des jours parfois) et les
utilisateurs voyaient des écrans à moitié stylés — un champ de recherche sans
mise en forme, un bouton mal placé — jusqu'à un Ctrl+F5. L'empreinte change
avec le contenu : une nouvelle version est une nouvelle adresse, téléchargée
d'office ; une version inchangée reste en cache.

Usage : {% load qs_statique %}<link rel="stylesheet" href="{% statique 'grader/quizscan.css' %}">
"""
import hashlib
import os
from functools import lru_cache

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@lru_cache(maxsize=64)
def _empreinte(chemin, date_modif):
    """Calculée une fois par version du fichier (la date de modification fait
    partie de la clé : un fichier modifié est relu, pas resservi du cache)."""
    with open(chemin, "rb") as fichier:
        return hashlib.sha1(fichier.read()).hexdigest()[:10]


def _chemin(nom):
    chemin = finders.find(nom)
    if not chemin and settings.STATIC_ROOT:          # après collectstatic
        candidat = os.path.join(settings.STATIC_ROOT, nom)
        chemin = candidat if os.path.exists(candidat) else None
    return chemin


@register.simple_tag
def statique(nom):
    url = static(nom)
    chemin = _chemin(nom)
    if not chemin:
        return url
    return f"{url}?v={_empreinte(chemin, os.stat(chemin).st_mtime_ns)}"
