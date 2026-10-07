from django.apps import AppConfig
from django.db.models.signals import post_migrate

# Django crée le nom de chaque permission en anglais, une fois pour toutes,
# au moment de la migration : « Can add log entry », « Can change classe »…
# Ces noms s'affichent tels quels dans la fiche d'un compte. On les renomme
# après chaque migration — y compris ceux des tables ajoutées plus tard.
VERBES = {"add": "Ajouter", "change": "Modifier", "delete": "Supprimer",
          "view": "Consulter"}


def franciser_permissions(**kwargs):
    """Renomme « Can add … » en « Ajouter », etc.

    Le nom de la table n'est pas répété : l'écran affiche déjà
    « Application | table | permission ».
    """
    from django.contrib.auth.models import Permission

    a_renommer = []
    for permission in Permission.objects.select_related("content_type"):
        action, _, reste = permission.codename.partition("_")
        if action not in VERBES or reste != permission.content_type.model:
            continue                   # permission propre à une application
        if permission.name != VERBES[action]:
            permission.name = VERBES[action]
            a_renommer.append(permission)
    if a_renommer:
        Permission.objects.bulk_update(a_renommer, ["name"])


class GraderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "grader"
    # sans cela, l'administration affiche « GRADER » en en-tête de section
    verbose_name = "Épreuves et copies"

    def ready(self):
        # Sans « sender » : chaque application crée ses permissions dans son
        # propre post_migrate ; on repasse après chacune (quelques dizaines de
        # lignes, l'opération est sans effet quand tout est déjà en français).
        post_migrate.connect(franciser_permissions,
                             dispatch_uid="grader.franciser_permissions")
