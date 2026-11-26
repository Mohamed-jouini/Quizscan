"""Gestion des comptes dans l'interface d'administration.

L'administrateur d'établissement règle ici, pour chaque enseignant :
  - les **classes** dont il a la charge — il ne verra que celles-là ;
  - les **concours** dont il a la charge (une liste de candidats par étape, à part
    des classes) ;
  - ce qu'il peut faire, dans un **tableau** (26 novembre 2026) : pour chaque domaine
    (concours et quiz, questions et corrigé, copies, candidats et inscriptions,
    communiqués), ajouter, modifier, supprimer, vérifier ; et lancer la correction.
Les cases sont indépendantes : préparer les sujets et dépouiller ne sont pas toujours
le travail de la même personne. Aucune ne permet de retoucher la note d'un QCM (voir
grader/droits.py).

L'écran livré par Django expose bien plus (groupes, permissions unitaires,
empreinte du mot de passe) ; on le réorganise pour que l'essentiel soit
devant et le reste replié.
"""
from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils.html import format_html
from django.utils.text import format_lazy
from django.utils.translation import gettext, gettext_lazy

from . import droits
from .admin_titres import TitresFrancais
from .models import ClassGroup

User = get_user_model()


class TableauDroits(forms.CheckboxSelectMultiple):
    """Les droits d'un compte en tableau (26 novembre 2026) : un domaine par ligne, un geste
    par colonne (ajouter, modifier, supprimer, vérifier), « lancer la correction » dessous,
    et ce que permet chaque case (grader/templates/grader/widgets/tableau_droits.html)."""
    template_name = "grader/widgets/tableau_droits.html"

    def get_context(self, name, value, attrs):
        contexte = super().get_context(name, value, attrs)
        cochees = {str(v) for v in (value or [])}
        lignes = []
        for domaine, libelle, gestes in droits.TABLEAU:
            cases, aides = [], []
            for geste in droits.GESTES:
                if geste not in gestes:
                    cases.append(None)
                    continue
                code = droits.code_case(domaine, geste)
                cases.append({"code": code, "coche": code in cochees, "aide": droits.AIDE[code],
                              "libelle": format_lazy("{} — {}", libelle,
                                                     droits.LIBELLES_GESTES[geste])})
                aides.append((droits.LIBELLES_GESTES[geste], droits.AIDE[code]))
            lignes.append({"libelle": libelle, "cases": cases, "aides": aides})
        contexte["tableau"] = {
            "gestes": [droits.LIBELLES_GESTES[g] for g in droits.GESTES], "lignes": lignes,
            "lancer": {"code": "lancer_correction", "coche": "lancer_correction" in cochees,
                       "libelle": gettext_lazy("Lancer la correction"),
                       "aide": droits.AIDE["lancer_correction"]}}
        return contexte


