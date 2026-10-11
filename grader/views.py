from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.static import serve as file_serve

from . import bulletin as bulletin_mod
from . import droits
from . import journal
from . import importers
from . import labels_pdf as labels_mod
from . import layout as layout_mod
from . import qr as qr_mod
from . import services, sheet_pdf
from .export import results_workbook
from .forms import (CONCOURS_ID_MODES, AdminLoginForm, AnswerKeyUploadForm,
                    BulkQcmForm, CandidateFileForm, ClassGroupForm,
                    QuestionBankForm, QuestionForm, QuestionImportForm,
                    QuizForm, StudentImportForm, UploadForm)
from .models import (ARABIC_LETTERS, Answer, ClassGroup, Question, Quiz,
                     ScanBatch, SheetScan, Student, choice_letter)


# Chaque enseignant ne voit que ses propres classes et quiz
# (l'administrateur — superuser — voit tout).

def _int_or_none(value):
    """Identifiant numérique d'un paramètre d'URL ou de formulaire, ou None.

    Sans ce filtrage, un paramètre bricolé (?batch=abc) atteint le filtre
    Django et lève ValueError — c'est-à-dire une erreur 500 au lieu d'un
    simple « introuvable »."""
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


# Les listes peuvent compter plusieurs milliers de lignes (concours) : une
# page qui affiche tout deviendrait inutilisable — la liste des candidats
# déclenche en plus une requête par QR code affiché.
PAGE_SIZE = 100


def _page(request, items, per_page=PAGE_SIZE):
    """Page courante d'une liste (paramètre ?page=), bornée aux pages
    existantes : un ?page= invalide montre la première / dernière page."""
    paginator = Paginator(items, per_page)
    number = _int_or_none(request.GET.get("page")) or 1
    return paginator.get_page(min(max(number, 1), paginator.num_pages))


def _my_classes(request):
    """Classes de l'enseignant connecté : celles dont il est l'un des
    enseignants (une classe peut en avoir plusieurs)."""
    qs = ClassGroup.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(enseignants=request.user).distinct()
    return qs


# Une épreuve appartient à qui l'a écrite OU à ceux qui tiennent sa classe :
# c'est l'administration qui attribue les classes (voir grader/useradmin.py),
# et chacun des enseignants d'une classe travaille sur ses épreuves.
def _filtre_epreuves(utilisateur, prefixe=""):
    """Q(...) : épreuves écrites par cette personne ou rattachées à ses classes."""
    return (Q(**{f"{prefixe}owner": utilisateur})
            | Q(**{f"{prefixe}class_group__enseignants": utilisateur}))


def _my_quizzes(request):
    qs = Quiz.objects.select_related("class_group")
    if not request.user.is_superuser:
        qs = qs.filter(_filtre_epreuves(request.user)).distinct()
    return qs


def _my_batches(request):
    qs = ScanBatch.objects.select_related("quiz")
    if not request.user.is_superuser:
        qs = qs.filter(_filtre_epreuves(request.user, "quiz__")).distinct()
    return qs


def _my_sheets(request):
    qs = SheetScan.objects.select_related("batch__quiz", "student")
    if not request.user.is_superuser:
        qs = qs.filter(
            _filtre_epreuves(request.user, "batch__quiz__")).distinct()
    return qs


class AdminLoginView(LoginView):
    """Page de connexion de l'espace d'administration.

    Sert aussi de page de connexion à l'interface d'administration Django
    (voir admin.site.login_template dans admin.py), pour que les deux
    chemins d'accès présentent le même écran."""
    template_name = "registration/login_admin.html"
    authentication_form = AdminLoginForm
    redirect_authenticated_user = False

    def get_default_redirect_url(self):
        return reverse("admin:index")


