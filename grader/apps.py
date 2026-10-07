from django.apps import AppConfig


class GraderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "grader"
    # sans cela, l'administration affiche « GRADER » en en-tête de section
    verbose_name = "Épreuves et copies"
