"""Gestion des comptes dans l'interface d'administration.

L'administrateur d'établissement ne fait qu'une chose ici : créer les comptes
enseignants, les activer ou les désactiver, et réinitialiser un mot de passe
oublié. L'écran livré par Django expose beaucoup plus (groupes, permissions
unitaires, empreinte du mot de passe) ; on le réorganise pour que l'essentiel
soit devant et le reste replié.
"""
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html

User = get_user_model()

ROLE_ENSEIGNANT = "enseignant"
ROLE_ADMIN = "admin"
ROLES = [
    (ROLE_ENSEIGNANT, "Enseignant — ses classes, ses épreuves, ses copies"),
    (ROLE_ADMIN, "Administrateur — gère les comptes et voit toutes les données"),
]


class CompteChangeForm(forms.ModelForm):
    """Formulaire d'un compte existant.

    Le mot de passe n'y figure pas : Django y affiche son empreinte
    (« pbkdf2_sha256 itérations: 1000000 salage: … »), illisible et inutile.
    Il est remplacé par une ligne en lecture seule avec un bouton — voir
    CompteAdmin.mot_de_passe.

    « Statut équipe » et « Statut super-utilisateur » sont remplacés par un
    choix unique « Rôle » : dans QuizScan les deux vont toujours ensemble,
    un compte ouvre l'administration et voit tout, ou ni l'un ni l'autre.
    """
    role = forms.ChoiceField(
        label="Rôle", choices=ROLES, widget=forms.RadioSelect,
        help_text="Un enseignant n'accède qu'à ses propres données.")

    class Meta:
        model = User
        exclude = ("password", "is_staff", "is_superuser")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance") or self.instance
        if instance and instance.pk:
            self.fields["role"].initial = (
                ROLE_ADMIN if instance.is_superuser else ROLE_ENSEIGNANT)
        self.fields["is_active"].help_text = (
            "Décochez pour fermer l'accès sans supprimer le compte : les "
            "classes, épreuves et copies de cet enseignant sont conservées.")

    def save(self, commit=True):
        compte = super().save(commit=False)
        administrateur = self.cleaned_data.get("role") == ROLE_ADMIN
        compte.is_staff = compte.is_superuser = administrateur
        if commit:
            compte.save()
            self.save_m2m()
        return compte


# django.contrib.auth enregistre déjà User : on reprend la place.
admin.site.unregister(User)


@admin.register(User)
class CompteAdmin(UserAdmin):
    form = CompteChangeForm
    list_display = ("username", "nom_complet", "email", "role", "actif",
                    "mot_de_passe_lien")
    list_filter = ("is_active", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    readonly_fields = ("mot_de_passe", "last_login", "date_joined")

    fieldsets = (
        ("Compte", {"fields": ("username", "mot_de_passe")}),
        ("Identité", {"fields": ("first_name", "last_name", "email")}),
        ("Accès", {"fields": ("role", "is_active")}),
        ("Permissions détaillées", {
            "classes": ("collapse",),
            "description": "Réglages avancés de Django. Le rôle ci-dessus "
                           "suffit dans la plupart des établissements.",
            "fields": ("groups", "user_permissions")}),
        ("Historique", {"classes": ("collapse",),
                        "fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        ("Nouveau compte", {
            "fields": ("username", "usable_password", "password1", "password2"),
            "description": "L'identifiant et le mot de passe seront remis à "
                           "l'enseignant ; il pourra changer son mot de passe "
                           "depuis son espace."}),
    )

    @admin.display(description="Nom", ordering="last_name")
    def nom_complet(self, obj):
        return obj.get_full_name() or "—"

    @admin.display(description="Rôle", ordering="is_superuser")
    def role(self, obj):
        if obj.is_superuser:
            return format_html('<span class="qs-tag qs-tag--admin">Administrateur</span>')
        return format_html('<span class="qs-tag">Enseignant</span>')

    @admin.display(description="Actif", boolean=True, ordering="is_active")
    def actif(self, obj):
        return obj.is_active

    @admin.display(description="Mot de passe")
    def mot_de_passe_lien(self, obj):
        return format_html(
            '<a class="qs-pw-link" href="{}/password/">Modifier</a>', obj.pk)

    @admin.display(description="Mot de passe")
    def mot_de_passe(self, obj):
        """Ligne du formulaire : pas d'empreinte, juste un bouton.

        Un mot de passe n'est jamais stocké en clair : il ne peut pas être
        relu, seulement remplacé."""
        if not obj.pk:
            return "—"
        if not obj.has_usable_password():
            return format_html(
                '<span class="qs-pw-none">Aucun mot de passe utilisable — '
                'ce compte ne peut pas se connecter.</span> '
                '<a class="button qs-pw-btn" href="{}/password/">Définir un '
                'mot de passe</a>', obj.pk)
        return format_html(
            '<a class="button qs-pw-btn" href="{}/password/">Modifier le mot '
            'de passe</a><p class="help qs-pw-help">Un mot de passe ne se '
            'relit pas : il ne peut être que remplacé.</p>', obj.pk)