@login_required
def dashboard(request):
    """Vue d'ensemble de l'enseignant : volumes, taux de lecture automatique
    et derniers lots de copies."""
    quizzes = _my_quizzes(request)
    classes = _my_classes(request)
    sheets = _my_sheets(request)

    n_sheets = sheets.count()
    # « lue avec succès » = repères détectés ET étudiant identifié, donc
    # aucune intervention nécessaire : c'est le taux que l'enseignant suit.
    n_auto = sheets.filter(status="ok").count()
    n_attention = n_sheets - n_auto
    auto_pct = round(n_auto / n_sheets * 100) if n_sheets else 0
    n_open_pending = Answer.objects.filter(
        sheet__in=sheets, question__qtype="open",
        points_awarded__isnull=True).count()

    # copies notées par jour sur les 7 derniers jours, pour l'histogramme
    today = timezone.localdate()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = dict(
        sheets.filter(created_at__date__gte=days[0])
        .annotate(d=TruncDate("created_at")).values_list("d")
        .annotate(n=Count("pk")))
    counts = [per_day.get(d, 0) for d in days]
    peak = max(counts) or 1
    week = [{"date": d, "count": c, "height": max(round(c / peak * 100), 4)}
            for d, c in zip(days, counts)]

    # --- Ce qui empêche le travail d'avancer -----------------------------
    # Deux situations bloquent réellement, et n'apparaissaient nulle part :
    # une épreuve dont le corrigé est incomplet (la correction refuse de
    # démarrer) et des copies reçues que personne n'a lancées.
    epreuves_bloquees = list(
        quizzes.filter(questions__qtype="qcm",
                       questions__correct_choice__isnull=True)
        .distinct()[:6])
    lots_en_attente = list(
        _my_batches(request).filter(status="pending").order_by("created_at")[:6])
    pages_en_attente = sum(b.total_pages for b in lots_en_attente)

    # --- Qualité de lecture du dernier lot -------------------------------
    # Le pourcentage cumulé ne bouge plus au bout de quelques centaines de
    # copies : ce qui renseigne sur la séance de scan qu'on vient de faire,
    # c'est le taux du dernier lot.
    dernier_lot = _my_batches(request).filter(status="done").first()
    lot_recent = None
    if dernier_lot:
        pages = dernier_lot.sheets.count()
        lues = dernier_lot.sheets.filter(status="ok").count()
        lot_recent = {"lot": dernier_lot, "pages": pages, "lues": lues,
                      "pct": round(lues / pages * 100) if pages else 0}

    # --- Causes des copies à reprendre ------------------------------------
    causes = [
        {"libelle": "à identifier", "n": sheets.filter(status="no_match").count(),
         "ton": "warn"},
        {"libelle": "repères non détectés",
         "n": sheets.filter(status="no_markers").count(), "ton": "bad"},
        {"libelle": "en erreur", "n": sheets.filter(status="error").count(),
         "ton": "bad"},
    ]

    # --- La dernière épreuve corrigée --------------------------------------
    derniere = None
    for quiz in quizzes.order_by("-created_at")[:5]:
        derniere = services.resume_epreuve(quiz)
        if derniere:
            break

    # --- Corrigés douteux, toutes épreuves confondues ----------------------
    douteux = services.corriges_douteux(quizzes)
    epreuves_douteuses = [
        {"quiz": q, "questions": douteux[q.pk]}
        for q in quizzes if q.pk in douteux
    ]

    # --- Temps de correction épargné ---------------------------------------
    # Base : les copies que la machine a effectivement lues. Une copie dont
    # les repères n'ont pas été trouvés n'a fait gagner aucun temps.
    illisibles = sum(c["n"] for c in causes if c["libelle"] != "à identifier")
    copies_lues = max(n_sheets - illisibles, 0)
    minutes = copies_lues * services.MINUTES_PAR_COPIE_A_LA_MAIN
    if minutes < 60:
        temps = {"valeur": minutes, "unite": "minutes" if minutes > 1 else "minute"}
    else:
        temps = {"valeur": round(minutes / 60, 1), "unite": "heures"}

    return render(request, "grader/dashboard.html", {
        "epreuves_bloquees": epreuves_bloquees,
        "lots_en_attente": lots_en_attente,
        "pages_en_attente": pages_en_attente,
        "lot_recent": lot_recent,
        "causes": [c for c in causes if c["n"]],
        "derniere": derniere,
        "epreuves_douteuses": epreuves_douteuses,
        "temps_epargne": temps,
        "copies_lues": copies_lues,
        "minutes_par_copie": services.MINUTES_PAR_COPIE_A_LA_MAIN,
        "quizzes": quizzes.select_related("class_group")[:8],
        "n_quizzes": quizzes.count(),
        "classes": classes,
        "n_classes": classes.count(),
        "n_students": Student.objects.filter(class_group__in=classes).count(),
        "n_sheets": n_sheets,
        "n_auto": n_auto,
        "n_attention": n_attention,
        "auto_pct": auto_pct,
        "n_open_pending": n_open_pending,
        "week": week,
        "week_total": sum(counts),
        "batches": _my_batches(request)[:6],
        "to_review": sheets.filter(status__in=["no_match", "no_markers", "error"])[:6],
    })


# ------------------------------------------------------------ classes

def _concours_group_pks(request):
    """Pks des conteneurs liés à au moins un concours (quiz auto_enroll)."""
    return list(_my_quizzes(request).filter(auto_enroll=True)
                .values_list("class_group_id", flat=True))


@login_required
def class_list(request):
    form = ClassGroupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        group = form.save(commit=False)
        group.owner = request.user
        group.save()          # devient aussi l'un de ses enseignants (modèle)
        return redirect("class_list")
    conc = _concours_group_pks(request)
    return render(request, "grader/class_list.html", {
        "classes": _my_classes(request).exclude(pk__in=conc),
        "quizzes": _my_quizzes(request).filter(auto_enroll=False),
        "form": form,
    })


@login_required
def concours_list(request):
    conc = _concours_group_pks(request)
    return render(request, "grader/concours_list.html", {
        "quizzes": _my_quizzes(request).filter(auto_enroll=True),
        "groups": _my_classes(request).filter(pk__in=conc),
    })


def _save_candidates(group, rows):
    """Crée / met à jour les candidats à partir de lignes (nom, prénom, n°).
    Un candidat portant déjà le même n° d'inscription est mis à jour."""
    created = updated = 0
    for last, first, number in rows:
        number = (number or "").strip()
        if number:
            existing = group.students.filter(student_number=number).first()
            if existing:
                existing.last_name = last.upper()
                existing.first_name = first
                existing.save(update_fields=["last_name", "first_name"])
                updated += 1
                continue
        _, was_created = Student.objects.get_or_create(
            class_group=group, last_name=last.upper(), first_name=first,
            defaults={"student_number": number})
        created += was_created
    return created, updated


@login_required
def class_detail(request, pk):
    group = get_object_or_404(_my_classes(request), pk=pk)
    import_form = StudentImportForm()
    xlsx_form = CandidateFileForm()
    if request.method == "POST":
        if "import" in request.POST:
            import_form = StudentImportForm(request.POST)
            if import_form.is_valid():
                rows = import_form.parse()
                _, updated = _save_candidates(group, rows)
                msg = f"{len(rows)} étudiant(s) importé(s)."
                if updated:
                    msg += f" ({updated} nom(s) mis à jour par numéro)"
                messages.success(request, msg)
                return redirect("class_detail", pk=pk)
        elif "import_xlsx" in request.POST:
            xlsx_form = CandidateFileForm(request.POST, request.FILES)
            if xlsx_form.is_valid():
                try:
                    rows = xlsx_form.parse()
                except importers.ImportError_ as exc:
                    messages.error(request, str(exc))
                    return redirect("class_detail", pk=pk)
                except Exception as exc:      # noqa: BLE001 — fichier corrompu
                    messages.error(request, f"Fichier illisible : {exc}")
                    return redirect("class_detail", pk=pk)
                if not rows:
                    messages.error(request, "Aucun candidat trouvé dans ce fichier.")
                    return redirect("class_detail", pk=pk)
                created, updated = _save_candidates(group, rows)
                messages.success(
                    request,
                    f"{len(rows)} candidat(s) importé(s) "
                    f"({created} créé(s), {updated} mis à jour). "
                    "Un QR code est disponible pour chacun.")
                return redirect("class_detail", pk=pk)
        elif "delete_student" in request.POST:
            student_pk = _int_or_none(request.POST.get("delete_student"))
            if student_pk is not None:
                Student.objects.filter(pk=student_pk, class_group=group).delete()
            return redirect("class_detail", pk=pk)
    return render(request, "grader/class_detail.html",
                  {"group": group, "import_form": import_form,
                   "xlsx_form": xlsx_form,
                   "students_page": _page(request, group.students.all()),
                   "students_total": group.students.count()})


