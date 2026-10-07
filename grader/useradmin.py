"""Gestion des comptes dans l'interface d'administration.

L'administrateur d'établissement règle ici, pour chaque enseignant :
  - les **classes** dont il a la charge — il ne verra que celles-là ;
  - le droit de **créer des épreuves** ;
  - le droit de **corriger les copies**.
Les deux droits sont indépendants : préparer les sujets et dépouiller ne sont
pas toujours le travail de la même personne. Aucun des deux ne permet de
retoucher la note d'un QCM (voir grader/droits.py).

L'écran livré par Django expose bien plus (groupes, permissions unitaires,
empreinte du mot de passe) ; on le réorganise pour que l'essentiel soit
devant et le reste replié.
"""
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Permission
from django.utils.html import format_html

from . import droits
from .models import ClassGroup

User = get_user_model()


def _permission(nom_complet):
    """La permission Django derrière « grader.creer_epreuve »."""
    application, code = nom_complet.split(".")
    return Permission.objects.get(
        content_type__app_label=application, codename=code)

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

    classes = forms.ModelMultipleChoiceField(
        label="Classes de cet enseignant", required=False,
        queryset=ClassGroup.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        help_text="Il ne voit que les classes cochées, leurs étudiants et les "
                  "épreuves qui s'y rattachent — y compris celles écrites par "
                  "un collègue, qui en garde l'accès. Une classe décochée "
                  "n'est pas supprimée : elle n'appartient plus à personne "
                  "tant qu'elle n'est pas attribuée à quelqu'un d'autre.")

    peut_creer = forms.BooleanField(
        label="Créer des épreuves", required=False,
        help_text="Saisir les questions, régler la fiche, produire les PDF.")
    peut_corriger = forms.BooleanField(
        label="Corriger les copies", required=False,
        help_text="Téléverser les copies scannées, lancer la correction et "
                  "la vérifier. Ne permet pas de modifier la note d'un QCM : "
                  "elle est établie par lecture optique.")

    class Meta:
        model = User
        exclude = ("password", "is_staff", "is_superuser")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance") or self.instance

        # Les classes proposées : celles de ce compte, plus celles qui
        # n'appartiennent à personne. Celles d'un collègue ne sont pas
        # reprises ici — on ne déshabille pas un enseignant par mégarde
        # depuis la fiche d'un autre.
        libres = ClassGroup.objects.filter(owner__isnull=True)
        if instance and instance.pk:
            self.fields["classes"].queryset = (
                ClassGroup.objects.filter(owner=instance) | libres).distinct()
            self.fields["classes"].initial = ClassGroup.objects.filter(
                owner=instance)
            self.fields["role"].initial = (
                ROLE_ADMIN if instance.is_superuser else ROLE_ENSEIGNANT)
            self.fields["peut_creer"].initial = droits.peut_creer(instance)
            self.fields["peut_corriger"].initial = droits.peut_corriger(instance)
        else:
            self.fields["classes"].queryset = libres
            # Un nouvel enseignant sait faire son métier : les deux droits
            # sont cochés d'office, à décocher si l'établissement le veut.
            self.fields["peut_creer"].initial = True
            self.fields["peut_corriger"].initial = True

        self.fields["is_active"].help_text = (
            "Décochez pour fermer l'accès sans supprimer le compte : les "
            "classes, épreuves et copies de cet enseignant sont conservées.")

    def clean_classes(self):
        """Refuse un transfert qui créerait deux classes de même nom.

        ClassGroup impose (enseignant, nom) unique : reprendre une classe
        libre nommée « 3ème A » alors que l'enseignant en a déjà une
        échouerait au moment d'enregistrer, sans explication.
        """
        choisies = self.cleaned_data["classes"]
        compte = self.instance
        if not (compte and compte.pk):
            return choisies
        deja = set(ClassGroup.objects.filter(owner=compte)
                   .exclude(pk__in=[c.pk for c in choisies])
                   .values_list("name", flat=True))
        collisions = sorted({c.name for c in choisies if c.name in deja})
        if collisions:
            raise forms.ValidationError(
                "Cet enseignant a déjà une classe nommée : "
                + ", ".join(collisions)
                + ". Renommez-la avant de lui attribuer celle-ci.")
        return choisies

    def save(self, commit=True):
        """Enregistre le compte, ses classes et ses droits.

        L'administration de Django n'appelle jamais save(commit=True) : elle
        fait save(commit=False), enregistre l'objet, puis appelle save_m2m().
        Les affectations sont donc accrochées à save_m2m, le seul point par
        lequel les deux chemins — admin et code ordinaire — passent.
        """
        compte = super().save(commit=False)
        administrateur = self.cleaned_data.get("role") == ROLE_ADMIN
        compte.is_staff = compte.is_superuser = administrateur

        liaisons_django = self.save_m2m

        def enregistrer_liaisons():
            liaisons_django()
            self._enregistrer_classes(compte)
            self._enregistrer_droits(compte)

        self.save_m2m = enregistrer_liaisons
        if commit:
            compte.save()
            self.save_m2m()
        return compte

    def _enregistrer_classes(self, compte):
        """Une classe appartient à un enseignant : c'est ClassGroup.owner.

        On ne crée pas de seconde liste d'affectations à côté : la relation
        existe déjà et c'est elle que lisent toutes les vues.
        """
        choisies = self.cleaned_data.get("classes")
        if choisies is None:
            return
        gardees = [c.pk for c in choisies]
        ClassGroup.objects.filter(owner=compte).exclude(
            pk__in=gardees).update(owner=None)
        ClassGroup.objects.filter(pk__in=gardees).update(owner=compte)

    def _enregistrer_droits(self, compte):
        """Les deux cases deviennent des permissions Django.

        Un administrateur les a toutes de toute façon (has_perm répond
        toujours oui à un super-utilisateur) : on ne lui en attribue pas.
        """
        if compte.is_superuser:
            return
        for nom, coche in ((droits.CREER, self.cleaned_data.get("peut_creer")),
                           (droits.CORRIGER,
                            self.cleaned_data.get("peut_corriger"))):
            permission = _permission(nom)
            if coche:
                compte.user_permissions.add(permission)
            else:
                compte.user_permissions.remove(permission)


