from django.contrib import admin, messages
from django.contrib.auth.models import Group
from django.utils.html import format_html

from . import journal, services, useradmin
from .admin_titres import TitresFrancais
from .forms import AdminLoginForm

# useradmin n'est importé que pour son effet : il remplace l'écran des
# comptes livré par Django (voir grader/useradmin.py).
__all__ = ["useradmin"]
from .models import (Answer, AnswerKeySheet, ClassGroup, Modification,
                     Question, Quiz, ScanBatch, SheetScan, Student)

# L'administrateur de l'établissement gère les comptes (Utilisateurs) et garde
# une vue d'ensemble de toutes les classes, épreuves et copies.

# /admin/login/ affiche le même écran ET le même formulaire que
# /comptes/administration/ : qu'on arrive par un lien direct ou par une
# redirection de Django, la page et les messages sont identiques — un
# enseignant qui se trompe de porte reçoit la même explication.
# La barre latérale de QuizScan (templates/admin/base.html) liste déjà les
# tables : celle de Django ferait double emploi.
admin.site.enable_nav_sidebar = False
# Sans cela, /admin/password_change/ et /comptes/password_change/ utilisent le
# MÊME gabarit (registration/password_change_form.html, fourni par
# contrib.admin) : l'enseignant qui change son mot de passe depuis son espace
# atterrissait sur un écran d'administration.
admin.site.password_change_template = "admin/password_change_form.html"
admin.site.password_change_done_template = "admin/password_change_done.html"
admin.site.login_template = "registration/login_admin.html"
admin.site.login_form = AdminLoginForm
admin.site.site_header = "QuizScan — administration"
admin.site.site_title = "QuizScan"
admin.site.index_title = "Comptes, classes, épreuves et copies"


# La page d'accueil de l'administration est la seule à lister les tables (la
# barre latérale ne les reprend plus). Pour qu'elle serve à quelque chose, on
# y ajoute deux informations que Django ne fournit pas : le nombre de lignes
# et de quoi choisir une icône. Dix COUNT(*) sur une page consultée de temps
# en temps : la dépense est sans commune mesure avec l'analyse d'une copie.
ICONES = {
    "group": "groupe", "user": "compte",
    "classgroup": "classe", "student": "etudiant",
    "quiz": "quiz", "question": "question", "answer": "reponse",
    "scanbatch": "lot", "sheetscan": "page", "answerkeysheet": "corrige",
    "modification": "journal",
}

_index_django = admin.site.index


def _index_enrichi(request, extra_context=None):
    """Complète app_list avec l'effectif et le nom d'icône de chaque table."""
    reponse = _index_django(request, extra_context)
    contexte = getattr(reponse, "context_data", None) or {}
    for application in contexte.get("app_list", []):
        for table in application["models"]:
            modele = table.get("model")
            # Une table sans permission de lecture n'a pas à être comptée.
            if modele is not None and table.get("admin_url"):
                table["effectif"] = modele._default_manager.count()
            table["icone"] = ICONES.get(
                table.get("object_name", "").lower(), "table")
            if table.get("object_name") == "Modification":
                table["name"] = "Journal des modifications"
    return reponse


admin.site.index = _index_enrichi


# Les intitulés des choix (Quiz.SHEET_MODES, Quiz.ID_MODES) sont des phrases
# explicatives, écrites pour les formulaires. Dans une liste ou un filtre
# elles débordent et rendent le tableau illisible : on en donne ici une
# version courte.
SHEET_MODE_COURT = {"full": "Questionnaire complet",
                    "split": "Sujet séparé",
                    "grid": "Grille seule"}
ID_MODE_COURT = {"name": "OCR du nom",
                 "grid": "Grille de n°",
                 "qr": "QR nominatif",
                 "sticker": "Étiquette QR"}


# État d'un lot, d'une copie ou d'un corrigé : une pastille de couleur se
# repère d'un coup d'œil dans une longue liste, un texte gris non.
TON_ETAT = {"done": "ok", "ok": "ok",
            "partial": "warn", "no_match": "warn",
            "pending": "grey", "processing": "info",
            "no_markers": "bad", "error": "bad"}


def pastille_etat(obj):
    return format_html('<span class="badge {}">{}</span>',
                       TON_ETAT.get(obj.status, "grey"), obj.get_status_display())


class AvecPastilleEtat:
    """Colonne « État » en pastille, triable comme le champ."""

    @admin.display(description="État", ordering="status")
    def etat(self, obj):
        return pastille_etat(obj)


