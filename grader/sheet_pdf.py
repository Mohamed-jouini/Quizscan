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
from .fonts import (font_for, has_arabic, load_fonts, runs, shape,
                    text_width_pt, wrap)
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
        "qrcorner": "CODE QR",
        "qrstick": "Collez ici votre étiquette QR",
        "ident": "IDENTIFICATION",
        "identhelp": "Collez votre étiquette QR dans le cadre ci-contre. "
                     "Sans étiquette, remplissez la grille du n° d'inscription "
                     "ci-dessous.",
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
        "qrcorner": "رمز QR",
        "qrstick": "ألصق هنا ملصق رمز QR",
        "ident": "التعريف",
        "identhelp": "ألصق ملصق رمز QR في الإطار المقابل. "
                     "إن لم يكن لديك ملصق، املأ شبكة رقم التسجيل أدناه.",
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
        qr_zone = page.get("qr")
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

        draw(f"{quiz.title} — page {pidx + 1}",
             L.PAGE_W / 2, _y(L.PAGE_H - 5.0), 6.5, align="center", gray=0.5)
        c.showPage()

    c.save()
    return buf.getvalue()


# Version du dessin du SUJET. À augmenter à chaque changement de ce qu'il
# imprime : les sujets déjà générés sont alors refaits à l'ouverture du quiz
# (services.actualiser_sujet). Sans risque, le sujet n'est jamais scanné.
#   2 — concours : les choix des QCM sont imprimés (avant : intitulés seuls).
#   3 — présentation des sujets de concours tunisiens : cartouche à trois
#       cases, page de garde « Remarques / Consignes », pagination n/N.
SUJET_VERSION = 3

