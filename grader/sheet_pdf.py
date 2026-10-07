"""Génération du PDF de la fiche (à imprimer puis scanner).

Deux modes (quiz.sheet_mode, figé dans layout["mode"]) :
  - "full" : questionnaire complet — questions et choix imprimés sur la fiche ;
  - "grid" : fiche de réponses seule — grille compacte de cases.

Le texte arabe est ligaturé et rendu droite->gauche (module fonts).
"""
import functools
import io

import cv2
import qrcode
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas

from . import layout as L
from .fonts import (font_for, has_arabic, load_fonts, runs, shape, wrap)
from .models import choice_letter

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
        "writename": "Écrivez votre nom et votre prénom dans les cases ci-dessus.",
        "fiche": "Fiche",
        "sticker": "Emplacement réservé — collez ici le QR code qui vous a été remis",
    },
    "ar": {
        "last": "اللقب", "first": "الاسم",
        "instr": "املأ خانة واحدة فقط لكل سؤال بشكل كامل.",
        "page": "صفحة", "question": "السؤال", "pt": "نقطة", "pts": "نقاط",
        "idnum": "رقم التسجيل — اكتب رقمك في الخانات ثم املأ الرقم المطابق في كل سطر:",
        "personal": "بطاقة شخصية — تأكد من اسمك ولا تتبادلها.",
        "writename": "اكتب لقبك واسمك بخط واضح في الخانات أعلاه.",
        "fiche": "بطاقة",
        "sticker": "مكان مخصّص — ألصق هنا رمز QR الذي سُلّم إليك",
    },
}


