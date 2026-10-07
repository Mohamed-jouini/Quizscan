"""Polices, mise en forme du texte arabe et mesure/retour à la ligne.

Partagé entre layout.py (calcul des positions) et sheet_pdf.py (dessin),
pour que la disposition enregistrée corresponde exactement au PDF imprimé.
"""
import re
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).resolve().parent / "fonts"
AR_FONT, AR_FONT_BOLD = "NotoNaskh", "NotoNaskh-Bold"
_loaded = False

_AR_RE = re.compile(r"[؀-ۿ]")
# Blocs arabes, y compris les formes de présentation produites par le reshaper
_AR_CHAR = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def load_fonts():
    global _loaded
    if _loaded:
        return
    pdfmetrics.registerFont(TTFont(AR_FONT, str(FONT_DIR / "NotoNaskhArabic-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(AR_FONT_BOLD, str(FONT_DIR / "NotoNaskhArabic-Bold.ttf")))
    _loaded = True


def has_arabic(text):
    return bool(text and _AR_RE.search(text))


def shape(text):
    """Chaîne arabe -> ligatures + ordre visuel droite->gauche."""
    if not has_arabic(text):
        return text
    return get_display(arabic_reshaper.reshape(text))


def font_for(text, bold=False):
    if has_arabic(text):
        return AR_FONT_BOLD if bold else AR_FONT
    return "Helvetica-Bold" if bold else "Helvetica"


def runs(visual_text, bold):
    """Découpe une chaîne (en ordre visuel) en segments (texte, police) :
    Noto Naskh Arabic n'a ni lettres latines ni ( ) / -, Helvetica prend
    le relais pour ces caractères."""
    out = []
    for ch in visual_text:
        font = (AR_FONT_BOLD if bold else AR_FONT) if _AR_CHAR.match(ch) \
            else ("Helvetica-Bold" if bold else "Helvetica")
        if out and out[-1][1] == font:
            out[-1][0] += ch
        else:
            out.append([ch, font])
    return out


def text_width_pt(logical_text, size, bold=False):
    """Largeur en points d'une chaîne logique (arabe compris)."""
    load_fonts()
    visual = shape(logical_text)
    return sum(pdfmetrics.stringWidth(t, f, size) for t, f in runs(visual, bold))


PT_PER_MM = 72.0 / 25.4


def wrap(logical_text, size, max_width_mm, bold=False):
    """Retour à la ligne par mots pour tenir dans max_width_mm.
    Retourne la liste des lignes (texte logique)."""
    max_pt = max_width_mm * PT_PER_MM
    words = logical_text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and text_width_pt(candidate, size, bold) > max_pt:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]
