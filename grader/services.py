"""Orchestration : téléversement -> lecture -> notation -> résultats.

La correction d'un lot se fait en arrière-plan dès l'envoi (thread) : la
requête de téléversement rend la main immédiatement et la page du lot
affiche un bandeau « N / total copies notées » qui se met à jour. Aucune
limite de volume : un concours de plusieurs milliers de copies est traité
page par page, et un traitement interrompu (redémarrage du serveur) peut
être relancé — les pages déjà notées ne sont pas relues.
"""
import contextlib
import logging
import os
import threading
import time
import uuid

import cv2
import numpy as np
import pymupdf
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection, transaction
from django.db.models import Avg, Count, Max, Min, Sum

from . import journal, omr, sheet_pdf
from . import layout as layout_mod
from .models import (Answer, AnswerKeySheet, Quiz, ScanBatch, SheetScan,
                     Student)

log = logging.getLogger(__name__)

SCAN_DPI = 200
# Un lot sans progression depuis ce délai (s) est considéré interrompu
STALLED_AFTER = 300
# Sérialise la lecture des pages (création automatique des candidats en
# mode concours, écritures SQLite) quand plusieurs lots tournent à la fois.
_PROCESS_LOCK = threading.Lock()


def _is_pdf(name):
    return name.lower().endswith(".pdf")


def _page_count(name, data):
    if _is_pdf(name):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    return 1


def _save_jpg(field, img_bgr, filename, quality=82):
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        field.save(filename, ContentFile(buf.tobytes()), save=False)


def create_batch(quiz: Quiz, files, label="", status="processing"):
    """Enregistre les fichiers téléversés et crée le lot (sans le traiter).
    status="pending" : lot mis en attente (concours en « correction après
    scan »), corrigé quand l'enseignant lance la correction."""
    batch = ScanBatch.objects.create(quiz=quiz, label=label, status=status)
    stored, total = [], 0
    used = set()
    for f in files:
        data = f.read()
        name = base = os.path.basename(f.name) or "scan"
        k = 2
        while name in used:          # deux fichiers de même nom dans le lot
            name = f"{base} ({k})"
            k += 1
        used.add(name)
        try:
            total += _page_count(name, data)
        except Exception:  # noqa: BLE001 — fichier illisible : signalé au traitement
            total += 1
        path = default_storage.save(
            f"uploads/lot_{batch.pk}/{uuid.uuid4().hex[:8]}_{name}", ContentFile(data))
        stored.append({"name": name, "path": path})
    batch.source_files = stored
    batch.total_pages = total
    batch.save(update_fields=["source_files", "total_pages", "updated_at"])
    return batch


def run_batch(batch_id):
    """Lit et note toutes les pages d'un lot. Reprenable : les pages déjà
    enregistrées (même nom de source) sont ignorées."""
    batch = ScanBatch.objects.select_related("quiz__class_group").get(pk=batch_id)
    quiz = batch.quiz
    layout = quiz.layout_json
    students = list(quiz.class_group.students.all())
    questions = {q.order: q for q in quiz.questions.all()}
    done = set(batch.sheets.values_list("source_name", flat=True))
    batch.processed_pages = len(done)
    batch.status = "processing"
    batch.save(update_fields=["processed_pages", "status", "updated_at"])
    try:
        for src in batch.source_files:
            with default_storage.open(src["path"], "rb") as fh:
                data = fh.read()
            # ExitStack : le document PDF est refermé à la fin de chaque
            # fichier, même si une page lève une erreur inattendue.
            with contextlib.ExitStack() as stack:
                try:
                    pages = stack.enter_context(_open_pages(src["name"], data))
                except Exception as e:  # noqa: BLE001 — fichier corrompu
                    if src["name"] not in done:
                        SheetScan.objects.create(
                            batch=batch, source_name=src["name"], status="error",
                            error_message=f"Fichier illisible : {e}")
                        done.add(src["name"])
                        batch.processed_pages = len(done)
                        batch.save(update_fields=["processed_pages", "updated_at"])
                    continue
                for page_name, render in pages:
                    if page_name in done:
                        continue
                    sheet = SheetScan(batch=batch, source_name=page_name)
                    with _PROCESS_LOCK:
                        try:
                            img = render()
                            _save_jpg(sheet.image, img, f"scan_{batch.pk}.jpg")
                            _process_sheet(sheet, img, layout, students,
                                           questions, quiz)
                        except omr.MarkerError as e:
                            sheet.status = "no_markers"
                            sheet.error_message = str(e)
                        except Exception as e:  # noqa: BLE001 — page illisible
                            log.exception("Page illisible (%s)", page_name)
                            sheet.status = "error"
                            sheet.error_message = f"{type(e).__name__}: {e}"
                        sheet.save()
                    done.add(page_name)
                    batch.processed_pages = len(done)
                    batch.save(update_fields=["processed_pages", "updated_at"])
        batch.status = "done"
        batch.total_pages = max(batch.total_pages, batch.processed_pages)
        batch.save(update_fields=["status", "total_pages", "updated_at"])
    except Exception as e:  # noqa: BLE001
        log.exception("Échec du traitement du lot %s", batch_id)
        batch.status = "error"
        batch.error_message = f"{type(e).__name__}: {e}"
        batch.save(update_fields=["status", "error_message", "updated_at"])
    return batch


