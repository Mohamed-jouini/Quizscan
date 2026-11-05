"""Écrans du personnel : registre des inscriptions, communiqués, et
publication des résultats d'une épreuve sur l'espace public."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import droits, importers, journal, pieces
from . import inscriptions as insc
from .forms import CandidateFileForm, CommuniqueForm, InscritForm
from .models import Communique, Inscrit, Student
from .views import (_concours_group_pks, _int_or_none, _my_classes, _my_quizzes,
                    _page)


def _peut_retirer(utilisateur, objet, champ):
    """Le créateur d'une fiche partagée, ou l'administrateur, la retire."""
    return utilisateur.is_superuser or getattr(objet, champ) == utilisateur.pk


def _filtrer(qs, recherche):
    """Chaque mot doit se trouver dans le nom, le prénom ou le n°."""
    return insc.filtrer_inscrits(qs, recherche)


def _retour(request):
    q = request.POST.get("q") or request.GET.get("q") or ""
    url = reverse("inscriptions")
    return redirect(f"{url}?q={q}" if q else url)


CLE_VERIFICATION = "verification_registre"


def _importer_au_registre(request, fichier):
    """La liste officielle de l'institut remplit le registre, sans toucher aux
    classes ni aux concours (sinon pour relier ceux de l'utilisateur à leur
    fiche). Le rapport reste en session pour la page, comme après un import
    dans une classe."""
    try:
        lignes = fichier.parse()
    except importers.ImportError_ as exc:
        messages.error(request, str(exc))
        return
    except Exception as exc:      # noqa: BLE001 — fichier corrompu
        messages.error(request, f"Fichier illisible : {exc}")
        return
    if not lignes:
        messages.error(request, "Aucune ligne trouvée dans ce fichier.")
        return
    if not any(insc._porte_une_fiche(ligne) for ligne in lignes):
        messages.error(request, "Ce fichier ne donne que des noms et des n° : il n'inscrit "
                                "personne au registre. Il lui faut aussi la CIN ou la date de "
                                "naissance. Une liste simple s'importe dans une classe ou un "
                                "concours (page Candidats).")
        return
    resultat, decomptes, nouvelles = insc.rapprocher(lignes, request.user)
    insc.rattacher_nouvelles(nouvelles, _my_classes(request))
    request.session[CLE_VERIFICATION] = insc.rapport_session(resultat, decomptes)
    messages.success(request, f"{len(nouvelles)} fiche(s) ajoutée(s) au registre, "
                              f"sur {len(lignes)} ligne(s) lue(s).")


@login_required
def modele_candidats(request):
    """La liste modèle à remplir, aux colonnes que l'import reconnaît."""
    reponse = HttpResponse(
        importers.modele_candidats_xlsx(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    reponse["Content-Disposition"] = 'attachment; filename="modele_candidats.xlsx"'
    return reponse


@login_required
def inscriptions(request):
    """Registre des inscriptions : inscrire (fiche complète, une par une, ou
    la liste officielle de l'institut), retrouver, ajouter à un concours ou à
    une classe, imprimer les cartes. Une liste simple (nom, prénom, n°) n'inscrit
    personne (demande de l'utilisateur, 30 octobre 2026) : seule une liste qui
    porte la CIN, la date de naissance ou la salle remplit le registre (5 novembre)."""
    # « Modifier » : la fiche d'un inscrit (CIN, date de naissance… à compléter)
    modifie = None
    pk_modifie = _int_or_none(request.GET.get("modifier") or request.POST.get("modifier"))
    if pk_modifie is not None:
        modifie = get_object_or_404(Inscrit, pk=pk_modifie)
        if not _peut_retirer(request.user, modifie, "cree_par_id"):
            raise PermissionDenied("Seul l'auteur de l'inscription ou "
                                   "l'administrateur peut la modifier.")
    form = InscritForm(instance=modifie)
    fichier = CandidateFileForm()
    if request.method == "POST":
        droits.exiger(request.user, droits.CREER, "modifier le registre des inscriptions")
        if "importer_liste" in request.POST:
            fichier = CandidateFileForm(request.POST, request.FILES)
            if fichier.is_valid():
                _importer_au_registre(request, fichier)
                return redirect("inscriptions")
        elif "fermer_verification" in request.POST:
            request.session.pop(CLE_VERIFICATION, None)
            return redirect("inscriptions")
        elif "inscrire" in request.POST:
            form = InscritForm(request.POST, instance=modifie)
            if form.is_valid():
                inscrit = form.save(commit=False)
                if modifie is None:
                    inscrit.cree_par = request.user
                inscrit.save()
                if modifie is None:
                    insc.rattacher_existants(inscrit, _my_classes(request))
                    messages.success(request, f"{inscrit.nom_complet} est inscrit(e) "
                                              f"sous le n° {inscrit.numero}. Sa carte "
                                              "avec QR code est prête.")
                else:
                    # le registre fait foi : les listes de classe et de
                    # concours reliées à cette inscription suivent — celles
                    # de l'utilisateur ; toutes pour l'administrateur
                    lies = Student.objects.filter(inscrit=inscrit)
                    if not request.user.is_superuser:
                        lies = lies.filter(class_group__in=_my_classes(request))
                    n = 0
                    for etudiant in lies:
                        avant = journal.identite(etudiant)
                        etudiant.last_name, etudiant.first_name = inscrit.nom, inscrit.prenom
                        etudiant.student_number = inscrit.numero
                        etudiant.save(update_fields=["last_name", "first_name",
                                                     "student_number"])
                        journal.etudiant_modifie(request.user, etudiant, avant)
                        n += 1
                    messages.success(request, f"Inscription de {inscrit.nom_complet} "
                                              "enregistrée"
                                     + (f" ({n} liste(s) de classe ou de concours "
                                        "mise(s) à jour)." if n else "."))
                return redirect(f"{reverse('inscriptions')}?q={inscrit.numero}")
        elif "supprimer" in request.POST:
            inscrit = get_object_or_404(Inscrit, pk=_int_or_none(request.POST["supprimer"]))
            if not _peut_retirer(request.user, inscrit, "cree_par_id"):
                raise PermissionDenied("Seul l'auteur de l'inscription ou "
                                       "l'administrateur peut la retirer.")
            nom = inscrit.nom_complet
            inscrit.delete()      # les classes et concours gardent leur candidat
            messages.success(request, f"Inscription de {nom} retirée du registre.")
            return _retour(request)
        elif "ajouter" in request.POST:
            groupe = get_object_or_404(_my_classes(request),
                                       pk=_int_or_none(request.POST.get("groupe")))
            choisis = Inscrit.objects.filter(
                pk__in=[p for p in map(_int_or_none, request.POST.getlist("choix"))
                        if p is not None])
            if not choisis:
                messages.error(request, "Cochez au moins un étudiant.")
                return _retour(request)
            ajoutes, deja = insc.ajouter_a_classe(groupe, choisis)
            msg = f"{ajoutes} étudiant(s) ajouté(s) à « {groupe.name} »"
            if deja:
                msg += f" ({deja} y figurai{'en' if deja > 1 else ''}t déjà)"
            messages.success(request, msg + ".")
            return _retour(request)

    recherche = " ".join(request.GET.get("q", "").split())[:100]
    qs = _filtrer(Inscrit.objects.annotate(n_places=Count("places"))
                  .order_by("nom", "prenom", "numero"), recherche)
    concours = set(_concours_group_pks(request))
    groupes = [(g, g.pk in concours) for g in _my_classes(request).order_by("name")]
    return render(request, "grader/inscriptions.html", {
        "form": form, "modifie": modifie, "fichier": fichier,
        "verification": request.session.get(CLE_VERIFICATION),
        "recherche": recherche, "page_inscrits": _page(request, qs),
        "querystring": urlencode({"q": recherche}) if recherche else "",
        "n_trouves": qs.count(), "total": Inscrit.objects.count(),
        "groupes": groupes,
    })


@login_required
def inscrit_qr(request, pk):
    """QR code de la carte d'inscription (n°, NOM, prénom)."""
    inscrit = get_object_or_404(Inscrit, pk=pk)
    return HttpResponse(insc.qr_png(inscrit), content_type="image/png")


@login_required
def inscriptions_cartes(request):
    """Cartes d'inscription en PDF : celles cochées, sinon celles de la
    recherche en cours (500 au plus)."""
    donnees = request.POST if request.method == "POST" else request.GET
    choix = [p for p in map(_int_or_none, donnees.getlist("choix")) if p is not None]
    if choix:
        qs = Inscrit.objects.filter(pk__in=choix)
    elif request.method == "POST":
        qs = Inscrit.objects.none()          # « cartes des cochés », rien de coché
    else:
        qs = _filtrer(Inscrit.objects.all(),
                      " ".join(donnees.get("q", "").split())[:100])
    inscrits = list(qs[:500])
    if not inscrits:
        messages.error(request, "Aucun étudiant à imprimer : cochez-en au moins un.")
        return _retour(request)
    resp = HttpResponse(insc.cartes_pdf(inscrits), content_type="application/pdf")
    resp["Content-Disposition"] = 'inline; filename="cartes_inscription.pdf"'
    return resp


# ------------------------------------------------------------ communiqués

@login_required
def communiques(request):
    """Avis publiés sur l'espace public (dates, convocations, salles…)."""
    modifie = None
    pk = _int_or_none(request.GET.get("modifier") or request.POST.get("modifier"))
    if pk is not None:
        modifie = get_object_or_404(Communique, pk=pk)
        if not _peut_retirer(request.user, modifie, "auteur_id"):
            raise PermissionDenied("Seul l'auteur du communiqué ou "
                                   "l'administrateur peut le modifier.")
    form = CommuniqueForm(instance=modifie)
    if request.method == "POST":
        droits.exiger(request.user, droits.CREER, "publier des communiqués")
        if "supprimer" in request.POST:
            avis = get_object_or_404(Communique, pk=_int_or_none(request.POST["supprimer"]))
            if not _peut_retirer(request.user, avis, "auteur_id"):
                raise PermissionDenied("Seul l'auteur du communiqué ou "
                                       "l'administrateur peut le supprimer.")
            avis.delete()
            messages.success(request, "Communiqué supprimé.")
            return redirect("communiques")
        form = CommuniqueForm(request.POST, request.FILES, instance=modifie)
        if form.is_valid():
            avis = form.save(commit=False)
            if avis.auteur_id is None:
                avis.auteur = request.user
            with transaction.atomic():
                avis.save()
                form.enregistrer_pieces(avis)
            messages.success(request, "Communiqué enregistré"
                             + (" et publié." if avis.publie else " (brouillon)."))
            return redirect("communiques")
    return render(request, "grader/communiques.html", {
        "form": form, "modifie": modifie, "pieces": pieces,
        "page_avis": _page(request, Communique.objects.select_related("auteur")
                           .prefetch_related("pieces")),
    })


# ------------------------------------------------------------ publication

def _etat_publication(quiz):
    parties = [nom for nom, oui in (("résultats", quiz.resultats_publies),
                                    ("liste des admis", quiz.admis_publies)) if oui]
    return " et ".join(parties) or "non publié"


@login_required
@require_POST
def quiz_publication(request, pk):
    """Publie (ou retire) les résultats d'une épreuve sur l'espace public."""
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    droits.exiger(request.user, droits.CORRIGER, "publier les résultats")
    avant = _etat_publication(quiz)
    quiz.resultats_publies = bool(request.POST.get("resultats_publies"))
    quiz.admis_publies = bool(request.POST.get("admis_publies"))
    if quiz.admis_publies and quiz.note_admission is None:
        quiz.admis_publies = False
        messages.error(request, "La liste des admis demande une note minimale : "
                                "indiquez-la d'abord.")
    if quiz.resultats_publies or quiz.admis_publies:
        quiz.publie_le = quiz.publie_le or timezone.now()
        messages.success(request, "Publication mise à jour : visible sur l'espace public.")
    else:
        quiz.publie_le = None
        messages.success(request, "Résultats retirés de l'espace public.")
    quiz.save(update_fields=["resultats_publies", "admis_publies", "publie_le"])
    journal.noter(request.user, "publication", quiz=quiz,
                  objet="Publication sur l'espace public",
                  avant=avant, apres=_etat_publication(quiz))
    return redirect(reverse("quiz_results", args=[pk]) + "#publication")
