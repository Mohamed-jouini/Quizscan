"""Les enseignants déjà en place gardent ce qu'ils savaient faire.

Avant cette version, tout compte connecté pouvait créer une épreuve et
corriger les copies. Les deux droits deviennent facultatifs : sans cette
migration, les enseignants existants perdraient en silence, du jour au
lendemain, l'accès à leur propre travail.

On les leur accorde donc tous les deux. L'administrateur les retire ensuite
compte par compte, en connaissance de cause — c'est le sens de la demande :
choisir qui peut faire quoi, pas priver tout le monde par défaut.

Subtilité : les lignes Permission ne sont PAS créées par la migration qui
déclare les permissions, mais par le signal `post_migrate` de contrib.auth,
qui ne s'exécute qu'une fois toutes les migrations passées. Se contenter de
les chercher ici ne trouve rien. On les crée donc soi-même ; `create_perm-
issions` les retrouvera ensuite et ne fera rien de plus.
"""
from django.conf import settings
from django.db import migrations

DROITS = [
    ("creer_epreuve",
     "Créer et modifier des épreuves (quiz, concours, questions)"),
    ("corriger_copies",
     "Téléverser les copies, lancer la correction et la vérifier"),
]


def _modele_utilisateur(apps):
    return apps.get_model(*settings.AUTH_USER_MODEL.split("."))


def _permissions(apps):
    """Les deux permissions, créées si le signal n'est pas encore passé."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    type_quiz, _ = ContentType.objects.get_or_create(
        app_label="grader", model="quiz")
    obtenues = []
    for code, intitule in DROITS:
        permission, _ = Permission.objects.get_or_create(
            codename=code, content_type=type_quiz,
            defaults={"name": intitule})
        obtenues.append(permission)
    return obtenues


def accorder(apps, schema_editor):
    User = _modele_utilisateur(apps)
    permissions = _permissions(apps)
    for compte in User.objects.filter(is_superuser=False):
        compte.user_permissions.add(*permissions)


def retirer(apps, schema_editor):
    User = _modele_utilisateur(apps)
    Permission = apps.get_model("auth", "Permission")
    permissions = list(Permission.objects.filter(
        codename__in=[code for code, _ in DROITS],
        content_type__app_label="grader"))
    if not permissions:
        return
    for compte in User.objects.all():
        compte.user_permissions.remove(*permissions)


class Migration(migrations.Migration):

    dependencies = [
        ("grader", "0016_alter_quiz_options"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(accorder, retirer),
    ]
