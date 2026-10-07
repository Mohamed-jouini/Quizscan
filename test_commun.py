"""Petits outils partagés par les scripts de test.

Depuis que « créer des épreuves » et « corriger les copies » s'accordent
compte par compte (grader/droits.py), un compte fraîchement créé ne peut
plus rien faire. Les tests qui éprouvent autre chose — l'import, la lecture
optique, les formulaires — ont besoin d'un enseignant complet ; ils passent
par `enseignant_complet` plutôt que de recopier la même incantation.

Le test des droits eux-mêmes, lui, n'utilise pas ce raccourci : il attribue
les permissions une par une, c'est tout son propos.
"""
from django.contrib.auth.models import Permission

from grader import droits


def enseignant_complet(compte):
    """Accorde les deux droits, comme un enseignant en service.

    Renvoie le compte, rechargé : `has_perm` garde en mémoire les
    permissions de l'instance, et l'objet d'origine répondrait encore non.
    """
    for nom in (droits.CREER, droits.CORRIGER):
        application, code = nom.split(".")
        compte.user_permissions.add(Permission.objects.get(
            content_type__app_label=application, codename=code))
    return type(compte).objects.get(pk=compte.pk)
