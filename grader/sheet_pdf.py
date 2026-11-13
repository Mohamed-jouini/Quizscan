"""Génération du PDF de la fiche (à imprimer puis scanner).

Deux modes (quiz.sheet_mode, figé dans layout["mode"]) :
  - "full" : questionnaire complet — questions et choix imprimés sur la fiche ;
  - "grid" : fiche de réponses seule — grille compacte de cases.

Le texte arabe est ligaturé et rendu droite->gauche (module fonts).
"""
import functools
import io
import re

import cv2
import pymupdf
import qrcode
from PIL import Image
from reportlab import rl_config
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from django.db.models import prefetch_related_objects
from reportlab.pdfgen import canvas as rl_canvas

from . import layout as L
from . import qr as qr_mod
from .fonts import (font_for, has_arabic, load_fonts, runs, shape,
                    text_width_pt, wrap)
from .labels_pdf import _ellipsize
from .models import choice_letter

# Images des PDF (QR de chaque copie) en binaire compressé, sans le codage ASCII85 :
# il grossit le fichier d'un quart et coûtait 14 % de la génération des copies
# nominatives (9 novembre 2026, 15 000 copies). Réglage de ReportLab, valable pour
# tous les PDF de l'application.
rl_config.useA85 = 0

ARUCO_DICT = cv2.aruco.getPredefinedDictionary(L.ARUCO_DICT_ID)


@functools.lru_cache(maxsize=4 * L.MAX_SHEET_PAGES)
def _marker_image(marker_id, px=300):
    """Image d'un repère ArUco.

    Mise en cache : un même repère est dessiné sur chaque exemplaire de la
    fiche (jusqu'à plusieurs milliers de fiches en mode nominatif ou
    concours), et le regénérer chaque fois ne sert à rien. La taille du PDF
    ne change pas — ReportLab ne stocke qu'une fois une image identique."""
    img = cv2.aruco.generateImageMarker(ARUCO_DICT, marker_id, px)
    # petite bordure blanche autour du repère pour une détection fiable
    img = cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    return ImageReader(Image.fromarray(img))


def _y(y_mm, h_mm=0.0):
    """mm depuis le haut de page -> points depuis le bas (repère reportlab)."""
    return (L.PAGE_H - y_mm - h_mm) * mm


def _draw_mixed(c, visual_text, x_pt, y_pt, size, bold, align):
    """Texte mixte arabe/latin : chaque segment avec la police qui possède
    ses caractères (Naskh n'a ni lettres latines ni parenthèses)."""
    parts = runs(visual_text, bold)
    widths = [pdfmetrics.stringWidth(t, f, size) for t, f in parts]
    total = sum(widths)
    if align == "center":
        cur = x_pt - total / 2
    elif align == "right":
        cur = x_pt - total
    else:
        cur = x_pt
    for (t, f), w in zip(parts, widths):
        c.setFont(f, size)
        c.drawString(cur, y_pt, t)
        cur += w


# Libellés de la fiche selon la langue du quiz
LABELS = {
    "fr": {
        "last": "NOM", "first": "PRÉNOM",
        "instr": "Noircissez complètement une seule case par question.",
        "page": "Page", "question": "Question", "pt": "pt", "pts": "pts",
        "idnum": "N° D'INSCRIPTION — écrivez votre numéro dans les cases puis "
                 "noircissez le chiffre correspondant sur chaque ligne :",
        "personal": "FICHE PERSONNELLE — vérifiez votre nom, ne l'échangez pas.",
        "place": "PLACE N° {n}",
        "talon_coupe": "Talon à découper avant la correction : il porte votre nom. "
                       "La copie reste identifiée par son QR code.",
        "talon_coupe_code": "Talon à découper avant la correction : il porte votre nom et "
                            "le code de la copie, imprimé aussi sous son QR code.",
        "cin": "CIN", "salle": "Salle", "code": "Code",
        "writename": "Écrivez votre nom et votre prénom dans les cases ci-dessus.",
        "fiche": "Fiche",
        "sticker": "Emplacement réservé — collez ici le QR code qui vous a été remis",
        "qrcorner": "CODE QR",
        "qrstick": "Collez ici votre étiquette QR",
        "ident": "IDENTIFICATION",
        "identhelp": "Collez votre étiquette QR dans le cadre ci-contre. "
                     "Sans étiquette, remplissez la grille du n° d'inscription "
                     "ci-dessous.",
        "ecrit_instr": "Rédigez sur les lignes ci-dessous et sur les pages "
                       "suivantes. Ne rien écrire dans le cadre du correcteur.",
        "note_titre": "RÉSERVÉ AU CORRECTEUR — NOTE SUR {n}",
        "note_aide": "Écrivez chaque chiffre dans sa case puis noircissez-le, 0 compris : "
                     "une ligne vide est à vérifier. Ex. 8,5 : 0 · 8 · 5 · 0.",
        "note_ecrite": "Note :",
        "signature": "Correcteur :",
        "centaines": "Centaines", "dizaines": "Dizaines", "unites": "Unités",
        "dixiemes": "Dixièmes", "centiemes": "Centièmes", "chiffre": "Chiffre",
        # écrit, note par demi-points (13 novembre 2026)
        "note_aide_demi": "Écrivez la note en clair, puis noircissez sa partie entière "
                          "(0 à {n}) et sa décimale (,0 ou ,5) : une case par ligne, ,0 "
                          "compris.",
        "entier": "Partie entière", "demi": "Décimale",
        "note_max_demi": "La note ne dépasse pas {n} : avec {n}, noircissez ,0.",
        "oral_instr": "Fiche d'évaluation orale — l'examinateur noircit la note "
                      "puis scanne la fiche : la note est affectée à l'étudiant.",
        "observations": "Observations de l'examinateur",
        "redaction": "Copie — page de rédaction",
        # page lignée : en recto-verso, elle passe au scanner avec la page de garde
        "non_lue": "Page non lue au scan : seule la page de garde porte la note.",
        # dos de la dernière feuille d'une copie A4, pour le recto-verso
        "page_blanche": "Page laissée blanche",
        "pli": "pli au milieu",
    },
    "ar": {
        "last": "اللقب", "first": "الاسم",
        "instr": "املأ خانة واحدة فقط لكل سؤال بشكل كامل.",
        "page": "صفحة", "question": "السؤال", "pt": "نقطة", "pts": "نقاط",
        "idnum": "رقم التسجيل — اكتب رقمك في الخانات ثم املأ الرقم المطابق في كل سطر:",
        "personal": "بطاقة شخصية — تأكد من اسمك ولا تتبادلها.",
        "place": "المكان رقم {n}",
        "talon_coupe": "قسيمة تُقصّ قبل الإصلاح: تحمل اسمك. تبقى الورقة معرّفة "
                       "برمز QR.",
        "talon_coupe_code": "قسيمة تُقصّ قبل الإصلاح: تحمل اسمك ورمز الورقة، وهو مطبوع "
                            "أيضا تحت رمز QR.",
        "cin": "ب.ت.و", "salle": "القاعة", "code": "الرمز",
        "writename": "اكتب لقبك واسمك بخط واضح في الخانات أعلاه.",
        "fiche": "بطاقة",
        "sticker": "مكان مخصّص — ألصق هنا رمز QR الذي سُلّم إليك",
        "qrcorner": "رمز QR",
        "qrstick": "ألصق هنا ملصق رمز QR",
        "ident": "التعريف",
        "identhelp": "ألصق ملصق رمز QR في الإطار المقابل. "
                     "إن لم يكن لديك ملصق، املأ شبكة رقم التسجيل أدناه.",
        "ecrit_instr": "اكتب على الأسطر أدناه وفي الصفحات الموالية. "
                       "لا تكتب شيئا في إطار المصحّح.",
        "note_titre": "خاص بالمصحّح — العدد على {n}",
        "note_aide": "اكتب كل رقم في خانته ثم املأه، والصفر كذلك؛ السطر الفارغ يُراجَع. مثال 8,5: 0 · 8 · 5 · 0.",
        "note_ecrite": "العدد:",
        "signature": "المصحّح:",
        "centaines": "المئات", "dizaines": "العشرات", "unites": "الآحاد",
        "dixiemes": "الأعشار", "centiemes": "أجزاء المائة", "chiffre": "رقم",
        # sans « ,0 » dans la phrase : de droite à gauche, la virgule passerait après le 0
        "note_aide_demi": "اكتب العدد بوضوح، ثم املأ جزأه الصحيح (من 0 إلى {n}) وكسره "
                          "(0 أو 5): خانة واحدة في كل سطر، والصفر كذلك.",
        "entier": "الجزء الصحيح", "demi": "الكسر",
        "note_max_demi": "لا يتجاوز العدد {n}: مع {n} املأ خانة الصفر في الكسر.",
        "oral_instr": "بطاقة تقييم شفاهي — يملأ الممتحن العدد ثم تُمسح البطاقة ضوئيا.",
        "observations": "ملاحظات الممتحن",
        "redaction": "ورقة التحرير",
        "non_lue": "هذه الصفحة لا تُقرأ عند المسح الضوئي: العدد على صفحة الغلاف فقط.",
        "page_blanche": "صفحة متروكة فارغة",
        "pli": "طيّ في الوسط",
    },
}