@contextlib.contextmanager
def _open_pages(name, data):
    """Gestionnaire de contexte donnant la liste des (nom de page, fonction
    de rendu -> image BGR) d'un fichier téléversé.

    Le PDF n'est ouvert qu'une fois et chaque page n'est rendue qu'à la
    demande (un lot de plusieurs milliers de pages ne tient pas en mémoire) ;
    le document est refermé à la sortie du bloc, sans quoi un gros lot
    accumulerait un document ouvert par fichier."""
    if not _is_pdf(name):
        def decode():
            img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"Format d'image non reconnu : {name}")
            return img
        yield [(name, decode)]
        return

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        def render(i):
            pix = doc[i].get_pixmap(dpi=SCAN_DPI, colorspace=pymupdf.csRGB)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, 3)
            return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        yield [(f"{name} (page {i + 1})", (lambda i=i: render(i)))
               for i in range(doc.page_count)]


def _run_in_thread(batch_ids):
    close_old_connections()
    try:
        for batch_id in batch_ids:
            run_batch(batch_id)
    finally:
        connection.close()


def start_batch(batch):
    """Lance la correction du lot en arrière-plan et rend la main.
    Lève CorrigeIncomplet si des bonnes réponses manquent encore."""
    corrige_incomplet(batch.quiz)
    return start_batches([batch])


def start_batches(batches):
    """Lance en arrière-plan la correction de plusieurs lots, l'un après
    l'autre (« Lancer la correction » d'un concours corrigé après scan).
    Les lots passent aussitôt « en cours » pour ne pas être lancés deux fois."""
    ids = [b.pk for b in batches]
    ScanBatch.objects.filter(pk__in=ids).update(status="processing")
    t = threading.Thread(target=_run_in_thread, args=(ids,),
                         name=f"quizscan-lots-{'-'.join(map(str, ids[:5]))}",
                         daemon=True)
    t.start()
    return t


class CorrigeIncomplet(RuntimeError):
    """Il manque des bonnes réponses : la correction ne peut pas démarrer."""

    def __init__(self, quiz):
        self.manquantes = list(
            quiz.questions_sans_corrige.values_list("order", flat=True))
        nums = ", ".join(str(n) for n in self.manquantes[:15])
        if len(self.manquantes) > 15:
            nums += " …"
        super().__init__(
            f"{len(self.manquantes)} question(s) QCM sans bonne réponse "
            f"(n° {nums}). Renseignez-les — à la main ou en scannant la fiche "
            "remplie avec le corrigé — avant de lancer la correction : sans "
            "elles, ces questions seraient comptées fausses pour tout le monde.")


def corrige_incomplet(quiz):
    """Lève CorrigeIncomplet si une question QCM n'a pas de bonne réponse."""
    if not quiz.corrige_complet:
        raise CorrigeIncomplet(quiz)


def launch_pending(quiz):
    """Lance la correction de tous les lots en attente du quiz.
    Retourne (nombre de lots, nombre de pages)."""
    pending = list(quiz.batches.filter(status="pending").order_by("created_at"))
    if pending:
        corrige_incomplet(quiz)      # refuse de noter sur un corrigé partiel
        start_batches(pending)
    return len(pending), sum(b.total_pages for b in pending)


def is_stalled(batch):
    """Lot resté « en cours » sans progression (serveur redémarré…)."""
    if batch.status != "processing":
        return False
    return (time.time() - batch.updated_at.timestamp()) > STALLED_AFTER


class LotEnCours(Exception):
    """Le lot est en cours de correction : on ne le supprime pas sous le
    travailleur qui le lit."""


def supprimer_lot(lot, utilisateur):
    """Supprime un lot téléversé par erreur, ses copies et leurs fichiers.

    Mauvais PDF, mauvaise classe, mauvaise épreuve : des copies étrangères
    mêlées aux résultats d'un concours sont graves, et seul l'administrateur
    pouvait jusqu'ici les retirer. La suppression est inscrite au journal
    avant d'avoir lieu, dans la même transaction.

    Les fichiers (scans d'origine, images redressées, images de contrôle,
    zones découpées) ne sont effacés qu'une fois la suppression validée en
    base : si elle échoue, rien n'est perdu.

    Renvoie le nombre de pages supprimées. Lève LotEnCours si le lot est en
    cours de lecture (un lot bloqué, lui, peut être supprimé).
    """
    if lot.status == "processing" and not is_stalled(lot):
        raise LotEnCours(
            "Ce lot est en cours de correction : attendez la fin du "
            "traitement avant de le supprimer.")

    copies = list(lot.sheets.all())
    fichiers = [getattr(c, champ).name for c in copies
                for champ in ("image", "warped_image", "overlay_image", "name_crop")
                if getattr(c, champ)]
    fichiers += [a.open_crop.name for a in
                 Answer.objects.filter(sheet__batch=lot).exclude(open_crop="")
                 if a.open_crop]
    fichiers += [f["path"] for f in (lot.source_files or []) if f.get("path")]
    n_pages = len(copies)
    n_attribuees = sum(1 for c in copies if c.student_id)
    dossier = f"uploads/lot_{lot.pk}"

    with transaction.atomic():
        journal.noter(utilisateur, "lot_supprime", quiz=lot.quiz, objet=str(lot),
                      avant=f"{n_pages} page(s), dont {n_attribuees} attribuée(s)",
                      apres="supprimé")
        lot.delete()
        transaction.on_commit(lambda: _effacer_fichiers(fichiers, dossier))
    return n_pages


