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

from . import droits, importers, journal, services, sheet_pdf
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


def epreuve_detail(request, quiz, fil=None):
    """fil : le concours et ses étapes, pour l'en-tête (services.fil_etapes)."""
    if request.method == "POST" and "passer_admis" in request.POST:
        # étape suivante d'un concours : les nouveaux admis de l'épreuve
        # précédente rejoignent la liste des candidats
        droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
        ajoutes, deja, n_admis = services.faire_passer_admis(quiz)
        messages.success(request, f"{ajoutes} admis ajouté(s) aux candidats "
                                  f"({n_admis} admis au total, {deja} déjà dans la liste).")
        return redirect("quiz_detail", pk=quiz.pk)
    if quiz.type_epreuve == "oral":
        return _oral(request, quiz, fil)
    return _ecrit(request, quiz, fil)


# ------------------------------------------------------------------- écrit

def nombre_de_copies(quiz, nb_exemplaires=1):
    """Copies que la génération va dessiner : de quoi choisir l'arrière-plan."""
    if quiz.exemplaires_anonymes:
        return nb_exemplaires
    if quiz.id_mode == "qr" or quiz.type_epreuve == "oral":
        return quiz.class_group.students.count()
    return 1


def suivre_generation(request, quiz):
    """Génération en arrière-plan, pour la page de l'épreuve : l'état en cours (ou
    interrompu), sinon None. Les messages d'une génération finie s'affichent une fois."""
    etat = services.etat_generation(quiz)
    if etat and etat["etat"] in ("fini", "erreur"):
        for niveau, texte in etat.get("messages", []):
            messages.add_message(request, getattr(messages, niveau.upper()), texte)
        services.oublier_generation(quiz)
        return None
    return etat


def generer(request, quiz, total, travail):
    """Génère tout de suite, ou en arrière-plan au-delà de services.SEUIL_GENERATION_FOND
    copies. travail(quiz, progres) -> [(niveau, texte)] ; il lève ValueError pour un
    refus à dire tel quel (fiche trop longue…)."""
    if services.generation_en_cours(quiz):
        messages.error(request, "Une génération des documents est déjà en cours : attendez "
                                "qu'elle se termine (l'avancement est affiché en haut).")
    elif total > services.SEUIL_GENERATION_FOND:
        services.lancer_generation(quiz, total, travail)
        messages.info(request, f"Génération de {total} copies lancée en arrière-plan : la page "
                               "suit l'avancement, vous pouvez la quitter et revenir. Les "
                               "documents précédents restent en place jusqu'à la fin.")
    else:
        try:
            for niveau, texte in travail(quiz, None):
                messages.add_message(request, getattr(messages, niveau.upper()), texte)
        except ValueError as exc:
            messages.error(request, str(exc))