def _filtre_court(champ, titre, libelles):
    """Filtre latéral reprenant les intitulés courts ci-dessus."""

    class Filtre(admin.SimpleListFilter):
        title = titre
        parameter_name = champ

        def lookups(self, request, model_admin):
            return list(libelles.items())

        def queryset(self, request, queryset):
            valeur = self.value()
            return queryset.filter(**{champ: valeur}) if valeur else queryset

    Filtre.__name__ = f"Filtre{champ.title().replace('_', '')}"
    return Filtre


# Les groupes de Django ne servent a rien ici : les droits se donnent
# compte par compte sur la fiche de l'enseignant (grader/useradmin.py), et
# la rubrique restait a zero en haut de la page d'accueil.
admin.site.unregister(Group)


@admin.register(ClassGroup)
class ClassGroupAdmin(TitresFrancais, admin.ModelAdmin):
    titre_liste = "Classes"
    titre_ajout = "Nouvelle classe"
    titre_modification = "Modifier la classe"
    list_display = ("name", "ses_enseignants", "nb_etudiants", "le_jour")
    list_filter = ("enseignants",)
    search_fields = ("name",)
    # Plusieurs enseignants par classe : sélecteur à deux listes, comme les
    # permissions — on peut en ajouter sans retirer ceux qui y sont.
    filter_horizontal = ("enseignants",)
    readonly_fields = ("owner",)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("enseignants")

    @admin.display(description="Enseignants")
    def ses_enseignants(self, obj):
        noms = [u.get_full_name() or u.username for u in obj.enseignants.all()]
        return ", ".join(noms) if noms else "—"

    @admin.display(description="Étudiants")
    def nb_etudiants(self, obj):
        return obj.students.count()

    @admin.display(description="Créée le", ordering="created_at")
    def le_jour(self, obj):
        return obj.created_at.strftime("%d/%m/%Y")


@admin.register(Student)
class StudentAdmin(TitresFrancais, admin.ModelAdmin):
    titre_liste = "Étudiants"
    titre_ajout = "Nouvel étudiant"
    titre_modification = "Modifier l'étudiant"
    list_display = ("last_name", "first_name", "student_number", "class_group")
    list_filter = ("class_group__owner",)
    search_fields = ("last_name", "first_name", "student_number")
    list_select_related = ("class_group",)
    # une classe de concours peut compter des milliers de candidats : une
    # liste déroulante les chargerait tous dans la page
    raw_id_fields = ("class_group",)


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = ("order", "qtype", "text", "correct_choice", "points")


@admin.register(Quiz)
class QuizAdmin(TitresFrancais, admin.ModelAdmin):
    titre_liste = "Quiz"
    titre_ajout = "Nouveau quiz"
    titre_modification = "Modifier le quiz"
    list_display = ("title", "class_group", "nb_questions", "type_de_fiche",
                    "identification", "le_jour")
    list_filter = ("owner", "language", "auto_enroll",
                   _filtre_court("sheet_mode", "type de fiche", SHEET_MODE_COURT),
                   _filtre_court("id_mode", "identification", ID_MODE_COURT))
    search_fields = ("title",)
    list_select_related = ("class_group", "owner")
    raw_id_fields = ("class_group",)
    inlines = [QuestionInline]

    def save_model(self, request, obj, form, change):
        avant = Quiz.objects.filter(pk=obj.pk).values_list(
            "wrong_penalty", flat=True).first() if change else None
        super().save_model(request, obj, form, change)
        if change:
            journal.noter(request.user, "penalite", quiz=obj,
                          objet="Pénalité par mauvaise réponse",
                          avant=avant, apres=obj.wrong_penalty)

    def save_formset(self, request, form, formset, change):
        """Questions modifiées ou supprimées depuis la fiche de l'épreuve."""
        if formset.model is not Question:
            return super().save_formset(request, form, formset, change)
        connues = [f.instance.pk for f in formset.forms if f.instance.pk]
        avant = {q.pk: (q.correct_choice, q.points)
                 for q in Question.objects.filter(pk__in=connues)}
        supprimees = {f.instance.pk for f in formset.deleted_forms
                      if f.instance.pk}
        for f in formset.deleted_forms:
            if f.instance.pk:
                journal.question_supprimee(request.user, f.instance)
        super().save_formset(request, form, formset, change)
        for f in formset.forms:
            q = f.instance
            if q.pk in avant and q.pk not in supprimees:
                bonne, bareme = avant[q.pk]
                journal.question_modifiee(request.user, q, bonne_avant=bonne,
                                          bareme_avant=bareme)

    @admin.display(description="Type de fiche", ordering="sheet_mode")
    def type_de_fiche(self, obj):
        return SHEET_MODE_COURT.get(obj.sheet_mode, obj.sheet_mode)

    @admin.display(description="Identification", ordering="id_mode")
    def identification(self, obj):
        return ID_MODE_COURT.get(obj.id_mode, obj.id_mode)

    @admin.display(description="Questions")
    def nb_questions(self, obj):
        return obj.questions.count()

    @admin.display(description="Créée le", ordering="created_at")
    def le_jour(self, obj):
        return obj.created_at.strftime("%d/%m/%Y")


