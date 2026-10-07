"""Titres des pages d'administration, en français correct.

La traduction de Django compose ses titres avec le nom du modèle, sans
article ni élision : « Modification de utilisateur », « Modification de
Réponse », « Sélectionnez l'objet Quiz à changer ». C'est du français, mais
fautif, et ces titres s'affichent en tête de chaque page.

Chaque écran déclare donc ses trois titres en clair.
"""
from django.contrib.admin.options import IS_POPUP_VAR
from django.utils.text import capfirst


class TitresFrancais:
    titre_liste = None          # par défaut : nom pluriel du modèle
    titre_ajout = None          # « Nouvelle classe »
    titre_modification = None   # « Modifier la classe »
    titre_consultation = None   # écran en lecture seule

    def changelist_view(self, request, extra_context=None):
        contexte = dict(extra_context or {})
        if IS_POPUP_VAR in request.GET:
            # Fenêtre de choix ouverte depuis un autre formulaire.
            contexte.setdefault(
                "title", f"Choisir : {self.model._meta.verbose_name}")
        else:
            contexte.setdefault(
                "title", self.titre_liste
                or capfirst(self.model._meta.verbose_name_plural))
        # Repris par le bouton d'ajout (admin/change_list_object_tools.html).
        if self.titre_ajout:
            contexte.setdefault("titre_ajout", self.titre_ajout)
        return super().changelist_view(request, contexte)

    def add_view(self, request, form_url="", extra_context=None):
        contexte = dict(extra_context or {})
        if self.titre_ajout:
            contexte.setdefault("title", self.titre_ajout)
        return super().add_view(request, form_url, contexte)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        contexte = dict(extra_context or {})
        titre = self.titre_modification
        if self.titre_consultation and not self.has_change_permission(request):
            titre = self.titre_consultation
        if titre:
            contexte.setdefault("title", titre)
        return super().change_view(request, object_id, form_url, contexte)