@login_required
def student_qr(request, pk):
    """Image PNG du QR code d'un candidat (affichée dans la liste)."""
    student = get_object_or_404(
        Student.objects.filter(class_group__in=_my_classes(request)), pk=pk)
    return HttpResponse(qr_mod.student_qr_png(student),
                        content_type="image/png")


@login_required
def class_labels_pdf(request, pk):
    """Planche A4 d'étiquettes autocollantes (QR + n° d'inscription) des
    candidats du concours — à imprimer sur papier autocollant."""
    group = get_object_or_404(_my_classes(request), pk=pk)
    students = list(group.students.all())
    if not students:
        messages.error(request, "Aucun candidat : importez d'abord la liste.")
        return redirect("class_detail", pk=pk)
    pdf = labels_mod.generate_labels_pdf(group, students)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = \
        f'inline; filename="etiquettes_{group.pk}.pdf"'
    return resp


# ------------------------------------------------------------ quiz

@login_required
def quiz_create(request):
    droits.exiger(request.user, droits.CREER, "créer une épreuve")
    # « Nouveau concours » (menu Concours) : liste de candidats propre au
    # concours, identification par étiquette QR ou grille de n°
    is_concours = request.GET.get("concours") == "1"
    form = QuizForm(request.POST or None, user=request.user, concours=is_concours)
    if request.method == "POST" and form.is_valid():
        quiz = form.save(commit=False)
        quiz.owner = request.user
        quiz.auto_enroll = is_concours
        # Concours : conteneur de candidats dédié, séparé des classes
        if is_concours:
            title = quiz.title.strip()
            base = (title if "concours" in title.lower()
                    else f"Concours — {title}")[:110]
            name, i = base, 2
            while ClassGroup.objects.filter(owner=request.user, name=name).exists():
                name = f"{base} ({i})"
                i += 1
            quiz.class_group = ClassGroup.objects.create(
                owner=request.user, name=name)
        quiz.save()
        return redirect("quiz_detail", pk=quiz.pk)
    return render(request, "grader/quiz_form.html",
                  {"form": form, "is_concours": is_concours})


def _deferred(quiz):
    """Concours corrigé « après scan » : les lots attendent le lancement."""
    return quiz.auto_enroll and quiz.grading_mode == "deferred"


def _id_modes(quiz):
    """Modes d'identification proposés pour ce quiz (concours : étiquette QR
    ou grille ; un ancien concours en « qr » garde son réglage)."""
    if not quiz.auto_enroll:
        return Quiz.ID_MODES
    keep = set(CONCOURS_ID_MODES) | {quiz.id_mode}
    return [c for c in Quiz.ID_MODES if c[0] in keep]


def _add_questions(quiz, items, next_order, errors):
    """Ajoute des questions (import de fichier ou banque de questions) à la
    suite du quiz. Retourne le nombre de questions créées."""
    created = 0
    with transaction.atomic():
        for it in items:
            n_boxes = None
            if it["qtype"] == "qcm":
                # La bonne réponse peut manquer : elle sera renseignée plus
                # tard, à la main ou par scan du corrigé.
                if it["correct"] is None:
                    n_boxes = len(it["choices"]) or it.get("num_choices") or 4
                else:
                    # sans texte de choix : 4 cases, ou plus si la réponse l'exige
                    n_boxes = (len(it["choices"]) or it.get("num_choices")
                               or max(4, it["correct"] + 1))
            Question.objects.create(
                quiz=quiz, order=next_order, qtype=it["qtype"],
                text=it["text"] or "", choices=it["choices"],
                correct_choice=it["correct"] if it["qtype"] == "qcm" else None,
                points=it["points"], num_choices=n_boxes,
                open_height_mm=it.get("open_height_mm") or 25)
            next_order += 1
            created += 1
    return created


