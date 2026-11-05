"""Orchestration : téléversement -> lecture -> notation -> résultats.

La correction d'un lot se fait en arrière-plan dès l'envoi (thread) : la
requête de téléversement rend la main immédiatement et la page du lot
affiche un bandeau « N / total copies notées » qui se met à jour. Aucune
limite de volume : un concours de plusieurs milliers de copies est traité
page par page, et un traitement interrompu (redémarrage du serveur) peut
être relancé — les pages déjà notées ne sont pas relues.
"""
import collections
import contextlib
import io
import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

import cv2
import numpy as np
import pymupdf
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import Count, Q, Sum

from . import journal, omr, sheet_pdf
from . import layout as layout_mod
from .models import (Answer, AnswerKeySheet, ClassGroup, Inscrit, Question, Quiz,
                     ScanBatch, SheetScan, Student)

log = logging.getLogger(__name__)

SCAN_DPI = 200
# Un lot sans progression depuis ce délai (s) est considéré interrompu
STALLED_AFTER = 300
# Sérialise la lecture des pages (création automatique des candidats en
# mode concours) quand plusieurs lots tournent à la fois dans un même processus.
_PROCESS_LOCK = threading.Lock()


def _is_pdf(name):
    return name.lower().endswith(".pdf")


def _images_tiff(data):
    """Nombre d'images d'un TIFF (1 pour tout autre format). Un scanner à chargeur peut
    réunir tout un paquet dans un seul TIFF : OpenCV n'en lit que la première image, sans
    rien signaler, et le lot se terminait « sans erreur », amputé de toutes les autres."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data), formats=("TIFF",)) as im:
            return im.n_frames
    except Exception:  # noqa: BLE001 — pas un TIFF, ou illisible : signalé au traitement
        return 1


def _page_count(name, data):
    if _is_pdf(name):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    return _images_tiff(data)


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
        # 10 000 fichiers : un seul ouvert à la fois, et la place rendue aussitôt
        getattr(f, "effacer", f.close)()
        # la fin du nom, avec son extension : « … (page 1234) » doit tenir en 200
        name = base = os.path.basename(f.name)[-180:] or "scan"
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


def _enregistrer_copie(sheet):
    """Une page ne doit jamais arrêter un lot de 10 000 copies. Si la base refuse
    sa fiche (donnée trop longue pour PostgreSQL…), la page est signalée en erreur
    et le lot continue : sinon la reprise retomberait toujours sur elle."""
    try:
        sheet.save()
    except DatabaseError as e:
        log.exception("Page non enregistrée (%s)", sheet.source_name)
        SheetScan.objects.create(batch=sheet.batch, source_name=sheet.source_name[:200],
                                 status="error", error_message=f"Page non enregistrée : {e}")


def _fils_de_lecture():
    """Pages lues en même temps dans un lot : une par processeur, 4 au plus
    (LECTURE_FILS dans .env pour en changer ; 1 : une page après l'autre).
    OpenCV relâche le verrou de Python pendant ses calculs : les fils lisent
    vraiment en parallèle. La base, elle, reste au fil du lot."""
    try:
        demande = int(os.environ.get("LECTURE_FILS", "0"))
    except ValueError:
        demande = 0
    return max(1, demande or min(4, os.cpu_count() or 1))


# Verrou de PostgreSQL d'un lot en lecture : (VERROU_LOTS, n° du lot). Verrou de
# session : il tombe de lui-même quand le processus qui lit meurt (sa connexion se
# ferme). C'est à lui qu'on reconnaît un lot vraiment lu, quelle que soit la
# durée d'une étape, et il empêche deux lecteurs du même lot (« Relancer » cliqué
# pendant que la reprise automatique s'en charge).
VERROU_LOTS = 7240
# Un lot « en cours » sans verrou ni progrès depuis ce délai est repris d'office
REPRISE_APRES = 60


@contextlib.contextmanager
def verrou_du_lot(batch_id):
    """Prend le verrou du lot s'il est libre ; donne True si on l'a."""
    with connection.cursor() as curseur:
        curseur.execute("SELECT pg_try_advisory_lock(%s, %s)", [VERROU_LOTS, batch_id])
        obtenu = curseur.fetchone()[0]
    try:
        yield obtenu
    finally:
        if obtenu:
            with connection.cursor() as curseur:
                curseur.execute("SELECT pg_advisory_unlock(%s, %s)", [VERROU_LOTS, batch_id])


def lots_verrouilles():
    """N° des lots qu'un processus lit en ce moment (verrou tenu)."""
    with connection.cursor() as curseur:
        curseur.execute("SELECT objid FROM pg_locks WHERE locktype = 'advisory' "
                        "AND classid = %s AND objsubid = 2 AND granted", [VERROU_LOTS])
        return {int(ligne[0]) for ligne in curseur.fetchall()}


def lots_a_reprendre():
    """Lots « en cours » que personne ne lit et qui n'avancent plus : à relancer."""
    lots = list(ScanBatch.objects.filter(status="processing").order_by("created_at"))
    if not lots:
        return []
    lus = lots_verrouilles()
    limite = time.time() - REPRISE_APRES
    return [lot for lot in lots if lot.pk not in lus and lot.updated_at.timestamp() < limite]


def run_batch(batch_id):
    """Lit et note toutes les pages d'un lot, sauf s'il est déjà lu ailleurs (son
    verrou est pris) : rien ne se fait alors deux fois."""
    with verrou_du_lot(batch_id) as obtenu:
        if not obtenu:
            log.info("Lot %s déjà en lecture par un autre processus", batch_id)
            return ScanBatch.objects.get(pk=batch_id)
        return _lire_le_lot(batch_id)


def _lire_le_lot(batch_id):
    """Lit et note toutes les pages d'un lot. Reprenable : les pages déjà
    enregistrées (même nom de source) sont ignorées.

    Les pages se lisent à plusieurs en même temps (_lire_page, dans des fils), puis
    s'enregistrent ici une à une, dans l'ordre du paquet : l'ordre du scan sert à
    l'identification (seconde passe, page précédente). Le PDF n'est ouvert que par
    ce fil-ci : PyMuPDF ne se partage pas entre fils."""
    batch = ScanBatch.objects.select_related("quiz__class_group").get(pk=batch_id)
    quiz = batch.quiz
    layout = quiz.layout_json
    students = list(quiz.class_group.students.all())
    questions = {q.order: q for q in quiz.questions.all()}
    done = set(batch.sheets.values_list("source_name", flat=True))
    batch.processed_pages = len(done)
    batch.status = "processing"
    batch.save(update_fields=["processed_pages", "status", "updated_at"])
    fils = _fils_de_lecture()
    lecteurs = ThreadPoolExecutor(max_workers=fils, thread_name_prefix="quizscan-lecture")
    # (nom de page, lecture à venir), dans l'ordre du lot ; borné : chaque page
    # en attente garde ses images en mémoire
    en_vol = collections.deque()

    def enregistrer_la_premiere():
        page_name, futur = en_vol.popleft()
        lecture = futur.result()           # _lire_page ne lève jamais
        sheet = SheetScan(batch=batch, source_name=page_name)
        with _PROCESS_LOCK:
            try:
                if lecture.image_jpg:
                    sheet.image.save(f"scan_{batch.pk}.jpg",
                                     ContentFile(lecture.image_jpg), save=False)
                if lecture.erreur is not None:
                    if lecture.page_index is not None:
                        sheet.page_index = lecture.page_index
                    raise lecture.erreur
                _enregistrer_page(sheet, lecture, students, questions, quiz)
            except omr.MarkerError as e:
                if quiz.type_epreuve == "ecrit":
                    # copie scannée en entier : les pages de rédaction n'ont pas
                    # de repères, seule la page de garde est lue
                    sheet.status = "ignoree"
                    sheet.error_message = (
                        "Page sans repères : page de rédaction, "
                        "ignorée. Si c'est une page de garde, "
                        "rescannez-la bien à plat.")
                elif lecture.blanche:
                    # sinon, en recto-verso, un paquet de 500 feuilles
                    # imprimées d'un seul côté donne 500 pages en erreur
                    sheet.status = "ignoree"
                    sheet.error_message = "Page blanche : verso vide, ignoré."
                else:
                    sheet.status = "no_markers"
                    sheet.error_message = str(e)
            except Exception as e:  # noqa: BLE001 — page illisible
                log.exception("Page illisible (%s)", page_name)
                sheet.status = "error"
                sheet.error_message = f"{type(e).__name__}: {e}"
            _enregistrer_copie(sheet)
        done.add(page_name)
        batch.processed_pages = len(done)
        batch.save(update_fields=["processed_pages", "updated_at"])

    def tout_enregistrer():
        while en_vol:
            enregistrer_la_premiere()

    try:
        for src in batch.source_files:
            with default_storage.open(src["path"], "rb") as fh:
                data = fh.read()
            # ExitStack : le document PDF est refermé à la fin de chaque
            # fichier, même si une page lève une erreur inattendue.
            with contextlib.ExitStack() as stack:
                try:
                    pages = stack.enter_context(_open_pages(src["name"], data, differer=True))
                except Exception as e:  # noqa: BLE001 — fichier corrompu
                    if src["name"] not in done:
                        tout_enregistrer()      # les pages d'avant d'abord : l'ordre
                        SheetScan.objects.create(
                            batch=batch, source_name=src["name"][:200], status="error",
                            error_message=f"Fichier illisible : {e}")
                        done.add(src["name"])
                        batch.processed_pages = len(done)
                        batch.save(update_fields=["processed_pages", "updated_at"])
                    continue
                for page_name, render in pages:
                    page_name = page_name[:200]     # lot envoyé avant la borne de create_batch
                    if page_name in done or any(nom == page_name for nom, _ in en_vol):
                        continue
                    try:
                        img = render()          # ici : le PDF reste à ce fil
                    except Exception as e:  # noqa: BLE001 — page illisible
                        futur = Future()
                        futur.set_result(_Lecture(erreur=e))
                    else:
                        futur = lecteurs.submit(_lire_page_dans_un_fil, img, layout,
                                                questions, quiz)
                    en_vol.append((page_name, futur))
                    while len(en_vol) > 2 * fils:
                        enregistrer_la_premiere()
        tout_enregistrer()
        # Seconde passe : les copies restées « à identifier » profitent de
        # ce que les autres ont été reconnues (élimination).
        try:
            reidentifier_lot(batch)
        except Exception:  # noqa: BLE001 — n'empêche jamais la fin du lot
            log.exception("Seconde identification du lot %s", batch_id)
        batch.status = "done"
        batch.total_pages = max(batch.total_pages, batch.processed_pages)
        batch.save(update_fields=["status", "total_pages", "updated_at"])
    except Exception as e:  # noqa: BLE001
        log.exception("Échec du traitement du lot %s", batch_id)
        batch.status = "error"
        batch.error_message = f"{type(e).__name__}: {e}"
        batch.save(update_fields=["status", "error_message", "updated_at"])
    finally:
        lecteurs.shutdown(wait=True, cancel_futures=True)
    return batch


# Une page A4 à 200 ppp fait 4 millions de pixels, une A3 8 millions. Au-delà
# de 40 millions, ce n'est plus une copie : un petit fichier piégé (PDF à
# page géante, PNG très compressé) ferait sinon allouer des gigaoctets.
MAX_PIXELS_PAGE = 40_000_000
FORMATS_SCAN = ("JPEG", "PNG", "TIFF", "BMP", "WEBP")


def _controler_dimensions(name, data):
    """Refuse une image démesurée, ou d'un autre format qu'un scan, avant de
    la décoder (l'en-tête suffit). Le format se lit dans le contenu, pas dans
    l'extension : sans cette liste, un fichier « .png » atteindrait tous les
    décodeurs de Pillow et d'OpenCV, y compris ceux de formats exotiques qui
    ont des failles connues (FlashPix, EPS, XBM…)."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data), formats=FORMATS_SCAN) as im:
            largeur, hauteur = im.size
    except Image.DecompressionBombError as exc:
        raise ValueError(f"Image démesurée : {name}") from exc
    except Exception as exc:  # noqa: BLE001 — quelle qu'en soit la raison
        raise ValueError(f"Format d'image non reconnu : {name}") from exc
    if largeur * hauteur > MAX_PIXELS_PAGE:
        raise ValueError(f"Image démesurée ({largeur} × {hauteur} pixels) : {name}")


class _ADecoder:
    """Une image à décoder plus tard, dans un fil de lecture : decoder() ne touche
    ni à PyMuPDF ni à Pillow ouvert, seulement à des octets. Le fil du lot garde
    ainsi pour lui ce qui ne se partage pas, et ne décode plus les pages des autres."""
    __slots__ = ("decoder",)

    def __init__(self, decoder):
        self.decoder = decoder


def _image_du_scanner(doc, page):
    """L'image JPEG qu'un scanner a mise dans la page, lue telle quelle (ou None :
    la page se dessine). Voir _jpeg_du_scanner."""
    a_decoder = _jpeg_du_scanner(doc, page)
    return a_decoder.decoder() if a_decoder is not None else None


def _jpeg_du_scanner(doc, page):
    """L'image JPEG qu'un scanner a mise dans la page, à décoder telle quelle.

    Redessiner la page à 200 ppp coûtait 0,6 s par copie, le quart de sa
    lecture ; décoder le JPEG qu'elle contient, quelques centièmes. Seulement
    pour une page qui n'est que cette image, à plat et pleine page, en gris ou
    en couleur : sinon (texte, dessin, rotation, masque, noir et blanc 1 bit,
    CMJN…) None, et la page est dessinée comme avant. Une image plus fine que
    200 ppp y est ramenée, comme le faisait le rendu. Tout ce qui demande le
    PDF se fait ici ; le décodage, dans _ADecoder.decoder (None s'il échoue)."""
    if page.rotation % 360:
        return None
    images = page.get_images(full=True)
    if len(images) != 1 or images[0][1]:                 # une image, sans masque
        return None
    xref = images[0][0]
    cles = {cle: doc.xref_get_key(xref, cle) for cle in
            ("Filter", "ColorSpace", "BitsPerComponent", "Decode")}
    if (cles["Filter"] != ("name", "/DCTDecode")
            or cles["ColorSpace"] not in (("name", "/DeviceGray"), ("name", "/DeviceRGB"))
            or cles["BitsPerComponent"] != ("int", "8") or cles["Decode"][0] != "null"):
        return None
    # sans « xrefs=True » : il décode chaque image pour la reconnaître (0,4 s) ;
    # une seule image dans la page et un seul placement, c'est la même
    placements = page.get_image_info()
    if len(placements) != 1:
        return None
    a, b, c, d, _e, _f = placements[0]["transform"]
    largeur_pt, hauteur_pt = page.rect.width, page.rect.height
    x0, y0, x1, y1 = placements[0]["bbox"]
    if (abs(b) > 1e-3 or abs(c) > 1e-3 or a <= 0 or d <= 0          # ni tournée, ni retournée
            or max(abs(x0), abs(y0), abs(x1 - largeur_pt), abs(y1 - hauteur_pt)) > 2
            or page.get_drawings() or page.get_text("text").strip()):
        return None
    try:
        w, h = (int(doc.xref_get_key(xref, cle)[1]) for cle in ("Width", "Height"))
    except (TypeError, ValueError):
        return None
    if not w or not h or abs(w / h - largeur_pt / hauteur_pt) > 0.02 * largeur_pt / hauteur_pt:
        return None                                       # image étirée sur la page
    if w * h > MAX_PIXELS_PAGE:
        raise ValueError(f"Image démesurée ({w} × {h} pixels) dans la page.")
    brut = doc.xref_stream_raw(xref)
    from PIL import Image
    try:        # l'en-tête seul (une fraction de milliseconde) : un JPEG que le décodage refuserait
        with Image.open(io.BytesIO(brut), formats=("JPEG",)) as entete:
            if entete.size != (w, h) or entete.mode not in ("L", "RGB"):
                return None
    except Exception:  # noqa: BLE001 — en-tête illisible : la page se dessine
        return None
    cible = (round(largeur_pt / 72 * SCAN_DPI), round(hauteur_pt / 72 * SCAN_DPI))

    def decoder():
        img = cv2.imdecode(np.frombuffer(brut, dtype=np.uint8),
                           cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is not None and img.shape[1] > cible[0] * 1.05:
            img = cv2.resize(img, cible, interpolation=cv2.INTER_AREA)
        return img
    return _ADecoder(decoder)


@contextlib.contextmanager
def _open_pages(name, data, differer=False):
    """Gestionnaire de contexte donnant la liste des (nom de page, fonction
    de rendu -> image BGR) d'un fichier téléversé.

    Le PDF n'est ouvert qu'une fois et chaque page n'est rendue qu'à la
    demande (un lot de plusieurs milliers de pages ne tient pas en mémoire) ;
    le document est refermé à la sortie du bloc, sans quoi un gros lot
    accumulerait un document ouvert par fichier. Même chose pour un TIFF multipage.

    differer : la fonction de rendu peut rendre un _ADecoder au lieu de l'image,
    quand le décodage peut se faire dans un fil de lecture (image seule, JPEG d'un
    PDF de scanner). Une page à dessiner, un TIFF : l'image, tout de suite."""
    if not _is_pdf(name):
        nb_images = _images_tiff(data)
        if nb_images > 1:
            from PIL import Image
            with Image.open(io.BytesIO(data), formats=("TIFF",)) as tiff:
                def image_tiff(i):
                    tiff.seek(i)
                    # seule la première image passe par _controler_dimensions
                    largeur, hauteur = tiff.size
                    if largeur * hauteur > MAX_PIXELS_PAGE:
                        raise ValueError(f"Image démesurée ({largeur} × {hauteur} pixels) : {name}")
                    return cv2.cvtColor(np.array(tiff.convert("RGB")), cv2.COLOR_RGB2BGR)
                yield [(f"{name} (page {i + 1})", (lambda i=i: image_tiff(i)))
                       for i in range(nb_images)]
            return

        def decode():
            _controler_dimensions(name, data)
            img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"Format d'image non reconnu : {name}")
            return img
        yield [(name, (lambda: _ADecoder(decode)) if differer else decode)]
        return

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        def render(i):
            rect = doc[i].rect
            if (rect.width / 72 * SCAN_DPI) * (rect.height / 72 * SCAN_DPI) > MAX_PIXELS_PAGE:
                raise ValueError(f"Page démesurée ({rect.width / 72 * 2.54:.0f} × "
                                 f"{rect.height / 72 * 2.54:.0f} cm) : une copie se "
                                 "scanne en A4 ou A3.")
            if differer:
                a_decoder = _jpeg_du_scanner(doc, doc[i])
                if a_decoder is not None:
                    return a_decoder
            else:
                img = _image_du_scanner(doc, doc[i])
                if img is not None:
                    return img
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
    """Lot resté « en cours » sans progression (serveur redémarré…), que personne
    ne lit plus : une étape longue d'un lot qui tourne (son verrou est tenu) n'est
    pas une interruption."""
    if batch.status != "processing":
        return False
    if (time.time() - batch.updated_at.timestamp()) <= STALLED_AFTER:
        return False
    return batch.pk not in lots_verrouilles()


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


# Le corrigé se lit pendant la requête : un PDF de milliers de pages occuperait
# un des quatre fils du serveur jusqu'à son délai de 10 minutes. Un corrigé tient
# en quelques pages.
MAX_PAGES_CORRIGE = 50


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
    n_pages = 0
    for f in files:
        if n_pages >= MAX_PAGES_CORRIGE:
            break
        data = f.read()
        f.close()
        nom = os.path.basename(f.name)[-180:] or "corrige"
        with contextlib.ExitStack() as stack:
            try:
                rendus = stack.enter_context(_open_pages(nom, data))
            except Exception as e:  # noqa: BLE001 — fichier corrompu
                pages.append(AnswerKeySheet.objects.create(
                    quiz=quiz, source_name=nom, status="error",
                    error_message=f"Fichier illisible : {e}"))
                continue
            for page_name, render in rendus:
                n_pages += 1
                if n_pages > MAX_PAGES_CORRIGE:
                    pages.append(AnswerKeySheet.objects.create(
                        quiz=quiz, source_name=page_name, status="error",
                        error_message=f"Corrigé limité à {MAX_PAGES_CORRIGE} pages : "
                                      "cette page et les suivantes n'ont pas été lues."))
                    n_pages = MAX_PAGES_CORRIGE
                    break
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
_ZONES_LUES = ("id_grid", "qcm", "open", "note_grid")


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
    if not regenerer_fiche_compatible(quiz):
        return False
    log.info("Fiche du quiz %s mise à jour : cadre QR à la place de NOM/PRÉNOM",
             quiz.pk)
    return True


def regenerer_fiche_compatible(quiz):
    """Refait la feuille de réponses enregistrée, à condition que rien de ce
    que le scan lit (grille de n°, cases QCM, zones manuscrites) ne bouge
    d'un millimètre : les copies déjà imprimées restent lisibles. Sert quand
    seul l'habillage change (titre, cases NOM/PRÉNOM retirées).

    Pas en mode « QR nominatif » : son PDF contient une fiche par étudiant
    (ou N fiches anonymes), il se refait par le bouton, qui sait pour qui et
    combien. Retourne True si la feuille a été remplacée."""
    ancienne = quiz.layout_json
    if quiz.id_mode == "qr" or not quiz.sheet_pdf or not ancienne:
        return False
    try:
        nouvelle = layout_mod.build_layout(quiz)
    except layout_mod.TooManyPages:
        return False
    if len(nouvelle["pages"]) != len(ancienne.get("pages", [])):
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
    return True


def moderniser_calage(quiz):
    """Refait un PDF enregistré qui porte encore les carrés noirs (repères
    ArUco) dans les coins : la fiche est redessinée avec le cadre fin
    (demande de l'utilisateur, 29 octobre 2026). Les copies déjà imprimées
    avec les carrés se lisent toujours (omr.detect_and_warp les essaie).

    Seulement si rien de ce qui est lu ne bouge (comme regenerer_fiche_
    compatible). Pas pour les fiches anonymes « QR » de concours, dont le
    nombre d'exemplaires n'est pas connu : elles se refont par le bouton.
    Retourne True si le PDF a été remplacé."""
    disposition = quiz.layout_json
    if not quiz.sheet_pdf or not disposition or disposition.get("calage") == "cadre":
        return False
    if quiz.type_epreuve == "oral":
        from .views_epreuves import _generer_fiches_orales
        _generer_fiches_orales(quiz)
        return True
    if quiz.type_epreuve == "ecrit":
        if quiz.exemplaires_anonymes:
            return False
        from .views_epreuves import _generer_documents
        _generer_documents(quiz)
        return True
    if quiz.id_mode == "qr":
        if quiz.exemplaires_anonymes or quiz.place_anonyme:
            return False                # places : se refait par le bouton
        try:
            nouvelle = layout_mod.build_layout(quiz)
        except layout_mod.TooManyPages:
            return False
        if len(nouvelle["pages"]) != len(disposition.get("pages", [])) or any(
                page.get(cle) != avant.get(cle)
                for page, avant in zip(nouvelle["pages"], disposition["pages"])
                for cle in _ZONES_LUES):
            return False
        pdf = sheet_pdf.generate_sheet_pdf(
            quiz, nouvelle, students=list(quiz.class_group.students.all()))
        nouvelle["signature"] = disposition.get("signature",
                                                layout_mod.layout_signature(quiz))
        if "sujet_version" in disposition:
            nouvelle["sujet_version"] = disposition["sujet_version"]
        quiz.layout_json = nouvelle
        quiz.sheet_pdf.save(f"fiches_nominatives_quiz_{quiz.pk}.pdf",
                            ContentFile(pdf), save=False)
        quiz.save(update_fields=["layout_json", "sheet_pdf"])
        return True
    return regenerer_fiche_compatible(quiz)


def renommer_epreuve(quiz, titre, utilisateur):
    titre = titre[:200]
    """Change le titre d'une épreuve et en tire les conséquences.

    - Concours : sa liste de candidats (« Concours — ancien titre ») suit.
    - Le titre est imprimé : le sujet est refait aussitôt (jamais scanné), la
      feuille de réponses aussi si rien de ce qui est lu ne bouge ; sinon
      l'alerte « fiche à régénérer » de la page prend le relais.
    - Le changement est inscrit au journal.
    Retourne (feuille_refaite, sujet_refait)."""
    titre = " ".join(titre.split())[:200]
    ancien = quiz.title
    if not titre or titre == ancien:
        return False, False
    quiz.title = titre
    quiz.save(update_fields=["title"])
    journal.noter(utilisateur, "titre", quiz=quiz, objet="Titre de l'épreuve",
                  avant=ancien, apres=titre)
    # Liste de candidats d'un concours : nommée d'après le titre à la
    # création (views.quiz_create, éventuellement suffixée « (2) »). On la
    # renomme si elle porte encore ce nom-là ; un nom choisi à la main reste.
    groupe = quiz.class_group
    anciens_noms = (_nom_liste_concours(ancien), f"Concours — {ancien}"[:110])
    if quiz.auto_enroll and groupe.name.startswith(anciens_noms):
        base = _nom_liste_concours(titre)
        nom, i = base, 2
        while (ClassGroup.objects.filter(owner=groupe.owner, name=nom)
               .exclude(pk=groupe.pk).exists()):
            nom = f"{base} ({i})"
            i += 1
        groupe.name = nom
        groupe.save(update_fields=["name"])
    feuille = regenerer_fiche_compatible(quiz)
    sujet = actualiser_sujet(quiz, forcer=True)
    return feuille, sujet


def _nom_liste_concours(titre):
    """Nom de la liste de candidats d'un concours, tiré de son titre (même
    règle que views.quiz_create)."""
    return (titre if "concours" in titre.lower() else f"Concours — {titre}")[:110]


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
    """Refait le SUJET (document des questions : mode « sujet séparé » ou
    épreuve écrite) s'il a été dessiné par une version antérieure — par exemple
    un sujet de concours sans les choix des QCM. Le sujet n'est jamais scanné :
    le refaire ne touche ni à la feuille de réponses ni à la lecture des copies.
    forcer : refaire même à jour (en-tête ou durée modifiés).
    Retourne True si le sujet a été remplacé."""
    disposition = quiz.layout_json
    a_un_sujet = quiz.sheet_mode == "split" or quiz.type_epreuve == "ecrit"
    if not a_un_sujet or not quiz.subject_pdf or not disposition:
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


def _deja_identifies(quiz, sheet):
    """Étudiants qui ont déjà une copie identifiée pour cette page du quiz."""
    return set(SheetScan.objects
               .filter(batch__quiz=quiz, page_index=sheet.page_index,
                       student__isnull=False)
               .exclude(pk=sheet.pk)
               .values_list("student_id", flat=True))


def _identifier_par_nom(sheet, warped, page_layout, quiz, students):
    """Lit NOM / PRÉNOM (plusieurs variantes d'image) et rapproche de la
    classe (omr.identifier). Renseigne sur la copie la lecture retenue, sa
    ressemblance et le recadrage ; retourne l'étudiant ou None."""
    lectures, recadrage = omr.lire_nom(warped, page_layout, quiz.language)
    if not lectures and sheet.ocr_name_raw:  # OCR muet : la lecture d'avant
        lectures = [(sheet.ocr_name_raw, "")]
    etudiant, score, lecture, _ = omr.identifier(
        lectures, students, deja_pris=_deja_identifies(quiz, sheet))
    if lectures or not sheet.ocr_name_raw:   # sans OCR, garder la lecture d'avant
        sheet.ocr_name_raw = " ".join(t for t in lecture if t)[:200]
        sheet.match_score = score
    _save_jpg(sheet.name_crop, recadrage, "name.jpg")
    return etudiant


def proposition(sheet, students):
    """Étudiant le plus probable d'une copie restée « à identifier », pour
    le présélectionner à l'écran de vérification : rapprochement du nom déjà
    lu (sans relancer l'OCR) parmi les étudiants qui n'ont pas encore leur
    copie. Retourne (étudiant|None, ressemblance en %)."""
    if sheet.student_id is not None:
        return None, 0.0
    pris = _deja_identifies(sheet.batch.quiz, sheet)
    libres = [s for s in students if s.pk not in pris]
    if not libres:
        return None, 0.0
    if len(libres) == 1:                      # dernier étudiant sans copie
        _, score, _, _ = omr.identifier([(sheet.ocr_name_raw or "", "")], libres)
        return libres[0], score
    if not sheet.ocr_name_raw:
        return None, 0.0
    _, score, _, classement = omr.identifier([(sheet.ocr_name_raw, "")], libres)
    if classement and classement[0][1] >= 35:
        return classement[0][0], classement[0][1]
    return None, score


def suite_de_copie(sheet, students):
    """Étudiant probable d'une page 2 ou suivante restée sans identification (étiquette
    oubliée) : celui de la page lue juste avant dans le lot, si c'est bien la page
    précédente de la fiche et qu'il n'a pas encore celle-ci.

    Simple présélection à l'écran de vérification, jamais une affectation : deux
    feuilles inversées dans le paquet donneraient à un candidat les réponses d'un autre."""
    if sheet.student_id is not None or not sheet.page_index or sheet.ocr_name_raw:
        return None
    # pages blanches ou illisibles sautées (verso d'une feuille imprimée d'un seul côté)
    avant = (sheet.batch.sheets.filter(pk__lt=sheet.pk, status__in=("ok", "no_match"))
             .order_by("-pk").first())
    if (avant is None or avant.student_id is None
            or avant.page_index != sheet.page_index - 1
            or avant.student_id in _deja_identifies(sheet.batch.quiz, sheet)):
        return None
    return next((s for s in students if s.pk == avant.student_id), None)


def etiquettes_par_candidat(groupe):
    """Étiquettes à imprimer par candidat sur la planche d'un groupe : autant que de pages
    à étiquette sur la plus longue de ses fiches (1 tant qu'aucune n'est générée)."""
    return max([layout_mod.pages_a_etiquette(d) for d in
                groupe.quizzes.values_list("layout_json", flat=True)] + [1])


def reidentifier_lot(batch, utilisateur=None):
    """Seconde chance pour les copies « à identifier » d'un lot : nouvelle
    lecture du nom (sur l'image redressée conservée) et rapprochement, cette
    fois en sachant quels étudiants ont déjà leur copie — quand il n'en
    reste qu'un, une lecture même médiocre le désigne.

    Lancée d'office à la fin de chaque lot, et par le bouton « Identifier
    automatiquement » de la page du lot (copies scannées avant). Retourne le
    nombre de copies identifiées."""
    quiz = batch.quiz
    disposition = quiz.layout_json or {}
    pages = disposition.get("pages", [])
    students = list(quiz.class_group.students.all())
    faites = 0
    for sheet in batch.sheets.filter(status="no_match", student__isnull=True):
        if sheet.page_index >= len(pages) or not sheet.warped_image:
            continue
        page_layout = pages[sheet.page_index]
        if not _has_name_zone(page_layout, quiz):
            continue
        try:
            with sheet.warped_image.open("rb") as f:
                warped = cv2.imdecode(np.frombuffer(f.read(), np.uint8),
                                      cv2.IMREAD_COLOR)
        except (OSError, ValueError):
            continue
        if warped is None:
            continue
        etudiant = _identifier_par_nom(sheet, warped, page_layout, quiz, students)
        if etudiant is not None:
            sheet.student = etudiant
            sheet.status = "ok"
            faites += 1
            if utilisateur is not None:
                journal.attribution(utilisateur, sheet, None)
        sheet.save()
    return faites


def _jpg(img_bgr, quality):
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


class _Lecture:
    """Ce qu'une page donne à la lecture de son image seule : recalage, QR, grille
    du n°, cases, note, images de contrôle déjà encodées. Rien n'y vient de la base
    ni de la liste des candidats : _lire_page tourne dans un fil de lecture,
    plusieurs pages à la fois. erreur : l'exception de la lecture, levée de nouveau
    à l'enregistrement, là où elle l'était avant (mêmes états, mêmes messages)."""
    __slots__ = ("image_jpg", "erreur", "blanche", "warped", "page_index", "page_layout",
                 "qr", "grille", "resultats", "note", "warped_jpg", "overlay_jpg")

    def __init__(self, image_jpg=None, erreur=None):
        self.image_jpg, self.erreur, self.blanche = image_jpg, erreur, False
        self.warped = self.page_index = self.page_layout = self.qr = None
        self.resultats = self.note = self.warped_jpg = self.overlay_jpg = None
        self.grille = ""


def _lire_page(img, layout, questions, quiz):
    """Toute la lecture d'une page qui ne demande que son image (ou un _ADecoder,
    décodé ici). Ne lève jamais : l'erreur est rangée dans la lecture."""
    if isinstance(img, _ADecoder):
        try:
            img = img.decoder()
            if img is None:
                raise ValueError("Image JPEG de la page illisible.")
        except Exception as e:  # noqa: BLE001 — même suite qu'une page illisible au rendu
            return _Lecture(erreur=e)
    lecture = _Lecture(image_jpg=_jpg(img, 82))
    try:
        warped, page_index = omr.detect_and_warp(img)
        lecture.page_index = page_index
        pages = layout["pages"]
        if page_index >= len(pages):
            raise omr.MarkerError(
                f"Repères de la page {page_index + 1} alors que la fiche du quiz "
                f"n'en compte que {len(pages)} : la fiche a été régénérée depuis "
                "l'impression de cette copie. Réimprimez la fiche en cours ou "
                "régénérez-la à l'identique.")
        page_layout = pages[page_index]
        lecture.warped, lecture.page_layout = warped, page_layout
        if page_layout.get("qr"):
            lecture.qr = omr.read_qr(warped, page_layout)
        if page_layout.get("id_grid"):
            # lue d'avance : la même lecture qu'au moment où l'identification la demande
            lecture.grille = omr.read_id_grid(warped, page_layout)
        resultats = omr.read_bubbles(warped, page_layout)
        overlay = omr.draw_overlay(warped, page_layout, resultats, questions,
                                   zone_nom=_has_name_zone(page_layout, quiz))
        # Écrit : note noircie par le correcteur dans la grille de la page de garde
        grille_note = page_layout.get("note_grid")
        if grille_note:
            lecture.note, chiffres = omr.lire_note(warped, grille_note, grille_note["sur"])
            omr.dessiner_note(overlay, grille_note, chiffres, lecture.note, grille_note["sur"])
        lecture.resultats = resultats
        lecture.warped_jpg = _jpg(warped, 70)
        lecture.overlay_jpg = _jpg(overlay, 70)
    except omr.MarkerError as e:
        lecture.erreur, lecture.blanche = e, omr.page_blanche(img)
    except Exception as e:  # noqa: BLE001 — page illisible : signalée à l'enregistrement
        lecture.erreur = e
    return lecture


def _lire_page_dans_un_fil(img, layout, questions, quiz):
    """_lire_page pour un fil de lecture : il ne touche pas à la base, mais s'il
    venait à ouvrir une connexion, elle ne resterait pas ouverte après lui."""
    try:
        return _lire_page(img, layout, questions, quiz)
    finally:
        connection.close()


def _process_sheet(sheet, img, layout, students, questions, quiz):
    """Lit et enregistre une page, d'un seul tenant (sans fil de lecture)."""
    lecture = _lire_page(img, layout, questions, quiz)
    if lecture.erreur is not None:
        if lecture.page_index is not None:
            sheet.page_index = lecture.page_index
        raise lecture.erreur
    _enregistrer_page(sheet, lecture, students, questions, quiz)


def _enregistrer_page(sheet, lecture, students, questions, quiz):
    """L'identification (liste des candidats, registre, OCR du nom en secours)
    et l'enregistrement d'une page lue : dans le fil qui a la base, page après page."""
    language = quiz.language
    warped, page_layout = lecture.warped, lecture.page_layout
    sheet.page_index = lecture.page_index

    # Identification : QR, puis grille de n° (lectures exactes), puis OCR du
    # nom. En mode concours, un candidat inconnu est créé automatiquement.
    student = None
    serial = None
    grid_id = ""
    if page_layout.get("qr"):
        kind, _quiz_id, value = lecture.qr
        if kind == "QS":
            student = next((s for s in students if s.pk == value), None)
            if student is not None:
                sheet.id_read = f"QR {student.student_number or student.pk}"[:20]
                sheet.match_score = 100.0
        elif kind == "QSA":
            serial = value
            sheet.id_read = f"Fiche {value:04d}"[:20]
        elif kind == "QSI":
            # carte d'inscription collée à la place de l'étiquette
            sheet.id_read = f"QR {value}"[:20]
            student = omr.match_student_by_id(value, students)
            if student is None and quiz.auto_enroll:
                grid_id = value       # concours : candidat créé ci-dessous
            if student is not None:
                sheet.match_score = 100.0
    if student is None and not grid_id and page_layout.get("id_grid"):
        grid_id = lecture.grille
        sheet.id_read = grid_id[:20] or sheet.id_read
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
            # n° présent au registre des inscriptions : identité officielle
            inscrit = Inscrit.objects.filter(numero=number).first()
            if inscrit is not None:
                student = Student.objects.create(
                    class_group=quiz.class_group, student_number=number,
                    last_name=inscrit.nom, first_name=inscrit.prenom,
                    inscrit=inscrit)
            else:
                student = Student.objects.create(
                    class_group=quiz.class_group, student_number=number,
                    last_name=(last_t.upper()[:80] or f"CANDIDAT {number}"),
                    first_name=first_t[:80])
        if student not in students:
            students.append(student)
        sheet.match_score = 100.0
    elif student is None and _has_name_zone(page_layout, quiz):
        # secours automatique dans tous les modes : OCR du nom manuscrit
        student = _identifier_par_nom(sheet, warped, page_layout, quiz, students)
    sheet.student = student

    # Cases QCM, note de l'écrit, images de contrôle : lues par _lire_page
    results = lecture.resultats
    grille_note = page_layout.get("note_grid")
    note_lue = lecture.note
    if lecture.warped_jpg:
        sheet.warped_image.save("warped.jpg", ContentFile(lecture.warped_jpg), save=False)
    if lecture.overlay_jpg:
        sheet.overlay_image.save("overlay.jpg", ContentFile(lecture.overlay_jpg), save=False)

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

    if grille_note:
        q = questions.get(grille_note["order"])
        if q is not None:
            # illisible ou au-dessus du barème : la note reste à saisir à
            # l'écran de vérification (« réponse manuscrite à noter »)
            a = Answer(sheet=sheet, question=q,
                       points_awarded=(note_lue if note_lue is not None
                                       and note_lue <= q.points else None))
            _save_jpg(a.open_crop, omr.crop_open_zone(warped, grille_note["rect"]),
                      "note.jpg")
            a.save()


# ------------------------------------------------- écrit et oral : la note

def question_note(quiz):
    """La question unique qui porte la note d'une épreuve écrite ou orale
    (réponse « manuscrite » notée sur quiz.note_sur). Créée au besoin ; son
    barème suit celui de l'épreuve. Ainsi résultats, admission, bulletins,
    publication et export marchent comme pour un QCM."""
    q = quiz.questions.filter(qtype="open").order_by("order").first()
    if q is None:
        q = Question.objects.create(quiz=quiz, order=1, qtype="open",
                                    points=quiz.note_sur,
                                    text="Note de l'épreuve")
    elif q.points != quiz.note_sur:
        q.points = quiz.note_sur
        q.save(update_fields=["points"])
    return q


# Lot des notes d'oral SAISIES À LA MAIN avant le 30 octobre 2026. Depuis, les
# notes d'oral ne viennent que des fiches scannées (demande de l'utilisateur) ;
# ce lot n'est plus alimenté, ses notes restent tant qu'on ne le supprime pas.
LOT_ORAL = "Notes de l'oral"


def _reponses_retenues(quiz, q):
    """{student_id: réponse} — la note qui compte pour chaque étudiant :
    celle de sa première copie (même règle que compute_results)."""
    retenues = {}
    for r in (Answer.objects.filter(sheet__batch__quiz=quiz, question=q,
                                    sheet__student__isnull=False)
              .select_related("sheet__batch").order_by("sheet__created_at", "pk")):
        retenues.setdefault(r.sheet.student_id, r)
    return retenues


def notes_orales(quiz):
    """Pour la page d'un oral : [(étudiant, état, réponse)] où l'état est
    « lue » (note lue sur la fiche scannée), « illisible » (fiche scannée,
    note à vérifier sur la fiche), « manuelle » (ancienne saisie à la main)
    ou « attendue » (fiche pas encore scannée)."""
    retenues = _reponses_retenues(quiz, question_note(quiz))
    lignes = []
    for etudiant in quiz.class_group.students.all():
        r = retenues.get(etudiant.pk)
        if r is None:
            etat = "attendue"
        elif r.sheet.batch.label == LOT_ORAL and not r.sheet.image:
            etat = "manuelle"
        elif r.points_awarded is None:
            etat = "illisible"
        else:
            etat = "lue"
        lignes.append((etudiant, etat, r))
    return lignes


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


class _Reponse:
    """Une réponse telle que les résultats s'en servent, sans objet Django : à
    10 000 copies de 40 questions, 400 000 objets complets coûtaient 35 s et
    600 Mo à chaque affichage des résultats. Mêmes règles que Answer, dont les
    propriétés sont reprises telles quelles."""
    __slots__ = ("question", "detected_choice", "is_multiple", "points_awarded")
    is_blank = Answer.is_blank
    is_correct = Answer.is_correct
    detected_letter = Answer.detected_letter

    def __init__(self, question, detected_choice, is_multiple, points_awarded):
        self.question = question
        self.detected_choice = detected_choice
        self.is_multiple = is_multiple
        self.points_awarded = points_awarded


def rescore_quiz(quiz):
    """Recalcule toutes les copies du quiz après une modification du corrigé,
    du barème ou de la pénalité. Retourne le nombre de copies touchées."""
    # Sans objet Django par réponse : à 10 000 copies, ces 400 000 objets
    # prenaient plus de mémoire que n'en a le serveur, à chaque réglage enregistré.
    questions = {q.pk: q for q in quiz.questions.filter(qtype="qcm")}
    changed, sheets = [], set()
    for pk, sheet_id, question_id, detected, multiple, points in (
            Answer.objects.filter(sheet__batch__quiz=quiz, question__qtype="qcm",
                                  manually_set=False)
            .order_by().values_list("pk", "sheet_id", "question_id", "detected_choice",
                                    "is_multiple", "points_awarded").iterator()):
        question = questions[question_id]
        pts = _auto_points(question, _Reponse(question, detected, multiple, points))
        if pts != points:
            changed.append(Answer(pk=pk, points_awarded=pts))
            sheets.add(sheet_id)
    Answer.objects.bulk_update(changed, ["points_awarded"], batch_size=500)
    return len(sheets)


def compute_results(quiz, batch=None, student=None):
    """Agrège les résultats par étudiant (ou pour un seul : espace public).

    Retourne une liste de dicts triée par nom :
    {student, score, max_score, answered, correct, wrong, blank,
     pending_open (questions manuscrites non corrigées),
     questions_manquantes (sans réponse lue : page non identifiée ou non reçue), sheets}
    """
    sheets = SheetScan.objects.filter(batch__quiz=quiz).exclude(student=None)
    if batch is not None:
        sheets = sheets.filter(batch=batch)
    if student is not None:
        sheets = sheets.filter(student=student)
    questions = {q.pk: q for q in quiz.questions.all()}
    # Une fois par question ce que chaque réponse en demande. Les règles sont
    # celles d'Answer.is_blank et Answer.is_correct, écrites dans la boucle : à
    # 10 000 copies de 30 questions, 300 000 appels de propriétés coûtaient
    # plusieurs secondes à chaque affichage des résultats.
    infos = {pk: (q, q.order, q.qtype == "qcm", q.qtype == "open", q.correct_choice)
             for pk, q in questions.items()}
    # Réponses lues à partir de l'épreuve, sans tri par la base : le tri de
    # Answer.Meta (n° de question) menait PostgreSQL à parcourir les réponses de
    # TOUTES les épreuves (2 s pour une épreuve de 3 copies, de plus en plus à
    # mesure que la table grandit). Le même ordre est rétabli ici, copie par copie.
    reponses_lues = Answer.objects.filter(sheet__batch__quiz=quiz, sheet__student__isnull=False)
    if batch is not None:
        reponses_lues = reponses_lues.filter(sheet__batch=batch)
    if student is not None:
        reponses_lues = reponses_lues.filter(sheet__student=student)
    par_copie = {}
    for sheet_id, question_id, detected, multiple, points in (
            reponses_lues.order_by()
            .values_list("sheet_id", "question_id", "detected_choice", "is_multiple",
                         "points_awarded").iterator(chunk_size=20000)):
        liste = par_copie.get(sheet_id)
        if liste is None:
            liste = par_copie[sheet_id] = []
        liste.append((question_id, detected, multiple, points))
    for liste in par_copie.values():
        liste.sort(key=lambda reponse: infos[reponse[0]][1])   # n° de la question
    sheets = sheets.select_related("student")

    per_student = {}
    for sheet in sheets:
        entry = per_student.setdefault(sheet.student_id, {
            "student": sheet.student, "score": 0.0,
            "answered": 0, "correct": 0, "wrong": 0, "blank": 0,
            "pending_open": 0, "sheets": [], "seen_questions": set(),
            "answers": {},
        })
        entry["sheets"].append(sheet)
        if sheet.image and "copie" not in entry:      # la page qu'ouvre sa ligne de résultats
            entry["copie"] = sheet
        vues, reponses = entry["seen_questions"], entry["answers"]
        repondues = justes = fausses = vides = en_attente = 0
        score = entry["score"]      # mêmes additions, dans le même ordre, qu'avant
        for question_id, detected, multiple, points in par_copie.pop(sheet.pk, ()):
            if question_id in vues:
                continue  # doublon (page scannée deux fois) : première lecture retenue
            vues.add(question_id)
            question, ordre, qcm, ouverte, bonne = infos[question_id]
            reponses[ordre] = _Reponse(question, detected, multiple, points)
            if qcm:
                if detected is None and not multiple:         # is_blank
                    vides += 1
                else:
                    repondues += 1
                    if not multiple and detected == bonne:     # is_correct
                        justes += 1
                    else:
                        fausses += 1
            if points is not None:
                score += points
                if ouverte:
                    repondues += 1
            elif ouverte:
                en_attente += 1
                entry.setdefault("a_noter", sheet)    # page où saisir la note
        entry["answered"] += repondues
        entry["correct"] += justes
        entry["wrong"] += fausses
        entry["blank"] += vides
        entry["pending_open"] += en_attente
        entry["score"] = score

    max_score = quiz.max_score
    # Une QCM = un point : le résultat se lit en bonnes réponses sur le
    # nombre de QCM, et en pourcentage de la note maximale.
    n_qcm = quiz.questions.filter(qtype="qcm").count()
    rows = []
    for entry in per_student.values():
        # Page non identifiée ou non reçue : ses questions ne sont ni justes, ni fausses,
        # ni vides. La note est partielle, et seul ce compte le dit.
        entry["questions_manquantes"] = max(len(questions) - len(entry.pop("seen_questions")), 0)
        entry["score"] = max(round(entry["score"], 2), 0.0)
        entry["max_score"] = max_score
        entry["n_qcm"] = n_qcm
        entry["pct"] = round(entry["score"] / max_score * 100) if max_score else 0
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
                         à noter ou une page de la copie à identifier — elles
                         peuvent encore le faire passer. Avec une pénalité, une
                         page manquante peut aussi faire baisser une note au-dessus
                         du seuil : même résultat ;
      - « refuse »     : sous le seuil, copie entièrement notée.
    Pose aussi row["resultat"] sur chaque ligne, pour le tableau principal.
    Les pages non identifiées n'y figurent pas (elles n'ont pas de candidat)."""
    seuil = quiz.note_admission
    if seuil is None:
        for r in rows:
            r["resultat"] = None
        return None
    for r in rows:
        manquantes = r["questions_manquantes"]
        if r["score"] >= seuil and not (manquantes and quiz.wrong_penalty):
            r["resultat"] = "admis"
        elif r["pending_open"] or manquantes:
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


def _discrimination(rows, ordre, deja_filtrees=False):
    """Écart de réussite entre le tiers fort et le tiers faible du classement.

    Renvoie (indice, étiquette, explication), ou None si l'effectif est trop
    petit pour que le calcul veuille dire quoi que ce soit : sous une
    dizaine de copies, trois bonnes réponses de plus d'un côté suffisent à
    faire basculer l'indice. Mieux vaut ne rien afficher qu'un chiffre
    trompeur sur lequel on refera une épreuve.
    deja_filtrees : rows ne contient que les lignes qui ont répondu (compute_stats).
    """
    MINIMUM = 10
    notes = list(rows) if deja_filtrees else [r for r in rows if ordre in r["answers"]]
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


# ------------------------------------------------ copies anonymes (places)

def places_anonymes(quiz):
    """Numéros de place des copies anonymes : [(n° de place, étudiant)].

    Première génération : par n° d'inscription puis par nom. Ensuite les
    places déjà attribuées (layout_json["places"]) NE CHANGENT PAS — les
    copies sont peut-être déjà imprimées — et un étudiant ajouté prend la
    place suivante. Un étudiant retiré libère sa place sans renuméroter."""
    def ordre(s):
        return ((s.student_number or "~").zfill(12), s.last_name, s.first_name)
    connues = {int(k): v for k, v in
               ((quiz.layout_json or {}).get("places") or {}).items()}
    etudiants = list(quiz.class_group.students.all())
    places = [(connues[s.pk], s) for s in etudiants if s.pk in connues]
    suivante = max([n for n, _ in places] or [0]) + 1
    for s in sorted((s for s in etudiants if s.pk not in connues), key=ordre):
        places.append((suivante, s))
        suivante += 1
    return sorted(places, key=lambda p: p[0])


def copies_nominatives_pdf(quiz, layout):
    """PDF d'une copie par étudiant, QR imprimé : nom imprimé (ou sur le
    talon), ou copie anonyme à n° de place — la correspondance place ->
    étudiant est alors gardée dans layout["places"] pour la liste
    d'émargement. Retourne (octets du PDF, nombre de copies)."""
    if quiz.place_anonyme:
        places = places_anonymes(quiz)
        layout["places"] = {str(s.pk): n for n, s in places}
        pdf = sheet_pdf.generate_sheet_pdf(
            quiz, layout, students=[s for _, s in places],
            places={s.pk: n for n, s in places})
        return pdf, len(places)
    etudiants = list(quiz.class_group.students.all())
    return sheet_pdf.generate_sheet_pdf(quiz, layout, students=etudiants), len(etudiants)