def _qr_image(payload):
    # correction H (30 % du QR peut manquer) depuis le 9 novembre 2026 : une tache, un
    # trait de stylo, un coin déchiré n'empêchent plus la lecture ; M (15 %) avant,
    # toujours lu (omr._qr_par_la_grille essaie les deux)
    qr = qrcode.QRCode(border=1, box_size=8,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(payload)
    qr.make(fit=True)
    return ImageReader(qr.make_image().get_image())


def generate_sheet_pdf(quiz, layout, students=None, serials=None, places=None, progres=None,
                       codes=None):
    """Retourne les octets du PDF de la fiche du quiz.

    places : copies ANONYMES à numéro de place ({id étudiant: n° de place}) :
    le bandeau porte « PLACE N° 12 » au lieu du nom ; le QR, lui, identifie
    toujours l'étudiant au scan. La correspondance place -> nom est sur la
    liste d'émargement du surveillant (bulletin.generate_liste_emargement).

    students : fiches nominatives (mode QR) — le jeu complet de pages est
    répété pour chaque étudiant, avec son nom imprimé et son QR code.
    serials : fiches anonymes (mode concours) — le jeu de pages est répété
    autant de fois qu'il y a d'éléments, pour imprimer N exemplaires
    identiques, avec l'emplacement réservé à l'étiquette du candidat
    (aucune case nom/prénom : le candidat n'écrit rien).

    codes : copies à talon ({id étudiant: code d'anonymat}) : le code est imprimé sur
    le talon et sous le QR de la copie, pour rapprocher l'un de l'autre à l'œil une
    fois le talon détaché (services.codes_anonymes).

    Lève layout.TooManyPages si la fiche dépasse le nombre de pages que les
    repères de calibration peuvent numéroter."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    questions = {q.order: q for q in quiz.questions.all()}
    lang = getattr(quiz, "language", "fr")
    mode = layout.get("mode", "grid")
    T = LABELS.get(lang, LABELS["fr"])
    # Concours : identification anonyme (n° de série + étiquette autocollante),
    # pas de nom de classe ni de cases nom/prénom manuscrites.
    concours = getattr(quiz, "auto_enroll", False)
    # pages d'une copie : à l'écrit, la page de garde et ses pages de rédaction (le
    # talon disait « page 1 / 1 » sur une copie de 3 pages)
    pages_copie = len(layout["pages"]) + (
        int(layout.get("pages_redaction", 0) or 0) if mode == "ecrit" else 0)

    def draw(text, x_mm_, y_pt, size, bold=False, align="left", gray=None):
        if gray is not None:
            c.setFillGray(gray)
        if has_arabic(text):
            _draw_mixed(c, shape(text), x_mm_ * mm, y_pt, size, bold, align)
        else:
            c.setFont(font_for(text, bold), size)
            if align == "center":
                c.drawCentredString(x_mm_ * mm, y_pt, text)
            elif align == "right":
                c.drawRightString(x_mm_ * mm, y_pt, text)
            else:
                c.drawString(x_mm_ * mm, y_pt, text)
        if gray is not None:
            c.setFillGray(0.0)

    # « serials » ne sert plus qu'à répéter le jeu de pages autant de fois
    # qu'il y a d'exemplaires à imprimer : la fiche de concours ne porte ni
    # n° de série ni QR imprimé, c'est l'étiquette collée qui identifie.
    if students and layout.get("talon"):
        # la CIN et la salle du talon viennent de la fiche du registre : une
        # requête pour toutes les copies, pas une par copie (des milliers)
        prefetch_related_objects([s for s in students if s is not None], "inscrit")
    units = students or serials or [None]
    pages_unite = len(layout["pages"])
    for rang, (unit, page) in enumerate([(u, p) for u in units for p in layout["pages"]]):
        if progres is not None and rang and rang % pages_unite == 0:
            progres(rang // pages_unite)       # copies terminées (génération en arrière-plan)
        student = unit if (students and unit is not None) else None
        pidx = page["page_index"]

        # Repères ArUco (avec leur marge blanche débordant légèrement)
        # Calage : cadre fin autour de la page et code de page en haut (plus
        # de carrés noirs dans les coins, voir layout.CADRE_MARGE).
        talon = bool(layout.get("talon"))
        _dessiner_cadre(c, pidx, layout.get("cadre_haut", L.CADRE_MARGE), talon)

        # En-tête (concours : titre seul, sans le nom du conteneur de candidats)
        head_title = (quiz.title if concours
                      else f"{quiz.title} — {quiz.class_group.name}")
        consigne = {"ecrit": T["ecrit_instr"], "oral": T["oral_instr"]}.get(
            mode, T["instr"])
        code = codes.get(student.pk) if (codes and student is not None) else None
        if talon:
            # nom, titre et consigne sur le talon, au-dessus du trait de coupe
            _dessiner_talon(c, draw, student, head_title,
                            f"{T['page']} {pidx + 1} / {pages_copie}",
                            consigne, T, lang, code)
        else:
            draw(head_title, L.PAGE_W / 2, _y(24.0),
                 13, bold=True, align="center")
            if mode in ("ecrit", "oral"):
                draw(consigne, L.PAGE_W / 2, _y(28.5), 8, align="center")
            elif lang == "ar":
                draw(consigne, L.PAGE_W / 2, _y(28.5), 8, align="center")
                draw(f"{T['page']} {pidx + 1} / {len(layout['pages'])}",
                     L.CONTENT_X1, _y(28.5), 8, align="right")
            else:
                draw(f"{T['page']} {pidx + 1} / {len(layout['pages'])} — {consigne}",
                     L.PAGE_W / 2, _y(28.5), 8, align="center")

        # Zone nom / prénom  (ou emplacement réservé à une étiquette autocollante)
        nb = page["name_boxes"]
        qr_zone = page.get("qr")
        image_qr = None         # QR imprimé de l'étudiant (copie nominative)
        # Pas de cases nom/prénom :
        # - fiche de concours à étiquette : l'identification est portée par
        #   l'étiquette apposée dessous, le candidat n'écrit rien (fiche
        #   technique p. 6) ;
        # - modes grille, étiquette et QR nominatif : la disposition n'en
        #   prévoit pas, le code QR (ou la grille de n°) identifie la copie.
        # Seul le mode « nom manuscrit » imprime les cases : l'OCR du nom y
        # est le moyen d'identification. Une disposition ancienne peut encore
        # en porter : elles sont alors imprimées, à l'identique.
        if not nb or (concours and (qr_zone or {}).get("sticker")):
            pass  # rien ici : voir la zone étiquette
        else:
            lx, ly, lw, lh = nb["last"]
            fx, fy, fw, fh = nb["first"]
            if lang == "ar":
                # sens de lecture droite -> gauche : la case de droite = اللقب (nom)
                draw(T["last"], fx - 3.0, _y(fy + fh / 2 + 1.5), 11, bold=True, align="right")
                draw(T["first"], lx - 3.0, _y(ly + lh / 2 + 1.5), 11, bold=True, align="right")
            else:
                draw(T["last"], lx - 14.0, _y(ly + lh / 2 + 1.2), 10, bold=True)
                draw(T["first"], fx - 18.0, _y(fy + fh / 2 + 1.2), 10, bold=True)
            c.setLineWidth(0.8)
            c.rect(lx * mm, _y(ly, lh), lw * mm, lh * mm)
            c.rect(fx * mm, _y(fy, fh), fw * mm, fh * mm)
            if student is not None:
                # fiche nominative : nom pré-imprimé dans les cases
                draw(student.last_name, lx + lw / 2, _y(ly + lh / 2 + 1.5), 11,
                     bold=True, align="center", gray=0.25)
                draw(student.first_name, fx + fw / 2, _y(fy + fh / 2 + 1.5), 11,
                     bold=True, align="center", gray=0.25)

        # Emplacement QR / étiquette
        # Cadre « coin » du mode grille : reconnu à la grille de n° de la
        # page, et non à l'absence de cases NOM/PRÉNOM — le mode étiquette
        # n'en a plus non plus, et son cadre est à gauche, sans place pour
        # la consigne d'identification.
        if qr_zone and qr_zone.get("sticker") and page.get("id_grid"):
            # Mode grille : cadre en haut à droite, à la place des cases
            # NOM/PRÉNOM, libellé court centré dessous ; à gauche, dans la
            # bande libérée par ces cases, la consigne d'identification.
            qx, qy, qw, qh = qr_zone["rect"]
            c.setLineWidth(0.9)
            c.setDash(3, 2)
            c.rect(qx * mm, _y(qy, qh), qw * mm, qh * mm)
            c.setDash()
            # gris clair : recouvert par l'étiquette, et effacé par la
            # binarisation de la lecture du QR s'il en dépasse
            draw(T["qrcorner"], qx + qw / 2, _y(qy + qh / 2 + 1.2), 9,
                 bold=True, align="center", gray=0.72)
            draw(T["qrstick"], qx + qw / 2, _y(qy + qh + 3.6), 7.5,
                 align="center", gray=0.45)
            largeur = qx - 6.0 - L.CONTENT_X0
            x_txt, sens = ((qx - 4.0, "right") if lang == "ar"
                           else (L.CONTENT_X0, "left"))
            draw(T["ident"], x_txt, _y(qy + 4.0), 10, bold=True, align=sens)
            for i, ligne in enumerate(wrap(T["identhelp"], 8, largeur)):
                draw(ligne, x_txt, _y(qy + 8.5 + i * 3.6), 8, align=sens,
                     gray=0.3)
        elif qr_zone and qr_zone.get("sticker"):
            # Étiquette / concours : pas de QR imprimé ni de n° de série. Tout l'emplacement
            # est un cadre réservé à l'étiquette (n° d'inscription + QR) que le
            # scan lira pour identifier le candidat.
            bx_, by_, bw_, bh_ = qr_zone["band"]
            qx, qy, qw, qh = qr_zone["rect"]
            zx0 = min(bx_, qx)
            zw = max(bx_ + bw_, qx + qw) - zx0
            # au-dessus du cadre ; à l'intérieur seulement sur une disposition
            # ancienne qui a encore les cases NOM/PRÉNOM juste au-dessus
            label_y = by_ - 1.5 if (concours or not nb) else by_ + 4.0
            # Libellé court, centré sur le cadre : le cadre ne fait que la
            # taille du QR découpé (44 mm). La phrase longue (T["sticker"],
            # 95 mm) en débordait ; en arabe, alignée sur le bord droit, elle
            # filait vers la gauche jusqu'au bord de la feuille.
            draw(T["qrstick"], zx0 + zw / 2, _y(label_y), 8,
                 align="center", gray=0.45)
            c.setLineWidth(0.9)
            c.setDash(3, 2)
            c.rect(zx0 * mm, _y(by_, bh_), zw * mm, bh_ * mm)
            c.setDash()
        elif qr_zone and student is not None:
            # une fois par copie : les pages de rédaction reprennent ce QR
            image_qr = _qr_image(f"QS|{quiz.pk}|{student.pk}")
            _dessiner_bande(c, draw, quiz, student, qr_zone, talon, places, T, lang,
                            image_qr, code)

        # Grille de numéro d'inscription
        grid = page.get("id_grid")
        if grid:
            # La consigne s'arrête avant l'emplacement de l'étiquette QR qui
            # occupe le coin haut droit en mode grille : elle passe alors sur
            # deux lignes, la dernière restant à sa place au-dessus des cases.
            right = L.CONTENT_X1
            zy, zh = (qr_zone or {}).get("rect", [0, -1, 0, 0])[1::2]
            if zy <= grid["label_y"] <= zy + zh:
                right = qr_zone["rect"][0] - 4.0
            lignes = wrap(T["idnum"], 8, right - L.CONTENT_X0, bold=True)
            for i, ligne in enumerate(reversed(lignes)):
                y_ligne = _y(grid["label_y"] - i * 3.6)
                if lang == "ar":
                    draw(ligne, right, y_ligne, 8, bold=True, align="right")
                else:
                    draw(ligne, L.CONTENT_X0, y_ligne, 8, bold=True)
            # en-tête 0..9 au-dessus des colonnes
            for k in range(10):
                cx = grid["rows"][0][k][0]
                draw(str(k), cx, _y(grid["header_y"]), 6.5, align="center", gray=0.35)
            for row, box in zip(grid["rows"], grid["write_boxes"]):
                bx_, by_, bw_, bh_ = box
                c.setLineWidth(0.6)
                c.rect(bx_ * mm, _y(by_, bh_), bw_ * mm, bh_ * mm)
                for k, (cx, cy) in enumerate(row):
                    c.setLineWidth(0.6)
                    c.circle(cx * mm, _y(cy), grid["r"] * mm, stroke=1, fill=0)
                    draw(str(k), cx, _y(cy + 0.7), 5, align="center", gray=0.6)

        # Textes calculés par la disposition (questions et choix, mode complet)
        for t in page.get("texts", []):
            draw(t["text"], t["x"], _y(t["y"]), t["size"],
                 bold=t.get("bold", False), align=t.get("align", "left"))

        # Cases QCM
        for item in page["qcm"]:
            q = questions.get(item["order"])
            bubbles = item["bubbles"]
            if mode == "grid":
                cy = bubbles[0][1]
                xs = [b[0] for b in bubbles]
                pts = q.points if q else 1
                unit = T["pt"] if pts <= 1 else T["pts"]
                if lang == "ar":
                    num_x = max(xs) + 6.5
                    c.setFont("Helvetica-Bold", 10)
                    c.drawString(num_x * mm, _y(cy + 1.5), f".{item['order']}")
                    draw(f"{pts:g} {unit}", num_x + 4.0, _y(cy + 4.8),
                         5.5, align="right", gray=0.45)
                else:
                    c.setFont("Helvetica-Bold", 10)
                    c.drawRightString((min(xs) - 6.5) * mm, _y(cy + 1.5),
                                      f"{item['order']}.")
                    draw(f"{pts:g} {unit}", min(xs) - 6.5, _y(cy + 4.6),
                         5.5, align="right", gray=0.45)
            for k, (bx, by) in enumerate(bubbles):
                c.setLineWidth(0.7)
                c.circle(bx * mm, _y(by), L.BUBBLE_R * mm, stroke=1, fill=0)
                letter = choice_letter(k, lang)
                draw(letter, bx, _y(by + (1.3 if lang == "ar" else 0.75)),
                     6, align="center", gray=0.55)

        # Zones de réponses manuscrites
        for item in page["open"]:
            q = questions.get(item["order"])
            x, y, w, h = item["rect"]
            if mode == "grid":
                # en mode complet, l'intitulé est déjà dans page["texts"]
                pts_txt = ""
                if q:
                    unit = T["pt"] if q.points <= 1 else T["pts"]
                    if lang == "ar":
                        pts_txt = f" ({q.points:g} {unit})"
                    else:
                        pts_txt = f" ({q.points:g} {unit})"
                # Numéro de question toujours en chiffres occidentaux
                label = f"{T['question']} {item['order']}{pts_txt}"
                if q and q.text:
                    label += f" — {q.text[:90]}"
                if lang == "ar":
                    draw(label, x + w, _y(y - 1.5), 10, bold=True, align="right")
                else:
                    draw(label, x, _y(y - 1.5), 10, bold=True)
            c.setLineWidth(0.6)
            c.setStrokeGray(0.3)
            c.rect(x * mm, _y(y, h), w * mm, h * mm)
            c.setStrokeGray(0.0)

        if page.get("note_grid"):
            _dessiner_grille_note(c, draw, page["note_grid"], T, lang)
        if page.get("lignes"):
            if page["lignes"].get("titre"):
                titre = T[page["lignes"]["titre"]]
                if lang == "ar":
                    draw(titre, L.CONTENT_X1, _y(page["lignes"]["y0"] - 4.0), 9,
                         bold=True, align="right")
                else:
                    draw(titre, L.CONTENT_X0, _y(page["lignes"]["y0"] - 4.0), 9,
                         bold=True)
            _dessiner_lignes(c, page["lignes"])

        draw(f"{quiz.title} — page {pidx + 1}",
             L.PAGE_W / 2, _y(L.PAGE_H - 5.0), 6.5, align="center", gray=0.5)
        c.showPage()

        # Écrit : pages lignées après la page de garde de chaque copie, en nombre qui
        # remplit des feuilles entières (layout). Sans repères : elles ne sont pas lues,
        # et sont ignorées quand elles passent au scanner (au dos de la page de garde,
        # en recto-verso).
        if mode == "ecrit" and pidx == len(layout["pages"]) - 1:
            n = int(layout.get("pages_redaction", 0) or 0)
            # A3 : les pages lignées sont sur la feuille de la page de garde (ou sur une
            # feuille emboîtée dedans), pliée au milieu, et passent au scanner avec elle.
            # Le massicot coupe toute la largeur : rien à écrire au-dessus du trait du
            # talon, quelle que soit la page.
            a3 = layout.get("format") == "a3"
            for k in range(n):
                # Talon et QR sur la page de garde SEULEMENT (demande de l'utilisateur, 13
                # novembre 2026) : les pages lignées n'en portent plus, ni au recto des
                # feuilles suivantes (repris là les 6 et 7 novembre, abandonné). Toujours
                # sans cadre ni code de page : au scan, une page lignée reste « ignorée ».
                if a3:
                    _dessiner_lignes(c, L.lignes_a3(
                        L.TALON_COUPE + L.A3_LIGNE if talon else L.A3_HAUT, lang))
                elif image_qr is not None:
                    # copie nominative : la bande du talon tombe au massicot avec celui de
                    # la page de garde, rien à y écrire
                    _dessiner_lignes(c, {"y0": L.TALON_COUPE + L.NOTE_LIGNE if talon
                                         else 22.0, "y1": L.PAGE_H - 16.0,
                                         "pas": L.NOTE_LIGNE})
                else:
                    draw(f"{T['redaction']} {k + 2} / {n + 1} — {quiz.title}",
                         L.PAGE_W / 2, _y(16.0), 9, bold=True, align="center")
                    draw(T["non_lue"], L.PAGE_W / 2, _y(21.0), 7.5,
                         align="center", gray=0.45)
                    _dessiner_lignes(c, {"y0": 30.0, "y1": L.PAGE_H - 16.0,
                                         "pas": L.NOTE_LIGNE})
                if a3 or image_qr is not None:
                    # n° de la page au pied, sur une page sans en-tête : l'ordre des pages
                    draw(f"{quiz.title} — page {k + 2}", L.PAGE_W / 2,
                         _y(L.PAGE_H - 5.0), 6.5, align="center", gray=0.5)
                c.showPage()
            # A4 : le dos de la dernière feuille, blanc après un nombre pair de pages
            # lignées (13 novembre 2026) : la copie suivante commence sur une feuille neuve
            for _ in range(int(layout.get("pages_blanches", 0) or 0)):
                draw(T["page_blanche"], L.PAGE_W / 2, _y(L.PAGE_H / 2), 9,
                     align="center", gray=0.6)
                c.showPage()

    if progres is not None:
        progres(len(units))
    c.save()
    if layout.get("format") == "a3":
        return _imposer_a3(buf.getvalue(), lang,
                           len(layout["pages"]) + int(layout.get("pages_redaction", 0)))
    return buf.getvalue()


def nombre_de_pages(pdf):
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return doc.page_count


def _cotes_a3(nb_pages, rtl):
    """Cahier plié au milieu : (page de gauche, page de droite) de chaque côté de
    feuille, recto puis verso, feuilles emboîtées. Pages numérotées depuis 1, complétées
    à un multiple de 4. La page 1 est sur le dessus du cahier plié : à droite du pli en
    français, à gauche en arabe, où le cahier s'ouvre de l'autre côté."""
    nb = (nb_pages + 3) // 4 * 4
    cotes = []
    for f in range(nb // 4):
        for g, d in ((nb - 2 * f, 1 + 2 * f), (2 + 2 * f, nb - 1 - 2 * f)):
            cotes.append((d, g) if rtl else (g, d))
    return cotes


def _calque_pli(lang):
    """Page A3 transparente : le pointillé du pli, hors du cadre de lecture."""
    largeur, hauteur = landscape(A3)
    texte = LABELS.get(lang, LABELS["fr"])["pli"]
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(largeur, hauteur))
    c.setStrokeGray(0.78)
    c.setLineWidth(0.6)
    c.setDash(2, 3)
    c.line(largeur / 2, 7 * mm, largeur / 2, hauteur - 7 * mm)
    c.setFillGray(0.55)
    if has_arabic(texte):
        _draw_mixed(c, shape(texte), largeur / 2, hauteur - 5 * mm, 6, False, "center")
    else:
        c.setFont(font_for(texte, False), 6)
        c.drawCentredString(largeur / 2, hauteur - 5 * mm, texte)
    c.save()
    return buf.getvalue()


def _imposer_a3(pdf, lang, par_exemplaire=None):
    """Pose les pages A4 du PDF sur des feuilles A3 paysage, à imprimer recto-verso sur
    le bord court puis à plier. Les pages restent celles de l'A4, au trait près : ce qui
    est lu au scan ne bouge pas. par_exemplaire : pages d'un cahier (copies répétées)."""
    largeur, hauteur = landscape(A3)
    moities = (pymupdf.Rect(0, 0, largeur / 2, hauteur),
               pymupdf.Rect(largeur / 2, 0, largeur, hauteur))
    with pymupdf.open(stream=pdf, filetype="pdf") as source, \
            pymupdf.open(stream=_calque_pli(lang), filetype="pdf") as calque, \
            pymupdf.open() as sortie:
        par_exemplaire = par_exemplaire or source.page_count
        cotes = _cotes_a3(par_exemplaire, lang == "ar")
        for debut in range(0, source.page_count, par_exemplaire):
            for cote in cotes:
                feuille = sortie.new_page(width=largeur, height=hauteur)
                for moitie, n in zip(moities, cote):
                    if n <= par_exemplaire:     # sinon : page laissée blanche
                        feuille.show_pdf_page(moitie, source, debut + n - 1)
                feuille.show_pdf_page(feuille.rect, calque, 0)
        return sortie.tobytes(garbage=1, deflate=True)


def _dessiner_cadre(c, page_index, haut=L.CADRE_MARGE, talon=False):
    """Trait du cadre de calage et tirets du code de page (n° et sens).
    Copie à talon : le cadre commence plus bas (haut) et un 9e tiret, plein,
    la distingue d'une copie normale à la lecture."""
    m, t = L.CADRE_MARGE, L.CADRE_TRAIT
    c.setStrokeGray(0.0)
    c.setLineWidth(t * mm)
    c.rect(m * mm, _y(haut, L.PAGE_H - m - haut), (L.PAGE_W - 2 * m) * mm,
           (L.PAGE_H - m - haut) * mm, stroke=1, fill=0)
    c.setFillGray(0.0)
    for (x, y, w, h), plein in zip(L.code_cases(haut), L.code_page(page_index)):
        if plein:
            c.rect(x * mm, _y(y, h), w * mm, h * mm, stroke=0, fill=1)
    if talon:
        x, y, w, h = L.case_variante(haut)
        c.rect(x * mm, _y(y, h), w * mm, h * mm, stroke=0, fill=1)
    c.setLineWidth(1)


def _ligne_salle(salle, T):
    """« Salle 12 » ; une salle déjà nommée (« قاعة عدد 37 ») s'imprime telle quelle."""
    return salle if re.match(r"\s*(salle|قاعة|القاعة)", salle, re.I) else f"{T['salle']} {salle}"


def _taille_pour(texte, taille, largeur_mm, plancher=7.0):
    """Corps qui fait tenir le texte dans la largeur, sans descendre sous le plancher."""
    while taille > plancher and text_width_pt(texte, taille) / mm > largeur_mm:
        taille -= 0.5
    return taille


def _dessiner_bande(c, draw, quiz, student, qr_zone, talon, places, T, lang, image_qr,
                    code=None):
    """QR de l'étudiant et bande grise d'une copie nominative, à la place que lui
    donne la disposition : sur chaque page lue (page de garde d'un écrit, chaque page
    d'un QCM), jamais sur une page lignée. code : celui du talon, imprimé sous le QR
    (hors des zones lues : entre le bas du QR et le contenu de la page, 4 mm)."""
    qx, qy, qw, qh = qr_zone["rect"]
    c.drawImage(image_qr, qx * mm, _y(qy, qh), qw * mm, qh * mm)
    if code:
        draw(qr_mod.code_lisible(code), qx + qw / 2, _y(qy + qh + 2.9), 8.5, bold=True,
             align="center")
    bx_, by_, bw_, bh_ = qr_zone["band"]
    c.setFillGray(0.955)
    c.roundRect(bx_ * mm, _y(by_, bh_), bw_ * mm, bh_ * mm, 2 * mm, stroke=0, fill=1)
    c.setFillGray(0.0)
    sub_txt = num_txt = ""
    if talon:
        # le nom est sur le talon découpé : dans le cadre, rien
        # qui identifie l'étudiant à l'œil
        title_txt = quiz.title
    elif places is not None:
        # copie anonyme : le n° de place, ni nom ni n° d'inscription
        title_txt = T["place"].format(n=places.get(student.pk, "—"))
    else:
        title_txt = student.full_name               # en arabe, le prénom d'abord
        sub_txt = T["personal"]
        num_txt = f"N° {student.student_number}" if student.student_number else ""
    if not sub_txt:
        # seul dans la bande : centré
        draw(title_txt, bx_ + bw_ / 2, _y(by_ + bh_ / 2 + 1.7), 14,
             bold=True, align="center")
    elif lang == "ar":
        draw(title_txt, bx_ + bw_ - 5, _y(by_ + 9.0), 14, bold=True, align="right")
        draw(sub_txt, bx_ + bw_ - 5, _y(by_ + 15.5), 8, align="right", gray=0.4)
        if num_txt:
            draw(num_txt, bx_ + 5, _y(by_ + 15.5), 8, gray=0.4)
    else:
        draw(title_txt, bx_ + 5, _y(by_ + 9.0), 14, bold=True)
        draw(sub_txt, bx_ + 5, _y(by_ + 15.5), 8, gray=0.4)
        if num_txt:
            draw(num_txt, bx_ + bw_ - 5, _y(by_ + 15.5), 8, align="right", gray=0.4)


def _dessiner_talon(c, draw, student, titre, page_txt, consigne, T, lang, code=None):
    """Talon du haut de la copie : nom et n° de l'étudiant, sous le n° sa CIN et
    sa salle (fiche du registre, demande du 5 novembre 2026), sous le nom le code
    d'anonymat, le même que sous le QR de la copie (demande du 13 novembre 2026),
    épreuve, consigne, puis le trait pointillé « couper ici » (hors du cadre de
    lecture : ce qu'on y ajoute ne change rien à la lecture)."""
    x0, x1 = 14.0, L.PAGE_W - 14.0
    rtl = lang == "ar"
    nom = student.full_name if student else ""      # en arabe, le prénom d'abord
    num = f"N° {student.student_number}" if student and student.student_number else ""
    fiche = getattr(student, "inscrit", None) if student else None
    details = [t for t in (f"{T['cin']} {fiche.cin}" if fiche and fiche.cin else "",
                           _ligne_salle(fiche.salle, T) if fiche and fiche.salle else "") if t]
    debut, fin, sens_debut, sens_fin = ((x1, x0, "right", "left") if rtl
                                        else (x0, x1, "left", "right"))
    draw(nom, debut, _y(11.5), 16, bold=True, align=sens_debut)
    if num:
        draw(num, fin, _y(11.5), 12, bold=True, align=sens_fin)
    for rang, texte in enumerate(details):
        draw(texte, fin, _y(16.8 + 4.4 * rang), 9, align=sens_fin)
    ligne_code = f"{T['code']} {qr_mod.code_lisible(code)}" if code else ""
    if ligne_code:
        draw(ligne_code, debut, _y(17.8), 11, bold=True, align=sens_debut)
    # Le titre, au milieu, et la consigne laissent la place à ce qui est imprimé
    # sous le n° (la page, ou la CIN et la salle) et au code : ils se resserrent.
    bloc = max(text_width_pt(t, 9 if details else 8) / mm for t in (details or [page_txt]))
    # le titre, centré, s'écarte des deux colonnes ; la consigne, du côté du code mais
    # plus bas que lui, seulement de celle d'en face
    bloc_titre = max(bloc, text_width_pt(ligne_code, 11, bold=True) / mm) if ligne_code else bloc
    place = L.PAGE_W - 2 * (x0 + bloc_titre + 4)
    taille = _taille_pour(titre, 9.5, place)
    draw(_ellipsize(titre, taille, place), L.PAGE_W / 2, _y(18.0), taille,
         align="center", gray=0.3)
    draw(page_txt, fin, _y(25.6 if details else 18.0), 8, align=sens_fin, gray=0.4)
    draw(consigne, debut, _y(23.5), _taille_pour(consigne, 8, x1 - x0 - bloc - 4, 6.5),
         align=sens_debut, gray=0.35)
    # trait de coupe, pointillé, sur toute la largeur
    c.setStrokeGray(0.35)
    c.setLineWidth(0.6)
    c.setDash(3, 2)
    c.line(4 * mm, _y(L.TALON_COUPE), (L.PAGE_W - 4) * mm, _y(L.TALON_COUPE))
    c.setDash()
    c.setStrokeGray(0.0)
    c.setFont("ZapfDingbats", 11)
    c.setFillGray(0.35)
    c.drawString(5 * mm, _y(L.TALON_COUPE + 1.3), "✂")
    c.setFillGray(0.0)
    # sans code (talon dessiné hors des copies à talon générées) : le texte n'en parle pas
    draw(T["talon_coupe_code" if code else "talon_coupe"], L.PAGE_W / 2,
         _y(L.TALON_COUPE - 1.6), 6.8,
         align="center", gray=0.45)


def _dessiner_lignes(c, zone):
    """Lignes d'écriture de la copie (gris clair : la lecture ne les voit pas,
    elles sont hors des zones lues)."""
    c.setStrokeGray(0.78)
    c.setLineWidth(0.4)
    x0, x1 = zone.get("x0", L.CONTENT_X0), zone.get("x1", L.CONTENT_X1)
    y = zone["y0"]
    while y <= zone["y1"]:
        c.line(x0 * mm, _y(y), x1 * mm, _y(y))
        y += zone["pas"]
    if zone.get("marge"):
        c.line(zone["marge"] * mm, _y(zone["y0"]), zone["marge"] * mm, _y(y - zone["pas"]))
    c.setStrokeGray(0.0)


def _dessiner_grille_note(c, draw, g, T, lang):
    """Cadre « réservé au correcteur » : grille de la note à noircir (même
    dessin que la grille de n° d'inscription), note en clair et signature."""
    if g.get("modele") == "demi":
        _dessiner_grille_demi(c, draw, g, T)
        return
    x, y, w, h = g["rect"]
    c.setLineWidth(1.1)
    c.roundRect(x * mm, _y(y, h), w * mm, h * mm, 2.5 * mm, stroke=1, fill=0)
    draw(T["note_titre"].format(n=f"{g['sur']:g}"), x + 4.0, _y(g["title_y"]),
         9.5, bold=True)
    draw(T["note_aide"], x + 4.0, _y(g["title_y"] + 4.5), 6.8, gray=0.35)
    for k in range(10):
        cx = g["rows"][0][k][0]
        draw(str(k), cx, _y(g["header_y"]), 6.5, align="center", gray=0.35)
    for kind, row, box in zip(g["kinds"], g["rows"], g["write_boxes"]):
        bx, by, bw, bh = box
        draw(T.get(kind, kind), x + 4.0, _y(by + bh / 2 + 1.2), 8, bold=True)
        c.setLineWidth(0.6)
        c.rect(bx * mm, _y(by, bh), bw * mm, bh * mm)
        for k, (cx, cy) in enumerate(row):
            c.circle(cx * mm, _y(cy), g["r"] * mm, stroke=1, fill=0)
            draw(str(k), cx, _y(cy + 0.7), 5, align="center", gray=0.6)
        if kind == "dixiemes":
            # virgule entre la partie entière et les dixièmes
            draw(",", bx - 2.2, _y(by + bh - 0.6), 12, bold=True)
    # à droite des cases : la note en clair et la signature du correcteur
    xr = g["rows"][0][-1][0] + 8.0
    draw(T["note_ecrite"], xr, _y(y + 16.0), 9, bold=True)
    c.setLineWidth(0.8)
    c.rect(xr * mm, _y(y + 18.0, 11.0), (x + w - 4.0 - xr) * mm, 11.0 * mm)
    draw(f"/ {g['sur']:g}", x + w - 6.0, _y(y + 25.5), 10, bold=True, align="right")
    draw(T["signature"], xr, _y(y + h - 3.0), 8, gray=0.35)


def _dessiner_grille_demi(c, draw, g, T):
    """Grille de la note d'un écrit par demi-points (demande de l'utilisateur, 13 novembre
    2026) : la note en clair et la signature en haut ; dessous, la partie entière de 0 au
    barème sur une ligne, puis la décimale, « ,0 » ou « ,5 » (layout._add_note_grid_demi)."""
    x, y, w, h = g["rect"]
    sur = f"{g['sur']:g}"
    c.setLineWidth(1.1)
    c.roundRect(x * mm, _y(y, h), w * mm, h * mm, 2.5 * mm, stroke=1, fill=0)
    draw(T["note_titre"].format(n=sur), x + 4.0, _y(g["title_y"]), 9.5, bold=True)
    draw(T["note_aide_demi"].format(n=sur), x + 4.0, _y(g["title_y"] + 4.5), 6.8, gray=0.35)
    # la note en clair, la signature du correcteur
    c.setLineWidth(0.8)
    nx, ny, nw, nh = g["note_box"]
    draw(T["note_ecrite"], nx, _y(ny - 1.4), 9, bold=True)
    c.rect(nx * mm, _y(ny, nh), nw * mm, nh * mm)
    draw(f"/ {sur}", nx + nw - 2.0, _y(ny + nh - 3.6), 10, bold=True, align="right")
    sx, sy, sw, sh = g["signature_box"]
    draw(T["signature"], sx, _y(sy - 1.4), 9, bold=True)
    c.rect(sx * mm, _y(sy, sh), sw * mm, sh * mm)
    # cases à noircir : partie entière (n° au-dessus de chaque case), puis décimale
    entiers, demis = g["rows"]
    for k, (cx, _) in enumerate(entiers):
        draw(str(k), cx, _y(g["header_y"]), 6.5, align="center", gray=0.35)
    c.setLineWidth(0.6)
    for kind, row, textes in (("entier", entiers, [str(k) for k in range(len(entiers))]),
                              ("demi", demis, [",0", ",5"])):
        draw(T[kind], x + 4.0, _y(row[0][1] + 1.2), 8, bold=True)
        for (cx, cy), texte in zip(row, textes):
            c.circle(cx * mm, _y(cy), g["r"] * mm, stroke=1, fill=0)
            draw(texte, cx, _y(cy + 0.7), 5, align="center", gray=0.6)
    # la note ne dépasse pas le barème : avec 20, « ,0 »
    draw(T["note_max_demi"].format(n=sur), demis[-1][0] + 6.0, _y(demis[0][1] + 1.0), 7,
         gray=0.35)


# Version du dessin du SUJET. À augmenter à chaque changement de ce qu'il
# imprime : les sujets déjà générés sont alors refaits à l'ouverture du quiz
# (services.actualiser_sujet). Sans risque, le sujet n'est jamais scanné.
#   2 — concours : les choix des QCM sont imprimés (avant : intitulés seuls).
#   3 — présentation des sujets de concours tunisiens : cartouche à trois
#       cases, page de garde « Remarques / Consignes », pagination n/N.
#   4 — plus de case « collez ici l'étiquette » dans le cartouche (l'étiquette
#       va sur la feuille de réponses) ; consignes selon la copie réellement
#       remise au candidat (étiquette, grille, talon, n° de place, nom).
#   5 — en-tête de l'institut si aucun n'est saisi ; une ligne laissée vide dans
#       l'en-tête reste un espace dans le cartouche.
#   6 — feuille à étiquette en plusieurs pages : consigne d'une étiquette par page.
#   7 — avec une date : la durée passe sous elle ; plus d'heure dans le cartouche.
SUJET_VERSION = 7

# QuizScan sert à cet institut : son en-tête s'imprime sur tout sujet dont le champ
# « En-tête du sujet » est vide. La ligne vide sépare les ministères de l'établissement.
ENTETE_INSTITUT = ("الجمهورية التونسية\nوزارة العدل\n"
                   "وزارة التعليم العالي و البحث العلمي\n\nالمعهد الأعلى للمحاماة")

# Textes du sujet. Les consignes sont écrites d'après ce que QuizScan lit
# réellement : cases à noircir (pas de croix), et une consigne d'identification
# par présentation de la copie (voir _cle_identification). {f} : le document
# que le candidat remplit, « feuille » ou « copie » (épreuve écrite).
SUJET_TEXTES = {
    "fr": {
        "epreuve_ecrite": "Épreuve écrite — note sur {n}",
        "consigne_ecrite": "Rédigez sur la copie fournie. Ne rien écrire dans "
                           "le cadre réservé au correcteur.",
        "epreuve": "Épreuve à choix multiples",
        "epreuve_mixte": "Questions à choix multiples et rédigées",
        "epreuve_redigee": "Questions rédigées",
        "feuille": "feuille de réponses",
        "copie": "copie",
        "duree": "Durée : {d}",
        "date": "Date",
        "duree_seule": "Durée",
        "nom": "Prénom et nom :",
        "classe": "Classe : {c}",
        "titre_main": "Intitulé de l'épreuve :",
        "remarques": "Remarques :",
        "consignes": "Consignes :",
        "r_pages": "Le sujet comporte {p} page(s) de questions, numérotées de 1 "
                   "à {n}, et une feuille de réponses séparée.",
        "r_nombre": "Nombre de questions : {n}.",
        "r_unique": "Chaque question n'admet qu'une seule bonne réponse.",
        "r_unique_qcm": "Chaque question à choix multiples n'admet qu'une seule "
                        "bonne réponse.",
        "r_penalite": "Chaque réponse fausse retire {x} point(s) ; une question "
                      "sans réponse ne retire rien.",
        "c_etiquette": "Collez l'étiquette QR qui vous a été remise dans "
                       "l'emplacement réservé de la {f}.",
        "c_grille": "Si une étiquette QR vous a été remise, collez-la dans le "
                    "cadre prévu de la {f} ; sinon, écrivez votre numéro "
                    "d'inscription et noircissez les chiffres correspondants "
                    "dans la grille.",
        "c_nom": "Écrivez lisiblement votre nom et votre prénom dans les cases "
                 "prévues de la {f}.",
        "c_nominative": "Vérifiez que le nom et le numéro imprimés sur la {f} "
                        "reçue sont bien les vôtres.",
        "c_talon": "Vérifiez le nom et le numéro imprimés sur le talon en haut "
                   "de la {f}. Ne découpez pas ce talon : il sera retiré avant "
                   "la correction. N'écrivez votre nom nulle part ailleurs.",
        "c_place": "Vérifiez que le numéro imprimé sur la {f} est bien celui de "
                   "votre place. N'y écrivez ni votre nom ni aucun signe "
                   "distinctif.",
        "c_chaque_page": "La feuille de réponses compte {p} pages : "
                         "identifiez-vous sur chacune d'elles.",
        "c_chaque_page_etiquette": "La feuille de réponses compte {p} pages : "
                                   "collez une étiquette QR sur chacune d'elles.",
        "c_noircir": "Pour chaque question, noircissez complètement une seule "
                     "case sur la feuille de réponses.",
        "c_noircir_qcm": "Pour chaque question à choix multiples, noircissez "
                         "complètement une seule case sur la feuille de réponses.",
        "c_redigees": "Rédigez les réponses aux questions rédigées dans les "
                      "cadres prévus de la feuille de réponses.",
        "c_brouillon": "Un seul exemplaire de la feuille de réponses est remis par "
                       "candidat : préparez vos réponses sur ce sujet avant de "
                       "les reporter.",
        "c_stylo": "Utilisez un stylo à bille noir ou bleu, à l'exclusion de "
                   "toute autre couleur.",
        "c_blanco": "N'utilisez pas de correcteur (blanco) et ne raturez pas la "
                    "feuille de réponses.",
        "c_plier": "Ne pliez pas la feuille de réponses.",
        "c_rendre": "À la fin de l'épreuve, rendez la feuille de réponses et le "
                    "sujet.",
        "page": "{i}/{n}",
    },
    "ar": {
        "epreuve_ecrite": "اختبار كتابي — العدد على {n}",
        "consigne_ecrite": "حرّر على الورقة المسلّمة إليك. لا تكتب شيئا في "
                           "الإطار المخصّص للمصحّح.",
        "epreuve": "اختبار في الأسئلة متعددة الاختيارات",
        "epreuve_mixte": "أسئلة متعددة الاختيارات وأسئلة تحريرية",
        "epreuve_redigee": "أسئلة تحريرية",
        "feuille": "ورقة الإجابة",
        "copie": "ورقة التحرير",
        "duree": "المدة : {d}",
        "date": "التاريخ",
        "duree_seule": "المدة",
        "nom": "الاسم واللقب :",
        "classe": "القسم : {c}",
        "titre_main": "عنوان الاختبار :",
        "remarques": "ملاحظات :",
        "consignes": "تعليمات :",
        "r_pages": "يتضمن الاختبار {p} من الأسئلة المرقمة من 1 إلى {n} "
                   "وورقة إجابة منفصلة.",
        "r_nombre": "عدد الأسئلة : {n}",
        "r_unique": "كل سؤال يحتمل إجابة واحدة صحيحة لا غير.",
        "r_unique_qcm": "كل سؤال متعدد الاختيارات يحتمل إجابة واحدة صحيحة لا غير.",
        "r_penalite": "تُطرح {x} نقطة عن كل إجابة خاطئة، ولا يُطرح شيء عن "
                      "السؤال الذي لم تتم الإجابة عنه.",
        "c_etiquette": "تُثبَّت لاصقة رمز QR المسلَّمة إليك في المكان المخصص لها "
                       "على {f}.",
        "c_grille": "إن سُلِّمت إليك لاصقة رمز QR تُثبَّت في الإطار المخصص لها "
                    "على {f}، وإلا يُكتب رقم التسجيل وتُملأ الأرقام المطابقة "
                    "له في الشبكة.",
        "c_nom": "يُكتب اللقب والاسم بخط واضح في الخانات المخصصة لهما على {f}.",
        "c_nominative": "يُتثبَّت من أن الاسم واللقب والرقم المطبوعة على {f} "
                        "تخصّك.",
        "c_talon": "يُتثبَّت من الاسم واللقب والرقم المطبوعة على القسيمة أعلى {f}. "
                   "لا تُقصّ هذه القسيمة، فهي تُزال قبل الإصلاح، ولا يُكتب الاسم "
                   "في أي مكان آخر.",
        "c_place": "يُتثبَّت من أن الرقم المطبوع على {f} هو رقم مكانك. لا يُكتب "
                   "عليها الاسم ولا أي علامة مميِّزة.",
        "c_chaque_page": "تتكوّن ورقة الإجابة من {p}: يُكتب التعريف على كل صفحة "
                         "منها.",
        "c_chaque_page_etiquette": "تتكوّن ورقة الإجابة من {p}: تُثبَّت لاصقة رمز QR "
                                   "على كل صفحة منها.",
        "c_noircir": "تُملأ كليًا خانة واحدة لكل سؤال على ورقة الإجابة.",
        "c_noircir_qcm": "تُملأ كليًا خانة واحدة لكل سؤال متعدد الاختيارات على "
                         "ورقة الإجابة.",
        "c_redigees": "تُحرَّر الإجابة عن الأسئلة التحريرية في الإطارات المخصصة "
                      "لها على ورقة الإجابة.",
        "c_brouillon": "لا تُسلَّم إلا نسخة واحدة من ورقة الإجابة لكل مترشح، "
                       "ويُستحسن تحضير الإجابة على ورقة الأسئلة قبل نقلها إلى "
                       "ورقة الإجابة.",
        "c_stylo": "يُستعمل القلم الجاف الأسود أو الأزرق دون سواهما.",
        "c_blanco": "يُمنع استعمال الماحي (Blanco) والتشطيب على ورقة الإجابة.",
        "c_plier": "عدم طيّ ورقة الإجابة.",
        "c_rendre": "تُرجَع ورقة الإجابة وأوراق الأسئلة في نهاية الاختبار.",
        "page": "{i}/{n}",
    },
}


def _nb_pages(p, lang):
    """« 3 page(s) » en français ; en arabe, l'accord du nom avec le nombre
    (صفحة واحدة، صفحتين، 3 صفحات، 11 صفحة)."""
    if lang != "ar":
        return p
    if p == 1:
        return "صفحة واحدة"
    if p == 2:
        return "صفحتين"
    if 3 <= p <= 10:
        return f"{p} صفحات"
    return f"{p} صفحة"


def _cle_identification(quiz):
    """Consigne d'identification : ce que le candidat a à faire sur SA copie,
    d'après ce qui y est imprimé (mêmes conditions que layout.build_layout)."""
    if getattr(quiz, "talon_nom", False):
        return "c_talon"
    if getattr(quiz, "place_anonyme", False):
        return "c_place"
    id_mode = getattr(quiz, "id_mode", "name")
    if id_mode == "grid":
        return "c_grille"
    if id_mode == "name":
        return "c_nom"
    return "c_etiquette" if L._uses_sticker(quiz) else "c_nominative"


def generate_subject_pdf(quiz, questions=None):
    """PDF du SUJET (questions seules, sans cases à noircir) — à distribuer
    aux étudiants ; la grille de réponses est un document séparé (sheet_pdf).
    Aucun repère ArUco : ce document n'est pas scanné.

    Présentation des sujets de concours : cartouche en tête de la première
    page (institutions | intitulé | date et durée | nom du candidat, chaque case
    seulement si elle a un contenu ; sans date, la durée reste sous l'intitulé),
    puis, pour un concours, une page de garde
    « Remarques / Consignes » ; les questions suivent, chaque choix sur sa
    ligne, pages numérotées n/N."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    lang = getattr(quiz, "language", "fr")
    rtl = lang == "ar"
    T = LABELS.get(lang, LABELS["fr"])
    S = SUJET_TEXTES.get(lang, SUJET_TEXTES["fr"])
    qs = list(questions if questions is not None
              else quiz.questions.order_by("order"))
    X0, X1 = L.CONTENT_X0, L.CONTENT_X1
    W = X1 - X0
    HAUT, BAS = 22.0, L.PAGE_H - 20.0

    def draw(text, x_mm_, y_mm_, size, bold=False, align="left", gray=None):
        y_pt = (L.PAGE_H - y_mm_) * mm
        if gray is not None:
            c.setFillGray(gray)
        if has_arabic(text):
            _draw_mixed(c, shape(text), x_mm_ * mm, y_pt, size, bold, align)
        else:
            c.setFont(font_for(text, bold), size)
            if align == "center":
                c.drawCentredString(x_mm_ * mm, y_pt, text)
            elif align == "right":
                c.drawRightString(x_mm_ * mm, y_pt, text)
            else:
                c.drawString(x_mm_ * mm, y_pt, text)
        if gray is not None:
            c.setFillGray(0.0)

    def ligne(text, y, size, bold=False, retrait=0.0, gray=None):
        """Ligne alignée sur le début du sens de lecture (droite en arabe)."""
        if rtl:
            draw(text, X1 - retrait, y, size, bold, align="right", gray=gray)
        else:
            draw(text, X0 + retrait, y, size, bold, gray=gray)

    sticker = getattr(quiz, "id_mode", "name") == "sticker"
    ecrit = getattr(quiz, "type_epreuve", "qcm") == "ecrit"
    concours = getattr(quiz, "auto_enroll", False)
    # page de garde « remarques / consignes » : concours QCM seulement (elle
    # décrit une épreuve à cases)
    garde = concours and not ecrit
    # Titre écrit à la main (mode étiquette hors concours) : le même sujet
    # peut servir à plusieurs épreuves, il ne porte ni titre ni classe.
    titre_a_la_main = sticker and not concours and not ecrit
    # Case « Prénom et nom » : pas sur le sujet d'un concours, d'une épreuve à
    # étiquette (elle va sur la feuille de réponses) ni d'une copie anonyme.
    nom_sur_sujet = not (concours or sticker or getattr(quiz, "talon_nom", False)
                         or getattr(quiz, "place_anonyme", False))

    def fmt_pts(points):
        unit = (T["pt"] if points <= 1 else T["pts"])
        return f"({points:g} {unit})"

    # ---------------------------------------------------------------- blocs
    # Chaque question est un bloc insécable : on calcule d'abord sa hauteur
    # pour paginer, puis on dessine — la pagination « n/N » a besoin du total.
    blocs = []
    if ecrit:
        # Épreuve écrite : l'énoncé saisi, paragraphe par paragraphe (une
        # ligne vide les sépare), puis la consigne de la copie.
        paragraphes = [p.strip() for p in
                       (quiz.sujet_texte or "").replace("\r", "").split("\n\n")
                       if p.strip()]
        paragraphes.append(S[_cle_identification(quiz)].format(f=S["copie"]))
        paragraphes.append(S.get("consigne_ecrite", ""))
        for p in paragraphes:
            lignes_p = []
            for l in p.split("\n"):
                lignes_p += wrap(l, 11, W) or [""]
            blocs.append((None, lignes_p, [], 0.0, len(lignes_p) * 6.0 + 4.0))
        qs = []
    for q in qs:
        tete = wrap(f"{q.order}. {q.text}".strip() + f" {fmt_pts(q.points)}",
                    11.5, W, bold=True)
        choix = []
        if q.qtype == "qcm":
            for k, ch in enumerate(q.choices or []):
                choix.append(wrap(f"{choice_letter(k, lang)}. {ch}", 10.5, W - 8.0))
        cadre = 0.0 if (q.qtype == "qcm" or concours) else float(q.open_height_mm)
        h = (len(tete) * 6.0 + 1.5 + sum(len(b) * 5.6 for b in choix)
             + (cadre + 3.0 if cadre else 0.0) + 6.0)
        blocs.append((q, tete, choix, cadre, h))
    a_qcm = any(q.qtype == "qcm" for q in qs)
    a_redigees = any(q.qtype != "qcm" for q in qs)

    CARTOUCHE_H = 36.0
    debut_questions_p1 = HAUT + CARTOUCHE_H + 10.0
    pages = [[]]                       # blocs de chaque page de questions
    y = HAUT if garde else debut_questions_p1
    for bloc in blocs:
        if y + bloc[4] > BAS and pages[-1]:
            pages.append([])
            y = HAUT
        pages[-1].append(bloc)
        y += bloc[4]
    total = len(pages) + (1 if garde else 0)
    page_no = [0]

    def entete_page():
        page_no[0] += 1
        draw(S["page"].format(i=page_no[0], n=total), L.PAGE_W / 2, 12.0, 9,
             align="center", gray=0.35)

    def pied_page():
        if not titre_a_la_main:
            draw(quiz.title, L.PAGE_W / 2, L.PAGE_H - 9.0, 6.5, align="center",
                 gray=0.5)

    # ------------------------------------------------------------ cartouche
    def cartouche():
        y0, h = HAUT, CARTOUCHE_H
        # institutions : l'en-tête saisi ou, à défaut, celui de l'institut. Une ligne
        # laissée vide y reste un espace.
        inst = [t.strip() for t in
                ((quiz.entete or "").strip() or ENTETE_INSTITUT).splitlines()]
        # Ordre de lecture : institutions | épreuve | date | identification, de
        # droite à gauche en arabe. Une case sans contenu n'est pas dessinée : elle
        # inviterait le candidat à y écrire ou à y coller quelque chose.
        jour = getattr(quiz, "date_epreuve", None)
        cases = (["inst", "epreuve"] + (["date"] if jour else [])
                 + (["id"] if nom_sur_sujet else []))
        wcol = W / len(cases)
        c.setStrokeGray(0.0)
        c.setLineWidth(0.8)
        c.rect(X0 * mm, (L.PAGE_H - (y0 + h)) * mm, W * mm, h * mm)
        for k in range(1, len(cases)):
            x = X0 + k * wcol
            c.line(x * mm, (L.PAGE_H - y0) * mm, x * mm, (L.PAGE_H - (y0 + h)) * mm)
        x_case = {nom: (X1 - (k + 1) * wcol) if rtl else (X0 + k * wcol)
                  for k, nom in enumerate(cases)}
        x_epr = x_case["epreuve"]

        def bloc_centre(lignes, x, y_haut, h_case, size=9.5, gras_premiere=False):
            textes = []
            for i, t in enumerate(lignes):
                if not t:
                    textes.append(("", False))
                    continue
                for morceau in wrap(t, size, wcol - 6.0, bold=gras_premiere and i == 0):
                    textes.append((morceau, gras_premiere and i == 0))
            pas = size * 0.47
            yy = y_haut + (h_case - len(textes) * pas) / 2 + pas - 0.8
            for t, gras in textes:
                if t:
                    draw(t, x + wcol / 2, yy, size, bold=gras, align="center")
                yy += pas

        # 1. institutions
        bloc_centre(inst, x_case["inst"], y0, h, size=9, gras_premiere=True)

        # 2. épreuve : titre en haut, nature et durée en bas
        moitie = h / 2
        c.setLineWidth(0.6)
        c.line(x_epr * mm, (L.PAGE_H - (y0 + moitie)) * mm,
               (x_epr + wcol) * mm, (L.PAGE_H - (y0 + moitie)) * mm)
        if titre_a_la_main:
            draw(S["titre_main"], x_epr + wcol / 2, y0 + 7.0, 8.5, align="center",
                 gray=0.35)
            c.setLineWidth(0.5)
            c.line((x_epr + 5) * mm, (L.PAGE_H - (y0 + 13.5)) * mm,
                   (x_epr + wcol - 5) * mm, (L.PAGE_H - (y0 + 13.5)) * mm)
        else:
            # un titre long se resserre pour rester dans sa moitié de case
            taille = 11.0
            while (taille > 7 and moitie - 2
                   < len(wrap(quiz.title, taille, wcol - 6.0, bold=True)) * taille * 0.47):
                taille -= 0.5
            bloc_centre([quiz.title], x_epr, y0, moitie, size=taille, gras_premiere=True)
        if ecrit:
            bas = [S.get("epreuve_ecrite", "{n}").format(n=f"{quiz.note_sur:g}")]
        else:
            bas = [S["epreuve"] if not a_redigees
                   else S["epreuve_mixte"] if a_qcm else S["epreuve_redigee"]]
        if quiz.duree and not jour:     # avec une date, la durée va sous elle
            bas.append(S["duree"].format(d=quiz.duree))
        bloc_centre(bas, x_epr, y0 + moitie, moitie, size=9, gras_premiere=True)

        # 3. date, et durée dessous : la valeur sur sa propre ligne, car dans une ligne
        # arabe l'ordre des mots latins s'inverse (« 2 h » s'y lit « h 2 »)
        if jour:
            x_date = x_case["date"]
            valeur_date = [S["date"], jour.strftime("%d/%m/%Y")]
            if quiz.duree:
                c.setLineWidth(0.6)
                c.line(x_date * mm, (L.PAGE_H - (y0 + moitie)) * mm,
                       (x_date + wcol) * mm, (L.PAGE_H - (y0 + moitie)) * mm)
                bloc_centre(valeur_date, x_date, y0, moitie, size=9, gras_premiere=True)
                bloc_centre([S["duree_seule"], quiz.duree], x_date, y0 + moitie, moitie,
                            size=9, gras_premiere=True)
            else:
                bloc_centre(valeur_date, x_date, y0, h, size=9, gras_premiere=True)

        # 4. identification
        if nom_sur_sujet:
            x_id = x_case["id"]
            draw(S["nom"], (x_id + wcol - 3) if rtl else (x_id + 3), y0 + 7.5, 9,
                 bold=True, align="right" if rtl else "left")
            c.setDash(1, 2)
            c.setLineWidth(0.6)
            for yy in (y0 + 17.0, y0 + 25.0):
                c.line((x_id + 4) * mm, (L.PAGE_H - yy) * mm,
                       (x_id + wcol - 4) * mm, (L.PAGE_H - yy) * mm)
            c.setDash()
            draw(S["classe"].format(c=quiz.class_group.name), x_id + wcol / 2,
                 y0 + 32.0, 8, align="center", gray=0.35)

    # ---------------------------------------------- page de garde (concours)
    def liste_numerotee(titre, items, y):
        y += 7.0
        ligne(titre, y, 12, bold=True)
        larg = text_width_pt(titre, 12, True) / mm
        c.setLineWidth(0.6)
        xa = (X1 - larg) if rtl else X0
        c.line(xa * mm, (L.PAGE_H - (y + 1.3)) * mm, (xa + larg) * mm,
               (L.PAGE_H - (y + 1.3)) * mm)
        y += 3.0
        for i, item in enumerate(items, start=1):
            morceaux = wrap(item, 10.5, W - 14.0)
            for j, m in enumerate(morceaux):
                y += 6.2
                if j == 0:
                    ligne(f"{i}.", y, 10.5, bold=True, retrait=2.0)
                ligne(m, y, 10.5, retrait=9.0)
            y += 1.2
        return y

    def page_de_garde():
        n_q = len(qs)
        remarques = [S["r_pages"].format(p=_nb_pages(len(pages), lang), n=n_q),
                     S["r_nombre"].format(n=n_q)]
        if a_qcm:
            remarques.append(S["r_unique_qcm"] if a_redigees else S["r_unique"])
            if quiz.wrong_penalty:
                remarques.append(S["r_penalite"].format(x=f"{quiz.wrong_penalty:g}"))
        cle = _cle_identification(quiz)
        consignes = [S[cle].format(f=S["feuille"])]
        # Chaque page de la feuille est identifiée séparément au scan.
        nb_feuilles = len((quiz.layout_json or {}).get("pages", []))
        if nb_feuilles > 1 and cle == "c_etiquette":
            consignes.append(S["c_chaque_page_etiquette"].format(p=_nb_pages(nb_feuilles, lang)))
        elif nb_feuilles > 1 and cle in ("c_grille", "c_nom"):
            consignes.append(S["c_chaque_page"].format(p=_nb_pages(nb_feuilles, lang)))
        if a_qcm:
            consignes.append(S["c_noircir_qcm"] if a_redigees else S["c_noircir"])
        if a_redigees:
            consignes.append(S["c_redigees"])
        consignes += [S["c_brouillon"], S["c_stylo"], S["c_blanco"], S["c_plier"],
                      S["c_rendre"]]
        y = debut_questions_p1 - 4.0
        y = liste_numerotee(S["remarques"], remarques, y)
        y = liste_numerotee(S["consignes"], consignes, y + 6.0)

    # --------------------------------------------------------------- dessin
    entete_page()
    cartouche()
    if garde:
        page_de_garde()
        pied_page()
        c.showPage()
        entete_page()
        y = HAUT
    else:
        y = debut_questions_p1

    for n, page in enumerate(pages):
        if n:
            pied_page()
            c.showPage()
            entete_page()
            y = HAUT
        for q, tete, choix, cadre, h in page:
            for t in tete:
                y += 6.0
                # un paragraphe d'énoncé (q None) en romain, un intitulé en gras
                ligne(t, y, 11.5 if q else 11, bold=q is not None)
            y += 1.5
            for bloc in choix:
                for t in bloc:
                    y += 5.6
                    ligne(t, y, 10.5, retrait=6.0)
            if cadre:
                y += 3.0
                c.setStrokeGray(0.4)
                c.setLineWidth(0.5)
                c.rect(X0 * mm, (L.PAGE_H - (y + cadre)) * mm, W * mm, cadre * mm,
                       stroke=1, fill=0)
                c.setStrokeGray(0.0)
                y += cadre
            y += 6.0

    pied_page()
    c.save()
    # Écrit : le format choisi est celui des copies, l'énoncé reste en A4 (souvent une
    # seule page : la feuille A3 serait à moitié vide).
    if not ecrit and getattr(quiz, "format_impression", "a4") == "a3":
        return _imposer_a3(buf.getvalue(), lang)
    return buf.getvalue()
