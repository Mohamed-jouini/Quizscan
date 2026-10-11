"""Bulletins PDF individuels : une page par étudiant avec la note et le
détail question par question. Gère les noms et textes en arabe."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas

from .fonts import font_for, has_arabic, load_fonts, runs, shape

PAGE_W, PAGE_H = 210.0, 297.0

# Marques juste / faux / sans réponse du tableau de détail.
# Helvetica (police PDF standard) ne possède pas ✓ ✗ ✱ : ReportLab les
# remplacerait silencieusement par des caractères de contrôle ou une
# virgule. On utilise ZapfDingbats, également intégrée au format PDF, qui
# possède une vraie coche et une vraie croix.
DINGBATS = "ZapfDingbats"
MARK_OK = "✔"        # ✔ dans ZapfDingbats
MARK_WRONG = "✘"     # ✘ dans ZapfDingbats
MARK_BLANK = "–"     # – (tiret), tracé en Helvetica

# Réponse à plusieurs cases cochées : « ✱ » n'existe pas en Helvetica
MULTI_TEXT = "multi"


def _y(y_mm):
    return (PAGE_H - y_mm) * mm


def _draw(c, text, x_mm_, y_mm_, size, bold=False, align="left", gray=None):
    text = str(text)
    if gray is not None:
        c.setFillGray(gray)
    if has_arabic(text):
        visual = shape(text)
        parts = runs(visual, bold)
        widths = [pdfmetrics.stringWidth(t, f, size) for t, f in parts]
        total = sum(widths)
        cur = x_mm_ * mm - (total / 2 if align == "center" else total if align == "right" else 0)
        for (t, f), w in zip(parts, widths):
            c.setFont(f, size)
            c.drawString(cur, _y(y_mm_), t)
            cur += w
    else:
        c.setFont(font_for(text, bold), size)
        if align == "center":
            c.drawCentredString(x_mm_ * mm, _y(y_mm_), text)
        elif align == "right":
            c.drawRightString(x_mm_ * mm, _y(y_mm_), text)
        else:
            c.drawString(x_mm_ * mm, _y(y_mm_), text)
    if gray is not None:
        c.setFillGray(0.0)


def _ligne_resultat(quiz, r, admission):
    """« ADMIS — rang 2 sur 6 », ou None sans note minimale réglée."""
    if not admission or not r.get("resultat"):
        return None, None
    concours = quiz.auto_enroll
    libelle = {"admis": "ADMIS" if concours else "RÉUSSI",
               "refuse": "NON ADMIS" if concours else "NON RÉUSSI",
               "en_attente": "EN ATTENTE — réponses manuscrites à noter"}[r["resultat"]]
    texte = f"{libelle} — rang {r['rang']} sur {admission['n']}"
    couleur = {"admis": (0.1, 0.5, 0.2), "refuse": (0.75, 0.15, 0.1),
               "en_attente": (0.6, 0.35, 0.0)}[r["resultat"]]
    return texte, couleur


def generate_bulletins_pdf(quiz, rows, batch=None, admission=None):
    """Un PDF avec une page de relevé par étudiant (rows = compute_results).

    admission (services.admissions) : avec une note minimale réglée, chaque
    relevé porte le résultat et le rang du candidat."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    X0, X1 = 22.0, 188.0

    for r in rows:
        s = r["student"]
        # En-tête
        _draw(c, "Relevé de notes — QuizScan", PAGE_W / 2, 18.0, 9,
              align="center", gray=0.45)
        _draw(c, quiz.title, PAGE_W / 2, 28.0, 15, bold=True, align="center")
        _draw(c, quiz.class_group.name, PAGE_W / 2, 34.5, 10, align="center", gray=0.3)
        c.setLineWidth(0.5)
        c.line(X0 * mm, _y(38.5), X1 * mm, _y(38.5))

        _draw(c, s.full_name, X0, 48.0, 13, bold=True)
        if s.student_number:
            _draw(c, f"N° {s.student_number}", X1, 48.0, 10, align="right", gray=0.3)

        # Note globale
        _draw(c, f"{r['score']:g} / {r['max_score']:g}", X0, 62.0, 22, bold=True)
        _draw(c, "NOTE", X0, 67.5, 8, gray=0.45)
        resume = (f"{r['answered']} répondues — {r['correct']} correctes — "
                  f"{r['wrong']} fausses — {r['blank']} sans réponse")
        if r["pending_open"]:
            resume += f" — {r['pending_open']} manuscrite(s) non corrigée(s)"
        _draw(c, resume, X1, 62.0, 9.5, align="right", gray=0.25)
        texte, couleur = _ligne_resultat(quiz, r, admission)
        if texte:
            c.setFillColorRGB(*couleur)
            _draw(c, texte, X1, 68.0, 10.5, bold=True, align="right")
            c.setFillGray(0.0)
            _draw(c, f"note minimale {admission['seuil']:g} / {r['max_score']:g}",
                  X1, 72.5, 8, align="right", gray=0.45)

        # Tableau du détail
        y = 78.0
        headers = ["Q", "Type", "Réponse", "Attendue", "Points"]
        cols = [X0, X0 + 14, X0 + 52, X0 + 92, X0 + 128]
        for h, x in zip(headers, cols):
            _draw(c, h, x, y, 8.5, bold=True, gray=0.35)
        y += 2.0
        c.setLineWidth(0.4)
        c.line(X0 * mm, _y(y), X1 * mm, _y(y))
        y += 5.5

        for q in quiz.questions.order_by("order"):
            a = r["answers"].get(q.order)
            if y > 280.0:
                c.showPage()
                y = 25.0
            _draw(c, str(q.order), cols[0], y, 9.5, bold=True)
            if q.qtype == "qcm":
                _draw(c, "QCM", cols[1], y, 9)
                if a is None:
                    detected = "—"
                    pts = "—"
                    color_ok = None
                else:
                    detected = (MULTI_TEXT if a.is_multiple
                                else a.detected_letter)
                    pts = f"{(a.points_awarded or 0):g} / {q.points:g}"
                    color_ok = a.is_correct
                _draw(c, detected, cols[2], y, 10, bold=True)
                _draw(c, q.correct_letter, cols[3], y, 10)
                _draw(c, pts, cols[4], y, 9.5)
                if a is not None:
                    if color_ok:
                        mark, font = MARK_OK, DINGBATS
                    elif a.is_blank:
                        mark, font = MARK_BLANK, "Helvetica-Bold"
                    else:
                        mark, font = MARK_WRONG, DINGBATS
                    c.setFillColorRGB(*(0.1, 0.55, 0.2) if color_ok
                                      else (0.55, 0.55, 0.55) if a.is_blank
                                      else (0.8, 0.15, 0.1))
                    c.setFont(font, 11)
                    c.drawString((cols[4] + 24) * mm, _y(y), mark)
                    c.setFillGray(0.0)
            else:
                _draw(c, "Manuscrite", cols[1], y, 9)
                if a is None or a.points_awarded is None:
                    _draw(c, "non corrigée", cols[2], y, 9, gray=0.45)
                    _draw(c, f"— / {q.points:g}", cols[4], y, 9.5)
                else:
                    _draw(c, f"{a.points_awarded:g} / {q.points:g}", cols[4], y,
                          9.5, bold=True)
            y += 6.2

        _draw(c, f"QuizScan — {quiz.class_group.name}", PAGE_W / 2, 290.0, 7,
              align="center", gray=0.5)
        c.showPage()

    c.save()
    return buf.getvalue()


