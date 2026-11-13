"""Espace public : ce que tout le monde voit, sans compte.

  - les communiqués publiés (dates d'examen, convocations, salles…) ;
  - les épreuves dont les résultats sont publiés, et pour un concours la
    liste des admis si elle est publiée ;
  - le résultat d'un étudiant, retrouvé par son n° d'inscription (choix de
    l'établissement : le n° suffit, comme pour les résultats du bac) ou par son
    n° de CIN, envoyé par le formulaire et jamais dans l'adresse de la page. Le
    résultat seul, « Admis » ou « Non admis », jamais la note (13 novembre 2026).

Rien n'y est visible tant que le personnel n'a pas publié (Quiz.resultats_publies,
Quiz.admis_publies, Communique.publie). Les recherches par n° sont freinées
par adresse pour décourager l'essai de tous les numéros à la suite.
"""
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from . import pieces, services
from .inscriptions import cin_propre, cin_valide, numero_propre
from .models import Communique, Inscrit, PieceJointe, Quiz, Student
from .securite import adresse_client
from .views import _page

LIMITE_RECHERCHES = 30          # par adresse…
FENETRE_S = 10 * 60             # …et par tranche de 10 minutes


def _adresse(request):
    return adresse_client(request)


def _trop_de_recherches(request):
    cle = f"qs-public-recherche:{_adresse(request)}"
    n = cache.get(cle, 0)
    if n >= LIMITE_RECHERCHES:
        return True
    cache.set(cle, n + 1, FENETRE_S)
    return False


def _epreuves_publiees():
    return (Quiz.objects.filter(Q(resultats_publies=True) | Q(admis_publies=True))
            .select_related("class_group").order_by("-publie_le", "-pk"))


def accueil(request):
    # Un concours en étapes n'est qu'une ligne, à la place de son étape publiée la dernière.
    epreuves = list(_epreuves_publiees()[:30])
    rangs = {q.pk: rang for rang, q in enumerate(epreuves)}
    groupes = services.groupes_epreuves(epreuves)
    for groupe in groupes:
        groupe["rang"] = min(rangs[etape["quiz"].pk] for etape in groupe["etapes"])
        groupe["publie_le"] = epreuves[groupe["rang"]].publie_le
    return render(request, "public/accueil.html", {
        "avis": Communique.objects.filter(publie=True).prefetch_related("pieces")[:4],
        "n_avis": Communique.objects.filter(publie=True).count(),
        "groupes": sorted(groupes, key=lambda groupe: groupe["rang"]),
    })


def _resultat(quiz, etudiant):
    """Note d'un étudiant à une épreuve, ou None s'il n'a pas de copie."""
    rows = services.compute_results(quiz, student=etudiant)
    if not rows:
        return None
    row = rows[0]
    services.admissions(quiz, rows)          # pose row["resultat"] (ou None)
    return row


def _etudiants_publies(cible):
    return (Student.objects.filter(cible, class_group__quizzes__resultats_publies=True)
            .distinct().select_related("class_group"))


def _par_cin(saisie):
    """Fiches d'étudiant de l'inscrit dont c'est la CIN (registre), sinon aucune. Une CIN
    compte 8 chiffres : une saisie plus courte est un n° d'inscription, sans zéro ajouté
    (un n° de 7 chiffres complété d'un zéro pourrait être la CIN d'un autre)."""
    cin = cin_propre(saisie)
    if len(cin) < 8 or not cin_valide(cin):
        return Student.objects.none()
    cible = Q(pk__in=[])
    for pk, numero in Inscrit.objects.filter(cin=cin).values_list("pk", "numero"):
        cible |= Q(inscrit_id=pk) | Q(student_number=numero)
    return _etudiants_publies(cible)


def resultat(request):
    """Résultats d'un étudiant, par son n° d'inscription ou par son n° de CIN (demande
    de l'utilisateur, 7 novembre 2026) : un bloc par concours, ses étapes publiées à
    l'intérieur (chaque étape a sa fiche d'étudiant, sous le même n°).

    Le formulaire s'envoie en POST : rien de ce qui est tapé ne s'inscrit dans l'adresse
    de la page (historique, journaux du serveur). La CIN ne se reconnaît que par lui ; un
    lien « ?numero= » (GET) ne cherche que le n° d'inscription, comme avant. Le n°
    d'inscription passe en premier : la CIN n'est cherchée que s'il ne donne rien."""
    par_formulaire = request.method == "POST"
    saisie = (request.POST if par_formulaire else request.GET).get("numero") or ""
    numero = numero_propre(saisie)
    contexte = {"numero": numero, "groupes": [], "identite": None, "trop": False}
    if numero:
        if _trop_de_recherches(request):
            contexte["trop"] = True
        else:
            etudiants = _etudiants_publies(Q(student_number=numero))
            if par_formulaire and not etudiants.exists():
                etudiants = _par_cin(saisie)
            epreuves, notes = [], {}
            for etudiant in etudiants:
                for quiz in (etudiant.class_group.quizzes
                             .filter(resultats_publies=True)
                             .order_by("-publie_le", "-pk")):
                    row = _resultat(quiz, etudiant)
                    if row is not None:
                        epreuves.append(quiz)
                        notes[quiz.pk] = row
                        contexte["identite"] = contexte["identite"] or etudiant
            contexte["groupes"] = services.groupes_epreuves(epreuves)
            for groupe in contexte["groupes"]:
                for etape in groupe["etapes"]:
                    etape["row"] = notes[etape["quiz"].pk]
    return render(request, "public/resultat.html", contexte)


def _liste_admis(quiz):
    """Admis (n°, prénom, nom), par n° d'inscription, gardés 5 minutes.

    Ni rang ni note : le candidat n'apprend que « Admis » ou « Non admis » (demande de
    l'utilisateur, 13 novembre 2026). L'ordre de mérite dirait le rang : la liste suit le
    n° d'inscription, où chacun cherche le sien. La liste complète, rang et notes, reste
    au personnel (page des résultats, PDF, Excel)."""
    cle = f"qs-public-admis5:{quiz.pk}:{quiz.publie_le and quiz.publie_le.timestamp()}"
    liste = cache.get(cle)
    if liste is None:
        admission = services.admissions(quiz, services.compute_results(quiz))
        liste = [{"numero": r["student"].student_number or "",
                  "nom": r["student"].last_name, "prenom": r["student"].first_name}
                 for r in (admission["admis"] if admission else [])]
        # n° d'inscription dans l'ordre des nombres (« 9 » avant « 10 »), sans n° à la fin
        liste.sort(key=lambda a: (not a["numero"], len(a["numero"]), a["numero"],
                                  a["nom"].lower(), a["prenom"].lower()))
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