@login_required
def quiz_detail(request, pk):
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    qform = QuestionForm(quiz=quiz)
    bulk_form = BulkQcmForm(quiz=quiz)
    import_form = QuestionImportForm()
    bank_form = QuestionBankForm(quizzes=_my_quizzes(request), exclude=quiz)

    # Fiche « grille » générée avant le cadre QR : elle portait encore les
    # cases NOM/PRÉNOM, et le bouton « Télécharger la fiche » rendait ce PDF
    # tant que personne ne cliquait sur « Régénérer ». Mise à jour ici, à
    # l'ouverture de la page, seulement si rien de ce qui est lu au scan ne
    # bouge (voir services.moderniser_fiche_grille).
    if request.method == "GET" and services.moderniser_fiche_grille(quiz):
        messages.info(request, "Fiche de réponses mise à jour : plus de cases NOM "
                               "et PRÉNOM, la copie est identifiée par le code QR. "
                               "Les copies déjà imprimées restent lisibles.")
    if request.method == "GET" and services.actualiser_sujet(quiz):
        messages.info(request, "Sujet mis à jour : chaque question QCM affiche "
                               "maintenant ses choix de réponse.")

    if request.method == "POST":
        # Deux droits distincts se croisent ici : modifier l'epreuve, et
        # lancer sa correction. On n'exige que celui du geste demande.
        if "launch_grading" in request.POST:
            droits.exiger(request.user, droits.CORRIGER,
                          "lancer la correction des copies")
        else:
            droits.exiger(request.user, droits.CREER, "modifier cette épreuve")
        next_order = (quiz.questions.order_by("-order").values_list("order", flat=True).first() or 0) + 1
        if "add_question" in request.POST:
            qform = QuestionForm(request.POST, quiz=quiz)
            if qform.is_valid():
                q = qform.save(commit=False)
                q.quiz = quiz
                q.order = next_order
                q.choices = qform.cleaned_data["choices_list"]
                q.num_choices = qform.cleaned_data["num_choices"]
                lettre = qform.cleaned_data["correct_letter"]
                if q.qtype == "qcm" and lettre not in ("", None):
                    q.correct_choice = int(lettre)
                else:
                    q.correct_choice = None
                q.save()
                return redirect("quiz_detail", pk=pk)
        elif "bulk_add" in request.POST:
            bulk_form = BulkQcmForm(request.POST, quiz=quiz)
            if bulk_form.is_valid():
                with transaction.atomic():
                    for letter in bulk_form.cleaned_data["answer_key"]:
                        Question.objects.create(
                            quiz=quiz, order=next_order, qtype="qcm",
                            points=bulk_form.cleaned_data["points"],
                            num_choices=bulk_form.cleaned_data["num_choices"],
                            correct_choice=ord(letter) - ord("A"))
                        next_order += 1
                messages.success(request, "Questions QCM créées.")
                return redirect("quiz_detail", pk=pk)
        elif "import_questions" in request.POST:
            import_form = QuestionImportForm(request.POST, request.FILES)
            if import_form.is_valid():
                f = import_form.cleaned_data["file"]
                try:
                    items, errors = importers.parse_questions_file(f.name, f.read())
                except importers.ImportError_ as exc:
                    items, errors = [], [str(exc)]
                except Exception as exc:  # noqa: BLE001 — fichier corrompu
                    items, errors = [], [f"Fichier illisible : {exc}"]
                n = _add_questions(quiz, items, next_order, errors)
                if n:
                    messages.success(request, f"{n} question(s) importée(s) depuis "
                                              f"« {f.name} ».")
                if errors:
                    messages.error(request, "Non importé — " + " ; ".join(errors[:12])
                                   + (" …" if len(errors) > 12 else ""))
                elif not n:
                    messages.error(request, "Aucune question trouvée dans ce fichier.")
                return redirect("quiz_detail", pk=pk)
        elif "copy_questions" in request.POST:
            bank_form = QuestionBankForm(request.POST, quizzes=_my_quizzes(request),
                                         exclude=quiz)
            if bank_form.is_valid():
                source = bank_form.cleaned_data["source"]
                wanted = bank_form.cleaned_data["orders"]
                items = [{"qtype": q.qtype, "text": q.text, "choices": list(q.choices),
                          "correct": q.correct_choice, "points": q.points,
                          "num_choices": q.num_bubbles if q.qtype == "qcm" else None,
                          "open_height_mm": q.open_height_mm}
                         for q in source.questions.order_by("order")
                         if wanted is None or q.order in wanted]
                errors = []
                n = _add_questions(quiz, items, next_order, errors)
                messages.success(request, f"{n} question(s) reprise(s) de « {source.title} ».")
                if errors:
                    messages.error(request, "Non reprises — " + " ; ".join(errors[:12]))
                return redirect("quiz_detail", pk=pk)
        elif "edit_question" in request.POST:
            # corrigé et barème modifiables à tout moment : les notes déjà
            # calculées sont mises à jour immédiatement
            q = get_object_or_404(
                quiz.questions, pk=_int_or_none(request.POST.get("edit_question")) or 0)
            ancien_bareme, ancienne_bonne = q.points, q.correct_choice
            try:
                q.points = max(float(request.POST.get("points", q.points)
                                     .replace(",", ".")), 0.0)
            except (ValueError, TypeError):
                messages.error(request, "Barème invalide.")
                return redirect("quiz_detail", pk=pk)
            if q.qtype == "qcm":
                raw = (request.POST.get("correct") or "").strip()
                if raw == "":
                    q.correct_choice = None     # « à définir » : corrigé retiré
                elif raw.isdigit() and int(raw) < q.num_bubbles:
                    q.correct_choice = int(raw)
            q.save(update_fields=["points", "correct_choice"])
            journal.question_modifiee(request.user, q, bonne_avant=ancienne_bonne,
                                      bareme_avant=ancien_bareme)
            n = services.rescore_quiz(quiz)
            messages.success(request, f"Question {q.order} modifiée"
                             + (f" — {n} copie(s) recalculée(s)." if n else "."))
            return redirect("quiz_detail", pk=pk)
        elif "launch_grading" in request.POST:
            try:
                n_batches, n_pages = services.launch_pending(quiz)
            except services.CorrigeIncomplet as exc:
                messages.error(request, str(exc))
                return redirect("quiz_detail", pk=pk)
            if n_batches:
                messages.success(
                    request, f"Correction lancée : {n_batches} lot(s), {n_pages} page(s) — "
                             "suivez l'avancement dans la liste des lots ci-dessous.")
            else:
                messages.error(request, "Aucune copie en attente de correction.")
            return redirect("quiz_detail", pk=pk)
        elif "delete_question" in request.POST:
            question_pk = _int_or_none(request.POST.get("delete_question"))
            question = (quiz.questions.filter(pk=question_pk).first()
                        if question_pk is not None else None)
            if question is not None:
                # Supprimer une question retire ses points de toutes les
                # copies : c'est un changement de note, il est tracé.
                journal.question_supprimee(request.user, question)
                question.delete()
            return redirect("quiz_detail", pk=pk)
        elif "update_settings" in request.POST:
            ok = True
            ancienne_penalite = quiz.wrong_penalty
            ancien_cartouche = (quiz.entete, quiz.duree)
            if "entete" in request.POST:
                quiz.entete = request.POST["entete"].strip()[:2000]
            if "duree" in request.POST:
                quiz.duree = request.POST["duree"].strip()[:60]
            try:
                raw = (request.POST.get("wrong_penalty") or "0").replace(",", ".")
                quiz.wrong_penalty = abs(float(raw))
            except (ValueError, TypeError):
                ok = False
            new_mode = request.POST.get("sheet_mode")
            if new_mode in dict(Quiz.SHEET_MODES):
                quiz.sheet_mode = new_mode
            new_id = request.POST.get("id_mode")
            if new_id in dict(_id_modes(quiz)):
                quiz.id_mode = new_id
            try:
                quiz.id_digits = max(3, min(int(request.POST.get(
                    "id_digits", quiz.id_digits)), 10))
            except (ValueError, TypeError):
                pass
            if "note_admission" in request.POST:
                valide, valeur = _lire_note_admission(request.POST["note_admission"])
                if valide:
                    quiz.note_admission = valeur
                else:
                    ok = False
            new_grading = request.POST.get("grading_mode")
            if quiz.auto_enroll and new_grading in dict(Quiz.GRADING_MODES):
                quiz.grading_mode = new_grading
            quiz.save(update_fields=["wrong_penalty", "sheet_mode", "grading_mode",
                                     "id_mode", "id_digits", "entete", "duree",
                                     "note_admission"])
            # Le cartouche n'est imprimé que sur le sujet, jamais scanné : on
            # le refait tout de suite, sans toucher à la feuille de réponses.
            if (quiz.entete, quiz.duree) != ancien_cartouche:
                services.actualiser_sujet(quiz, forcer=True)
            journal.noter(request.user, "penalite", quiz=quiz,
                          objet="Pénalité par mauvaise réponse",
                          avant=ancienne_penalite, apres=quiz.wrong_penalty)
            services.rescore_quiz(quiz)     # la pénalité a pu changer
            messages.success(request, "Réglages du quiz enregistrés.") if ok else \
                messages.error(request, "Certaines valeurs étaient invalides.")
            return redirect("quiz_detail", pk=pk)
        elif "generate_pdf" in request.POST:
            students = list(quiz.class_group.students.all())
            if not quiz.questions.exists():
                messages.error(request, "Ajoutez d'abord des questions.")
            elif quiz.id_mode == "qr" and not quiz.auto_enroll and not students:
                messages.error(
                    request, "Le mode QR génère une fiche par étudiant : "
                             "importez d'abord la liste des étudiants de la classe "
                             "(ou activez le mode concours pour des fiches anonymes).")
            else:
                try:
                    layout = layout_mod.build_layout(quiz)
                except layout_mod.TooManyPages as exc:
                    messages.error(request, str(exc))
                    return redirect("quiz_detail", pk=pk)
                if quiz.id_mode == "qr" and quiz.auto_enroll:
                    try:
                        count = max(1, min(int(request.POST.get("qr_count", 100)), 2000))
                    except ValueError:
                        count = 100
                    pdf_bytes = sheet_pdf.generate_sheet_pdf(
                        quiz, layout, serials=list(range(1, count + 1)))
                    filename = f"fiches_concours_quiz_{quiz.pk}.pdf"
                    msg = (f"{count} exemplaires de la fiche anonyme générés. "
                           "Chaque copie est identifiée par l'étiquette QR du "
                           "candidat, collée dans l'emplacement réservé : "
                           "imprimez aussi la planche d'étiquettes (page "
                           "« Candidats & étiquettes QR »). Les candidats "
                           "inconnus seront créés automatiquement au scan.")
                elif quiz.id_mode == "qr":
                    pdf_bytes = sheet_pdf.generate_sheet_pdf(quiz, layout,
                                                             students=students)
                    filename = f"fiches_nominatives_quiz_{quiz.pk}.pdf"
                    msg = (f"Fiches nominatives générées : {len(students)} fiches "
                           "avec nom et QR code — distribuez à chaque étudiant la sienne.")
                else:
                    pdf_bytes = sheet_pdf.generate_sheet_pdf(quiz, layout)
                    filename = f"fiche_quiz_{quiz.pk}.pdf"
                    msg = "Fiche de réponses générée. Imprimez-la pour vos étudiants."
                    if quiz.id_mode == "grid":
                        msg += (" Chaque élève colle son étiquette QR dans le cadre "
                                "en haut à droite (planche d'étiquettes : page "
                                "« Candidats & étiquettes QR »), ou à défaut "
                                "noircit son n° d'inscription dans la grille.")
                    if quiz.id_mode == "sticker" or (quiz.auto_enroll and quiz.id_mode == "qr"):
                        msg += (" Imprimez aussi la planche d'étiquettes QR des candidats "
                                "(page « Candidats & étiquettes QR ») à coller sur les copies.")
                layout["signature"] = layout_mod.layout_signature(quiz)
                quiz.layout_json = layout
                quiz.sheet_pdf.save(filename, ContentFile(pdf_bytes), save=False)
                # Mode « sujet séparé » : générer aussi le document des questions
                if quiz.sheet_mode == "split":
                    subject_bytes = sheet_pdf.generate_subject_pdf(quiz)
                    quiz.subject_pdf.save(f"sujet_quiz_{quiz.pk}.pdf",
                                          ContentFile(subject_bytes), save=False)
                    layout["sujet_version"] = sheet_pdf.SUJET_VERSION
                    msg = ("Deux documents générés : le SUJET (questions) à "
                           "distribuer, et la FEUILLE DE RÉPONSES à imprimer, "
                           "remplir et scanner.")
                quiz.save()
                messages.success(request, msg)
            return redirect("quiz_detail", pk=pk)

    layout_stale = False
    if quiz.layout_json:
        if quiz.layout_json.get("signature"):
            # toute modification de ce qui est imprimé (questions, choix,
            # barème, réglages de la fiche) déclenche l'alerte
            layout_stale = (quiz.layout_json["signature"]
                            != layout_mod.layout_signature(quiz))
        else:   # fiche générée par une version antérieure
            current_orders = sorted(quiz.questions.values_list("order", flat=True))
            layout_orders = sorted(
                item["order"] for p in quiz.layout_json["pages"]
                for item in (p["qcm"] + p["open"]))
            layout_stale = current_orders != layout_orders

    sans_corrige = list(quiz.questions_sans_corrige.values_list("order", flat=True))
    pending = list(quiz.batches.filter(status="pending").order_by("created_at"))
    processing = [b for b in quiz.batches.filter(status="processing")
                  if not services.is_stalled(b)]
    return render(request, "grader/quiz_detail.html", {
        "quiz": quiz, "qform": qform, "bulk_form": bulk_form,
        "import_form": import_form, "bank_form": bank_form,
        "id_modes": _id_modes(quiz),
        "pending_batches": pending,
        "pending_pages": sum(b.total_pages for b in pending),
        "processing_pages": sum(b.total_pages for b in processing),
        "processing_done": sum(b.processed_pages for b in processing),
        "deferred": _deferred(quiz),
        "layout_stale": layout_stale,
        "sans_corrige": sans_corrige,
        "n_qcm": quiz.questions.filter(qtype="qcm").count(),
        "answer_key_form": AnswerKeyUploadForm(),
        "answer_key_sheets": quiz.answer_key_sheets.all()[:12],
        "n_answer_key_sheets": quiz.answer_key_sheets.count(),
        "batches": quiz.batches.all(),
        "upload_form": UploadForm(),
        "choice_letters_full": [choice_letter(i, quiz.language)
                                for i in range(len(ARABIC_LETTERS))],
        "journal": quiz.modifications.all()[:15],
        "n_journal": quiz.modifications.count(),
    })


