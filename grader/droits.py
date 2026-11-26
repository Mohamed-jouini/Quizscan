"""Les droits accordés compte par compte, et leur vérification.

Un enseignant peut recevoir séparément le droit de **créer des épreuves** et
celui de **corriger les copies** : un professeur qui prépare les sujets n'est
pas forcément celui qui dépouille, et l'inverse est vrai aussi. Depuis le 26
novembre 2026, trois gestes se donnent aussi seuls : **téléverser les copies
scannées**, **lancer la correction**, **saisir les questions** (et le corrigé) ;
le droit large qui les englobe les permet toujours.

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
from django.utils.translation import gettext

CREER = "grader.creer_epreuve"
CORRIGER = "grader.corriger_copies"
# Droits fins (26 novembre 2026, demande de l'utilisateur) : chacun se donne seul, à qui ne
# fait que ce geste (le secrétariat qui scanne, l'enseignant qui saisit son sujet…).
TELEVERSER = "grader.televerser_copies"
LANCER = "grader.lancer_correction"
QUESTIONS = "grader.saisir_questions"

# Un geste, et les droits qui le permettent : son droit fin, ou le droit large qui l'englobe.
# Les comptes d'avant gardent ainsi tout ce qu'ils faisaient : « créer des épreuves » permet
# toujours de saisir les questions, « corriger les copies » de téléverser et de lancer.
SAISIR_QUESTIONS = (QUESTIONS, CREER)
TELEVERSER_COPIES = (TELEVERSER, CORRIGER)
LANCER_CORRECTION = (LANCER, CORRIGER)


def a_le_droit(utilisateur, droit):
    """Le compte a-t-il ce droit, ou l'un de ceux d'un geste (tuple ci-dessus) ?"""
    return any(utilisateur.has_perm(d)
               for d in (droit if isinstance(droit, tuple) else (droit,)))


def peut_creer(utilisateur):
    """Droit de créer les concours et les quiz, d'en régler la fiche et d'en saisir les
    questions."""
    return utilisateur.has_perm(CREER)


def peut_corriger(utilisateur):
    """Droit de téléverser des copies, de lancer et de vérifier la correction."""
    return utilisateur.has_perm(CORRIGER)


def contexte(request):
    """Les gestes permis au compte connecté, pour les gabarits : droits_compte.questions,
    .televerser, .lancer (droit fin ou droit large), .creer, .corriger."""
    utilisateur = getattr(request, "user", None)
    if utilisateur is None or not utilisateur.is_authenticated:
        return {"droits_compte": {}}
    return {"droits_compte": {
        "creer": peut_creer(utilisateur), "corriger": peut_corriger(utilisateur),
        "questions": a_le_droit(utilisateur, SAISIR_QUESTIONS),
        "televerser": a_le_droit(utilisateur, TELEVERSER_COPIES),
        "lancer": a_le_droit(utilisateur, LANCER_CORRECTION)}}


# Pas de droit de retoucher la réponse lue sur un QCM : la case retenue est
# le constat de la lecture optique, et la changer change la note. Retiré à
# tous, administrateur compris (demande de l'utilisateur, 11 octobre 2026) —
# ni l'écran de vérification ni l'administration ne le permettent.


def exiger(utilisateur, droit, geste):
    """Refuse l'action si le droit manque, avec une phrase compréhensible.

    PermissionDenied ramène un compte connecté au tableau de bord (views.acces_refuse) ;
    le message explique quoi demander à l'administrateur à qui verrait la page 403.
    droit : un droit, ou les droits d'un geste (SAISIR_QUESTIONS…), l'un d'eux suffisant.
    """
    if not a_le_droit(utilisateur, droit):
        raise PermissionDenied(
            gettext("Votre compte n'a pas l'autorisation de %(geste)s. Demandez-la à l'administrateur de l'établissement.") % {"geste": geste})