def _qr_image(payload):
    qr = qrcode.QRCode(border=1, box_size=8,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    return ImageReader(qr.make_image().get_image())


def generate_sheet_pdf(quiz, layout, students=None, serials=None):
    """Retourne les octets du PDF de la fiche du quiz.

    students : fiches nominatives (mode QR) — le jeu complet de pages est
    répété pour chaque étudiant, avec son nom imprimé et son QR code.
    serials : fiches anonymes (mode concours) — le jeu de pages est répété
    autant de fois qu'il y a d'éléments, pour imprimer N exemplaires
    identiques, avec l'emplacement réservé à l'étiquette du candidat
    (aucune case nom/prénom : le candidat n'écrit rien).

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
    units = students or serials or [None]
    for unit, page in [(u, p) for u in units for p in layout["pages"]]:
        student = unit if (students and unit is not None) else None
        pidx = page["page_index"]

        # Repères ArUco (avec leur marge blanche débordant légèrement)
        for mid, (x, y) in L.marker_positions(pidx).items():
            pad = L.MARKER_SIZE * (20 / 300)
            c.drawImage(_marker_image(mid),
                        (x - pad) * mm, _y(y - pad, L.MARKER_SIZE + 2 * pad),
                        (L.MARKER_SIZE + 2 * pad) * mm, (L.MARKER_SIZE + 2 * pad) * mm)

        # En-tête (concours : titre seul, sans le nom du conteneur de candidats)
        head_title = (quiz.title if concours
                      else f"{quiz.title} — {quiz.class_group.name}")
        draw(head_title, L.PAGE_W / 2, _y(24.0),
             13, bold=True, align="center")
        if lang == "ar":
            draw(T["instr"], L.PAGE_W / 2, _y(28.5), 8, align="center")
            draw(f"{T['page']} {pidx + 1} / {len(layout['pages'])}",
                 L.CONTENT_X1, _y(28.5), 8, align="right")
        else:
            draw(f"{T['page']} {pidx + 1} / {len(layout['pages'])} — {T['instr']}",
                 L.PAGE_W / 2, _y(28.5), 8, align="center")

        # Zone nom / prénom  (ou emplacement réservé à une étiquette autocollante)
        nb = page["name_boxes"]
        lx, ly, lw, lh = nb["last"]
        fx, fy, fw, fh = nb["first"]
        qr_zone = page.get("qr")
        # Fiche à étiquette autocollante (concours, ou mode « étiquette ») :
        # aucune case nom/prénom, l'identification est portée par l'étiquette
        # apposée dessous — le candidat n'écrit rien (fiche technique p. 6).
        # Partout ailleurs, les cases sont imprimées : l'OCR du nom y sert de
        # secours d'identification, y compris pour un concours identifié par
        # grille de n° d'inscription.
        if concours and (qr_zone or {}).get("sticker"):
            pass  # rien ici : voir la zone étiquette (ancien emplacement QR)
        else:
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
        if qr_zone and qr_zone.get("sticker"):
            # Étiquette / concours : pas de QR imprimé ni de n° de série. Tout l'emplacement
            # est un cadre réservé à l'étiquette (n° d'inscription + QR) que le
            # scan lira pour identifier le candidat.
            bx_, by_, bw_, bh_ = qr_zone["band"]
            qx, qy, qw, qh = qr_zone["rect"]
            zx0 = min(bx_, qx)
            zw = max(bx_ + bw_, qx + qw) - zx0
            # hors concours, les cases NOM/PRÉNOM sont juste au-dessus :
            # le libellé passe à l'intérieur du cadre
            label_y = by_ - 1.5 if concours else by_ + 4.0
            # Aligné sur le bord du cadre et non centré : le cadre ne fait
            # que la taille du QR découpé, un texte centré dessus déborderait
            # hors de la zone imprimable.
            if lang == "ar":
                draw(T["sticker"], zx0 + zw, _y(label_y), 8,
                     align="right", gray=0.45)
            else:
                draw(T["sticker"], zx0, _y(label_y), 8, gray=0.45)
            c.setLineWidth(0.9)
            c.setDash(3, 2)
            c.rect(zx0 * mm, _y(by_, bh_), zw * mm, bh_ * mm)
            c.setDash()
        elif qr_zone and student is not None:
            payload = f"QS|{quiz.pk}|{student.pk}"
            qx, qy, qw, qh = qr_zone["rect"]
            c.drawImage(_qr_image(payload), qx * mm, _y(qy, qh),
                        qw * mm, qh * mm)
            bx_, by_, bw_, bh_ = qr_zone["band"]
            c.setFillGray(0.955)
            c.roundRect(bx_ * mm, _y(by_, bh_), bw_ * mm, bh_ * mm, 2 * mm,
                        stroke=0, fill=1)
            c.setFillGray(0.0)
            title_txt = f"{student.last_name} {student.first_name}"
            sub_txt = T["personal"]
            num_txt = f"N° {student.student_number}" if student.student_number else ""
            if lang == "ar":
                draw(title_txt, bx_ + bw_ - 5, _y(by_ + 9.0), 14,
                     bold=True, align="right")
                if sub_txt:
                    draw(sub_txt, bx_ + bw_ - 5, _y(by_ + 15.5), 8,
                         align="right", gray=0.4)
                if num_txt:
                    draw(num_txt, bx_ + 5, _y(by_ + 15.5), 8, gray=0.4)
            else:
                draw(title_txt, bx_ + 5, _y(by_ + 9.0), 14, bold=True)
                if sub_txt:
                    draw(sub_txt, bx_ + 5, _y(by_ + 15.5), 8, gray=0.4)
                if num_txt:
                    draw(num_txt, bx_ + bw_ - 5, _y(by_ + 15.5), 8,
                         align="right", gray=0.4)

        # Grille de numéro d'inscription
        grid = page.get("id_grid")
        if grid:
            if lang == "ar":
                draw(T["idnum"], L.CONTENT_X1, _y(grid["label_y"]), 8, bold=True,
                     align="right")
            else:
                draw(T["idnum"], L.CONTENT_X0, _y(grid["label_y"]), 8, bold=True)
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

        draw(f"{quiz.title} — page {pidx + 1}",
             L.PAGE_W / 2, _y(L.PAGE_H - 5.0), 6.5, align="center", gray=0.5)
        c.showPage()

    c.save()
    return buf.getvalue()


def generate_subject_pdf(quiz, questions=None):
    """PDF du SUJET (questions seules, sans cases à noircir) — à distribuer
    aux étudiants ; la grille de réponses est un document séparé (sheet_pdf).
    Aucun repère ArUco : ce document n'est pas scanné."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    lang = getattr(quiz, "language", "fr")
    rtl = lang == "ar"
    T = LABELS.get(lang, LABELS["fr"])
    qs = list(questions if questions is not None
              else quiz.questions.order_by("order"))
    X0, X1 = L.CONTENT_X0, L.CONTENT_X1
    W = X1 - X0
    BOTTOM = L.PAGE_H - 18.0

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

    page_no = [0]

    sticker = getattr(quiz, "id_mode", "name") == "sticker"
    concours = getattr(quiz, "auto_enroll", False)
    subj_txt = ("السؤال — أجب على ورقة الإجابة المنفصلة" if rtl
                else "SUJET — répondez sur la feuille de réponses séparée")

    def header():
        page_no[0] += 1
        if concours:
            # Concours : titre une seule fois (sans nom de classe), pas de champ
            # nom/prénom, et un emplacement réservé à l'étiquette autocollante.
            draw(quiz.title, L.PAGE_W / 2, 18.0, 14, bold=True, align="center")
            rw = X1 - X0
            box_top, rh = 23.5, 10.0
            draw(T["sticker"], L.PAGE_W / 2, box_top - 1.5, 7.5,
                 align="center", gray=0.45)
            c.setStrokeGray(0.45)
            c.setLineWidth(0.9)
            c.setDash(3, 2)          # cadre en pointillés = zone à coller
            c.rect(X0 * mm, (L.PAGE_H - (box_top + rh)) * mm, rw * mm, rh * mm)
            c.setDash()
            draw(subj_txt, L.PAGE_W / 2, box_top + rh + 4.0, 8.5,
                 align="center", gray=0.4)
        elif sticker:
            # Titre à écrire à la main : une ligne vide (pas de nom de quiz/classe,
            # pas de champ NOM/PRÉNOM — l'identification passe par l'étiquette).
            c.setStrokeGray(0.45)
            c.setLineWidth(0.7)
            c.line(40 * mm, (L.PAGE_H - 22.0) * mm, (L.PAGE_W - 40) * mm,
                   (L.PAGE_H - 22.0) * mm)
            draw(subj_txt, L.PAGE_W / 2, 31.0, 8.5, align="center", gray=0.4)
        else:
            draw(f"{quiz.title} — {quiz.class_group.name}", L.PAGE_W / 2, 20.0,
                 14, bold=True, align="center")
            if rtl:
                draw("الاسم واللقب: .............................................",
                     X1, 30.0, 10, align="right")
            else:
                draw("NOM et PRÉNOM : ...........................................",
                     X0, 30.0, 10)
            draw(subj_txt, L.PAGE_W / 2, 36.0, 8.5, align="center", gray=0.4)
        c.setStrokeGray(0.75)
        c.setLineWidth(0.5)
        c.line(X0 * mm, (L.PAGE_H - 39.0) * mm, X1 * mm, (L.PAGE_H - 39.0) * mm)
        return 46.0

    def footer():
        foot = (f"page {page_no[0]}" if sticker
                else f"{quiz.title} — page {page_no[0]}")
        draw(foot, L.PAGE_W / 2, L.PAGE_H - 8.0, 6.5, align="center", gray=0.5)

    def fmt_pts(points):
        unit = (T["pt"] if points <= 1 else T["pts"])
        return f"({points:g} {unit})"

    y = header()
    for q in qs:
        # intitulé de la question (avec numéro et barème)
        head = f"{q.order}. {q.text}".strip() + f" {fmt_pts(q.points)}"
        head_lines = wrap(head, 11, W, bold=True)
        # lignes de choix (QCM) ou espace de réponse (manuscrite)
        # Concours : on n'imprime QUE les intitulés de questions (pas les choix,
        # pas de cadre de réponse) — les réponses vont sur la feuille séparée.
        choice_blocks = []
        if concours:
            block_h = len(head_lines) * 5.4 + 3.0
        elif q.qtype == "qcm":
            for k, ch in enumerate(q.choices or []):
                letter = choice_letter(k, lang)
                prefix = f"{letter}. "
                choice_blocks.append(wrap(f"{prefix}{ch}", 10.5, W - 8.0))
            block_h = (len(head_lines) * 5.4 + 1.5
                       + sum(len(b) * 5.0 for b in choice_blocks) + 5.0)
        else:
            block_h = len(head_lines) * 5.4 + float(q.open_height_mm) + 8.0

        if y + block_h > BOTTOM and (q is not qs[0]):
            footer(); c.showPage(); y = header()

        for line in head_lines:
            y += 5.4
            draw(line, X1 if rtl else X0, y, 11, bold=True,
                 align="right" if rtl else "left")
        y += 1.5
        if concours:
            pass  # aucun choix ni cadre de réponse sur le sujet concours
        elif q.qtype == "qcm":
            for block in choice_blocks:
                for j, line in enumerate(block):
                    y += 5.0
                    x = (X1 - 6.0) if rtl else (X0 + 6.0)
                    draw(line, x, y, 10.5, align="right" if rtl else "left")
        else:
            # cadre de réponse libre
            y += 2.0
            c.setStrokeGray(0.4); c.setLineWidth(0.5)
            c.rect(X0 * mm, (L.PAGE_H - (y + float(q.open_height_mm))) * mm,
                   W * mm, float(q.open_height_mm) * mm, stroke=1, fill=0)
            y += float(q.open_height_mm)
        y += 5.0

    footer()
    c.save()
    return buf.getvalue()