@admin.register(Question)
class QuestionAdmin(TitresFrancais, admin.ModelAdmin):
    titre_liste = "Questions"
    titre_ajout = "Nouvelle question"
    titre_modification = "Modifier la question"
    # La premiere colonne ouvre la question : elle doit donc la designer,
    # pas nommer l'epreuve a laquelle elle appartient.
    list_display = ("intitule_court", "quiz", "qtype", "points")
    list_filter = ("qtype", "quiz__owner")
    search_fields = ("text", "quiz__title")
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)
    ordering = ("quiz", "order")

    def save_model(self, request, obj, form, change):
        ancienne = Question.objects.filter(pk=obj.pk).first() if change else None
        super().save_model(request, obj, form, change)
        if ancienne:
            journal.question_modifiee(request.user, obj,
                                      bonne_avant=ancienne.correct_choice,
                                      bareme_avant=ancienne.points)

    def delete_model(self, request, obj):
        journal.question_supprimee(request.user, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for q in queryset.select_related("quiz"):
            journal.question_supprimee(request.user, q)
        super().delete_queryset(request, queryset)

    @admin.display(description="Question", ordering="order")
    def intitule_court(self, obj):
        texte = (obj.text or "").strip()
        return f"Q{obj.order} — {texte[:60]}" if texte else f"Question {obj.order}" 


@admin.register(ScanBatch)
class ScanBatchAdmin(AvecPastilleEtat, TitresFrancais, admin.ModelAdmin):
    titre_liste = "Lots de scans"
    titre_ajout = "Nouveau lot de scans"
    titre_modification = "Modifier le lot de scans"
    list_display = ("__str__", "quiz", "etat", "avancement", "le_jour")
    list_filter = ("status", "quiz__owner")
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)

    # Supprimer un lot depuis l'administration fait la même chose que depuis
    # l'application : inscription au journal ET effacement des fichiers
    # scannés, que la suppression par défaut de Django laisserait sur disque.
    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status == "processing" \
                and not services.is_stalled(obj):
            return False                 # pas sous le travailleur qui lit
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        services.supprimer_lot(obj, request.user)

    def delete_queryset(self, request, queryset):
        for lot in queryset:
            try:
                services.supprimer_lot(lot, request.user)
            except services.LotEnCours:
                messages.warning(request, f"« {lot} » est en cours de "
                                          "correction : il n'a pas été supprimé.")

    @admin.display(description="Avancement")
    def avancement(self, obj):
        return f"{obj.processed_pages} / {obj.total_pages}"

    @admin.display(description="Reçu le", ordering="created_at")
    def le_jour(self, obj):
        return obj.created_at.strftime("%d/%m/%Y %H:%M")


@admin.register(SheetScan)
class SheetScanAdmin(AvecPastilleEtat, TitresFrancais, admin.ModelAdmin):
    titre_liste = "Pages scannées"
    titre_ajout = "Nouvelle page scannée"
    titre_modification = "Modifier la page scannée"
    list_display = ("__str__", "batch", "student", "etat", "numero_lu")
    list_filter = ("status", "batch__quiz__owner")
    search_fields = ("source_name", "ocr_name_raw", "id_read")
    list_select_related = ("batch", "student")
    raw_id_fields = ("batch", "student")
    list_display_links = ("__str__",)

    def save_model(self, request, obj, form, change):
        ancien = (SheetScan.objects.select_related("student")
                  .filter(pk=obj.pk).first() if change else None)
        super().save_model(request, obj, form, change)
        if ancien:
            journal.attribution(request.user, obj, ancien.student)

    def delete_model(self, request, obj):
        journal.copie_supprimee(request.user, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for copie in queryset.select_related("batch__quiz", "student"):
            journal.copie_supprimee(request.user, copie)
        super().delete_queryset(request, queryset)

    @admin.display(description="N° lu", ordering="id_read")
    def numero_lu(self, obj):
        return obj.id_read or "—"


@admin.register(AnswerKeySheet)
class AnswerKeySheetAdmin(AvecPastilleEtat, TitresFrancais, admin.ModelAdmin):
    """Corrigés scannés — conservés en lecture seule : ce sont des pièces
    justificatives de l'origine du barème, pas des données à retoucher."""
    titre_liste = "Corrigés scannés"
    titre_modification = "Modifier le corrigé scanné"
    titre_consultation = "Corrigé scanné"
    # Le lien ouvrait la fiche du corrige mais affichait le nom de
    # l'epreuve : on nomme le corrige, l'epreuve suit dans sa colonne.
    list_display = ("fichier", "quiz", "page", "nb_lues", "etat", "le_jour")
    list_display_links = ("fichier",)
    list_filter = ("status", "quiz__owner")
    search_fields = ("source_name", "quiz__title")
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)
    readonly_fields = ("quiz", "page_index", "source_name", "image",
                       "overlay_image", "status", "detected", "unreadable",
                       "error_message", "created_at")

    @admin.display(description="Corrigé scanné", ordering="source_name")
    def fichier(self, obj):
        return obj.source_name or f"corrigé #{obj.pk}"

    @admin.display(description="Page", ordering="page_index")
    def page(self, obj):
        return obj.page_index + 1

    @admin.display(description="Scanné le", ordering="created_at")
    def le_jour(self, obj):
        return obj.created_at.strftime("%d/%m/%Y")

    def has_add_permission(self, request):
        return False