def liste_emargement(quiz):
    """[(n° de place, étudiant)] des copies anonymes imprimées (places
    gardées à la génération), ou None si elles n'ont pas été générées."""
    places = (quiz.layout_json or {}).get("places")
    if not places:
        return None
    par_pk = {s.pk: s for s in quiz.class_group.students.all()}
    return sorted(((n, par_pk[int(k)]) for k, n in places.items() if int(k) in par_pk),
                  key=lambda p: p[0])


# ------------------------------------------- concours en plusieurs étapes

def admis_de(quiz):
    """Étudiants admis à une épreuve (note minimale atteinte), par ordre de
    mérite. Liste vide sans note minimale. « En attente » n'est pas admis."""
    admission = admissions(quiz, compute_results(quiz))
    return [r["student"] for r in admission["admis"]] if admission else []


def faire_passer_admis(cible):
    """Ajoute aux candidats de l'épreuve « cible » les admis de son épreuve
    précédente (QCM -> écrit -> oral). Ne retire personne : un candidat déjà
    présent, ou déjà noté, garde sa place. Retourne (ajoutés, déjà là, admis)."""
    from .inscriptions import copier_etudiants
    precedente = cible.epreuve_precedente
    if precedente is None:
        return 0, 0, 0
    admis = admis_de(precedente)
    ajoutes, deja = copier_etudiants(cible.class_group, admis)
    return ajoutes, deja, len(admis)