ROLE_ENSEIGNANT = "enseignant"
ROLE_ADMIN = "admin"
ROLES = [
    (ROLE_ENSEIGNANT, gettext_lazy("Enseignant — ses classes, ses épreuves, ses copies")),
    (ROLE_ADMIN, gettext_lazy("Administrateur — gère les comptes et voit toutes les données")),
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
        label=gettext_lazy("Rôle"), choices=ROLES, widget=forms.RadioSelect,
        help_text=gettext_lazy("Un enseignant n'accède qu'à ses propres données."))

    classes = forms.ModelMultipleChoiceField(
        label=gettext_lazy("Classes de cet enseignant"), required=False,
        queryset=ClassGroup.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        help_text=gettext_lazy("Il voit les classes cochées, leurs étudiants et les "
                  "épreuves qui s'y rattachent, ainsi que les épreuves qu'il a créées "
                  "lui-même, même si leur classe est décochée. Une classe peut être "
                  "confiée à plusieurs enseignants : la cocher ici n'en retire pas les "
                  "collègues qui l'ont déjà. Une classe décochée n'est pas "
                  "supprimée."))

    # La liste de candidats d'un concours n'est pas une classe (23 novembre 2026) : elle se
    # coche à part, une par étape du concours.
    concours = forms.ModelMultipleChoiceField(
        label=gettext_lazy("Concours de cet enseignant"), required=False,
        queryset=ClassGroup.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        help_text=gettext_lazy("Les listes de candidats des concours, une par étape : il voit les "
                  "étapes cochées, leurs candidats, copies et résultats."))

    # Le tableau des droits (26 novembre 2026, demande de l'utilisateur : « donne la main de
    # choisir Ajouter, Modifier, Supprimer, Vérifier ») : une case par domaine et par geste.
    gestes = forms.MultipleChoiceField(
        label=gettext_lazy("Ce que ce compte peut faire"), required=False,
        choices=[(code, code) for code in droits.CASES], widget=TableauDroits)

    class Meta:
        model = User
        exclude = ("password", "is_staff", "is_superuser")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance") or self.instance
        self._compte_existant = bool(instance and instance.pk)

        # Toutes les classes sont proposées : une classe peut avoir plusieurs
        # enseignants, en cocher une ici n'en retire personne.
        self.fields["classes"].queryset = ClassGroup.objects.classes()
        self.fields["concours"].queryset = ClassGroup.objects.de_concours()
        if instance and instance.pk:
            self.fields["classes"].initial = instance.classes_attribuees.classes()
            self.fields["concours"].initial = instance.classes_attribuees.de_concours()
            self.fields["role"].initial = (
                ROLE_ADMIN if instance.is_superuser else ROLE_ENSEIGNANT)
            # les cases qu'il a, par elles-mêmes ou par un droit d'avant (paquet)
            self.fields["gestes"].initial = droits.cases_permises(instance)
        else:
            # Un nouvel enseignant sait faire son métier : tout est coché d'office, à
            # décocher si l'établissement le veut.
            self.fields["gestes"].initial = list(droits.CASES)

        self.fields["is_active"].help_text = (
            gettext_lazy("Décochez pour fermer l'accès sans supprimer le compte : les "
            "classes, épreuves et copies de cet enseignant sont conservées."))

    def clean_classes(self):
        """Refuse deux classes de même nom pour un même enseignant.

        Il ne saurait plus laquelle est laquelle dans ses listes (deux
        « 3ème A » de deux établissements ou de deux collègues).
        """
        choisies = self.cleaned_data["classes"]
        noms = [c.name for c in choisies]
        collisions = sorted({n for n in noms if noms.count(n) > 1})
        if collisions:
            raise forms.ValidationError(gettext(
                "Plusieurs des classes cochées s'appellent : %(noms)s. Renommez l'une d'elles "
                "avant de les attribuer ensemble.") % {"noms": ", ".join(collisions)})
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
        """Les classes de l'enseignant : ClassGroup.enseignants, que lisent
        toutes les vues. Les autres enseignants de ces classes restent."""
        choisies = self.cleaned_data.get("classes")
        if choisies is None:
            return
        compte.classes_attribuees.set(list(choisies)
                                      + list(self.cleaned_data.get("concours") or []))

    def _enregistrer_droits(self, compte):
        """Le tableau devient des permissions Django, case par case.

        Un administrateur les a toutes de toute façon (has_perm répond
        toujours oui à un super-utilisateur) : on ne lui en attribue pas.

        Seules les cases qu'on a cochées ou décochées agissent : les mêmes droits figurent
        dans les permissions détaillées, enregistrées juste avant (une case laissée telle
        quelle y retirait le droit qu'on venait d'ajouter, 26 novembre 2026) ; si les deux se
        contredisent, la case l'emporte. Décocher une case qu'un droit d'avant (paquet)
        donnait remplace ce paquet par ses autres cases, une à une : on ne retire que ce qu'on
        a décoché. Un compte neuf reçoit ce que disent ses cases.
        """
        if compte.is_superuser:
            return
        voulues = set(self.cleaned_data.get("gestes") or [])
        avant = set(self.fields["gestes"].initial or []) if self._compte_existant else set()
        a_ajouter, a_retirer = voulues - avant, avant - voulues
        if not (a_ajouter or a_retirer):
            return
        par_code = {p.codename: p for p in Permission.objects.filter(
            content_type__app_label="grader",
            codename__in=list(droits.CASES) + list(droits.PAQUETS))}
        directes = set(compte.user_permissions.filter(content_type__app_label="grader")
                       .values_list("codename", flat=True))
        for paquet, cases in droits.PAQUETS.items():
            if paquet in directes and a_retirer & set(cases):
                compte.user_permissions.remove(par_code[paquet])
                a_ajouter |= set(cases) - a_retirer
        compte.user_permissions.add(*[par_code[c] for c in a_ajouter if c in par_code])
        compte.user_permissions.remove(*[par_code[c] for c in a_retirer if c in par_code])


# django.contrib.auth enregistre déjà User : on reprend la place.
admin.site.unregister(User)