def _effacer_fichiers(noms, dossier=None):
    """Efface des fichiers du stockage ; un fichier déjà absent n'est pas une
    erreur (il a pu être supprimé à la main)."""
    for nom in noms:
        try:
            default_storage.delete(nom)
        except OSError:
            log.warning("Fichier impossible à effacer : %s", nom)
    if dossier:
        try:
            os.rmdir(default_storage.path(dossier))
        except (OSError, NotImplementedError):
            pass                     # dossier non vide ou stockage distant


def process_uploaded_files(quiz: Quiz, files, label=""):
    """Crée un lot et le traite immédiatement (appel synchrone)."""
    batch = create_batch(quiz, files, label)
    return run_batch(batch.pk)


class _Attendu:
    """Porte une bonne réponse pour l'image de contrôle du corrigé : les
    cases lues y apparaissent en vert, comme une réponse juste."""

    def __init__(self, choice):
        self.correct_choice = choice


def read_answer_key(quiz, files, label=""):
    """Lit une fiche d'épreuve remplie avec les bonnes réponses et renseigne
    le corrigé du quiz.

    La fiche se scanne comme une copie ordinaire : mêmes repères, mêmes
    cases. Chaque page lue est conservée (AnswerKeySheet) avec son image de
    contrôle — elle fait foi sur l'origine du barème.

    Une case vide ou plusieurs cases cochées rendent la question ambiguë :
    elle est signalée et reste à renseigner à la main, le reste est appliqué.
    Retourne (pages, nb_renseignees, questions_ambigues)."""
    layout = quiz.layout_json
    if not layout:
        raise ValueError("Générez d'abord la fiche de réponses PDF.")
    questions = {q.order: q for q in quiz.questions.filter(qtype="qcm")}

    pages, ambigues, appliquees = [], [], {}
    for f in files:
        data = f.read()
        nom = os.path.basename(f.name) or "corrige"
        with contextlib.ExitStack() as stack:
            try:
                rendus = stack.enter_context(_open_pages(nom, data))
            except Exception as e:  # noqa: BLE001 — fichier corrompu
                pages.append(AnswerKeySheet.objects.create(
                    quiz=quiz, source_name=nom, status="error",
                    error_message=f"Fichier illisible : {e}"))
                continue
            for page_name, render in rendus:
                feuille = AnswerKeySheet(quiz=quiz, source_name=page_name)
                try:
                    img = render()
                    _save_jpg(feuille.image, img, "corrige.jpg")
                    _lire_page_corrige(feuille, img, layout, questions,
                                       appliquees, ambigues)
                except omr.MarkerError as e:
                    feuille.status = "no_markers"
                    feuille.error_message = str(e)
                except Exception as e:  # noqa: BLE001
                    log.exception("Corrigé illisible (%s)", page_name)
                    feuille.status = "error"
                    feuille.error_message = f"{type(e).__name__}: {e}"
                feuille.save()
                pages.append(feuille)

    for order, choix in appliquees.items():
        question = questions[order]
        question.correct_choice = choix
        question.save(update_fields=["correct_choice"])
    if appliquees:
        rescore_quiz(quiz)       # les copies déjà lues suivent le corrigé
    return pages, len(appliquees), sorted(set(ambigues))


def _lire_page_corrige(feuille, img, layout, questions, appliquees, ambigues):
    """Lit une page du corrigé et range ce qu'elle apporte."""
    warped, page_index = omr.detect_and_warp(img)
    feuille.page_index = page_index
    if page_index >= len(layout["pages"]):
        raise omr.MarkerError(
            f"Repères de la page {page_index + 1} alors que la fiche n'en "
            f"compte que {len(layout['pages'])} : cette page ne vient pas de "
            "la fiche de ce quiz.")
    page_layout = layout["pages"][page_index]
    resultats = omr.read_bubbles(warped, page_layout)

    lues = {}
    for item in page_layout["qcm"]:
        order = item["order"]
        res = resultats.get(order)
        if order not in questions or res is None:
            continue
        if res["choice"] is None or res["multiple"]:
            # case vide ou plusieurs cases : on ne devine pas un corrigé
            ambigues.append(order)
            continue
        lues[order] = res["choice"]
        appliquees[order] = res["choice"]

    _save_jpg(feuille.overlay_image,
              omr.draw_overlay(warped, page_layout, resultats,
                               {o: _Attendu(c) for o, c in lues.items()},
                               zone_nom=False),     # un corrigé n'a pas de nom
              "corrige_controle.jpg", quality=70)
    feuille.detected = {str(o): c for o, c in lues.items()}
    feuille.unreadable = sorted(
        o for o in (i["order"] for i in page_layout["qcm"])
        if o in questions and o not in lues)
    feuille.status = "ok" if not feuille.unreadable else "partial"


def _has_name_zone(page_layout, quiz):
    """La fiche porte-t-elle des cases NOM/PRÉNOM manuscrites à lire ?

    L'OCR du nom reste le secours d'identification des fiches qui en ont —
    pas des fiches où l'emplacement de l'étiquette QR remplace les cases
    nom/prénom : concours à étiquette autocollante, et mode grille (la
    disposition n'a alors pas de « name_boxes » ; une fiche grille générée
    avant ce changement en a, et son nom manuscrit reste lu). Le candidat
    n'y écrit rien, il n'y a donc rien à lire. Cette condition reproduit
    exactement ce que sheet_pdf.generate_sheet_pdf imprime."""
    if not page_layout.get("name_boxes"):
        return False
    sticker = (page_layout.get("qr") or {}).get("sticker")
    return not (quiz.auto_enroll and sticker)


