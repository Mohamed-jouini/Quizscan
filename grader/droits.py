"""Les deux droits accordés compte par compte, et leur vérification.

Un enseignant peut recevoir séparément le droit de **créer des épreuves** et
celui de **corriger les copies** : un professeur qui prépare les sujets n'est
pas forcément celui qui dépouille, et l'inverse est vrai aussi.

Troisième règle, celle-ci non négociable : **personne, hors administrateur,
ne modifie une note de QCM**. Le QCM est corrigé par lecture optique, et sa
note est le constat de ce qui est coché. Laisser un enseignant la retoucher
ôterait tout sens à la correction automatique et au recours d'un candidat.
Les réponses manuscrites, elles, n'ont pas de bonne réponse mécanique : leur
notation est le travail de l'enseignant et reste ouverte.

Les noms sont ceux de Django (`application.codename`) : les vérifications
passent donc par `user.has_perm`, qui répond toujours oui à un super-
utilisateur. L'administrateur n'a pas de cas particulier à écrire.
"""
from django.core.exceptions import PermissionDenied

CREER = "grader.creer_epreuve"
CORRIGER = "grader.corriger_copies"


def peut_creer(utilisateur):
    """Droit de créer une épreuve et d'en modifier les questions."""
    return utilisateur.has_perm(CREER)


def peut_corriger(utilisateur):
    """Droit de téléverser des copies, de lancer et de vérifier la correction."""
    return utilisateur.has_perm(CORRIGER)


# Pas de droit de retoucher la réponse lue sur un QCM : la case retenue est
# le constat de la lecture optique, et la changer change la note. Retiré à
# tous, administrateur compris (demande de l'utilisateur, 11 octobre 2026) —
# ni l'écran de vérification ni l'administration ne le permettent.


def exiger(utilisateur, droit, geste):
    """Refuse l'action si le droit manque, avec une phrase compréhensible.

    PermissionDenied donne une page 403 ; le message explique quoi demander
    à l'administrateur plutôt que de laisser l'enseignant deviner.
    """
    if not utilisateur.has_perm(droit):
        raise PermissionDenied(
            f"Votre compte n'a pas l'autorisation de {geste}. "
            "Demandez-la à l'administrateur de l'établissement.")
