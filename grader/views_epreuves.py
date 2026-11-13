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
    fois = max(len(quiz.matieres), 1)         # écrit en matières : des copies pour chacune
    if quiz.exemplaires_anonymes:
        return nb_exemplaires * fois
    if quiz.id_mode == "qr" or quiz.type_epreuve == "oral":
        return quiz.class_group.students.count() * fois
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
    # Écrit en matières (13 novembre 2026) : chaque matière a ses copies (sa page de
    # garde et ses pages lignées) et son sujet, imprimés à part, chacun pour sa séance.
    matieres = quiz.matieres
    pdfs, fait = [], [0]
    for page in (range(len(matieres)) if matieres else [None]):
        une = layout if page is None else {**layout, "pages": [layout["pages"][page]]}
        suivi = progres
        if progres is not None and matieres:
            def suivi(n, avant=fait[0]):            # copies des matières déjà faites
                progres(avant + n)
        if quiz.exemplaires_anonymes:
            n = nb_exemplaires
            pdf = sheet_pdf.generate_sheet_pdf(quiz, une, serials=list(range(1, n + 1)),
                                               progres=suivi)
        elif quiz.id_mode == "qr":
            pdf, n = services.copies_nominatives_pdf(quiz, layout, suivi, page=page)
        else:
            n = 1
            pdf = sheet_pdf.generate_sheet_pdf(quiz, une)
        pdfs.append(pdf)
        fait[0] += n
    if quiz.exemplaires_anonymes:
        quoi = f"{n} copies anonymes"
    elif quiz.id_mode == "qr":
        quoi = (f"{n} copies anonymes à n° de place (et la liste des candidats et des places)"
                if quiz.place_anonyme else
                f"{n} copies à talon (nom à découper avant la correction, "
                "QR imprimé)" if quiz.talon_nom else
                f"{n} copies nominatives (nom et QR imprimés)")
    else:
        quoi = "la copie (une par étudiant)"
    layout["nb_copies"] = n
    layout["nb_pages"] = sheet_pdf.nombre_de_pages(pdfs[0])
    layout["signature"] = layout_mod.layout_signature(quiz)
    layout["sujet_version"] = sheet_pdf.SUJET_VERSION
    if matieres:
        layout["documents_matieres"] = services.enregistrer_documents_matieres(
            quiz, matieres, pdfs, (ancienne or {}).get("documents_matieres", []))
        # la première matière tient lieu de copies et de sujet de l'épreuve
        quiz.sheet_pdf.name = layout["documents_matieres"][0]["copies"]
        quiz.subject_pdf.name = layout["documents_matieres"][0]["sujet"]
        quoi = f"{quoi}, pour chacune des {len(matieres)} matières"
    else:
        quiz.sheet_pdf.save(f"copies_ecrit_{quiz.pk}.pdf", ContentFile(pdfs[0]), save=False)
        quiz.subject_pdf.save(f"sujet_ecrit_{quiz.pk}.pdf",
                              ContentFile(sheet_pdf.generate_subject_pdf(quiz)), save=False)
    quiz.layout_json = layout
    quiz.save()
    msg = f"Documents prêts : {'les sujets' if matieres else 'le sujet'} et {quoi}."
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


def _lire_matieres(post, matieres):
    """Réglages des matières d'un écrit, envoyés avec ceux de l'épreuve : les champs
    « matiere_<pk>_nom », « _coefficient », « _sujet », « _duree », « _date ». Un champ
    absent garde la valeur de la matière. Retourne ({matière: {champ: valeur}}, erreurs)."""
    from django.core.exceptions import ValidationError

    from .models import Question
    champ_date = Question._meta.get_field("date_epreuve").formfield()
    valeurs, erreurs = {}, []
    for m in matieres:
        cle = f"matiere_{m.pk}_"
        v = {}
        if cle + "nom" in post:
            nom = post[cle + "nom"].strip()[:500]
            if nom:
                v["text"] = nom
            else:
                erreurs.append(f"Donnez un nom à la matière « {m.text} ».")
        if cle + "coefficient" in post:
            try:
                coefficient = float(post[cle + "coefficient"].replace(",", "."))
            except ValueError:
                coefficient = None
            if coefficient is None or not 0.1 <= coefficient <= 99:
                erreurs.append(f"{m.text} : le coefficient va de 0,1 à 99.")
            else:
                v["coefficient"] = coefficient
        if cle + "sujet" in post:
            v["sujet"] = post[cle + "sujet"].replace("\r", "").strip()
        if cle + "duree" in post:
            v["duree"] = post[cle + "duree"].strip()[:60]
        if cle + "date" in post:
            try:
                v["date_epreuve"] = champ_date.clean(post[cle + "date"])
            except ValidationError:
                erreurs.append(f"{m.text} : date invalide.")
        valeurs[m] = v
    return valeurs, erreurs


