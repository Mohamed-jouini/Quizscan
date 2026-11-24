"""Filtres de traduction des gabarits (interface en français ou en arabe, 24 novembre 2026).

oui_non : comme « yesno » de Django, mais le mot retenu est traduit. Les gabarits écrivaient
« {{ quiz.auto_enroll|yesno:"candidats,étudiants" }} » : en arabe, le mot français restait au
milieu de la phrase. Les mots possibles sont listés dans MOTS_OUI_NON, pour que
test_e2e_langue.py vérifie leur traduction.

Usage : {% load qs_langue %}{{ quiz.auto_enroll|oui_non:"candidats,étudiants" }}
"""
from django import template
from django.utils.translation import gettext

register = template.Library()

MOTS_OUI_NON = ("candidat", "étudiant", "candidats", "étudiants", "candidat(s)", "étudiant(s)",
                "admis", "étudiant ayant réussi")


@register.filter
def oui_non(valeur, choix):
    oui, _, non = choix.partition(",")
    return gettext(oui if valeur else non)
