"""Planche d'étiquettes autocollantes des candidats d'un concours.

Chaque vignette porte, de gauche à droite : le n° d'inscription, le NOM et le
prénom du candidat, puis son QR code (format « QS|<groupe>|<id> », lu par le
scanner). Le bloc nom/numéro sert à savoir à qui remettre quelle étiquette ;
**seul le QR, entouré de pointillés, est découpé et collé** par le candidat
dans l'emplacement réservé de sa feuille de réponses.

À imprimer sur papier autocollant A4. Disposition : 2 colonnes × 8 lignes,
soit 16 candidats par page (voir les constantes STICKER_* de layout.py, qui
dimensionnent aussi l'emplacement réservé sur la copie).
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas
from PIL import Image
from reportlab.lib.utils import ImageReader

from . import layout as L
from . import qr as qr_mod
from .fonts import font_for, has_arabic, load_fonts, runs, shape, text_width_pt

# Grille (mm) — définie dans layout.py, qui dimensionne aussi l'emplacement
# réservé sur la copie pour que le QR découpé y tienne.
MARGIN_X = L.STICKER_MARGIN_X
MARGIN_TOP = L.STICKER_MARGIN_Y
COLS = L.STICKER_COLS
ROWS = L.STICKER_ROWS
PAGE_W, PAGE_H = L.PAGE_W, L.PAGE_H
CELL_W = L.STICKER_W
CELL_H = L.STICKER_H
QR_MM = L.STICKER_QR_MM
QR_PAD = L.STICKER_QR_PAD
PAD = 3.0


def _draw_text(c, text, x_pt, y_pt, size, bold=False, align="left"):
    """Texte mixte arabe/latin, chaque segment avec la bonne police."""
    if not text:
        return
    if has_arabic(text):
        parts = runs(shape(text), bold)
        widths = [pdfmetrics.stringWidth(t, f, size) for t, f in parts]
        total = sum(widths)
        cur = x_pt - total if align == "right" else (
            x_pt - total / 2 if align == "center" else x_pt)
        for (t, f), w in zip(parts, widths):
            c.setFont(f, size)
            c.drawString(cur, y_pt, t)
            cur += w
    else:
        c.setFont(font_for(text, bold), size)
        if align == "center":
            c.drawCentredString(x_pt, y_pt, text)
        elif align == "right":
            c.drawRightString(x_pt, y_pt, text)
        else:
            c.drawString(x_pt, y_pt, text)


def _ellipsize(text, size, max_w_mm, bold=False):
    """Tronque le texte pour qu'il tienne dans la largeur donnée.

    Mesure via fonts.text_width_pt, qui sait mesurer l'arabe (ligatures et
    police Naskh) — un simple stringWidth se tromperait sur ces noms."""
    if not text:
        return ""
    max_pt = max_w_mm * mm
    if text_width_pt(text, size, bold) <= max_pt:
        return text
    while text and text_width_pt(text + "…", size, bold) > max_pt:
        text = text[:-1]
    return text + "…"


def generate_labels_pdf(group, students):
    """Retourne les octets du PDF de la planche d'étiquettes du concours."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    def y_top(mm_from_top):
        return (PAGE_H - mm_from_top) * mm

    per_page = COLS * ROWS
    pages = max(1, -(-len(students) // per_page))   # plafond

    def entete(page_no):
        """Marge haute : de quelle liste il s'agit et où l'on en est.
        La consigne de découpe va en pied de page — sur une seule ligne, un
        nom de concours un peu long chevaucherait le numéro de page."""
        c.setFillGray(0.45)
        titre = _ellipsize(f"{group.name} — étiquettes des candidats", 8,
                           PAGE_W - 2 * MARGIN_X - 28, bold=True)
        _draw_text(c, titre, MARGIN_X * mm, y_top(6.5), 8, bold=True)
        _draw_text(c, f"page {page_no} / {pages}",
                   (PAGE_W - MARGIN_X) * mm, y_top(6.5), 7.5, align="right")
        _draw_text(c, "Découpez chaque QR le long des pointillés et remettez-le au "
                      "candidat dont le nom figure à côté : il le colle lui-même "
                      "dans l'emplacement réservé de sa feuille de réponses.",
                   (PAGE_W / 2) * mm, y_top(PAGE_H - 5.0), 7.5, align="center")
        c.setFillGray(0.0)

    entete(1)
    for i, s in enumerate(students):
        slot = i % per_page
        if i and slot == 0:
            c.showPage()
            entete(i // per_page + 1)
        col = slot % COLS
        row = slot // COLS
        x0 = MARGIN_X + col * CELL_W          # bord gauche de la cellule (mm)
        y0 = MARGIN_TOP + row * CELL_H        # bord haut de la cellule (mm)

        # cadre léger de la cellule (repère de découpe de la planche)
        c.setStrokeGray(0.82)
        c.setLineWidth(0.4)
        c.rect(x0 * mm, y_top(y0 + CELL_H), CELL_W * mm, CELL_H * mm)

        # ---- QR à droite, entouré de pointillés : c'est la partie à coller
        qr_side = QR_MM + 2 * QR_PAD
        qx = x0 + CELL_W - PAD - qr_side
        qy = y0 + (CELL_H - qr_side) / 2
        c.setStrokeGray(0.35)
        c.setLineWidth(0.7)
        c.setDash(2, 2)
        c.rect(qx * mm, y_top(qy + qr_side), qr_side * mm, qr_side * mm)
        c.setDash()
        qr_img = ImageReader(Image.open(io.BytesIO(qr_mod.student_qr_png(s))))
        c.drawImage(qr_img, (qx + QR_PAD) * mm, y_top(qy + QR_PAD + QR_MM),
                    QR_MM * mm, QR_MM * mm)

        # ---- Identité à gauche du QR : n° d'inscription, NOM, prénom
        tx = x0 + PAD
        text_w = qx - tx - 3.0
        rtl = has_arabic(f"{s.last_name} {s.first_name}")
        # en arabe, le bloc se lit de droite à gauche : on l'aligne sur le QR
        ax = (qx - 3.0) if rtl else tx
        align = "right" if rtl else "left"

        numero = f"N° {s.student_number}" if s.student_number else "N° —"
        _draw_text(c, _ellipsize(numero, 10, text_w, bold=True),
                   ax * mm, y_top(y0 + 10.0), 10, bold=True, align=align)
        _draw_text(c, _ellipsize(s.last_name.upper(), 11, text_w, bold=True),
                   ax * mm, y_top(y0 + 17.5), 11, bold=True, align=align)
        _draw_text(c, _ellipsize(s.first_name, 10.5, text_w),
                   ax * mm, y_top(y0 + 24.0), 10.5, align=align)
        c.setFillGray(0.5)
        _draw_text(c, _ellipsize(group.name, 7.5, text_w),
                   ax * mm, y_top(y0 + 30.0), 7.5, align=align)
        c.setFillGray(0.0)

    c.showPage()
    c.save()
    return buf.getvalue()
