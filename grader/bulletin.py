"""Bulletins PDF individuels : une page par étudiant avec la note et le
détail question par question. Gère les noms et textes en arabe."""
import io
import itertools
import re

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as rl_canvas

from .fonts import font_for, has_arabic, load_fonts, runs, shape, text_width_pt, wrap
from .labels_pdf import _ellipsize
from .models import nom_et_prenom

PAGE_W, PAGE_H = 210.0, 297.0


def nom_ajuste(etudiant, largeur_mm, taille, gras=False):
    """(texte, corps) du nom complet qui tient dans la largeur, sans déborder sur la
    colonne voisine. Le corps diminue jusqu'à 6,5 pt ; au-delà, les prénoms passent à leurs
    initiales et le nom de famille reste entier (le prénom venant d'abord depuis le
    7 novembre 2026, c'est lui qui aurait débordé), quitte à descendre à 5 pt ; en
    dernier recours, le texte est coupé « … »."""
    largeur = largeur_mm * mm
    initiales = " ".join(f"{mot[0]}." for mot in (etudiant.first_name or "").split())
    for texte, plancher in ((etudiant.full_name, 6.5),
                            (nom_et_prenom(etudiant.last_name, initiales), 5.0)):
        corps = taille
        while corps > plancher and text_width_pt(texte, corps, gras) > largeur:
            corps -= 0.5
        if text_width_pt(texte, corps, gras) <= largeur:
            return texte, corps
    return _ellipsize(texte, 5.0, largeur_mm, gras), 5.0

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
    attente = ("EN ATTENTE — page de la copie à identifier" if r["questions_manquantes"]
               else "EN ATTENTE — réponses manuscrites à noter")
    libelle = {"admis": "ADMIS" if concours else "RÉUSSI",
               "refuse": "NON ADMIS" if concours else "NON RÉUSSI",
               "en_attente": attente}[r["resultat"]]
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

        nom, corps = nom_ajuste(s, X1 - X0 - 32, 13, gras=True)     # N° à droite
        _draw(c, nom, X0, 48.0, corps, bold=True)
        if s.student_number:
            _draw(c, f"N° {s.student_number}", X1, 48.0, 10, align="right", gray=0.3)

        # Résultat : bonnes réponses (une QCM = 1 point) et note sur 20 (QCM, demande du
        # 13 novembre 2026), ou pourcentage (écrit, oral)
        if quiz.a_des_qcm:
            _draw(c, f"{r['correct']} / {r['n_qcm']}", X0, 62.0, 22, bold=True)
            _draw(c, "BONNES RÉPONSES", X0, 67.5, 8, gray=0.45)
        else:                       # écrit ou oral : la note elle-même
            _draw(c, f"{r['score']:g} / {r['max_score']:g}", X0, 62.0, 22, bold=True)
            _draw(c, "NOTE", X0, 67.5, 8, gray=0.45)
        if quiz.est_qcm:
            _draw(c, f"{_nombre(r['note20'])} / 20", X0 + 46, 62.0, 22, bold=True)
            _draw(c, "NOTE SUR 20", X0 + 46, 67.5, 8, gray=0.45)
        else:
            _draw(c, f"{r['pct']} %", X0 + 46, 62.0, 22, bold=True)
            _draw(c, "POURCENTAGE", X0 + 46, 67.5, 8, gray=0.45)
        resume = f"{r['wrong']} fausse(s) — {r['blank']} sans réponse"
        if not quiz.a_des_qcm:
            resume = {"ecrit": "Épreuve écrite", "oral": "Épreuve orale"}.get(
                quiz.type_epreuve, "")
        elif quiz.note_distincte:
            resume = (f"{'points' if quiz.est_qcm else 'note'} {r['score']:g} / "
                      f"{r['max_score']:g} — " + resume)
        if r["pending_open"]:
            resume += f" — {r['pending_open']} manuscrite(s) non corrigée(s)"
        if r["questions_manquantes"]:
            resume += f" — {r['questions_manquantes']} question(s) non lue(s)"
        _draw(c, resume, X1, 62.0, 9.5, align="right", gray=0.25)
        texte, couleur = _ligne_resultat(quiz, r, admission)
        if texte:
            c.setFillColorRGB(*couleur)
            _draw(c, texte, X1, 68.0, 10.5, bold=True, align="right")
            c.setFillGray(0.0)
            _draw(c, f"note minimale {admission['seuil']:g} / {r['max_score']:g}",
                  X1, 72.5, 8, align="right", gray=0.45)

        # Tableau du détail (pas pour un écrit ou un oral : une seule note)
        if not quiz.a_des_qcm:
            _draw(c, f"QuizScan — {quiz.class_group.name}", PAGE_W / 2, 290.0, 7,
                  align="center", gray=0.5)
            c.showPage()
            continue
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
    cols = [X0 + 2, X0 + 20, X0 + 96]           # rang, nom, n° ; résultat à droite

    def entete_tableau(y):
        c.setFillGray(0.93)
        c.rect(X0 * mm, _y(y + 2.4), (X1 - X0) * mm, 7.6 * mm, stroke=0, fill=1)
        c.setFillGray(0.0)
        for texte, x in zip(("Rang", "Prénom et nom", "N° d'inscription"), cols):
            _draw(c, texte, x, y, 9, bold=True, gray=0.25)
        _draw(c, "Bonnes rép." if quiz.a_des_qcm else "Note", X1 - 24, y, 9,
              bold=True, align="right", gray=0.25)
        _draw(c, "Note / 20" if quiz.est_qcm else "%", X1 - 2, y, 9, bold=True,
              align="right", gray=0.25)
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
    sur20 = f" ({_nombre(admission['seuil20'])} / 20)" if quiz.est_qcm else ""
    resume = (f"Note minimale {'d’admission' if concours else 'de réussite'} : "
              f"{admission['seuil']:g} / {admission['max_score'] or 0:g}{sur20}   —   "
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
        nom, corps = nom_ajuste(s, cols[2] - cols[1] - 3, 10)
        _draw(c, nom, cols[1], y, corps)
        _draw(c, s.student_number or "—", cols[2], y, 10)
        _draw(c, (f"{r['correct']} / {r['n_qcm']}" if quiz.a_des_qcm
                  else f"{r['score']:g} / {r['max_score']:g}"), X1 - 24, y, 10,
              bold=True, align="right")
        _draw(c, _nombre(r["note20"]) if quiz.est_qcm else f"{r['pct']} %", X1 - 2, y, 10,
              align="right")
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


def _nombre(valeur):
    """14.5 -> « 14,50 », 5.0 -> « 5 » : comme à l'écran."""
    valeur = float(valeur)
    return f"{valeur:g}" if valeur.is_integer() else f"{valeur:.2f}".replace(".", ",")


def generate_parcours_pdf(parcours):
    """Résultats de tout un concours (services.parcours_concours), à imprimer ou à signer :
    chaque candidat, étape par étape, et son résultat final, sur autant de pages qu'il faut
    (intitulés de colonnes répétés). Mêmes candidats et même ordre que la page, sans sa
    recherche, comme l'export Excel."""
    from django.utils import timezone

    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    X0, X1 = 14.0, 196.0
    LIGNE, BAS = 10.6, 277.0
    L_NOM, L_FINAL = 54.0, 38.0
    etapes, candidats = parcours["etapes"], parcours["candidats"]
    racine = parcours["epreuves"][0]
    edite = timezone.localtime().strftime("%d/%m/%Y")
    largeur_etape = (X1 - X0 - L_NOM - L_FINAL) / len(etapes)
    x_etapes = [X0 + L_NOM + k * largeur_etape for k in range(len(etapes))]
    x_final = X1 - L_FINAL + 1
    verdicts = {"admis": "Admis", "refuse": "Non admis", "en_attente": "En attente"}

    def taille_pour(texte, taille, largeur, gras=False):
        """Un nom ou un titre trop long s'écrit plus petit plutôt que de déborder."""
        while taille > 6.5 and text_width_pt(texte, taille, gras) > largeur * mm:
            taille -= 0.5
        return taille

    def tete_tableau(y):
        c.setFillGray(0.93)
        c.rect(X0 * mm, _y(y + 2.4), (X1 - X0) * mm, 7.6 * mm, stroke=0, fill=1)
        c.setFillGray(0.0)
        _draw(c, "Candidat", X0 + 2, y, 9, bold=True, gray=0.25)
        for etape, x in zip(etapes, x_etapes):
            _draw(c, etape["quiz"].type_court, x, y, 9, bold=True, gray=0.25)
        _draw(c, "Résultat final", x_final, y, 9, bold=True, gray=0.25)
        return y + 8.5

    lignes_entete = [t.strip() for t in (racine.entete or "").splitlines() if t.strip()]
    haut_titre = 22.0 + 4.2 * len(lignes_entete)
    debut_p1 = haut_titre + 22.5 + 4.2 * len(etapes) + 4.5
    debut_suite = 26.0
    par_page_1 = max(int((BAS - debut_p1 - 8.5) // LIGNE), 1)
    par_page = int((BAS - debut_suite - 8.5) // LIGNE)
    reste = max(len(candidats) - par_page_1, 0)
    total_pages = 1 + (-(-reste // par_page) if reste else 0)

    def pied(page):
        _draw(c, f"{racine.title} — résultats du concours — édités le {edite}",
              X0, 287.0, 7.5, gray=0.45)
        _draw(c, f"page {page} / {total_pages}", X1, 287.0, 7.5, align="right", gray=0.45)

    # ---- première page
    y = 18.0
    for ligne in lignes_entete:
        _draw(c, ligne, PAGE_W / 2, y, 9, align="center", gray=0.3)
        y += 4.2
    _draw(c, "Résultats du concours", PAGE_W / 2, haut_titre + 4.0, 17, bold=True, align="center")
    _draw(c, racine.title, PAGE_W / 2, haut_titre + 11.5,
          taille_pour(racine.title, 12, X1 - X0), align="center")
    n = len(candidats)
    _draw(c, f"{parcours['n_admis_final']} admis au concours sur {n} candidat{'s' if n > 1 else ''}",
          PAGE_W / 2, haut_titre + 17.5, 9.5, bold=True, align="center")
    y = haut_titre + 22.5
    for etape in etapes:
        conv, notes, seuil = etape["convoques"], etape["presents"], etape["seuil"]
        _draw(c, f"{etape['quiz'].type_court} : {conv} convoqué{'s' if conv > 1 else ''} · "
                 f"{notes} noté{'s' if notes > 1 else ''} · "
                 + (f"{etape['admis']} admis (note minimale {_nombre(seuil)})"
                    if seuil is not None else "note minimale à fixer"),
              PAGE_W / 2, y, 8.5, align="center", gray=0.3)
        y += 4.2
    c.setLineWidth(0.5)
    c.line(X0 * mm, _y(debut_p1 - 4.5), X1 * mm, _y(debut_p1 - 4.5))
    y = tete_tableau(debut_p1)
    page, sur_la_page, capacite = 1, 0, par_page_1

    for i, candidat in enumerate(candidats):
        if sur_la_page == capacite:
            pied(page)
            c.showPage()
            page += 1
            _draw(c, f"Résultats du concours — {racine.title} (suite)", X0, 18.0,
                  taille_pour(f"Résultats du concours — {racine.title} (suite)", 10, X1 - X0, True),
                  bold=True)
            y = tete_tableau(debut_suite)
            sur_la_page, capacite = 0, par_page
        if i % 2:
            c.setFillGray(0.97)
            c.rect(X0 * mm, _y(y + LIGNE - 3.6), (X1 - X0) * mm, LIGNE * mm, stroke=0, fill=1)
            c.setFillGray(0.0)
        s = candidat["etudiant"]
        nom, corps = nom_ajuste(s, L_NOM - 4, 9.5, gras=True)
        _draw(c, nom, X0 + 2, y, corps, bold=True)
        _draw(c, f"n° {s.student_number}" if s.student_number else "sans n°", X0 + 2, y + 4.2, 8,
              gray=0.35)
        for case, x in zip(candidat["cases"], x_etapes):
            if case["etat"] == "note":
                row = case["row"]
                _draw(c, f"{_nombre(row['score'])} / {_nombre(row['max_score'])}", x, y, 9.5, bold=True)
                verdict = verdicts.get(case["resultat"])
                part = (f"{_nombre(row['note20'])} / 20" if row.get("est_qcm")
                        else f"{row['pct']} %")
                _draw(c, part + (f" · {verdict}" if verdict else ""), x, y + 4.2, 8, gray=0.3)
            elif case["etat"] == "non_note":
                _draw(c, "Non noté", x, y, 8.5, gray=0.3)
            else:
                _draw(c, "non convoqué", x, y, 8, gray=0.5)
        for k, morceau in enumerate(wrap(candidat["final"], 8.5, L_FINAL - 2, bold=True)[:2]):
            _draw(c, morceau, x_final, y + k * 4.2, 8.5, bold=True)
        y += LIGNE
        sur_la_page += 1

    if not candidats:
        _draw(c, "Aucun candidat.", X0 + 2, y + 2, 10, gray=0.35)
        y += LIGNE
    c.setLineWidth(0.4)
    c.line(X0 * mm, _y(y - 3.0), X1 * mm, _y(y - 3.0))
    pied(page)
    c.showPage()
    c.save()
    return buf.getvalue()


def _intitule_salle(salle):
    """« Salle : 15 » ; une salle déjà nommée (« قاعة عدد 15 », « Salle B ») telle quelle ;
    sans salle au registre : à remplir à la main."""
    if not salle:
        return "Salle : ………………"
    return salle if re.match(r"\s*(salle|قاعة|القاعة)", salle, re.I) else f"Salle : {salle}"


def generate_liste_emargement(quiz, lignes):
    """Listes d'émargement, une par salle (demande de l'utilisateur, 8 novembre 2026).

    lignes : [(n° de place ou None, étudiant)] dans l'ordre des salles et des copies
    (services.par_salle). Chaque salle commence sur une nouvelle page, sa salle imprimée en
    tête, ses pages numérotées, le décompte des présents à la fin ; les étudiants sans
    salle au registre viennent en dernier. Copies anonymes à n° de place : colonne
    « Place », le surveillant y lit qui s'assoit où (la copie ne porte que le n° de place
    et un QR). Chaque candidat signe en face de son nom, deux fois : « Signature entrée »
    et « Signature sortie » (demande de l'utilisateur, 13 novembre 2026)."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    avec_place = any(place is not None for place, _ in lignes)
    groupes = [list(paquet) for _, paquet in
               itertools.groupby(lignes, key=lambda ligne: ligne[1].salle.casefold())]
    for paquet in groupes or [[]]:
        _liste_d_une_salle(c, quiz, paquet, paquet[0][1].salle if paquet else "", avec_place)
    c.save()
    return buf.getvalue()


def _liste_d_une_salle(c, quiz, lignes, salle, avec_place):
    """Pages de la liste d'une salle : en-tête, tableau, décompte, pieds numérotés."""
    from django.utils import timezone

    X0, X1 = 18.0, 192.0
    LIGNE, BAS = 9.0, 270.0
    # place, n° d'inscription, nom ; sans place, le n° et le nom passent à gauche
    cols = [X0 + 3, X0 + 16, X0 + 43] if avec_place else [None, X0 + 3, X0 + 33]
    # deux cases de signature, à l'entrée puis à la sortie (13 novembre 2026)
    L_SIG = 32.0
    x_sortie = X1 - 1 - L_SIG
    x_entree = x_sortie - 2 - L_SIG
    signatures = (("Signature entrée", x_entree), ("Signature sortie", x_sortie))
    x_cin = x_entree - 23.0                   # n° de CIN du registre (8 novembre 2026)
    # « Liste des candidats » (demande de l'utilisateur, 8 novembre 2026), des étudiants
    # pour une classe ; avec les places des copies anonymes
    qui = "candidats" if quiz.auto_enroll else "étudiants"
    titre = f"Liste des {qui}" + (" et des places" if avec_place else "")
    entete = [t.strip() for t in (quiz.entete or "").splitlines() if t.strip()]
    edite = timezone.localtime().strftime("%d/%m/%Y")
    debut_p1 = 70.0 + 4.2 * len(entete)
    par_p1 = max(int((BAS - debut_p1 - 9) // LIGNE), 1)
    par_page = int((BAS - 34.0 - 9) // LIGNE)
    reste = max(len(lignes) - par_p1, 0)
    total = 1 + (-(-reste // par_page) if reste else 0)
    intitule = _intitule_salle(salle)

    def tete_tableau(y):
        c.setFillGray(0.92)
        c.rect(X0 * mm, _y(y + 2.6), (X1 - X0) * mm, 8.2 * mm, stroke=0, fill=1)
        c.setFillGray(0.0)
        for texte, x in zip(("Place", "N° d'inscription", "Prénom et nom"), cols):
            if x is not None:
                _draw(c, texte, x, y, 9, bold=True, gray=0.2)
        _draw(c, "N° CIN", x_cin, y, 9, bold=True, gray=0.2)
        for texte, x in signatures:
            _draw(c, texte, x + L_SIG / 2, y, 9, bold=True, align="center", gray=0.2)
        return y + 9.0

    # Le titre de l'épreuve et la salle peuvent être en arabe : chacun sur sa ligne, jamais
    # dans une phrase française (l'ordre de lecture d'une ligne mêlée se brouille).
    def pied(page):
        _draw(c, quiz.title, X0, 287.0, 7.5, gray=0.45)
        _draw(c, f"liste des {qui} — éditée le {edite} — page {page} / {total}", X1, 287.0, 7.5,
              align="right", gray=0.45)

    y = 18.0
    for t in entete:
        _draw(c, t, PAGE_W / 2, y, 9, align="center", gray=0.3)
        y += 4.2
    _draw(c, titre, PAGE_W / 2, y + 6, 16, bold=True, align="center")
    _draw(c, quiz.title, PAGE_W / 2, y + 13, 12, align="center")
    _draw(c, intitule, PAGE_W / 2, y + 20.5, 13, bold=True, align="center")
    _draw(c, "Date : ……/……/…………      Surveillant : ……………………………",
          PAGE_W / 2, y + 26.5, 9.5, align="center", gray=0.25)
    consigne = ("Copies anonymes (n° de place et QR code, jamais le nom) : chaque candidat "
                "s'assoit à sa place et signe à l'entrée puis à la sortie." if avec_place else
                "Chaque candidat signe en face de son nom, à l'entrée puis à la sortie ; la "
                "liste suit l'ordre des copies.")
    _draw(c, consigne, PAGE_W / 2, y + 32, 8, align="center", gray=0.4)
    c.setLineWidth(0.5)
    c.line(X0 * mm, _y(y + 35.5), X1 * mm, _y(y + 35.5))
    y = tete_tableau(debut_p1)
    page, sur_page, capacite = 1, 0, par_p1
    for i, (place, s) in enumerate(lignes):
        if sur_page == capacite:
            pied(page)
            c.showPage()
            page += 1
            _draw(c, f"{titre} (suite)", X0, 20.0, 10, bold=True)
            _draw(c, intitule, X1, 20.0, 10, bold=True, align="right")
            y = tete_tableau(32.0)
            sur_page, capacite = 0, par_page
        if i % 2:
            c.setFillGray(0.965)
            c.rect(X0 * mm, _y(y + 3.4), (X1 - X0) * mm, LIGNE * mm, stroke=0, fill=1)
            c.setFillGray(0.0)
        if avec_place:
            _draw(c, str(place), cols[0] + 5, y + 0.6, 12, bold=True, align="center")
        _draw(c, s.student_number or "—", cols[1], y + 0.6, 10)
        _draw(c, s.cin or "—", x_cin, y + 0.6, 10)
        nom, corps = nom_ajuste(s, x_cin - cols[2] - 3, 10.5, gras=True)
        _draw(c, nom, cols[2], y + 0.6, corps, bold=True)
        c.setStrokeGray(0.55)
        c.setLineWidth(0.5)
        for _, x in signatures:
            c.rect(x * mm, _y(y + 2.8), L_SIG * mm, (LIGNE - 1.6) * mm)
        c.setStrokeGray(0.0)
        y += LIGNE
        sur_page += 1
    y += 8
    _draw(c, f"Présents : ……… / {len(lignes)}      Absents : ………", X0, y, 10, bold=True)
    _draw(c, "Signature du surveillant :", X1 - 70, y, 10, bold=True)
    pied(page)
    c.showPage()