# ------------------------------------------------------------ scans

@login_required
def quiz_upload(request, pk):
    droits.exiger(request.user, droits.CORRIGER, "téléverser des copies")
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    if not quiz.layout_json:
        messages.error(request, "Générez d'abord la fiche de réponses PDF.")
        return redirect("quiz_detail", pk=pk)
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid():
            files = request.FILES.getlist("files")
            if _deferred(quiz):
                # concours « correction après scan » : lot mis en attente
                batch = services.create_batch(quiz, files, status="pending",
                                              label=form.cleaned_data["label"])
                pending = quiz.batches.filter(status="pending")
                n_pages = sum(b.total_pages for b in pending)
                messages.success(
                    request,
                    f"{batch.total_pages} page(s) reçue(s) et mises en attente "
                    f"({n_pages} page(s) en attente au total). Téléversez les autres "
                    "copies, puis cliquez sur « Lancer la correction ».")
                return redirect("quiz_detail", pk=pk)
            if not quiz.corrige_complet:
                # Corrigé incomplet : on garde les copies sans les noter —
                # elles seraient comptées fausses. L'enseignant complète le
                # corrigé puis lance la correction.
                batch = services.create_batch(quiz, files, status="pending",
                                              label=form.cleaned_data["label"])
                messages.error(
                    request,
                    f"{batch.total_pages} page(s) reçue(s) et mises en attente : "
                    + str(services.CorrigeIncomplet(quiz)))
                return redirect("quiz_detail", pk=pk)
            batch = services.create_batch(quiz, files, label=form.cleaned_data["label"])
            services.start_batch(batch)
            messages.success(request,
                             f"{batch.total_pages} page(s) reçue(s) — correction "
                             "automatique en cours, les notes arrivent au fil de l'eau.")
            return redirect("batch_detail", pk=batch.pk)
    return redirect("quiz_detail", pk=pk)