def _generer_documents(quiz, nb_exemplaires=100, progres=None):
    """Copies à imprimer (page de garde + pages lignées) et sujet.
    Retourne les messages à afficher : [(niveau, texte)]."""
    services.question_note(quiz)
    layout = layout_mod.build_layout(quiz)
    # copies imprimées avant : lues de travers si ce que le scan lit a bougé (comme pour
    # la feuille d'un QCM, views._generer_fiche_qcm)
    ancienne = quiz.layout_json
    zones_changees = bool(ancienne) and services.zones_lues_changees(ancienne, layout)
    grille_avant = ((ancienne or {}).get("pages") or [{}])[0].get("note_grid") or {}
    if quiz.exemplaires_anonymes:
        n = nb_exemplaires
        pdf = sheet_pdf.generate_sheet_pdf(quiz, layout, serials=list(range(1, n + 1)),
                                           progres=progres)
        quoi = f"{n} copies anonymes"
    elif quiz.id_mode == "qr":
        pdf, n = services.copies_nominatives_pdf(quiz, layout, progres)
        quoi = (f"{n} copies anonymes à n° de place (et la liste des candidats et des places)"
                if quiz.place_anonyme else
                f"{n} copies à talon (nom à découper avant la correction, "
                "QR imprimé)" if quiz.talon_nom else
                f"{n} copies nominatives (nom et QR imprimés)")
    else:
        n = 1
        pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
        quoi = "la copie (une par étudiant)"
    layout["nb_copies"] = n
    layout["nb_pages"] = sheet_pdf.nombre_de_pages(pdf)
    layout["signature"] = layout_mod.layout_signature(quiz)
    layout["sujet_version"] = sheet_pdf.SUJET_VERSION
    quiz.layout_json = layout
    quiz.sheet_pdf.save(f"copies_ecrit_{quiz.pk}.pdf", ContentFile(pdf), save=False)
    quiz.subject_pdf.save(f"sujet_ecrit_{quiz.pk}.pdf",
                          ContentFile(sheet_pdf.generate_subject_pdf(quiz)), save=False)
    quiz.save()
    msg = f"Documents prêts : le sujet et {quoi}."
    # talon ou n° de place : le QR est déjà imprimé sur la copie, rien à coller
    if not quiz.copies_nominatives and (quiz.id_mode in ("sticker", "grid") or quiz.auto_enroll):
        msg += (" Imprimez aussi la planche d'étiquettes QR (page de la classe "
                "ou des candidats) : chaque étudiant colle la sienne sur sa copie.")
    a_dire = [("success", msg)]
    if zones_changees:
        grille = layout["pages"][0].get("note_grid") or {}
        if grille.get("modele") == "demi" and grille_avant and not grille_avant.get("modele"):
            a_dire.append(("warning", "Nouvelle grille de la note : partie entière de 0 à "
                                      f"{grille['sur']:g}, décimale ,0 ou ,5. Les copies "
                                      "imprimées avant cette génération portent l'ancienne "
                                      "grille : imprimez et distribuez les nouvelles. "
                                      "Scannée malgré tout, une ancienne copie aurait sa note "
                                      "« à vérifier », jamais fausse."))
        else:
            a_dire.append(("warning", "Ce que le scan lit sur la copie a changé de place : "
                                      "une copie imprimée avant cette génération serait lue "
                                      "de travers. Imprimez et distribuez les nouvelles."))
    return a_dire


def _ecrit(request, quiz, fil=None):
    form = EpreuveEcritForm(instance=quiz)
    if request.method == "POST":
        droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
        if "reglages" in request.POST:
            ancienne_note = quiz.note_admission     # avant : le formulaire valide l'instance
            form = EpreuveEcritForm(request.POST, instance=quiz)
            if form.is_valid():
                _renommer(request, quiz)
                form.save()
                services.question_note(quiz)
                journal.noter(request.user, "note_admission", quiz=quiz,
                              objet="Note minimale d'admission",
                              avant=ancienne_note, apres=quiz.note_admission)
                # Le sujet n'est jamais scanné : on le refait tout de suite, comme pour un
                # QCM ; pas pour la note minimale, qui n'y est pas imprimée.
                if set(form.changed_data) - {"note_admission"}:
                    services.actualiser_sujet(quiz, forcer=True)
                messages.success(request, "Épreuve enregistrée.")
                return redirect("quiz_detail", pk=quiz.pk)
        elif "generer" in request.POST:
            if quiz.id_mode == "qr" and not quiz.exemplaires_anonymes \
                    and not quiz.class_group.students.exists():
                messages.error(request, "Copies nominatives : ajoutez d'abord les "
                                        "étudiants de la classe.")
            else:
                try:
                    n = max(1, min(int(request.POST.get("nb", 100)), 2000))
                except ValueError:
                    n = 100
                generer(request, quiz, nombre_de_copies(quiz, n),
                        lambda q, progres: _generer_documents(q, n, progres))
            return redirect("quiz_detail", pk=quiz.pk)

    generation = suivre_generation(request, quiz)
    perimee = bool(quiz.layout_json) and (
        quiz.layout_json.get("signature") != layout_mod.layout_signature(quiz))
    return render(request, "grader/epreuve_ecrit.html", {
        "quiz": quiz, "fil": fil, "form": form, "retour": _retour(quiz),
        "generation": generation,
        "perimee": perimee, "upload_form": UploadForm(),
        "batches": quiz.batches.all(), "etape": services.etat_etape(quiz),
        "journal": quiz.modifications.all()[:15],
        "n_journal": quiz.modifications.count(),
    })