def etat_etape(cible):
    """Pour la page d'une épreuve « étape suivante » : combien d'admis à
    l'épreuve précédente, combien déjà dans la liste, combien à ajouter."""
    precedente = cible.epreuve_precedente
    if precedente is None:
        return None
    admis = admis_de(precedente)
    presents_num = set(cible.class_group.students.exclude(student_number="")
                       .values_list("student_number", flat=True))
    presents_nom = set(cible.class_group.students.values_list("last_name", "first_name"))
    manquants = sum(1 for s in admis
                    if not ((s.student_number and s.student_number in presents_num)
                            or (not s.student_number
                                and (s.last_name, s.first_name) in presents_nom)))
    return {"precedente": precedente, "n_admis": len(admis), "manquants": manquants,
            "seuil": precedente.note_admission}


# ------------------------------------- résultats d'un concours en étapes

def chaine_epreuves(quiz, autorisees=None):
    """Les épreuves d'un concours en étapes (QCM -> écrit -> oral), de la
    première à la dernière : on remonte les « épreuve précédente », puis on
    descend les étapes suivantes (largeur d'abord, par ordre de création).
    autorisees : queryset des épreuves visibles par l'utilisateur. Une
    épreuve seule (sans étape) donne une chaîne d'un élément."""
    vues = {quiz.pk}
    racine = quiz
    while racine.epreuve_precedente_id and racine.epreuve_precedente_id not in vues:
        vues.add(racine.epreuve_precedente_id)
        racine = racine.epreuve_precedente
    chaine, a_voir, vus = [], [racine], {racine.pk}
    while a_voir:
        q = a_voir.pop(0)
        chaine.append(q)
        for s in q.epreuves_suivantes.order_by("pk"):
            if s.pk not in vus:
                vus.add(s.pk)
                a_voir.append(s)
    if autorisees is not None:
        visibles = set(autorisees.filter(pk__in=[q.pk for q in chaine])
                       .values_list("pk", flat=True))
        chaine = [q for q in chaine if q.pk in visibles]
    return chaine