@admin.register(User)
class CompteAdmin(TitresFrancais, UserAdmin):
    titre_liste = gettext_lazy("Utilisateurs")
    titre_ajout = gettext_lazy("Nouveau compte")
    titre_modification = gettext_lazy("Modifier le compte")
    form = CompteChangeForm
    list_display = ("username", "nom_complet", "role", "ses_classes",
                    "autorisations", "actif", "mot_de_passe_lien")
    list_filter = ("is_active", "is_superuser")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    readonly_fields = ("mot_de_passe", "last_login", "date_joined")

    fieldsets = (
        (gettext_lazy("Compte"), {"fields": ("username", "mot_de_passe")}),
        (gettext_lazy("Identité"), {"fields": ("first_name", "last_name", "email")}),
        (gettext_lazy("Accès"), {"fields": ("role", "is_active")}),
        (gettext_lazy("Classes"), {
            "description": gettext_lazy("Les classes dont cet enseignant a la charge."),
            "fields": ("classes",)}),
        (gettext_lazy("Concours"), {
            "description": gettext_lazy("Les concours dont il a la charge (chaque étape a sa "
                                        "liste de candidats)."),
            "fields": ("concours",)}),
        (gettext_lazy("Autorisations"), {
            "description": gettext_lazy("Cochez, domaine par domaine, ce que ce compte peut "
                                        "ajouter, modifier, supprimer ou vérifier ; le détail "
                                        "de chaque case est sous le tableau. Dans tous les cas, "
                                        "la note d'un QCM reste celle de la lecture optique : "
                                        "un enseignant ne la modifie pas."),
            "fields": ("gestes",)}),
        (gettext_lazy("Permissions détaillées"), {
            "classes": ("collapse",),
            "description": format_lazy(
                "{} {}",
                gettext_lazy("Réglages avancés de Django. Le rôle ci-dessus "
                             "suffit dans la plupart des établissements."),
                gettext_lazy("Les droits de QuizScan y figurent aussi (« quiz | … ») : les "
                             "ajouter ou les retirer ici revient à cocher ou décocher leur "
                             "case dans « Autorisations » ; si les deux se contredisent, la "
                             "case l'emporte.")),
            "fields": ("groups", "user_permissions")}),
        (gettext_lazy("Historique"), {"classes": ("collapse",),
                                      "fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (gettext_lazy("Nouveau compte"), {
            "fields": ("username", "usable_password", "password1", "password2"),
            "description": gettext_lazy("L'identifiant et le mot de passe seront remis à "
                                        "l'enseignant ; il pourra changer son mot de passe "
                                        "depuis son espace. L'écran suivant demandera ses "
                                        "classes et ses autorisations.")}),
    )

    @admin.display(description=gettext_lazy("Nom"), ordering="last_name")
    def nom_complet(self, obj):
        return obj.get_full_name() or "—"

    @admin.display(description=gettext_lazy("Rôle"), ordering="is_superuser")
    def role(self, obj):
        if obj.is_superuser:
            return format_html('<span class="qs-tag qs-tag--admin">{}</span>',
                               gettext("Administrateur"))
        return format_html('<span class="qs-tag">{}</span>', gettext("Enseignant"))

    @admin.display(description=gettext_lazy("Classes"))
    def ses_classes(self, obj):
        noms = list(obj.classes_attribuees.classes().values_list("name", flat=True))
        return ", ".join(noms) if noms else "—"

    @admin.display(description=gettext_lazy("Autorisations"))
    def autorisations(self, obj):
        if obj.is_superuser:
            return format_html('<span class="qs-tag qs-tag--admin">{}</span>', gettext("tout"))
        # un domaine par étiquette : entier, ou le nombre de ses cases cochées (tableau des droits)
        permises = set(droits.cases_permises(obj))
        tags = []
        for domaine, libelle, gestes in droits.TABLEAU:
            n = sum(1 for g in gestes if droits.code_case(domaine, g) in permises)
            if n == len(gestes):
                tags.append(str(libelle))
            elif n:
                tags.append(f"{libelle} {n}/{len(gestes)}")
        if "lancer_correction" in permises:
            tags.append(gettext("lancer la correction"))
        if not tags:
            return format_html('<span class="qs-tag">{}</span>', gettext("lecture seule"))
        return format_html(
            "".join('<span class="qs-tag">{}</span> ' for _ in tags), *tags)

    @admin.display(description=gettext_lazy("Actif"), boolean=True, ordering="is_active")
    def actif(self, obj):
        return obj.is_active

    def _url_mot_de_passe(self, obj):
        """Adresse absolue de l'écran de changement du mot de passe.

        Pas de lien relatif (« 3/password/ ») : juste depuis la liste
        (/admin/auth/user/), il devient « /admin/auth/user/3/change/3/password/ »
        depuis la fiche du compte — « l'utilisateur n'existe pas »."""
        meta = obj._meta
        return reverse(
            f"{self.admin_site.name}:{meta.app_label}_{meta.model_name}"
            "_password_change", args=[obj.pk])

    @admin.display(description=gettext_lazy("Mot de passe"))
    def mot_de_passe_lien(self, obj):
        return format_html(
            '<a class="qs-pw-link" href="{}">{}</a>',
            self._url_mot_de_passe(obj), gettext("Modifier"))

    @admin.display(description=gettext_lazy("Mot de passe"))
    def mot_de_passe(self, obj):
        """Ligne du formulaire : pas d'empreinte, juste un bouton.

        Un mot de passe n'est jamais stocké en clair : il ne peut pas être
        relu, seulement remplacé."""
        if not obj.pk:
            return "—"
        if not obj.has_usable_password():
            return format_html(
                '<span class="qs-pw-none">{}</span> <a class="button qs-pw-btn" href="{}">{}</a>',
                gettext("Aucun mot de passe utilisable — ce compte ne peut pas se connecter."),
                self._url_mot_de_passe(obj), gettext("Définir un mot de passe"))
        return format_html(
            '<a class="button qs-pw-btn" href="{}">{}</a><p class="help qs-pw-help">{}</p>',
            self._url_mot_de_passe(obj), gettext("Modifier le mot de passe"),
            gettext("Un mot de passe ne se relit pas : il ne peut être que remplacé."))
