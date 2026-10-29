"""Pages des épreuves écrites et orales (la page d'un QCM reste dans views).

Écrit : on rédige le sujet, on télécharge le sujet et les copies à imprimer
(page de garde avec l'identification et la grille de la note), puis on
scanne les copies corrigées : la note noircie par le correcteur et le QR
(ou le n°) sont lus, la note est affectée à l'étudiant.

Oral : pas de feuille, les notes se saisissent à l'écran.

Dans les deux cas la note est portée par une seule question « Note »
(services.question_note) : résultats, admission, publication, bulletins et
export fonctionnent comme pour un QCM.
"""
from django.contrib import messages
from django.core.files.base import ContentFile
from django.shortcuts import redirect, render
from django.urls import reverse

from . import droits, services, sheet_pdf
from . import layout as layout_mod
from .forms import EpreuveEcritForm, EpreuveOralForm, UploadForm


def _retour(quiz):
    if quiz.auto_enroll:
        return reverse("concours_list"), "Retour aux concours"
    return reverse("class_list"), "Retour aux classes"


def _renommer(request, quiz):
    titre = request.POST.get("title", "").strip()
    if titre and titre != quiz.title:
        services.renommer_epreuve(quiz, titre, request.user)


def epreuve_detail(request, quiz):
    if quiz.type_epreuve == "oral":
        return _oral(request, quiz)
    return _ecrit(request, quiz)


# ------------------------------------------------------------------- écrit

def _generer_documents(quiz, nb_exemplaires=100):
    """Copies à imprimer (page de garde + pages lignées) et sujet.
    Retourne le message à afficher."""
    services.question_note(quiz)
    layout = layout_mod.build_layout(quiz)
    students = list(quiz.class_group.students.all())
    if quiz.id_mode == "qr" and quiz.auto_enroll:
        pdf = sheet_pdf.generate_sheet_pdf(
            quiz, layout, serials=list(range(1, nb_exemplaires + 1)))
        quoi = f"{nb_exemplaires} copies anonymes"
    elif quiz.id_mode == "qr":
        pdf = sheet_pdf.generate_sheet_pdf(quiz, layout, students=students)
        quoi = f"{len(students)} copies nominatives (nom et QR imprimés)"
    else:
        pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
        quoi = "la copie (une par étudiant)"
    layout["signature"] = layout_mod.layout_signature(quiz)
    quiz.layout_json = layout
    quiz.sheet_pdf.save(f"copies_ecrit_{quiz.pk}.pdf", ContentFile(pdf), save=False)
    quiz.subject_pdf.save(f"sujet_ecrit_{quiz.pk}.pdf",
                          ContentFile(sheet_pdf.generate_subject_pdf(quiz)), save=False)
    quiz.save()
    msg = f"Documents prêts : le sujet et {quoi}."
    if quiz.id_mode in ("sticker", "grid") or quiz.auto_enroll:
        msg += (" Imprimez aussi la planche d'étiquettes QR (page de la classe "
                "ou des candidats) : chaque étudiant colle la sienne sur sa copie.")
    return msg


def _ecrit(request, quiz):
    form = EpreuveEcritForm(instance=quiz)
    if request.method == "POST":
        droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
        if "reglages" in request.POST:
            form = EpreuveEcritForm(request.POST, instance=quiz)
            if form.is_valid():
                _renommer(request, quiz)
                form.save()
                services.question_note(quiz)
                messages.success(request, "Épreuve enregistrée.")
                return redirect("quiz_detail", pk=quiz.pk)
        elif "generer" in request.POST:
            if quiz.id_mode == "qr" and not quiz.auto_enroll and \
                    not quiz.class_group.students.exists():
                messages.error(request, "Copies nominatives : ajoutez d'abord les "
                                        "étudiants de la classe.")
            else:
                try:
                    n = max(1, min(int(request.POST.get("nb", 100)), 2000))
                except ValueError:
                    n = 100
                messages.success(request, _generer_documents(quiz, n))
            return redirect("quiz_detail", pk=quiz.pk)

    perimee = bool(quiz.layout_json) and (
        quiz.layout_json.get("signature") != layout_mod.layout_signature(quiz))
    return render(request, "grader/epreuve_ecrit.html", {
        "quiz": quiz, "form": form, "retour": _retour(quiz),
        "perimee": perimee, "upload_form": UploadForm(),
        "batches": quiz.batches.all(),
        "journal": quiz.modifications.all()[:15],
        "n_journal": quiz.modifications.count(),
    })