def _intitule(quiz, racine):
    """Le titre d'une étape s'il dit autre chose que celui de son concours."""
    return "" if quiz.title.startswith(racine.title) else quiz.title


def fil_etapes(quiz, autorisees=None):
    """Le concours d'une épreuve, pour l'en-tête de ses pages : son nom et ses étapes,
    au lieu de « jhjh — écrit — oral ». None pour une épreuve seule."""
    chaine = chaine_epreuves(quiz, autorisees)
    if len(chaine) < 2:
        return None
    racine = chaine[0]
    corrigees = set(ScanBatch.objects.filter(quiz__in=chaine).values_list("quiz_id", flat=True))
    return {"titre": racine.title, "intitule": _intitule(quiz, racine),
            "etapes": [{"quiz": q, "numero": n, "courante": q.pk == quiz.pk,
                        "fait": q.pk in corrigees}
                       for n, q in enumerate(chaine, start=1)]}


def groupes_epreuves(epreuves):
    """Regroupe des épreuves par concours, pour les listes : QCM -> écrit -> oral
    forment un seul groupe et non trois lignes, sans requête par épreuve. Une
    épreuve seule forme un groupe d'une étape ; celle dont la précédente n'est pas
    dans « epreuves » (épreuve d'un collègue) ouvre son propre groupe.

    Un groupe : racine, titre, multi (plusieurs étapes), concours, et etapes, chacune
    {quiz, numero, intitule}. L'intitulé n'est rempli que si le titre de l'étape dit
    autre chose que celui du concours (« jhjh — écrit » pour « jhjh » : non).
    Le groupe le plus récent vient d'abord."""
    par_pk = {q.pk: q for q in epreuves}
    ordre = sorted(par_pk.values(), key=lambda q: q.pk)
    suivantes = {}
    racines = []
    for q in ordre:
        if q.epreuve_precedente_id in par_pk:
            suivantes.setdefault(q.epreuve_precedente_id, []).append(q)
        else:
            racines.append(q)
    groupes, vus = [], set()
    # après les racines : ce qui reste n'arrive que par une boucle de liens
    # (possible depuis l'Administration), et ne doit pas disparaître de la liste
    for depart in racines + ordre:
        etapes, a_voir = [], [depart]
        while a_voir:
            q = a_voir.pop(0)
            if q.pk not in vus:
                vus.add(q.pk)
                etapes.append(q)
                a_voir.extend(suivantes.get(q.pk, []))
        if etapes:
            racine = etapes[0]
            groupes.append({
                "racine": racine, "titre": racine.title, "multi": len(etapes) > 1,
                "concours": any(q.auto_enroll for q in etapes),
                "etapes": [{"quiz": q, "numero": n, "intitule": _intitule(q, racine)}
                           for n, q in enumerate(etapes, start=1)],
            })
    groupes.sort(key=lambda g: max(e["quiz"].pk for e in g["etapes"]), reverse=True)
    return groupes


