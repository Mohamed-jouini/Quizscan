"""Espace public : ce que tout le monde voit, sans compte.

  - les communiqués publiés (dates d'examen, convocations, salles…) ;
  - les épreuves dont les résultats sont publiés, et pour un concours la
    liste des admis si elle est publiée ;
  - la note d'un étudiant, retrouvée par son n° d'inscription (choix de
    l'établissement : le n° suffit, comme pour les résultats du bac).

Rien n'y est visible tant que le personnel n'a pas publié (Quiz.resultats_publies,
Quiz.admis_publies, Communique.publie). Les recherches par n° sont freinées
par adresse pour décourager l'essai de tous les numéros à la suite.
"""
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from . import pieces, services
from .inscriptions import numero_propre
from .models import Communique, PieceJointe, Quiz, Student
from .views import _page

LIMITE_RECHERCHES = 30          # par adresse…
FENETRE_S = 10 * 60             # …et par tranche de 10 minutes


def _adresse(request):
    transmise = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return transmise or request.META.get("REMOTE_ADDR", "")


def _trop_de_recherches(request):
    cle = f"qs-public-recherche:{_adresse(request)}"
    n = cache.get(cle, 0)
    if n >= LIMITE_RECHERCHES:
        return True
    cache.set(cle, n + 1, FENETRE_S)
    return False


def _epreuves_publiees():
    return (Quiz.objects.filter(Q(resultats_publies=True) | Q(admis_publies=True))
            .order_by("-publie_le", "-pk"))


def accueil(request):
    return render(request, "public/accueil.html", {
        "avis": Communique.objects.filter(publie=True).prefetch_related("pieces")[:4],
        "n_avis": Communique.objects.filter(publie=True).count(),
        "epreuves": _epreuves_publiees()[:30],
    })


def _resultat(quiz, etudiant):
    """Note d'un étudiant à une épreuve, ou None s'il n'a pas de copie."""
    rows = services.compute_results(quiz, student=etudiant)
    if not rows:
        return None
    row = rows[0]
    services.admissions(quiz, rows)          # pose row["resultat"] (ou None)
    return row


def resultat(request):
    """Résultats d'un étudiant, par son n° d'inscription."""
    numero = numero_propre(request.GET.get("numero"))
    contexte = {"numero": numero, "lignes": [], "identite": None, "trop": False}
    if numero:
        if _trop_de_recherches(request):
            contexte["trop"] = True
        else:
            etudiants = (Student.objects
                         .filter(student_number=numero,
                                 class_group__quizzes__resultats_publies=True)
                         .distinct().select_related("class_group"))
            for etudiant in etudiants:
                for quiz in (etudiant.class_group.quizzes
                             .filter(resultats_publies=True)
                             .order_by("-publie_le", "-pk")):
                    row = _resultat(quiz, etudiant)
                    if row is not None:
                        contexte["lignes"].append({"quiz": quiz, "row": row})
                        contexte["identite"] = contexte["identite"] or etudiant
    return render(request, "public/resultat.html", contexte)


def _liste_admis(quiz):
    """Admis par ordre de mérite (rang, n°, nom, prénom), gardés 5 minutes."""
    cle = f"qs-public-admis3:{quiz.pk}:{quiz.publie_le and quiz.publie_le.timestamp()}"
    liste = cache.get(cle)
    if liste is None:
        admission = services.admissions(quiz, services.compute_results(quiz))
        liste = [{"rang": r["rang"], "numero": r["student"].student_number,
                  "nom": r["student"].last_name, "prenom": r["student"].first_name,
                  "correct": r["correct"], "n_qcm": r["n_qcm"], "pct": r["pct"],
                  "note": r["score"], "sur": r["max_score"]}
                 for r in (admission["admis"] if admission else [])]
        cache.set(cle, liste, 300)
    return liste


def epreuve(request, pk):
    quiz = get_object_or_404(_epreuves_publiees(), pk=pk)
    admis = None
    if quiz.admis_publies and quiz.note_admission is not None:
        admis = _liste_admis(quiz)
    return render(request, "public/epreuve.html", {
        "quiz": quiz, "admis": admis,
        "admis_page": _page(request, admis, per_page=100) if admis else None,
    })


def communiques(request):
    return render(request, "public/communiques.html", {
        "page_avis": _page(request, Communique.objects.filter(publie=True)
                           .prefetch_related("pieces"), per_page=10),
    })


def communique(request, pk):
    return render(request, "public/communique.html", {
        "avis": get_object_or_404(Communique.objects.prefetch_related("pieces"),
                                  pk=pk, publie=True),
    })


def piece_jointe(request, pk, nom=""):
    """Fichier joint à un communiqué : au public si le communiqué est publié,
    au personnel connecté même en brouillon (aperçu avant publication)."""
    piece = get_object_or_404(PieceJointe.objects.select_related("communique"), pk=pk)
    if not (piece.communique.publie or request.user.is_authenticated):
        raise Http404("Fichier introuvable")
    return pieces.servir(request, piece)
