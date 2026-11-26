"""Les droits accordés compte par compte, et leur vérification.

Depuis le 26 novembre 2026 (demande de l'utilisateur : « donne la main de choisir Ajouter,
Modifier, Supprimer, Vérifier »), ce qu'un compte peut faire se règle dans un **tableau** :
une ligne par domaine (concours et quiz, questions et corrigé, copies, candidats et
inscriptions, communiqués), une case par geste (ajouter, modifier, supprimer, vérifier),
plus « lancer la correction ». Chaque case est une permission Django.

Les droits d'avant valent toujours, comme des paquets de cases : **créer des épreuves**
(tout sauf les copies), **corriger les copies** (les copies et la correction), **saisir les
questions** (la ligne des questions). Aucun compte en place ne perd rien ; décocher une case
d'un paquet le remplace par ses autres cases, une à une (grader/useradmin.py).

Règle non négociable : **personne, hors administrateur, ne modifie une note de QCM**. Le QCM
est corrigé par lecture optique, et sa note est le constat de ce qui est coché. Laisser un
enseignant la retoucher ôterait tout sens à la correction automatique et au recours d'un
candidat. Les réponses manuscrites, elles, n'ont pas de bonne réponse mécanique : leur
notation est le travail de l'enseignant et reste ouverte (« copies — modifier »).

Les noms sont ceux de Django (`application.codename`) : les vérifications passent donc par
`user.has_perm`, qui répond toujours oui à un super-utilisateur. L'administrateur n'a pas de
cas particulier à écrire.
"""
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext, gettext_lazy

# Les paquets : les droits d'avant le tableau (et du matin du 26 novembre 2026)
CREER = "grader.creer_epreuve"
CORRIGER = "grader.corriger_copies"
QUESTIONS = "grader.saisir_questions"
TELEVERSER = "grader.televerser_copies"     # c'est aussi la case « copies — ajouter »
LANCER = "grader.lancer_correction"         # et la case « lancer la correction »

# Le tableau : (domaine, son libellé, ses gestes), dans l'ordre de l'écran
GESTES = ("ajouter", "modifier", "supprimer", "verifier")
LIBELLES_GESTES = {"ajouter": gettext_lazy("Ajouter"), "modifier": gettext_lazy("Modifier"),
                   "supprimer": gettext_lazy("Supprimer"), "verifier": gettext_lazy("Vérifier")}
TABLEAU = (
    ("epreuve", gettext_lazy("Concours et quiz"), ("ajouter", "modifier")),
    ("question", gettext_lazy("Questions et corrigé"), ("ajouter", "modifier", "supprimer")),
    ("copie", gettext_lazy("Copies"), ("ajouter", "modifier", "supprimer", "verifier")),
    ("candidat", gettext_lazy("Candidats et inscriptions"), ("ajouter", "modifier", "supprimer")),
    ("communique", gettext_lazy("Communiqués"), ("ajouter", "modifier", "supprimer")),
)


def code_case(domaine, geste):
    """Le codename de la permission d'une case. « Copies — ajouter » est l'ancien droit de
    téléverser les copies scannées : les comptes qui l'ont déjà gardent la case cochée."""
    return "televerser_copies" if (domaine, geste) == ("copie", "ajouter") else f"{domaine}_{geste}"


# Toutes les cases, et « lancer la correction » en dernier
CASES = [code_case(d, g) for d, _, gestes in TABLEAU for g in gestes] + ["lancer_correction"]

# Ce que coche chaque paquet
PAQUETS = {
    "creer_epreuve": [code_case(d, g) for d, _, gestes in TABLEAU if d != "copie"
                      for g in gestes],
    "corriger_copies": [code_case("copie", g) for g in GESTES] + ["lancer_correction"],
    "saisir_questions": [code_case("question", g) for g in ("ajouter", "modifier",
                                                             "supprimer")],
}

# Ce que permet chaque case, sous le tableau et en infobulle
AIDE = {
    "epreuve_ajouter": gettext_lazy("créer un concours ou un quiz, et l'étape suivante d'un "
                                    "concours"),
    "epreuve_modifier": gettext_lazy("régler la fiche, produire les PDF, la note minimale, "
                                     "les coefficients, faire passer les admis"),
    "question_ajouter": gettext_lazy("une à une, ajout rapide, import, banque de questions"),
    "question_modifier": gettext_lazy("la bonne réponse et le barème, le corrigé scanné"),
    "question_supprimer": gettext_lazy("des questions, un corrigé scanné"),
    "televerser_copies": gettext_lazy("téléverser les scans des copies"),
    "copie_modifier": gettext_lazy("noter les réponses manuscrites et les notes d'écrit, "
                                   "importer les notes d'oral — jamais la note d'un QCM"),
    "copie_supprimer": gettext_lazy("un lot de copies, des notes d'oral importées"),
    "copie_verifier": gettext_lazy("vérifier les copies, affecter une copie non identifiée, "
                                   "publier les résultats"),
    "candidat_ajouter": gettext_lazy("créer une liste, importer des candidats, inscrire au "
                                     "registre"),
    "candidat_modifier": gettext_lazy("une fiche du registre (les siennes)"),
    "candidat_supprimer": gettext_lazy("des candidats sans copie, les absents, une fiche du "
                                       "registre (les siennes)"),
    "communique_ajouter": gettext_lazy("publier un communiqué"),
    "communique_modifier": gettext_lazy("ses communiqués"),
    "communique_supprimer": gettext_lazy("ses communiqués"),
    "lancer_correction": gettext_lazy("lancer ou reprendre la correction, identifier "
                                      "automatiquement les pages d'un lot"),
}