def _cle_candidat(s):
    """Même personne d'une étape à l'autre : n° d'inscription (celui du QR,
    copié d'une étape à la suivante), sinon fiche du registre, sinon nom et
    prénom."""
    if (s.student_number or "").strip():
        return ("n", s.student_number.strip())
    if s.inscrit_id:
        return ("i", s.inscrit_id)
    return ("p", s.last_name.strip().lower(), s.first_name.strip().lower())


def parcours_concours(chaine):
    """Résultats d'un concours épreuve par épreuve, pour chaque candidat.

    Retourne {"epreuves": [...], "etapes": [{quiz, convoques, presents,
    admis, seuil}], "candidats": [{etudiant, cases: [case par épreuve],
    final, final_ton, atteinte}]}. Une case : {etat: « note » | « non_note »
    (convoqué : absent, ou copie pas encore scannée) | « non_convoque », row
    (compute_results), resultat}. Le résultat final
    se lit à la dernière étape où le candidat a été convoqué."""
    etapes, par_epreuve = [], []
    for quiz in chaine:
        rows = compute_results(quiz)
        admissions(quiz, rows)                       # pose row["resultat"]
        notes = {_cle_candidat(r["student"]): r for r in rows}
        convoques = {_cle_candidat(s): s for s in quiz.class_group.students.all()}
        par_epreuve.append((quiz, notes, convoques))
        etapes.append({"quiz": quiz, "convoques": len(convoques),
                       "presents": len(notes), "seuil": quiz.note_admission,
                       "admis": sum(1 for r in rows if r.get("resultat") == "admis")})
    personnes = {}
    for _, notes, convoques in par_epreuve:
        for cle, s in convoques.items():
            personnes.setdefault(cle, s)
        for cle, r in notes.items():
            personnes.setdefault(cle, r["student"])
    derniere = len(chaine) - 1
    candidats = []
    for cle, etudiant in personnes.items():
        cases, atteinte = [], -1
        for k, (quiz, notes, convoques) in enumerate(par_epreuve):
            r = notes.get(cle)
            if r is not None:
                cases.append({"etat": "note", "row": r, "resultat": r.get("resultat")})
                atteinte = k
            elif cle in convoques:
                cases.append({"etat": "non_note", "row": None, "resultat": None})
                atteinte = k
            else:
                cases.append({"etat": "non_convoque", "row": None, "resultat": None})
        final, ton = "—", "grey"
        if atteinte >= 0:
            case, titre = cases[atteinte], chaine[atteinte].type_court
            if case["etat"] == "non_note":            # absent, ou pas encore scanné
                final, ton = f"Non noté : {titre}", "warn"
            elif case["resultat"] == "refuse":
                final, ton = f"Non admis : {titre}", "bad"
            elif case["resultat"] == "en_attente":
                final, ton = f"En attente : {titre}", "warn"
            elif case["resultat"] == "admis":
                if atteinte == derniere:
                    final, ton = "Admis au concours", "ok"
                else:
                    final, ton = f"Admis : {titre} — étape suivante à venir", "info"
            else:
                final, ton = f"Noté : {titre} (sans note minimale)", "grey"
        derniere_note = cases[atteinte]["row"]["score"] if (
            atteinte >= 0 and cases[atteinte]["row"]) else -1
        candidats.append({"etudiant": etudiant, "student": etudiant,
                          "cases": cases, "final": final,
                          "final_ton": ton, "atteinte": atteinte,
                          "_tri": (-atteinte, ton != "ok", -derniere_note,
                                   etudiant.last_name.lower(), etudiant.first_name.lower())})
    candidats.sort(key=lambda c: c["_tri"])
    return {"epreuves": chaine, "etapes": etapes, "candidats": candidats,
            "n_admis_final": sum(1 for c in candidats if c["final_ton"] == "ok")}