# -------------------------------------------------------------------- oral

def _generer_fiches_orales(quiz):
    """Une fiche d'évaluation par étudiant : nom et QR imprimés, grille de la
    note à noircir par l'examinateur. Scannée, la note va à l'étudiant."""
    services.question_note(quiz)
    etudiants = list(quiz.class_group.students.all())
    quiz.id_mode = "qr"                 # fiches nominatives
    layout = layout_mod.build_layout(quiz)
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout, students=etudiants)
    layout["signature"] = layout_mod.layout_signature(quiz)
    quiz.layout_json = layout
    quiz.sheet_pdf.save(f"fiches_oral_{quiz.pk}.pdf", ContentFile(pdf), save=False)
    quiz.save()
    return len(etudiants)


def _lire_note(texte, sur):
    """« 14,5 » -> 14.5 ; vide -> None ; hors barème ou illisible -> ValueError."""
    texte = (texte or "").strip().replace(",", ".")
    if not texte:
        return None
    note = float(texte)
    if not 0 <= note <= sur:
        raise ValueError
    return round(note, 2)


def _oral(request, quiz):
    form = EpreuveOralForm(instance=quiz)
    etudiants = list(quiz.class_group.students.all())
    if request.method == "POST":
        if "reglages" in request.POST:
            droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
            form = EpreuveOralForm(request.POST, instance=quiz)
            if form.is_valid():
                _renommer(request, quiz)
                form.save()
                services.question_note(quiz)
                messages.success(request, "Épreuve enregistrée.")
                return redirect("quiz_detail", pk=quiz.pk)
        elif "generer" in request.POST:
            droits.exiger(request.user, droits.CREER, "préparer l'épreuve")
            if not etudiants:
                messages.error(request, "Ajoutez d'abord les étudiants : une fiche "
                                        "est imprimée pour chacun.")
            else:
                n = _generer_fiches_orales(quiz)
                messages.success(request, f"{n} fiche(s) d'évaluation prête(s), une par "
                                          "étudiant (nom et QR imprimés).")
            return redirect("quiz_detail", pk=quiz.pk)
        elif "notes" in request.POST:
            droits.exiger(request.user, droits.CORRIGER, "noter les étudiants")
            notes, erreurs = {}, []
            for e in etudiants:
                champ = f"note_{e.pk}"
                if champ not in request.POST:
                    continue
                try:
                    notes[e] = _lire_note(request.POST[champ], quiz.note_sur)
                except ValueError:
                    erreurs.append(f"{e.full_name} (« {request.POST[champ]} »)")
            if erreurs:
                messages.error(request, f"Notes refusées (entre 0 et {quiz.note_sur:g}) : "
                               + ", ".join(erreurs[:10]) + ". Les autres sont enregistrées.")
            n = services.enregistrer_notes_orales(quiz, notes, request.user)
            messages.success(request, f"{n} note(s) enregistrée(s)." if n
                             else "Aucune note modifiée.")
            return redirect("quiz_detail", pk=quiz.pk)

    deja = services.notes_orales(quiz)
    lignes = [(e, "" if deja.get(e.pk) is None
               else f"{deja[e.pk]:g}".replace(".", ",")) for e in etudiants]
    perimee = bool(quiz.layout_json) and (
        quiz.layout_json.get("signature") != layout_mod.layout_signature(quiz))
    return render(request, "grader/epreuve_oral.html", {
        "quiz": quiz, "form": form, "retour": _retour(quiz), "lignes": lignes,
        "n_notes": len(deja), "perimee": perimee, "upload_form": UploadForm(),
        "batches": quiz.batches.exclude(label=services.LOT_ORAL),
        "journal": quiz.modifications.all()[:15],
        "n_journal": quiz.modifications.count(),
    })
