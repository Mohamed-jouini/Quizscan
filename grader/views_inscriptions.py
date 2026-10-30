"""Écrans du personnel : registre des inscriptions, communiqués, et
publication des résultats d'une épreuve sur l'espace public."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import droits
from . import inscriptions as insc
from .forms import CommuniqueForm, InscritForm
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


@login_required
def inscriptions(request):
    """Registre des inscriptions : inscrire (fiche complète, une par une),
    retrouver, ajouter à un concours ou à une classe, imprimer les cartes.
    Plus d'inscription par liste (demande de l'utilisateur, 30 octobre 2026) :
    une liste ne porte ni CIN ni date de naissance."""
    # « Modifier » : la fiche d'un inscrit (CIN, date de naissance… à compléter)
    modifie = None
    pk_modifie = _int_or_none(request.GET.get("modifier") or request.POST.get("modifier"))
    if pk_modifie is not None:
        modifie = get_object_or_404(Inscrit, pk=pk_modifie)
        if not _peut_retirer(request.user, modifie, "cree_par_id"):
            raise PermissionDenied("Seul l'auteur de l'inscription ou "
                                   "l'administrateur peut la modifier.")
    form = InscritForm(instance=modifie)
    if request.method == "POST":
        if "inscrire" in request.POST:
            form = InscritForm(request.POST, instance=modifie)
            if form.is_valid():
                inscrit = form.save(commit=False)
                if modifie is None:
                    inscrit.cree_par = request.user
                inscrit.save()
                if modifie is None:
                    insc.rattacher_existants(inscrit)
                    messages.success(request, f"{inscrit.nom_complet} est inscrit(e) "
                                              f"sous le n° {inscrit.numero}. Sa carte "
                                              "avec QR code est prête.")
                else:
                    # le registre fait foi : les listes de classe et de
                    # concours reliées à cette inscription suivent
                    n = (Student.objects.filter(inscrit=inscrit)
                         .update(last_name=inscrit.nom, first_name=inscrit.prenom,
                                 student_number=inscrit.numero))
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
        "form": form, "modifie": modifie,
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
        if "supprimer" in request.POST:
            avis = get_object_or_404(Communique, pk=_int_or_none(request.POST["supprimer"]))
            if not _peut_retirer(request.user, avis, "auteur_id"):
                raise PermissionDenied("Seul l'auteur du communiqué ou "
                                       "l'administrateur peut le supprimer.")
            avis.delete()
            messages.success(request, "Communiqué supprimé.")
            return redirect("communiques")
        form = CommuniqueForm(request.POST, instance=modifie)
        if form.is_valid():
            avis = form.save(commit=False)
            if avis.auteur_id is None:
                avis.auteur = request.user
            avis.save()
            messages.success(request, "Communiqué enregistré"
                             + (" et publié." if avis.publie else " (brouillon)."))
            return redirect("communiques")
    return render(request, "grader/communiques.html", {
        "form": form, "modifie": modifie,
        "page_avis": _page(request, Communique.objects.select_related("auteur")),
    })


# ------------------------------------------------------------ publication

@login_required
@require_POST
def quiz_publication(request, pk):
    """Publie (ou retire) les résultats d'une épreuve sur l'espace public."""
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    droits.exiger(request.user, droits.CORRIGER, "publier les résultats")
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
    return redirect(reverse("quiz_results", args=[pk]) + "#publication")