def droits_de_case(code):
    """Les permissions qui ouvrent une case : la sienne, ou un paquet qui la contient."""
    return (f"grader.{code}",) + tuple(f"grader.{p}" for p, cases in PAQUETS.items()
                                       if code in cases)


def _geste(domaine, geste):
    return droits_de_case(code_case(domaine, geste))


# Les gestes, tels que les vues les exigent (l'un des droits du tuple suffit)
EPREUVE_AJOUTER = _geste("epreuve", "ajouter")
EPREUVE_MODIFIER = _geste("epreuve", "modifier")
QUESTION_AJOUTER = _geste("question", "ajouter")
QUESTION_MODIFIER = _geste("question", "modifier")
QUESTION_SUPPRIMER = _geste("question", "supprimer")
COPIE_AJOUTER = _geste("copie", "ajouter")
COPIE_MODIFIER = _geste("copie", "modifier")
COPIE_SUPPRIMER = _geste("copie", "supprimer")
COPIE_VERIFIER = _geste("copie", "verifier")
CANDIDAT_AJOUTER = _geste("candidat", "ajouter")
CANDIDAT_MODIFIER = _geste("candidat", "modifier")
CANDIDAT_SUPPRIMER = _geste("candidat", "supprimer")
COMMUNIQUE_AJOUTER = _geste("communique", "ajouter")
COMMUNIQUE_MODIFIER = _geste("communique", "modifier")
COMMUNIQUE_SUPPRIMER = _geste("communique", "supprimer")
LANCER_CORRECTION = droits_de_case("lancer_correction")


def a_le_droit(utilisateur, droit):
    """Le compte a-t-il ce droit, ou l'un de ceux d'un geste (tuple ci-dessus) ?"""
    return any(utilisateur.has_perm(d)
               for d in (droit if isinstance(droit, tuple) else (droit,)))


def cases_permises(utilisateur):
    """Les cases du tableau que le compte a, par elles-mêmes ou par un paquet."""
    return [code for code in CASES if a_le_droit(utilisateur, droits_de_case(code))]


def peut_creer(utilisateur):
    """Le paquet d'avant « créer des épreuves »."""
    return utilisateur.has_perm(CREER)


def peut_corriger(utilisateur):
    """Le paquet d'avant « corriger les copies »."""
    return utilisateur.has_perm(CORRIGER)


def contexte(request):
    """Les gestes permis au compte connecté, pour les gabarits : droits_compte.epreuve_ajouter,
    .question_modifier, .copie_ajouter, .candidat_supprimer… et .lancer."""
    utilisateur = getattr(request, "user", None)
    if utilisateur is None or not utilisateur.is_authenticated:
        return {"droits_compte": {}}
    permises = set(cases_permises(utilisateur))
    gestes = {f"{d}_{g}": code_case(d, g) in permises
              for d, _, liste in TABLEAU for g in liste}
    gestes["lancer"] = "lancer_correction" in permises
    return {"droits_compte": gestes}


# Pas de droit de retoucher la réponse lue sur un QCM : la case retenue est
# le constat de la lecture optique, et la changer change la note. Retiré à
# tous, administrateur compris (demande de l'utilisateur, 11 octobre 2026) —
# ni l'écran de vérification ni l'administration ne le permettent.


def exiger(utilisateur, droit, geste):
    """Refuse l'action si le droit manque, avec une phrase compréhensible.

    PermissionDenied ramène un compte connecté au tableau de bord (views.acces_refuse) ;
    le message explique quoi demander à l'administrateur à qui verrait la page 403.
    droit : un droit, ou les droits d'un geste (QUESTION_AJOUTER…), l'un d'eux suffisant.
    """
    if not a_le_droit(utilisateur, droit):
        raise PermissionDenied(
            gettext("Votre compte n'a pas l'autorisation de %(geste)s. Demandez-la à l'administrateur de l'établissement.") % {"geste": geste})