@login_required
def quiz_answer_key(request, pk):
    """Téléversement de la fiche remplie avec les bonnes réponses.

    La fiche est lue comme une copie, renseigne le corrigé des questions QCM
    et reste conservée avec l'épreuve (historique du barème)."""
    droits.exiger(request.user, droits.CREER, "renseigner le corrigé")
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    if request.method != "POST":
        return redirect("quiz_detail", pk=pk)
    if not quiz.layout_json:
        messages.error(request, "Générez d'abord la fiche de réponses PDF : "
                                "c'est elle qu'on imprime pour le corrigé.")
        return redirect("quiz_detail", pk=pk)

    form = AnswerKeyUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Sélectionnez la fiche scannée du corrigé.")
        return redirect("quiz_detail", pk=pk)
    # Instantané des bonnes réponses : la lecture du corrigé peut en
    # changer plusieurs d'un coup, chacune doit apparaître au journal.
    bonnes_avant = {q.pk: q.correct_choice for q in quiz.questions.all()}
    try:
        pages, n_lues, ambigues = services.read_answer_key(
            quiz, request.FILES.getlist("files"))
    except Exception as exc:  # noqa: BLE001 — fichier illisible
        messages.error(request, f"Corrigé illisible : {exc}")
        return redirect("quiz_detail", pk=pk)

    for q in quiz.questions.all():
        journal.noter(request.user, "bonne_reponse", quiz=quiz,
                      objet=f"Question {q.order} (corrigé scanné)",
                      avant=journal.lettre(q, bonnes_avant.get(q.pk)),
                      apres=journal.lettre(q, q.correct_choice))

    echecs = [p for p in pages if p.status in ("no_markers", "error")]
    if n_lues:
        messages.success(
            request,
            f"Corrigé lu : {n_lues} bonne(s) réponse(s) renseignée(s) sur "
            f"{len(pages)} page(s). Les copies déjà scannées ont été "
            "recalculées.")
    reste = list(quiz.questions_sans_corrige.values_list("order", flat=True))
    if reste:
        nums = ", ".join(str(n) for n in reste[:15])
        messages.error(
            request,
            f"{len(reste)} question(s) restent sans bonne réponse (n° {nums}) : "
            "case laissée vide ou plusieurs cases noircies sur la fiche. "
            "Renseignez-les à la main dans le tableau des questions, ou "
            "rescannez un corrigé plus net.")
    for page in echecs:
        messages.error(request, f"{page.source_name} : {page.error_message}")
    if not n_lues and not echecs:
        messages.error(request, "Aucune bonne réponse n'a pu être lue sur "
                                "cette fiche.")
    return redirect("quiz_detail", pk=pk)


@login_required
def batch_delete(request, pk):
    """Confirmation, puis suppression d'un lot téléversé par erreur."""
    batch = get_object_or_404(_my_batches(request), pk=pk)
    droits.exiger(request.user, droits.CORRIGER, "supprimer un lot de copies")
    if request.method == "POST":
        quiz_pk = batch.quiz_id
        nom = str(batch)
        try:
            n = services.supprimer_lot(batch, request.user)
        except services.LotEnCours as exc:
            messages.error(request, str(exc))
            return redirect("batch_detail", pk=pk)
        messages.success(request, f"Lot « {nom} » supprimé : {n} page(s) retirée(s) "
                                  "des résultats. La suppression est inscrite au journal.")
        return redirect("quiz_detail", pk=quiz_pk)
    copies = batch.sheets
    return render(request, "grader/batch_delete.html", {
        "batch": batch,
        "n_pages": copies.count(),
        "n_attribuees": copies.exclude(student=None).count(),
        "en_cours": batch.status == "processing" and not services.is_stalled(batch),
    })


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(_my_batches(request), pk=pk)
    stalled = services.is_stalled(batch)
    if request.method == "POST" and ("launch_grading" in request.POST
                                     or "resume" in request.POST):
        droits.exiger(request.user, droits.CORRIGER,
                      "lancer la correction des copies")
    if request.method == "POST" and "launch_grading" in request.POST:
        try:
            n_batches, n_pages = services.launch_pending(batch.quiz)
        except services.CorrigeIncomplet as exc:
            messages.error(request, str(exc))
            return redirect("quiz_detail", pk=batch.quiz_id)
        if n_batches:
            messages.success(request, f"Correction lancée : {n_batches} lot(s), "
                                      f"{n_pages} page(s).")
        return redirect("batch_detail", pk=pk)
    if request.method == "POST" and "resume" in request.POST:
        if batch.status == "error" or stalled:
            try:
                services.corrige_incomplet(batch.quiz)
            except services.CorrigeIncomplet as exc:
                messages.error(request, str(exc))
                return redirect("quiz_detail", pk=batch.quiz_id)
            batch.status = "processing"
            batch.save(update_fields=["status", "updated_at"])
            services.start_batch(batch)
            messages.success(request, "Correction du lot relancée.")
        return redirect("batch_detail", pk=pk)
    sheets_page = _page(request, batch.sheets.select_related("student"))
    first_review_id = (batch.sheets.filter(status__in=["ok", "no_match"])
                       .values_list("pk", flat=True).first())
    n_total = batch.sheets.count()
    n_auto = batch.sheets.filter(status="ok").count()
    n_attention = n_total - n_auto
    n_open_pending = Answer.objects.filter(
        sheet__batch=batch, question__qtype="open",
        points_awarded__isnull=True).count()
    return render(request, "grader/batch_detail.html",
                  {"batch": batch, "sheets_page": sheets_page,
                   "sheets": sheets_page, "stalled": stalled,
                   "first_review_id": first_review_id,
                   "n_total": n_total, "n_auto": n_auto,
                   "n_attention": n_attention,
                   "n_open_pending": n_open_pending})