# Ce que le scan lit sur une page : si ces positions sont identiques, une
# copie imprimée avec l'ancienne fiche se lit avec la nouvelle disposition.
_ZONES_LUES = ("id_grid", "qcm", "open")


def fiche_grille_perimee(quiz):
    """La feuille de réponses de ce quiz porte-t-elle encore les cases
    NOM/PRÉNOM, retirées depuis là où un code QR identifie la copie ?
    Modes « grille » et « étiquette » ; pas le mode « QR nominatif », dont le
    PDF contient une fiche par étudiant (ou N fiches anonymes) : il se
    régénère par le bouton, qui sait pour qui et combien.

    L'empreinte de la fiche n'entre pas en compte : les fiches générées par
    les premières versions n'en ont pas, et c'étaient justement celles qui
    restaient avec NOM/PRÉNOM. Le critère de sûreté est ailleurs : la
    comparaison des zones lues, dans moderniser_fiche_grille."""
    disposition = quiz.layout_json
    if (quiz.id_mode not in ("grid", "sticker") or not quiz.sheet_pdf
            or not disposition):
        return False
    return any(page.get("name_boxes") for page in disposition.get("pages", []))


def moderniser_fiche_grille(quiz):
    """Régénère la fiche d'un quiz en mode grille dont le PDF porte encore les
    cases NOM/PRÉNOM : l'emplacement de l'étiquette QR les remplace.

    Sans risque pour les copies déjà imprimées : on ne remplace la fiche que
    si tout ce que le scan lit (grille de n°, cases QCM, zones manuscrites)
    est au millimètre à la même place dans la nouvelle disposition. Sinon on
    ne touche à rien — la page du quiz signale déjà qu'une fiche modifiée
    est à régénérer. Retourne True si la fiche a été remplacée."""
    if not fiche_grille_perimee(quiz):
        return False
    ancienne = quiz.layout_json
    try:
        nouvelle = layout_mod.build_layout(quiz)
    except layout_mod.TooManyPages:
        return False
    if len(nouvelle["pages"]) != len(ancienne["pages"]):
        return False
    for page, avant in zip(nouvelle["pages"], ancienne["pages"]):
        if any(page.get(cle) != avant.get(cle) for cle in _ZONES_LUES):
            return False
    pdf = sheet_pdf.generate_sheet_pdf(quiz, nouvelle)
    # Le PDF est celui du quiz tel qu'il est : son empreinte est l'actuelle.
    nouvelle["signature"] = layout_mod.layout_signature(quiz)
    if "sujet_version" in ancienne:
        nouvelle["sujet_version"] = ancienne["sujet_version"]
    quiz.layout_json = nouvelle
    quiz.sheet_pdf.save(f"fiche_quiz_{quiz.pk}.pdf", ContentFile(pdf), save=False)
    quiz.save(update_fields=["layout_json", "sheet_pdf"])
    log.info("Fiche du quiz %s mise à jour : cadre QR à la place de NOM/PRÉNOM",
             quiz.pk)
    return True


# Marque des images de contrôle nettoyées (nom du fichier) : le nettoyage
# ne se refait pas à chaque ouverture ni à chaque démarrage.
_CONTROLE_NET = "controle_net"


def _sans_cases_nom_imprimees(quiz):
    """Fiche de concours à étiquette : les cases NOM/PRÉNOM n'y ont jamais
    été imprimées (voir sheet_pdf.generate_sheet_pdf), mais l'ancienne image
    de contrôle les encadrait quand même."""
    return quiz.auto_enroll and quiz.id_mode in ("sticker", "qr")


def nettoyer_controle(sheet):
    """Efface les deux cadres bleus « NOM / PRÉNOM » d'une image de contrôle
    enregistrée avant la correction de omr.draw_overlay (zone_nom).

    Seulement là où ces cases n'existent pas sur la feuille (concours à
    étiquette). Dans la zone des cadres, l'image de contrôle n'est que l'image
    redressée plus ces cadres : on y recopie l'image redressée, conservée
    elle aussi. Le reste (cercles des réponses, QR) ne bouge pas.
    Retourne True si l'image a été réécrite."""
    quiz = sheet.batch.quiz
    if (not _sans_cases_nom_imprimees(quiz) or not sheet.overlay_image
            or not sheet.warped_image
            or _CONTROLE_NET in (sheet.overlay_image.name or "")):
        return False
    try:
        with sheet.overlay_image.open("rb") as f:
            controle = cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
        with sheet.warped_image.open("rb") as f:
            redressee = cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return False
    if controle is None or redressee is None or controle.shape != redressee.shape:
        return False
    marge = 6   # trait de 2 px, débordement de l'antialiasing et du JPEG
    # Les cadres étaient dessinés aux emplacements standard des cases.
    for x, y, w, h in layout_mod._name_boxes().values():
        x0, y0 = max(omr._mm(x) - marge, 0), max(omr._mm(y) - marge, 0)
        x1, y1 = omr._mm(x + w) + marge, omr._mm(y + h) + marge
        controle[y0:y1, x0:x1] = redressee[y0:y1, x0:x1]
    _save_jpg(sheet.overlay_image, controle, f"{_CONTROLE_NET}.jpg", quality=70)
    sheet.save(update_fields=["overlay_image"])
    return True


