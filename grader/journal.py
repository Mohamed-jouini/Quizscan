"""Écriture du journal des modifications (voir models.Modification).

Toute vue ou tout écran d'administration qui change une note, l'attribution
d'une copie ou une bonne réponse passe par `noter`. Une seule fonction, pour
que la règle soit la même partout : on n'écrit que ce qui a réellement
changé, avec l'auteur, l'ancienne et la nouvelle valeur.
"""
from .models import Modification, choice_letter


def texte(valeur):
    """Valeur lisible : « — » pour vide, nombres à la française."""
    if valeur is None or valeur == "":
        return "—"
    if isinstance(valeur, float):
        return f"{valeur:g}".replace(".", ",")
    return str(valeur)[:200]


def lettre(question, case):
    """Lettre d'une case de QCM, dans la langue de l'épreuve."""
    if case is None:
        return "—"
    return choice_letter(case, question.quiz.language) or str(case)


def libelle_copie(copie):
    return f"{copie.source_name} (page {copie.page_index + 1})"


def libelle_candidat(etudiant):
    return etudiant.full_name if etudiant else "non attribuée"


def noter(utilisateur, action, *, objet, avant=None, apres=None,
          quiz=None, copie=None):
    """Enregistre une modification, si elle en est une.

    Renvoie l'entrée créée, ou None quand l'ancienne et la nouvelle valeur
    sont identiques : un formulaire renvoyé tel quel ne doit pas remplir le
    journal de non-événements, qui noieraient les vrais.
    """
    avant, apres = texte(avant), texte(apres)
    if avant == apres:
        return None
    if quiz is None and copie is not None:
        quiz = copie.batch.quiz
    nom = ""
    if utilisateur is not None and getattr(utilisateur, "is_authenticated", False):
        nom = utilisateur.get_full_name() or utilisateur.get_username()
    return Modification.objects.create(
        auteur=utilisateur if nom else None,
        auteur_nom=nom or "système",
        action=action,
        quiz=quiz,
        quiz_titre=quiz.title if quiz else "",
        copie=copie,
        objet=str(objet)[:200],
        avant=avant,
        apres=apres,
    )


# ------------------------------------------------------------------ gestes
# Fonctions partagées par les vues et par l'administration : un même geste
# s'inscrit de la même façon, d'où qu'il vienne.

def question_modifiee(utilisateur, question, *, bonne_avant, bareme_avant):
    """Bonne réponse et barème d'une question, comparés à leur valeur d'avant."""
    quiz, objet = question.quiz, f"Question {question.order}"
    noter(utilisateur, "bonne_reponse", quiz=quiz, objet=objet,
          avant=lettre(question, bonne_avant),
          apres=lettre(question, question.correct_choice))
    noter(utilisateur, "bareme", quiz=quiz, objet=objet,
          avant=bareme_avant, apres=question.points)


def question_supprimee(utilisateur, question):
    """Supprimer une question retire ses points de toutes les copies."""
    noter(utilisateur, "question_supprimee", quiz=question.quiz,
          objet=f"Question {question.order}",
          avant=f"{question.get_qtype_display()}, {texte(question.points)} pt",
          apres="supprimée")


def attribution(utilisateur, copie, ancien):
    noter(utilisateur, "identification", copie=copie,
          objet=libelle_copie(copie),
          avant=libelle_candidat(ancien),
          apres=libelle_candidat(copie.student))


def reponse_modifiee(utilisateur, reponse, *, case_avant, multiple_avant,
                     note_avant):
    """Case lue et note d'une réponse (rectification par l'administration)."""
    copie, question = reponse.sheet, reponse.question
    objet = f"{libelle_copie(copie)} — question {question.order}"
    noter(utilisateur, "case_qcm", copie=copie, objet=objet,
          avant="plusieurs cases" if multiple_avant else lettre(question, case_avant),
          apres="plusieurs cases" if reponse.is_multiple
          else lettre(question, reponse.detected_choice))
    noter(utilisateur, "note", copie=copie, objet=objet,
          avant=note_avant, apres=reponse.points_awarded)


def copie_supprimee(utilisateur, copie):
    noter(utilisateur, "copie_supprimee", quiz=copie.batch.quiz,
          objet=libelle_copie(copie),
          avant=libelle_candidat(copie.student), apres="supprimée")