def _identification(sheet):
    """Comment la copie a été rattachée à son étudiant, en clair.

    L'écran affichait « Nom lu par OCR : (rien) — confiance 100 % » pour une
    copie reconnue par son code QR : l'OCR n'avait rien lu parce qu'il
    n'avait rien à lire, et les 100 % étaient ceux du QR."""
    if sheet.student is None:
        return None
    lu = sheet.id_read or ""
    if lu.startswith(("QR", "Fiche")):
        return {"methode": "qr", "texte": "Reconnu par son code QR"}
    if lu and not sheet.ocr_name_raw:
        return {"methode": "grille",
                "texte": f"Reconnu par son n° d'inscription ({lu})"}
    if sheet.ocr_name_raw:
        return {"methode": "ocr",
                "texte": f"Reconnu par le nom écrit, lu « {sheet.ocr_name_raw} » "
                         f"(ressemblance {sheet.match_score:.0f} %)"}
    return {"methode": "main", "texte": "Affecté à la main"}


@login_required
def sheet_review(request, pk):
    sheet = get_object_or_404(_my_sheets(request), pk=pk)
    quiz = sheet.batch.quiz
    students = quiz.class_group.students.all()
    answers = sheet.answers.select_related("question")

    # navigation en série dans le lot (copies vérifiables uniquement)
    sheet_ids = list(sheet.batch.sheets
                     .filter(status__in=["ok", "no_match"])
                     .values_list("pk", flat=True))
    try:
        position = sheet_ids.index(sheet.pk)
    except ValueError:
        sheet_ids, position = [sheet.pk], 0
    prev_id = sheet_ids[position - 1] if position > 0 else None
    next_id = sheet_ids[position + 1] if position + 1 < len(sheet_ids) else None

    # Ce qui se fait sur cet écran : affecter une copie NON identifiée, et
    # noter les réponses manuscrites. Une copie reconnue (QR, n°) garde son
    # étudiant ; une case lue sur un QCM ne se retouche pas — pour personne
    # (voir grader/droits.py).
    a_enregistrer = sheet.student is None or any(
        a.question.qtype == "open" for a in answers)
    # Image de contrôle d'avant la correction : cadres NOM/PRÉNOM sur une
    # fiche de concours qui n'en a pas — effacés une fois pour toutes.
    services.nettoyer_controle(sheet)

    if request.method == "POST":
        droits.exiger(request.user, droits.CORRIGER, "vérifier les copies")
        # affectation — seulement d'une copie non identifiée : la liste
        # n'est affichée que pour elle, et un envoi fabriqué à la main ne
        # doit pas davantage réaffecter une copie reconnue.
        if "student" in request.POST and sheet.student is None:
            sid = _int_or_none(request.POST.get("student"))
            ancien = sheet.student
            sheet.student = (students.filter(pk=sid).first()
                             if sid is not None else None)
            journal.attribution(request.user, sheet, ancien)
            sheet.student_confirmed = sheet.student is not None
            if sheet.status in ("no_match", "ok"):
                sheet.status = "ok" if sheet.student else "no_match"
            sheet.save()
        # corrections des réponses
        for a in answers:
            if a.question.qtype == "qcm":
                # La réponse lue ne se modifie pas : un champ « choice_… »
                # envoyé à la main est ignoré.
                continue
            else:
                val = request.POST.get(f"points_{a.pk}", "").strip().replace(",", ".")
                if val != "":
                    try:
                        pts = float(val)
                    except ValueError:
                        messages.error(request, f"Note invalide pour la question "
                                                f"{a.question.order} : « {val} ».")
                        continue
                    nouvelle = max(min(pts, a.question.points), 0.0)
                    journal.noter(
                        request.user, "note", copie=sheet,
                        objet=f"{journal.libelle_copie(sheet)} — question {a.question.order}",
                        avant=a.points_awarded, apres=nouvelle)
                    a.points_awarded = nouvelle
                    a.manually_set = True
                    a.save()
        services.rescore_sheet(sheet)
        if "save_next" in request.POST and next_id:
            messages.success(
                request, f"Copie {position + 1}/{len(sheet_ids)} enregistrée.")
            return redirect("sheet_review", pk=next_id)
        messages.success(request, "Corrections enregistrées.")
        return redirect("batch_detail", pk=sheet.batch_id)

    return render(request, "grader/sheet_review.html", {
        "sheet": sheet, "quiz": quiz, "students": students,
        "identification": _identification(sheet),
        "answers": answers,
        "position": position + 1, "total": len(sheet_ids),
        "prev_id": prev_id, "next_id": next_id,
        "a_enregistrer": a_enregistrer,
        "historique": sheet.modifications.all()[:30],
    })


# ------------------------------------------------------------ résultats

def _lire_note_admission(texte):
    """« 10 », « 10,5 » ou vide (pas de seuil) -> (valide, valeur)."""
    brut = (texte or "").strip().replace(",", ".")
    if not brut:
        return True, None
    try:
        valeur = float(brut)
    except ValueError:
        return False, None
    return (valeur >= 0), (valeur if valeur >= 0 else None)