def actualiser_sujet(quiz, forcer=False):
    """Refait le SUJET (document des questions, mode « sujet séparé ») s'il a
    été dessiné par une version antérieure — par exemple un sujet de concours
    sans les choix des QCM. Le sujet n'est jamais scanné : le refaire ne
    touche ni à la feuille de réponses ni à la lecture des copies.
    forcer : refaire même à jour (en-tête ou durée modifiés).
    Retourne True si le sujet a été remplacé."""
    disposition = quiz.layout_json
    if quiz.sheet_mode != "split" or not quiz.subject_pdf or not disposition:
        return False
    if not forcer and disposition.get("sujet_version") == sheet_pdf.SUJET_VERSION:
        return False
    pdf = sheet_pdf.generate_subject_pdf(quiz)
    quiz.subject_pdf.save(f"sujet_quiz_{quiz.pk}.pdf", ContentFile(pdf), save=False)
    quiz.layout_json = {**disposition, "sujet_version": sheet_pdf.SUJET_VERSION}
    quiz.save(update_fields=["layout_json", "subject_pdf"])
    log.info("Sujet du quiz %s mis à jour (version %s)", quiz.pk,
             sheet_pdf.SUJET_VERSION)
    return True


def _process_sheet(sheet, img, layout, students, questions, quiz):
    language = quiz.language
    warped, page_index = omr.detect_and_warp(img)
    sheet.page_index = page_index
    pages = layout["pages"]
    if page_index >= len(pages):
        raise omr.MarkerError(
            f"Repères de la page {page_index + 1} alors que la fiche du quiz "
            f"n'en compte que {len(pages)} : la fiche a été régénérée depuis "
            "l'impression de cette copie. Réimprimez la fiche en cours ou "
            "régénérez-la à l'identique.")
    page_layout = pages[page_index]

    # Identification : QR, puis grille de n° (lectures exactes), puis OCR du
    # nom. En mode concours, un candidat inconnu est créé automatiquement.
    student = None
    serial = None
    grid_id = ""
    if page_layout.get("qr"):
        kind, _quiz_id, value = omr.read_qr(warped, page_layout)
        if kind == "QS":
            student = next((s for s in students if s.pk == value), None)
            if student is not None:
                sheet.id_read = f"QR {student.student_number or student.pk}"
                sheet.match_score = 100.0
        elif kind == "QSA":
            serial = value
            sheet.id_read = f"Fiche {value:04d}"
    if student is None and page_layout.get("id_grid"):
        grid_id = omr.read_id_grid(warped, page_layout)
        sheet.id_read = grid_id or sheet.id_read
        student = omr.match_student_by_id(grid_id, students)
        if student is not None:
            sheet.match_score = 100.0

    if student is None and quiz.auto_enroll and (serial is not None or grid_id):
        # Mode concours : le candidat est créé à partir du numéro lu,
        # son nom est rempli avec ce que l'OCR arrive à lire. Sur une fiche
        # concours sans zone nom/prénom imprimée (identification portée par
        # l'étiquette autocollante), l'OCR n'a rien à lire : on s'en passe.
        number = f"{serial:04d}" if serial is not None else grid_id
        last_t = first_t = ""
        if _has_name_zone(page_layout, quiz):
            ocr_text, name_crop, (last_t, first_t) = omr.read_name(
                warped, page_layout, language)
            sheet.ocr_name_raw = ocr_text[:200]
            _save_jpg(sheet.name_crop, name_crop, "name.jpg")
        student = next((s for s in students
                        if (s.student_number or "").strip() == number), None)
        if student is None:
            student = quiz.class_group.students.filter(
                student_number=number).first()
        if student is None:
            student = Student.objects.create(
                class_group=quiz.class_group, student_number=number,
                last_name=(last_t.upper()[:80] or f"CANDIDAT {number}"),
                first_name=first_t[:80])
        if student not in students:
            students.append(student)
        sheet.match_score = 100.0
    elif student is None and _has_name_zone(page_layout, quiz):
        # secours automatique dans tous les modes : OCR du nom manuscrit
        ocr_text, name_crop, _parts = omr.read_name(warped, page_layout, language)
        sheet.ocr_name_raw = ocr_text[:200]
        student, score = omr.match_student(ocr_text, students)
        sheet.match_score = score
        _save_jpg(sheet.name_crop, name_crop, "name.jpg")
    sheet.student = student

    # Lecture des cases QCM
    results = omr.read_bubbles(warped, page_layout)
    overlay = omr.draw_overlay(warped, page_layout, results, questions,
                               zone_nom=_has_name_zone(page_layout, quiz))
    _save_jpg(sheet.warped_image, warped, "warped.jpg", quality=70)
    _save_jpg(sheet.overlay_image, overlay, "overlay.jpg", quality=70)

    sheet.status = "ok" if student is not None else "no_match"
    sheet.save()

    answers = []
    for item in page_layout["qcm"]:
        q = questions.get(item["order"])
        res = results.get(item["order"])
        if q is None or res is None:
            continue
        a = Answer(sheet=sheet, question=q,
                   detected_choice=res["choice"], is_multiple=res["multiple"],
                   fill_ratios=res["ratios"])
        a.points_awarded = _auto_points(q, a)
        answers.append(a)
    Answer.objects.bulk_create(answers)

    # Zones manuscrites : recadrage pour la correction à l'écran
    for item in page_layout["open"]:
        q = questions.get(item["order"])
        if q is None:
            continue
        a = Answer(sheet=sheet, question=q)
        crop = omr.crop_open_zone(warped, item["rect"])
        _save_jpg(a.open_crop, crop, "open.jpg")
        a.save()