@admin.register(Answer)
class AnswerAdmin(TitresFrancais, admin.ModelAdmin):
    titre_liste = "Réponses"
    titre_ajout = "Nouvelle réponse"
    titre_modification = "Modifier la réponse"
    list_display = ("reponse", "sheet", "intitule_question", "points",
                    "a_la_main")
    list_display_links = ("reponse",)
    list_filter = ("is_multiple", "manually_set", "question__qtype")
    list_select_related = ("sheet", "question__quiz")
    # un concours produit une réponse par question et par copie, soit des
    # dizaines de milliers de lignes : jamais de liste déroulante ici
    raw_id_fields = ("sheet", "question")

    def has_add_permission(self, request):
        # Une réponse naît de la lecture d'une copie, jamais d'une saisie.
        return False

    def get_readonly_fields(self, request, obj=None):
        """La réponse lue sur un QCM ne se retouche pas, ici non plus : ni la
        case retenue, ni ses points (calculés). Seule la note d'une réponse
        manuscrite se saisit. Voir grader/droits.py."""
        figes = ["sheet", "question", "detected_choice", "is_multiple"]
        if obj is not None and obj.question.qtype == "qcm":
            figes += ["points_awarded", "manually_set"]
        return figes

    def save_model(self, request, obj, form, change):
        ancienne = Answer.objects.filter(pk=obj.pk).first() if change else None
        super().save_model(request, obj, form, change)
        if ancienne:
            journal.reponse_modifiee(request.user, obj,
                                     case_avant=ancienne.detected_choice,
                                     multiple_avant=ancienne.is_multiple,
                                     note_avant=ancienne.points_awarded)

    @admin.display(description="Réponse lue")
    def reponse(self, obj):
        return obj.detected_letter or "—"

    @admin.display(description="Question", ordering="question__order")
    def intitule_question(self, obj):
        # « Q1 (QCM (lecture automatique)) » tenait sur deux lignes dans une
        # colonne qui n'a besoin que du numero et du genre de question.
        genre = "QCM" if obj.question.qtype == "qcm" else "manuscrite"
        return f"Q{obj.question.order} · {genre}"

    @admin.display(description="Points", ordering="points_awarded")
    def points(self, obj):
        if obj.points_awarded is None:
            return "—"
        return f"{obj.points_awarded:g}"

    @admin.display(description="Note manuelle", boolean=True,
                   ordering="manually_set")
    def a_la_main(self, obj):
        return obj.manually_set


@admin.register(Modification)
class JournalAdmin(TitresFrancais, admin.ModelAdmin):
    """Journal des modifications — consultable, jamais modifiable.

    Ni ajout, ni modification, ni suppression, même pour un administrateur :
    un journal qu'on peut retoucher ne prouve plus rien.
    """
    titre_liste = "Journal des modifications"
    titre_consultation = "Détail d'une modification"
    list_display = ("le_moment", "auteur_nom", "action", "quiz_titre", "objet",
                    "avant", "apres")
    list_display_links = ("le_moment",)
    list_filter = ("action",)
    search_fields = ("objet", "quiz_titre", "auteur_nom")
    date_hierarchy = "quand"
    list_per_page = 50

    @admin.display(description="Date", ordering="quand")
    def le_moment(self, obj):
        return obj.quand.strftime("%d/%m/%Y %H:%M")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