@login_required
def quiz_note_admission(request, pk):
    """Règle la note minimale d'admission depuis la page des résultats.

    Le réglage existait sur la page de l'épreuve, mais c'est sur les
    résultats qu'on cherche les admis : sans ce raccourci, le bouton
    « Bulletins des admis » restait introuvable tant qu'on ignorait qu'il
    fallait d'abord passer par les réglages."""
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    retour = reverse("quiz_results", args=[pk])
    lot = _int_or_none(request.POST.get("batch"))
    if lot is not None:
        retour += f"?batch={lot}"
    if request.method != "POST":
        return redirect(retour)
    droits.exiger(request.user, droits.CREER, "régler la note d'admission")
    valide, valeur = _lire_note_admission(request.POST.get("note_admission"))
    if not valide:
        messages.error(request, "Note minimale invalide : saisissez un nombre, "
                                "par exemple 10 ou 10,5.")
    else:
        quiz.note_admission = valeur
        quiz.save(update_fields=["note_admission"])
        if valeur is None:
            messages.success(request, "Note minimale retirée.")
        else:
            qui = "admis" if quiz.auto_enroll else "réussites"
            messages.success(request, f"Note minimale fixée à {valeur:g} : liste "
                                      f"et bulletins des {qui} ci-dessous.")
    return redirect(retour + "#admission")


@login_required
def quiz_results(request, pk):
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    batch = None
    batch_pk = _int_or_none(request.GET.get("batch"))
    if batch_pk is not None:
        batch = quiz.batches.filter(pk=batch_pk).first()
    rows = services.compute_results(quiz, batch)
    admission = services.admissions(quiz, rows)   # pose aussi row["resultat"]
    unmatched = SheetScan.objects.filter(batch__quiz=quiz, student=None)
    if batch:
        unmatched = unmatched.filter(batch=batch)
    # Les statistiques portent sur toutes les copies ("rows") ; seul le
    # tableau des notes est paginé ("rows_page"), un concours pouvant
    # compter des milliers de candidats.
    return render(request, "grader/results.html", {
        "quiz": quiz, "rows": rows, "rows_page": _page(request, rows),
        "rows_total": len(rows), "batch": batch,
        # conservé dans les liens de pagination : filtre par lot
        "batch_qs": f"batch={batch.pk}" if batch else "",
        "batches": quiz.batches.all(), "unmatched": unmatched[:200],
        "unmatched_total": unmatched.count(),
        "pending_pages": sum(b.total_pages for b in quiz.batches.filter(status="pending")),
        "stats": services.compute_stats(quiz, rows),
        "admission": admission,
    })


@login_required
def quiz_results_xlsx(request, pk):
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    batch = None
    batch_pk = _int_or_none(request.GET.get("batch"))
    if batch_pk is not None:
        batch = quiz.batches.filter(pk=batch_pk).first()
    rows = services.compute_results(quiz, batch)
    admission = services.admissions(quiz, rows)
    data = results_workbook(quiz, rows, services.compute_stats(quiz, rows),
                            admission)
    resp = HttpResponse(
        data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="resultats_{quiz.pk}.xlsx"'
    return resp


@login_required
def quiz_bulletins_pdf(request, pk):
    quiz = get_object_or_404(_my_quizzes(request), pk=pk)
    batch = None
    batch_pk = _int_or_none(request.GET.get("batch"))
    if batch_pk is not None:
        batch = quiz.batches.filter(pk=batch_pk).first()
    rows = services.compute_results(quiz, batch)
    admission = services.admissions(quiz, rows)   # résultat et rang par ligne
    nom = f"bulletins_{quiz.pk}.pdf"
    if request.GET.get("admis"):
        # Bulletins des seuls admis, par ordre de mérite.
        if admission is None:
            messages.error(request, "Indiquez d'abord la note minimale "
                                    "d'admission dans les réglages de l'épreuve.")
            return redirect("quiz_results", pk=pk)
        rows = admission["admis"]
        nom = f"bulletins_admis_{quiz.pk}.pdf"
    student_pk = _int_or_none(request.GET.get("student"))
    if student_pk is not None:
        rows = [r for r in rows if r["student"].pk == student_pk]
    data = bulletin_mod.generate_bulletins_pdf(quiz, rows, batch, admission)
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{nom}"'
    return resp


# ------------------------------------------------------------ documents

def _media_quiz(path):
    """Quiz auquel se rattache un document de /media/ (fiche PDF, scan,
    image redressée, image de contrôle, recadrage, fichier téléversé),
    ou None si le document n'est rattaché à aucun quiz."""
    quiz = Quiz.objects.filter(Q(sheet_pdf=path) | Q(subject_pdf=path)).first()
    if quiz is not None:
        return quiz.pk
    sheet = (SheetScan.objects.select_related("batch")
             .filter(Q(image=path) | Q(warped_image=path) | Q(overlay_image=path)
                     | Q(name_crop=path)).first())
    if sheet is not None:
        return sheet.batch.quiz_id
    answer = (Answer.objects.select_related("sheet__batch")
              .filter(open_crop=path).first())
    if answer is not None:
        return answer.sheet.batch.quiz_id
    if path.startswith("uploads/lot_"):
        pk = path[len("uploads/lot_"):].split("/", 1)[0]
        if pk.isdigit():
            batch = ScanBatch.objects.filter(pk=pk).first()
            if batch is not None:
                return batch.quiz_id
    return None


@login_required
def protected_media(request, path):
    """Sert un document de /media/ à qui a accès au quiz concerné — son
    auteur et chacun des enseignants de sa classe — et à l'administrateur.
    Les autres reçoivent 404.

    Même règle que les pages (_filtre_epreuves) : un second enseignant de la
    classe voyait l'épreuve, mais ni sa fiche PDF ni les scans."""
    if not request.user.is_superuser:
        quiz_pk = _media_quiz(path)
        if quiz_pk is None or not Quiz.objects.filter(pk=quiz_pk).filter(
                _filtre_epreuves(request.user)).exists():
            raise Http404("Document introuvable")
    return file_serve(request, path, document_root=settings.MEDIA_ROOT)