def _ecrit(request, quiz, fil=None):
    form = EpreuveEcritForm(instance=quiz)
    if request.method == "POST":
        droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
        if "creer_matieres" in request.POST:
            # un écrit de concours créé avant ce choix : ses trois matières
            services.creer_matieres(quiz)
            messages.success(request, "L'écrit compte désormais trois matières ; réglez leurs "
                                      "coefficients et leurs sujets, puis générez les documents.")
            return redirect("quiz_detail", pk=quiz.pk)
        if "ajouter_matiere" in request.POST:
            # une matière de plus (le premier ajout fait d'un écrit d'une note un écrit en
            # matières, 13 novembre 2026) ; les copies sont alors à régénérer
            m = services.ajouter_matiere(quiz)
            messages.success(request, f"Matière ajoutée : « {m.text} ». Donnez-lui son nom "
                                      "et son sujet, puis régénérez les documents.")
            return redirect("quiz_detail", pk=quiz.pk)
        if "retirer_matiere" in request.POST:
            m = quiz.questions.filter(pk=request.POST["retirer_matiere"]).first()
            try:
                if m is None:
                    raise ValueError("Matière introuvable.")
                services.retirer_matiere(quiz, m)
                messages.success(request, f"Matière retirée : « {m.text} ».")
            except ValueError as exc:
                messages.error(request, str(exc))
            return redirect("quiz_detail", pk=quiz.pk)
        if "reglages" in request.POST:
            ancienne_note = quiz.note_admission     # avant : le formulaire valide l'instance
            ancienne_annee = quiz.annee
            matieres = quiz.matieres
            reglages_matieres, erreurs = _lire_matieres(request.POST, matieres)
            form = EpreuveEcritForm(request.POST, instance=quiz)
            for erreur in erreurs:
                messages.error(request, erreur)
            if form.is_valid() and not erreurs:
                _renommer(request, quiz)
                form.save()
                changees = False
                for m, v in reglages_matieres.items():
                    changees |= any(getattr(m, k) != val for k, val in v.items())
                    if v:
                        for k, val in v.items():
                            setattr(m, k, val)
                        m.save(update_fields=list(v))
                if changees and quiz.subject_pdf:
                    services.actualiser_sujet(quiz, forcer=True)    # sujets des matières
                if quiz.annee and quiz.annee != ancienne_annee:
                    services.changer_annee(quiz, quiz.annee)    # toutes les étapes du concours
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
    # écrit en matières : les documents de chacune, de la dernière génération
    from django.core.files.storage import default_storage
    matieres = quiz.matieres
    documents = [dict(d, copies_url=default_storage.url(d["copies"]),
                      sujet_url=default_storage.url(d["sujet"]))
                 for d in (quiz.layout_json or {}).get("documents_matieres", [])
                 if matieres and quiz.sheet_pdf]
    return render(request, "grader/epreuve_ecrit.html", {
        "quiz": quiz, "fil": fil, "form": form, "retour": _retour(quiz),
        "matieres": matieres, "documents_matieres": documents,
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
        if "ajouter_matiere" in request.POST or "retirer_matiere" in request.POST:
            # notes de l'oral par matière (13 novembre 2026) : une de plus, ou une de moins
            droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
            if "ajouter_matiere" in request.POST:
                m = services.ajouter_matiere(quiz)
                messages.success(request, f"Note ajoutée : « {m.text} ». Donnez-lui son nom, "
                                          "puis téléchargez le nouveau modèle Excel.")
            else:
                m = quiz.questions.filter(pk=request.POST["retirer_matiere"]).first()
                try:
                    if m is None:
                        raise ValueError("Matière introuvable.")
                    services.retirer_matiere(quiz, m)
                    messages.success(request, f"Note retirée : « {m.text} ».")
                except ValueError as exc:
                    messages.error(request, str(exc))
            return redirect("quiz_detail", pk=quiz.pk)
        if "reglages" in request.POST:
            droits.exiger(request.user, droits.CREER, "modifier l'épreuve")
            ancienne_note = quiz.note_admission     # avant : le formulaire valide l'instance
            reglages_matieres, erreurs = _lire_matieres(request.POST, quiz.matieres)
            for erreur in erreurs:
                messages.error(request, erreur)
            form = EpreuveOralForm(request.POST, instance=quiz)
            if form.is_valid() and not erreurs:
                _renommer(request, quiz)
                form.save()
                for m, v in reglages_matieres.items():
                    if v:
                        for k, val in v.items():
                            setattr(m, k, val)
                        m.save(update_fields=list(v))
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
                    notes = importers.notes_du_classeur(fichier.read(), quiz.note_sur,
                                                        quiz.matieres)
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
    matieres = quiz.matieres
    if matieres:
        # oral en matières : la moyenne pondérée de chacun, comme dans les résultats
        moyennes = {r["student"].pk: r["score"] for r in services.compute_results(quiz)}
        lignes = [(e, etat, reponses, moyennes.get(e.pk)) for e, etat, reponses in lignes]
    else:
        lignes = [(e, etat, reponses, None) for e, etat, reponses in lignes]
    return render(request, "grader/epreuve_oral.html", {
        "quiz": quiz, "fil": fil, "form": form, "retour": _retour(quiz), "lignes": lignes,
        "matieres": matieres,
        "n_notes": sum(1 for _, etat, reponses, _ in lignes
                       if etat != "attendue"
                       and all(r is not None and r.points_awarded is not None
                               for r in reponses)),
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