# Tranches de la répartition des résultats (en % du barème).
TRANCHES = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]


def bilan_admissions(quizzes, par_epreuve_max=8):
    """Tableau de bord : admis, non admis et en attente sur toutes les
    épreuves, moyenne générale, répartition des résultats, et le détail des
    épreuves les plus récentes.

    Deux requêtes en tout, quel que soit le nombre de copies : le total de
    points par (épreuve, étudiant), puis le barème de chaque épreuve. Mêmes
    règles que services.admissions : admis si la note atteint la note
    minimale ; sinon « en attente » tant qu'une réponse manuscrite reste à
    noter ; sinon non admis. Une épreuve sans note minimale compte dans la
    moyenne et la répartition, pas dans l'admission."""
    quizzes = list(quizzes)
    par_pk = {q.pk: q for q in quizzes}
    baremes = dict(Question.objects.filter(quiz__in=quizzes)
                   .values_list("quiz_id").annotate(m=Sum("points")))
    lignes = (Answer.objects
              .filter(sheet__batch__quiz__in=quizzes, sheet__student__isnull=False)
              .values("sheet__batch__quiz_id", "sheet__student_id")
              .annotate(total=Sum("points_awarded"),
                        attente=Count("pk", filter=Q(question__qtype="open",
                                                     points_awarded__isnull=True))))
    epreuves = {}
    pcts = []
    for l in lignes:
        quiz = par_pk[l["sheet__batch__quiz_id"]]
        bareme = baremes.get(quiz.pk) or 0
        note = max(l["total"] or 0.0, 0.0)
        pct = round(note / bareme * 100) if bareme else 0
        pcts.append(pct)
        e = epreuves.setdefault(quiz.pk, {"quiz": quiz, "n": 0, "admis": 0,
                                          "attente": 0, "refuses": 0, "somme": 0})
        e["n"] += 1
        e["somme"] += pct
        if quiz.note_admission is None:
            continue
        if note >= quiz.note_admission:
            e["admis"] += 1
        elif l["attente"]:
            e["attente"] += 1
        else:
            e["refuses"] += 1

    for e in epreuves.values():
        e["moyenne_pct"] = round(e["somme"] / e["n"])
        e["seuil"] = e["quiz"].note_admission is not None
        jugees = e["admis"] + e["attente"] + e["refuses"]
        e["pct_admis"] = round(e["admis"] / jugees * 100) if jugees else 0
        # largeurs des segments de la barre empilée (en %)
        for cle in ("admis", "attente", "refuses"):
            e[f"w_{cle}"] = round(e[cle] / jugees * 100, 1) if jugees else 0

    avec_seuil = [e for e in epreuves.values() if e["seuil"]]
    admis = sum(e["admis"] for e in avec_seuil)
    attente = sum(e["attente"] for e in avec_seuil)
    refuses = sum(e["refuses"] for e in avec_seuil)
    jugees = admis + attente + refuses
    repartition = []
    for bas, haut in TRANCHES:
        n = sum(1 for p in pcts if bas <= p < haut)
        repartition.append({"libelle": f"{bas}–{min(haut, 100)} %", "n": n})
    pic = max([r["n"] for r in repartition] or [0]) or 1
    for r in repartition:
        r["hauteur"] = round(r["n"] / pic * 100) if r["n"] else 0
        r["part"] = round(r["n"] / len(pcts) * 100) if pcts else 0
    recentes = sorted(epreuves.values(), key=lambda e: -e["quiz"].pk)
    return {
        "n_notes": len(pcts),
        "n_epreuves": len(epreuves),
        "n_sans_seuil": len(epreuves) - len(avec_seuil),
        "jugees": jugees,
        "admis": admis, "attente": attente, "refuses": refuses,
        "pct_admis": round(admis / jugees * 100) if jugees else 0,
        "pct_refuses": round(refuses / jugees * 100) if jugees else 0,
        "w_admis": round(admis / jugees * 100, 1) if jugees else 0,
        "w_attente": round(attente / jugees * 100, 1) if jugees else 0,
        "w_refuses": round(refuses / jugees * 100, 1) if jugees else 0,
        "moyenne_pct": round(sum(pcts) / len(pcts)) if pcts else 0,
        "reussite_50": round(sum(1 for p in pcts if p >= 50) / len(pcts) * 100)
        if pcts else 0,
        "repartition": repartition,
        "par_epreuve": recentes[:par_epreuve_max],
    }


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

    Une requête : le total de points par candidat (un nombre par candidat).
    Moyenne, extrêmes et admis s'en déduisent ; deux requêtes de plus sur les
    mêmes réponses coûtaient 0,8 s au tableau de bord d'un concours de 10 000
    copies. Renvoie None si aucune copie n'est encore notée.
    """
    totaux = list(Answer.objects
                  .filter(sheet__batch__quiz=quiz,
                          sheet__student__isnull=False,
                          points_awarded__isnull=False)
                  .values("sheet__student_id")
                  .annotate(total=Sum("points_awarded"))
                  .order_by()
                  .values_list("total", flat=True))
    if not totaux:
        return None
    bareme = quiz.max_score or 0
    moyenne = round(sum(totaux) / len(totaux), 2)
    return {
        "quiz": quiz,
        "n": len(totaux),
        "moyenne": moyenne,
        "meilleure": round(max(totaux), 2),
        "plus_basse": round(min(totaux), 2),
        "bareme": bareme,
        # Part du barème, pour la jauge : une moyenne de 8,5 ne veut rien
        # dire sans savoir si l'épreuve est sur 14 ou sur 20.
        "pct": round(moyenne / bareme * 100) if bareme else 0,
        # Avec une note minimale : combien l'atteignent.
        "admis": (sum(1 for t in totaux if t >= quiz.note_admission)
                  if quiz.note_admission is not None else None),
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

    # Les lignes qui ont répondu à chaque question, dans l'ordre des lignes : un
    # seul passage sur les 300 000 réponses d'un grand concours, au lieu de trois
    # par question (comptes, indice de discrimination, répartition des cases).
    par_question = {}
    for r in rows:
        for ordre in r["answers"]:
            par_question.setdefault(ordre, []).append(r)
    questions = []
    for q in quiz.questions.order_by("order"):
        presentes = par_question.get(q.order, [])
        reponses = [r["answers"][q.order] for r in presentes]
        st = {"question": q, "correct": 0, "wrong": 0, "blank": 0,
              "graded": 0, "points_sum": 0.0, "n": len(reponses)}
        if q.qtype == "qcm":
            for a in reponses:          # règles d'Answer.is_blank / is_correct
                if a.detected_choice is None and not a.is_multiple:
                    st["blank"] += 1
                elif not a.is_multiple and a.detected_choice == q.correct_choice:
                    st["correct"] += 1
                else:
                    st["wrong"] += 1
        else:
            for a in reponses:
                if a.points_awarded is not None:
                    st["graded"] += 1
                    st["points_sum"] += a.points_awarded
        if q.qtype == "qcm":
            st["success"] = round(st["correct"] / st["n"] * 100) if st["n"] else 0
            mesure = _discrimination(presentes, q.order, deja_filtrees=True)
            if mesure:
                st["discrimination"], st["discrimination_label"], \
                    st["discrimination_aide"] = mesure
                st["corrige_suspect"] = st["discrimination"] < 0
            st["repartition"] = _repartition(q, reponses)
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