# django.contrib.auth enregistre déjà User : on reprend la place.
admin.site.unregister(User)


@admin.register(User)
class CompteAdmin(UserAdmin):
    form = CompteChangeForm
    list_display = ("username", "nom_complet", "role", "ses_classes",
                    "autorisations", "actif", "mot_de_passe_lien")
    list_filter = ("is_active", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    readonly_fields = ("mot_de_passe", "last_login", "date_joined")

    fieldsets = (
        ("Compte", {"fields": ("username", "mot_de_passe")}),
        ("Identité", {"fields": ("first_name", "last_name", "email")}),
        ("Accès", {"fields": ("role", "is_active")}),
        ("Classes", {
            "description": "Les classes dont cet enseignant a la charge.",
            "fields": ("classes",)}),
        ("Autorisations", {
            "description": "Les deux gestes se donnent séparément. Dans tous "
                           "les cas, la note d'un QCM reste celle de la "
                           "lecture optique : un enseignant ne la modifie pas.",
            "fields": ("peut_creer", "peut_corriger")}),
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
                           "depuis son espace. L'écran suivant demandera ses "
                           "classes et ses autorisations."}),
    )

    @admin.display(description="Nom", ordering="last_name")
    def nom_complet(self, obj):
        return obj.get_full_name() or "—"

    @admin.display(description="Rôle", ordering="is_superuser")
    def role(self, obj):
        if obj.is_superuser:
            return format_html('<span class="qs-tag qs-tag--admin">Administrateur</span>')
        return format_html('<span class="qs-tag">Enseignant</span>')

    @admin.display(description="Classes")
    def ses_classes(self, obj):
        noms = list(obj.class_groups.values_list("name", flat=True))
        return ", ".join(noms) if noms else "—"

    @admin.display(description="Autorisations")
    def autorisations(self, obj):
        if obj.is_superuser:
            return format_html('<span class="qs-tag qs-tag--admin">tout</span>')
        tags = []
        if droits.peut_creer(obj):
            tags.append("créer")
        if droits.peut_corriger(obj):
            tags.append("corriger")
        if not tags:
            return format_html('<span class="qs-tag">lecture seule</span>')
        return format_html(
            "".join('<span class="qs-tag">{}</span> ' for _ in tags), *tags)

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