# -------------------------------------------------------------------- oral
# Depuis le 13 novembre 2026, les notes de l'oral s'importent d'un fichier Excel, et de
# lui seul (plus de fiches d'évaluation imprimées puis scannées). Les fiches déjà scannées
# avant comptent toujours ; un import remplace leur note.

def _oral(request, quiz, fil=None):
    form = EpreuveOralForm(instance=quiz)
    if request.method == "POST":
        if "reglages" in request.POST:
            droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
            ancienne_note = quiz.note_admission     # avant : le formulaire valide l'instance
            form = EpreuveOralForm(request.POST, instance=quiz)
            if form.is_valid():
                _renommer(request, quiz)
                form.save()
                services.question_note(quiz)
                journal.noter(request.user, "note_admission", quiz=quiz,
                              objet="Note minimale d'admission",
                              avant=ancienne_note, apres=quiz.note_admission)
                messages.success(request, "Épreuve enregistrée.")
                return redirect("quiz_detail", pk=quiz.pk)
        elif "importer_notes" in request.POST:
            # Les notes de l'oral ne viennent QUE d'un fichier Excel (demande de
            # l'utilisateur, 13 novembre 2026) : ni fiche scannée, ni saisie à l'écran.
            droits.exiger(request.user, droits.CORRIGER, "saisir les notes")
            fichier = request.FILES.get("fichier")
            if fichier is None or not fichier.name.lower().endswith(".xlsx"):
                messages.error(request, "Choisissez le fichier Excel (.xlsx) des notes : le "
                                        "modèle de l'épreuve, rempli.")
            else:
                try:
                    notes = importers.notes_du_classeur(fichier.read(), quiz.note_sur)
                    nouvelles, modifiees, inchangees = services.enregistrer_notes_orales(
                        quiz, notes, request.user)
                except ValueError as exc:            # importers.ImportError_ compris
                    messages.error(request, str(exc))
                else:
                    if not notes:
                        messages.warning(request, "Aucune note dans le fichier : remplissez "
                                                  "la colonne « Note » du modèle.")
                    else:
                        messages.success(request, f"Notes importées : {nouvelles} nouvelle(s), "
                                                  f"{modifiees} modifiée(s), {inchangees} "
                                                  "inchangée(s).")
            return redirect("quiz_detail", pk=quiz.pk)

    lignes = services.notes_orales(quiz)
    return render(request, "grader/epreuve_oral.html", {
        "quiz": quiz, "fil": fil, "form": form, "retour": _retour(quiz), "lignes": lignes,
        "n_notes": sum(1 for _, etat, r in lignes
                       if etat != "attendue" and r.points_awarded is not None),
        "lot_import": quiz.batches.filter(label=services.LOT_ORAL).first(),
        "etape": services.etat_etape(quiz),
        "journal": quiz.modifications.all()[:15],
        "n_journal": quiz.modifications.count(),
    })


def notes_orales_xlsx(request, quiz):
    """Modèle Excel des notes de l'oral : les candidats, et leur note s'ils en ont une."""
    from django.http import HttpResponse

    from .export import notes_orales_workbook
    reponse = HttpResponse(
        notes_orales_workbook(quiz, services.notes_orales(quiz)),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    reponse["Content-Disposition"] = f'attachment; filename="notes_oral_{quiz.pk}.xlsx"'
    return reponse