# Textes du sujet. Les consignes sont écrites d'après ce que QuizScan lit
# réellement : cases à noircir (pas de croix), étiquette QR ou grille de n°.
SUJET_TEXTES = {
    "fr": {
        "epreuve": "Épreuve à choix multiples",
        "epreuve_mixte": "Questions à choix multiples et rédigées",
        "duree": "Durée : {d}",
        "etiquette": "Collez ici l'étiquette portant votre nom",
        "nom": "Nom et prénom :",
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
                       "l'emplacement réservé de la feuille de réponses.",
        "c_grille": "Écrivez votre numéro d'inscription sur la feuille de "
                    "réponses et noircissez les chiffres correspondants dans "
                    "la grille.",
        "c_noircir": "Pour chaque question, noircissez complètement une seule "
                     "case sur la feuille de réponses.",
        "c_brouillon": "Une seule feuille de réponses est remise par candidat : "
                       "préparez vos réponses sur ce sujet avant de les reporter.",
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
        "epreuve": "اختبار في الأسئلة متعددة الاختيارات",
        "epreuve_mixte": "أسئلة متعددة الاختيارات وأسئلة تحريرية",
        "duree": "المدة : {d}",
        "etiquette": "ألصق هنا اللاصقة الحاملة للاسم واللقب",
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
                       "على ورقة الإجابة.",
        "c_grille": "يُكتب رقم التسجيل على ورقة الإجابة وتُملأ الأرقام المطابقة "
                    "له في الشبكة.",
        "c_noircir": "تُملأ كليًا خانة واحدة لكل سؤال على ورقة الإجابة.",
        "c_brouillon": "لا تُسلَّم إلا ورقة إجابة واحدة لكل مترشح، ويُستحسن "
                       "تحضير الإجابة على ورقة الأسئلة قبل نقلها إلى ورقة الإجابة.",
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


def generate_subject_pdf(quiz, questions=None):
    """PDF du SUJET (questions seules, sans cases à noircir) — à distribuer
    aux étudiants ; la grille de réponses est un document séparé (sheet_pdf).
    Aucun repère ArUco : ce document n'est pas scanné.

    Présentation des sujets de concours : cartouche à trois cases en tête de
    la première page (institutions | intitulé et durée | identification),
    puis, pour un concours, une page de garde « Remarques / Consignes » ; les
    questions suivent, chaque choix sur sa ligne, pages numérotées n/N."""
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
    concours = getattr(quiz, "auto_enroll", False)
    # Titre écrit à la main (mode étiquette hors concours) : le même sujet
    # peut servir à plusieurs épreuves, il ne porte ni titre ni classe.
    titre_a_la_main = sticker and not concours

    def fmt_pts(points):
        unit = (T["pt"] if points <= 1 else T["pts"])
        return f"({points:g} {unit})"

    # ---------------------------------------------------------------- blocs
    # Chaque question est un bloc insécable : on calcule d'abord sa hauteur
    # pour paginer, puis on dessine — la pagination « n/N » a besoin du total.
    blocs = []
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

    CARTOUCHE_H = 36.0
    debut_questions_p1 = HAUT + CARTOUCHE_H + 10.0
    pages = [[]]                       # blocs de chaque page de questions
    y = HAUT if concours else debut_questions_p1
    for bloc in blocs:
        if y + bloc[4] > BAS and pages[-1]:
            pages.append([])
            y = HAUT
        pages[-1].append(bloc)
        y += bloc[4]
    total = len(pages) + (1 if concours else 0)
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
        wcol = W / 3
        c.setStrokeGray(0.0)
        c.setLineWidth(0.8)
        c.rect(X0 * mm, (L.PAGE_H - (y0 + h)) * mm, W * mm, h * mm)
        for k in (1, 2):
            x = X0 + k * wcol
            c.line(x * mm, (L.PAGE_H - y0) * mm, x * mm, (L.PAGE_H - (y0 + h)) * mm)
        # Ordre de lecture : institutions | épreuve | identification,
        # de droite à gauche en arabe.
        cases = [X0 + 2 * wcol, X0 + wcol, X0] if rtl else [X0, X0 + wcol, X0 + 2 * wcol]
        x_inst, x_epr, x_id = cases

        def bloc_centre(lignes, x, y_haut, h_case, size=9.5, gras_premiere=False):
            textes = []
            for i, t in enumerate(lignes):
                for morceau in wrap(t, size, wcol - 6.0, bold=gras_premiere and i == 0):
                    textes.append((morceau, gras_premiere and i == 0))
            pas = size * 0.47
            yy = y_haut + (h_case - len(textes) * pas) / 2 + pas - 0.8
            for t, gras in textes:
                draw(t, x + wcol / 2, yy, size, bold=gras, align="center")
                yy += pas

        # 1. institutions (en-tête saisi ; à défaut, le nom du groupe)
        inst = [t.strip() for t in (quiz.entete or "").splitlines() if t.strip()]
        if not inst and not concours and not titre_a_la_main:
            inst = [quiz.class_group.name]
        if inst:
            bloc_centre(inst, x_inst, y0, h, size=9, gras_premiere=True)

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
            bloc_centre([quiz.title], x_epr, y0, moitie, size=11, gras_premiere=True)
        bas = [S["epreuve"] if all(q.qtype == "qcm" for q in qs)
               else S["epreuve_mixte"]]
        if quiz.duree:
            bas.append(S["duree"].format(d=quiz.duree))
        bloc_centre(bas, x_epr, y0 + moitie, moitie, size=9, gras_premiere=True)

        # 3. identification
        if concours or sticker:
            bloc_centre([S["etiquette"]], x_id, y0, h, size=9.5, gras_premiere=True)
        else:
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
        tous_qcm = all(q.qtype == "qcm" for q in qs)
        remarques = [S["r_pages"].format(p=_nb_pages(len(pages), lang), n=n_q),
                     S["r_nombre"].format(n=n_q),
                     S["r_unique"] if tous_qcm else S["r_unique_qcm"]]
        if quiz.wrong_penalty:
            remarques.append(S["r_penalite"].format(x=f"{quiz.wrong_penalty:g}"))
        consignes = []
        if quiz.id_mode == "grid":
            consignes.append(S["c_grille"])
        else:
            consignes.append(S["c_etiquette"])
        consignes += [S["c_noircir"], S["c_brouillon"], S["c_stylo"],
                      S["c_blanco"], S["c_plier"], S["c_rendre"]]
        y = debut_questions_p1 - 4.0
        y = liste_numerotee(S["remarques"], remarques, y)
        y = liste_numerotee(S["consignes"], consignes, y + 6.0)

    # --------------------------------------------------------------- dessin
    entete_page()
    cartouche()
    if concours:
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
                ligne(t, y, 11.5, bold=True)
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
    return buf.getvalue()