def generate_liste_admis_pdf(quiz, admission, batch=None):
    """Liste des admis par ordre de mérite, prête à afficher ou à signer.

    admission : services.admissions(quiz, rows), avec une note minimale
    réglée. En tête : l'en-tête du sujet s'il est saisi, l'intitulé, la note
    minimale et le décompte ; puis rang, nom, n° d'inscription et note, sur
    autant de pages qu'il faut (intitulés de colonnes répétés), et la mention
    « Liste arrêtée à N admis ». Rangs ex aequo identiques (1, 2, 2, 4)."""
    from django.utils import timezone

    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    X0, X1 = 22.0, 188.0
    concours = quiz.auto_enroll
    titre = ("Liste des admis" if concours
             else "Liste des étudiants ayant réussi")
    qui = "admis" if concours else "réussite(s)"
    admis = admission["admis"]
    edite = timezone.localtime().strftime("%d/%m/%Y")

    LIGNE, BAS = 7.2, 272.0
    cols = [X0 + 2, X0 + 20, X0 + 112]          # rang, nom, n° ; note à droite

    def entete_tableau(y):
        c.setFillGray(0.93)
        c.rect(X0 * mm, _y(y + 2.4), (X1 - X0) * mm, 7.6 * mm, stroke=0, fill=1)
        c.setFillGray(0.0)
        for texte, x in zip(("Rang", "Nom et prénom", "N° d'inscription"), cols):
            _draw(c, texte, x, y, 9, bold=True, gray=0.25)
        _draw(c, "Note", X1 - 2, y, 9, bold=True, align="right", gray=0.25)
        return y + 8.5

    # En-tête de la première page : quelques lignes de plus que les suivantes.
    lignes_entete = [t.strip() for t in (quiz.entete or "").splitlines() if t.strip()]
    debut_p1 = 62.0 + 4.2 * len(lignes_entete)
    par_page_1 = max(int((BAS - debut_p1 - 8.5) // LIGNE), 1)
    par_page = int((BAS - 30.0 - 8.5) // LIGNE)
    reste = max(len(admis) - par_page_1, 0)
    total_pages = 1 + (-(-reste // par_page) if reste else 0)

    def pied(page):
        _draw(c, f"{quiz.title} — {titre.lower()} — éditée le {edite}",
              X0, 287.0, 7.5, gray=0.45)
        _draw(c, f"page {page} / {total_pages}", X1, 287.0, 7.5,
              align="right", gray=0.45)

    # ---- première page
    y = 18.0
    for ligne in lignes_entete:
        _draw(c, ligne, PAGE_W / 2, y, 9, align="center", gray=0.3)
        y += 4.2
    y += 4.0
    _draw(c, titre, PAGE_W / 2, y + 4.0, 17, bold=True, align="center")
    _draw(c, quiz.title, PAGE_W / 2, y + 11.5, 12, align="center")
    sous_titre = quiz.class_group.name
    if batch is not None:
        sous_titre += f" — lot {batch}"
    _draw(c, sous_titre, PAGE_W / 2, y + 17.0, 9.5, align="center", gray=0.35)
    resume = (f"Note minimale {'d’admission' if concours else 'de réussite'} : "
              f"{admission['seuil']:g} / {admission['max_score'] or 0:g}   —   "
              f"{admission['n_admis']} {qui} sur {admission['n']} "
              f"{'candidats notés' if concours else 'étudiants notés'} "
              f"({admission['pct']} %)")
    _draw(c, resume, PAGE_W / 2, y + 24.0, 9.5, align="center", gray=0.2)
    c.setLineWidth(0.5)
    c.line(X0 * mm, _y(y + 28.0), X1 * mm, _y(y + 28.0))
    y = entete_tableau(debut_p1)
    page, sur_la_page = 1, 0
    capacite = par_page_1

    for i, r in enumerate(admis):
        if sur_la_page == capacite:
            pied(page)
            c.showPage()
            page += 1
            _draw(c, f"{titre} — {quiz.title} (suite)", X0, 18.0, 10, bold=True)
            y = entete_tableau(30.0)
            sur_la_page, capacite = 0, par_page
        if i % 2:
            c.setFillGray(0.97)
            c.rect(X0 * mm, _y(y + 2.2), (X1 - X0) * mm, LIGNE * mm, stroke=0, fill=1)
            c.setFillGray(0.0)
        s = r["student"]
        _draw(c, str(r["rang"]), cols[0], y, 10, bold=True)
        _draw(c, s.full_name, cols[1], y, 10)
        _draw(c, s.student_number or "—", cols[2], y, 10)
        _draw(c, f"{r['score']:g} / {r['max_score']:g}", X1 - 2, y, 10,
              bold=True, align="right")
        y += LIGNE
        sur_la_page += 1

    if not admis:
        _draw(c, f"Aucun {'candidat admis' if concours else 'étudiant n’atteint la note minimale'}.",
              X0 + 2, y + 2, 10, gray=0.35)
        y += LIGNE
    c.setLineWidth(0.4)
    c.line(X0 * mm, _y(y - 3.0), X1 * mm, _y(y - 3.0))
    if y + 8 <= BAS:
        _draw(c, f"Liste arrêtée à {admission['n_admis']} {qui}.", X0, y + 4.0,
              10, bold=True)
    pied(page)
    c.showPage()
    c.save()
    return buf.getvalue()
