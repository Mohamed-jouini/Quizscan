from django.contrib import admin

from . import useradmin
from .forms import AdminLoginForm

# useradmin n'est importé que pour son effet : il remplace l'écran des
# comptes livré par Django (voir grader/useradmin.py).
__all__ = ["useradmin"]
from .models import (Answer, AnswerKeySheet, ClassGroup, Question, Quiz,
                     ScanBatch, SheetScan, Student)

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


@admin.register(ClassGroup)
class ClassGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "nb_etudiants", "created_at")
    list_filter = ("owner",)
    search_fields = ("name",)
    list_select_related = ("owner",)

    @admin.display(description="Étudiants")
    def nb_etudiants(self, obj):
        return obj.students.count()


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
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
class QuizAdmin(admin.ModelAdmin):
    list_display = ("title", "class_group", "type_de_fiche", "identification",
                    "created_at")
    list_filter = ("owner", "language", "auto_enroll",
                   _filtre_court("sheet_mode", "Type de fiche", SHEET_MODE_COURT),
                   _filtre_court("id_mode", "Identification", ID_MODE_COURT))
    search_fields = ("title",)
    list_select_related = ("class_group", "owner")
    raw_id_fields = ("class_group",)
    inlines = [QuestionInline]

    @admin.display(description="Type de fiche", ordering="sheet_mode")
    def type_de_fiche(self, obj):
        return SHEET_MODE_COURT.get(obj.sheet_mode, obj.sheet_mode)

    @admin.display(description="Identification", ordering="id_mode")
    def identification(self, obj):
        return ID_MODE_COURT.get(obj.id_mode, obj.id_mode)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ("quiz", "order", "qtype", "text", "points")
    list_filter = ("qtype", "quiz__owner")
    search_fields = ("text",)
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)


@admin.register(ScanBatch)
class ScanBatchAdmin(admin.ModelAdmin):
    list_display = ("__str__", "quiz", "status", "processed_pages", "total_pages",
                    "created_at")
    list_filter = ("status", "quiz__owner")
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)


@admin.register(SheetScan)
class SheetScanAdmin(admin.ModelAdmin):
    list_display = ("__str__", "batch", "student", "status", "id_read")
    list_filter = ("status", "batch__quiz__owner")
    search_fields = ("source_name", "ocr_name_raw", "id_read")
    list_select_related = ("batch", "student")
    raw_id_fields = ("batch", "student")


@admin.register(AnswerKeySheet)
class AnswerKeySheetAdmin(admin.ModelAdmin):
    """Corrigés scannés — conservés en lecture seule : ce sont des pièces
    justificatives de l'origine du barème, pas des données à retoucher."""
    list_display = ("quiz", "source_name", "page_index", "nb_lues", "status",
                    "created_at")
    list_filter = ("status", "quiz__owner")
    search_fields = ("source_name", "quiz__title")
    list_select_related = ("quiz",)
    raw_id_fields = ("quiz",)
    readonly_fields = ("quiz", "page_index", "source_name", "image",
                       "overlay_image", "status", "detected", "unreadable",
                       "error_message", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ("sheet", "question", "detected_letter", "points_awarded",
                    "manually_set")
    list_filter = ("is_multiple", "manually_set", "question__qtype")
    list_select_related = ("sheet", "question__quiz")
    # un concours produit une réponse par question et par copie, soit des
    # dizaines de milliers de lignes : jamais de liste déroulante ici
    raw_id_fields = ("sheet", "question")

    @admin.display(description="Réponse détectée")
    def detected_letter(self, obj):
        return obj.detected_letter