def _auto_points(question, answer):
    quiz = question.quiz
    if question.correct_choice is None:
        # Bonne réponse pas encore renseignée : on ne note ni ne pénalise.
        # Filet de sécurité — la correction d'un lot est refusée tant que le
        # corrigé est incomplet (voir corrige_incomplet).
        return 0.0
    if answer.is_multiple:
        return -abs(quiz.wrong_penalty) if quiz.wrong_penalty else 0.0
    if answer.detected_choice is None:
        return 0.0
    if answer.detected_choice == question.correct_choice:
        return question.points
    return -abs(quiz.wrong_penalty) if quiz.wrong_penalty else 0.0


def rescore_sheet(sheet):
    """Recalcule les points automatiques (après correction d'identification
    ou modification manuelle d'une réponse)."""
    for a in sheet.answers.select_related("question"):
        if a.question.qtype == "qcm" and not a.manually_set:
            a.points_awarded = _auto_points(a.question, a)
            a.save(update_fields=["points_awarded"])


def rescore_quiz(quiz):
    """Recalcule toutes les copies du quiz après une modification du corrigé,
    du barème ou de la pénalité. Retourne le nombre de copies touchées."""
    answers = list(Answer.objects.filter(
        sheet__batch__quiz=quiz, question__qtype="qcm", manually_set=False)
        .select_related("question__quiz"))
    changed, sheets = [], set()
    for a in answers:
        pts = _auto_points(a.question, a)
        if pts != a.points_awarded:
            a.points_awarded = pts
            changed.append(a)
            sheets.add(a.sheet_id)
    Answer.objects.bulk_update(changed, ["points_awarded"], batch_size=500)
    return len(sheets)


def compute_results(quiz, batch=None):
    """Agrège les résultats par étudiant.

    Retourne une liste de dicts triée par nom :
    {student, score, max_score, answered, correct, wrong, blank,
     pending_open (questions manuscrites non corrigées), sheets}
    """
    sheets = SheetScan.objects.filter(batch__quiz=quiz).exclude(student=None)
    if batch is not None:
        sheets = sheets.filter(batch=batch)
    sheets = sheets.select_related("student").prefetch_related("answers__question")

    per_student = {}
    for sheet in sheets:
        entry = per_student.setdefault(sheet.student_id, {
            "student": sheet.student, "score": 0.0,
            "answered": 0, "correct": 0, "wrong": 0, "blank": 0,
            "pending_open": 0, "sheets": [], "seen_questions": set(),
            "answers": {},
        })
        entry["sheets"].append(sheet)
        for a in sheet.answers.all():
            if a.question_id in entry["seen_questions"]:
                continue  # doublon (page scannée deux fois) : première lecture retenue
            entry["seen_questions"].add(a.question_id)
            entry["answers"][a.question.order] = a
            if a.question.qtype == "qcm":
                if a.is_blank:
                    entry["blank"] += 1
                else:
                    entry["answered"] += 1
                    if a.is_correct:
                        entry["correct"] += 1
                    else:
                        entry["wrong"] += 1
            if a.points_awarded is not None:
                entry["score"] += a.points_awarded
                if a.question.qtype == "open":
                    entry["answered"] += 1
            elif a.question.qtype == "open":
                entry["pending_open"] += 1

    max_score = quiz.max_score
    rows = []
    for entry in per_student.values():
        entry.pop("seen_questions")
        entry["score"] = max(round(entry["score"], 2), 0.0)
        entry["max_score"] = max_score
        rows.append(entry)
    rows.sort(key=lambda r: (r["student"].last_name.lower(), r["student"].first_name.lower()))
    return rows


def admissions(quiz, rows):
    """Réussite au regard de la note minimale (concours : d'admission).

    Retourne None si aucun seuil n'est réglé. Sinon le classement par note
    décroissante (rang partagé en cas d'égalité : 1, 2, 2, 4) et, pour chaque
    candidat, son résultat :
      - « admis »      : note ≥ seuil ;
      - « en_attente » : sous le seuil, mais des réponses manuscrites restent
                         à noter — elles peuvent encore le faire passer ;
      - « refuse »     : sous le seuil, copie entièrement notée.
    Pose aussi row["resultat"] sur chaque ligne, pour le tableau principal.
    Les pages non identifiées n'y figurent pas (elles n'ont pas de candidat)."""
    seuil = quiz.note_admission
    if seuil is None:
        for r in rows:
            r["resultat"] = None
        return None
    for r in rows:
        if r["score"] >= seuil:
            r["resultat"] = "admis"
        elif r["pending_open"]:
            r["resultat"] = "en_attente"
        else:
            r["resultat"] = "refuse"
    classement = sorted(rows, key=lambda r: (-r["score"],
                                             r["student"].last_name.lower(),
                                             r["student"].first_name.lower()))
    rang, precedente = 0, None
    for i, r in enumerate(classement, start=1):
        if r["score"] != precedente:
            rang, precedente = i, r["score"]
        r["rang"] = rang
    admis = [r for r in classement if r["resultat"] == "admis"]
    n = len(rows)
    return {
        "seuil": seuil,
        "max_score": rows[0]["max_score"] if rows else None,
        "n": n,
        "n_admis": len(admis),
        "n_attente": sum(1 for r in rows if r["resultat"] == "en_attente"),
        "n_refuses": sum(1 for r in rows if r["resultat"] == "refuse"),
        "pct": round(len(admis) / n * 100) if n else 0,
        "admis": admis,
        "classement": classement,
    }


# Lecture de l'indice de discrimination (seuils usuels de la docimologie).
# Le cas qui compte ici est le dernier : un indice négatif n'est presque
# jamais une question difficile, c'est un corrigé faux.
DISCRIMINATION = [
    (0.40, "forte",
     "Sépare nettement ceux qui savent de ceux qui ne savent pas."),
    (0.20, "correcte", "Sépare convenablement."),
    (0.00, "faible",
     "Ne sépare presque pas : énoncé ambigu, ou réponse devinable."),
]
DISCRIMINATION_NEGATIVE = (
    "suspecte",
    "Les meilleures copies s'y trompent plus que les plus faibles : "
    "vérifiez la bonne réponse, elle est probablement erronée.")


def _discrimination(rows, ordre):
    """Écart de réussite entre le tiers fort et le tiers faible du classement.

    Renvoie (indice, étiquette, explication), ou None si l'effectif est trop
    petit pour que le calcul veuille dire quoi que ce soit : sous une
    dizaine de copies, trois bonnes réponses de plus d'un côté suffisent à
    faire basculer l'indice. Mieux vaut ne rien afficher qu'un chiffre
    trompeur sur lequel on refera une épreuve.
    """
    MINIMUM = 10
    notes = [r for r in rows if ordre in r["answers"]]
    if len(notes) < MINIMUM:
        return None
    notes.sort(key=lambda r: r["score"], reverse=True)
    taille = max(len(notes) // 3, 1)
    fort, faible = notes[:taille], notes[-taille:]

    def reussite(groupe):
        return sum(1 for r in groupe if r["answers"][ordre].is_correct) / len(groupe)

    indice = round(reussite(fort) - reussite(faible), 2)
    if indice < 0:
        return (indice,) + DISCRIMINATION_NEGATIVE
    for seuil, etiquette, explication in DISCRIMINATION:
        if indice >= seuil:
            return indice, etiquette, explication
    return indice, "faible", DISCRIMINATION[-1][2]


def _repartition(question, reponses):
    """Combien de copies pour chaque case, et combien hors des cases.

    « Multiple » (plusieurs cases noircies) et « sans réponse » sont comptés
    à part : ce ne sont pas des choix, mais ils expliquent une partie des
    fausses et méritent d'être visibles.
    """
    total = len(reponses)
    if not total:
        return []
    comptes = {i: 0 for i, _ in question.letter_options}
    multiples = vides = 0
    for a in reponses:
        if a.is_multiple:
            multiples += 1
        elif a.detected_choice is None:
            vides += 1
        elif a.detected_choice in comptes:
            comptes[a.detected_choice] += 1

    lignes = [{"lettre": lettre, "titre": f"Réponse {lettre}", "n": comptes[i],
               "pct": round(comptes[i] / total * 100),
               "bonne": i == question.correct_choice}
              for i, lettre in question.letter_options]
    # Colonnes étroites : le libellé court sous la barre, le long en infobulle.
    for court, titre, n in (("✱", "Plusieurs cases cochées", multiples),
                            ("∅", "Sans réponse", vides)):
        if n:
            lignes.append({"lettre": court, "titre": titre, "n": n,
                           "pct": round(n / total * 100), "bonne": False})
    return lignes


# Convergence : part des copies sur une même case, au-delà de laquelle une
# question que personne ne réussit devient suspecte. Deux tiers est prudent
# — une question difficile étale ses erreurs sur plusieurs distracteurs.
CONVERGENCE = 0.66
CONVERGENCE_MINIMUM = 5


def _convergence_suspecte(stat, repartition):
    """Personne n'a la bonne réponse, et presque tous ont coché la même autre.

    Renvoie l'explication à afficher, ou None. Ce test complète l'indice de
    discrimination, qui vaut 0 dans ce cas précis : si aucun des deux
    groupes ne réussit, l'écart entre eux est nul et rien ne ressort.
    """
    if stat["n"] < CONVERGENCE_MINIMUM or stat["correct"]:
        return None
    autres = [c for c in repartition if not c["bonne"] and c["n"]]
    if not autres:
        return None
    majoritaire = max(autres, key=lambda c: c["n"])
    if majoritaire["n"] / stat["n"] < CONVERGENCE:
        return None
    return (f"Aucune copie n'a la réponse enregistrée, et "
            f"{majoritaire['pct']} % ont coché « {majoritaire['lettre']} » : "
            "la bonne réponse est très probablement celle-là.")


# ----------------------------------------------------------- tableau de bord

# Durée d'une correction à la main, pour chiffrer le temps épargné. C'est
# une hypothèse, pas une mesure : elle est affichée en clair à l'écran
# plutôt que fondue dans le résultat.
MINUTES_PAR_COPIE_A_LA_MAIN = 2


def corriges_douteux(quizzes):
    """Épreuves dont une question a toutes les copies sur une même mauvaise case.

    Même règle que `_convergence_suspecte`, mais calculée par un GROUP BY :
    le nombre de lignes rendues suit le nombre de questions, pas le nombre
    de candidats. Seul ce détecteur-là est utilisé ici ; l'indice de
    discrimination, qui demande le score de chaque copie, reste sur la page
    de résultats de l'épreuve.

    Renvoie {id d'épreuve: [numéros de questions douteuses]}.
    """
    lignes = (Answer.objects
              .filter(question__quiz__in=quizzes, question__qtype="qcm",
                      question__correct_choice__isnull=False,
                      sheet__student__isnull=False)
              .values("question__quiz_id", "question__order",
                      "question__correct_choice", "detected_choice")
              .annotate(n=Count("pk")))

    # {(épreuve, question): {case lue: nombre, ...}}
    par_question = {}
    bonne_reponse = {}
    for ligne in lignes:
        cle = (ligne["question__quiz_id"], ligne["question__order"])
        par_question.setdefault(cle, {})[ligne["detected_choice"]] = ligne["n"]
        bonne_reponse[cle] = ligne["question__correct_choice"]

    douteux = {}
    for (quiz_id, ordre), comptes in par_question.items():
        total = sum(comptes.values())
        if total < CONVERGENCE_MINIMUM:
            continue
        if comptes.get(bonne_reponse[(quiz_id, ordre)]):
            continue                      # au moins une copie a la bonne
        autres = [n for case, n in comptes.items() if case is not None]
        if autres and max(autres) / total >= CONVERGENCE:
            douteux.setdefault(quiz_id, []).append(ordre)
    return {q: sorted(ordres) for q, ordres in douteux.items()}


def resume_epreuve(quiz):
    """Effectif, moyenne et extrêmes d'une épreuve, sans charger les copies.

    Deux requêtes : le total de points par candidat, puis la moyenne de ces
    totaux. Renvoie None si aucune copie n'est encore notée.
    """
    par_candidat = (Answer.objects
                    .filter(sheet__batch__quiz=quiz,
                            sheet__student__isnull=False,
                            points_awarded__isnull=False)
                    .values("sheet__student_id")
                    .annotate(total=Sum("points_awarded")))
    resume = par_candidat.aggregate(n=Count("sheet__student_id", distinct=True),
                                    moyenne=Avg("total"),
                                    meilleure=Max("total"),
                                    plus_basse=Min("total"))
    if not resume["n"]:
        return None
    bareme = quiz.max_score or 0
    moyenne = round(resume["moyenne"] or 0, 2)
    return {
        "quiz": quiz,
        "n": resume["n"],
        "moyenne": moyenne,
        "meilleure": round(resume["meilleure"] or 0, 2),
        "plus_basse": round(resume["plus_basse"] or 0, 2),
        "bareme": bareme,
        # Part du barème, pour la jauge : une moyenne de 8,5 ne veut rien
        # dire sans savoir si l'épreuve est sur 14 ou sur 20.
        "pct": round(moyenne / bareme * 100) if bareme else 0,
    }


def compute_stats(quiz, rows):
    """Statistiques de l'épreuve à partir des résultats par étudiant :
    résumé global, histogramme des notes et taux de réussite par question."""
    if not rows:
        return None
    scores = sorted(r["score"] for r in rows)
    n = len(scores)
    mid = n // 2
    median = scores[mid] if n % 2 else (scores[mid - 1] + scores[mid]) / 2
    max_score = quiz.max_score or 1
    moyenne = sum(scores) / n
    # Écart-type de population : on décrit les copies que l'on a, on
    # n'estime pas celles d'une promotion plus large.
    ecart_type = (sum((x - moyenne) ** 2 for x in scores) / n) ** 0.5

    # histogramme en 5 tranches de 20 % du barème
    bins = [0] * 5
    for s in scores:
        bins[min(int(s / max_score * 5), 4)] += 1
    biggest = max(bins) or 1
    histogram = [{"label": f"{i * 20}–{(i + 1) * 20} %", "count": c,
                  "pct": round(c / n * 100), "bar": round(c / biggest * 100)}
                 for i, c in enumerate(bins)]

    questions = []
    for q in quiz.questions.order_by("order"):
        st = {"question": q, "correct": 0, "wrong": 0, "blank": 0,
              "graded": 0, "points_sum": 0.0, "n": 0}
        for r in rows:
            a = r["answers"].get(q.order)
            if a is None:
                continue
            st["n"] += 1
            if q.qtype == "qcm":
                if a.is_blank:
                    st["blank"] += 1
                elif a.is_correct:
                    st["correct"] += 1
                else:
                    st["wrong"] += 1
            elif a.points_awarded is not None:
                st["graded"] += 1
                st["points_sum"] += a.points_awarded
        if q.qtype == "qcm":
            st["success"] = round(st["correct"] / st["n"] * 100) if st["n"] else 0
            mesure = _discrimination(rows, q.order)
            if mesure:
                st["discrimination"], st["discrimination_label"], \
                    st["discrimination_aide"] = mesure
                st["corrige_suspect"] = st["discrimination"] < 0
            st["repartition"] = _repartition(
                q, [r["answers"][q.order] for r in rows if q.order in r["answers"]])
            convergence = _convergence_suspecte(st, st["repartition"])
            if convergence:
                st["corrige_suspect"] = True
                st["discrimination_aide"] = convergence
                st["discrimination_label"] = "suspecte"
        else:
            st["success"] = (round(st["points_sum"] / (st["graded"] * q.points) * 100)
                             if st["graded"] and q.points else 0)
        questions.append(st)

    return {
        "n": n,
        "mean": round(moyenne, 2),
        "median": round(median, 2),
        "stdev": round(ecart_type, 2),
        "best": scores[-1],
        "worst": scores[0],
        "max_score": quiz.max_score,
        "histogram": histogram,
        "questions": questions,
        # Les questions dont la bonne réponse est probablement fausse :
        # remontées en tête de la page de résultats.
        "suspectes": [st["question"].order for st in questions
                      if st.get("corrige_suspect")],
    }
