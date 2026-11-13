"""Export Excel des résultats."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


RESULTAT = {"admis": "Admis", "en_attente": "En attente", "refuse": "Non admis"}


def _sans_formule(ligne):
    """Un nom ou un n° commençant par « = » (saisi, importé, ou lu dans un QR
    fabriqué par un candidat) deviendrait une formule active dans Excel :
    il est écrit comme texte, précédé d'une apostrophe."""
    return [f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r")
            and not _nombre(v) else v for v in ligne]


def _nombre(v):
    try:
        float(v.replace(",", "."))
        return True
    except ValueError:
        return False


def notes_orales_workbook(quiz, lignes):
    """Modèle Excel des notes d'un oral (seule voie de saisie depuis le 13 novembre 2026) :
    un candidat par ligne, dans l'ordre des salles, avec la note déjà importée s'il en a
    une. Rempli puis importé sur la page de l'épreuve (importers.notes_du_classeur).
    lignes : services.notes_orales."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Notes de l'oral"
    sur = f"{quiz.note_sur:g}"
    entetes = ["N° d'inscription", "Prénom", "Nom", f"Note / {sur}"]
    ws.append(_sans_formule([f"{quiz.title} — notes de l'oral sur {sur} : remplissez la "
                             "colonne « Note », puis importez ce fichier sur la page de "
                             "l'épreuve."]))
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(entetes))
    ws["A1"].font = Font(bold=True, size=12)
    ws.append(entetes)
    fond = PatternFill("solid", fgColor="1F4E79")
    for col in range(1, len(entetes) + 1):
        cellule = ws.cell(row=2, column=col)
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = fond
    for etudiant, _, reponse in lignes:
        note = reponse.points_awarded if reponse is not None else None
        ws.append(_sans_formule([etudiant.student_number or "", etudiant.first_name,
                                 etudiant.last_name, note]))
        ws.cell(row=ws.max_row, column=1).number_format = "@"      # zéros de tête gardés
    for i, largeur in enumerate([18, 20, 24, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = largeur
    ws.freeze_panes = "A3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def candidats_workbook(group, etudiants, avec_fiches):
    """La liste des candidats en Excel, colonnes et ordre de la page « Candidats »
    (demande du 8 novembre 2026). Les intitulés sont de ceux que l'import
    reconnaît : le fichier se réimporte tel quel, dans l'épreuve suivante ou une
    autre classe. avec_fiches : les colonnes de la fiche au registre (CIN…)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidats"
    headers = ["N° d'inscription", "Prénom", "Nom"]
    if avec_fiches:
        headers += ["CIN", "Lieu de délivrance", "Délivrée le", "Né(e) le", "Centre", "Salle"]
    ws.append([f"{group.name} — liste des candidats ({len(etudiants)})"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(headers)
    fond = PatternFill("solid", fgColor="1F4E79")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fond
        cell.alignment = Alignment(horizontal="center")
    for s in etudiants:
        ligne = [s.student_number, s.first_name, s.last_name]
        if avec_fiches:
            i = s.inscrit
            ligne += ([i.cin, i.cin_lieu, i.cin_date, i.date_naissance, i.centre, i.salle]
                      if i is not None else [None] * 6)
        ws.append(_sans_formule([v if v != "" else None for v in ligne]))
        rang = ws.max_row
        ws.cell(rang, 1).number_format = "@"              # n° et CIN : du texte, zéro
        if avec_fiches:                                   # de tête compris
            ws.cell(rang, 4).number_format = "@"
            for col in (6, 7):
                ws.cell(rang, col).number_format = "dd/mm/yyyy"
    largeurs = [16, 20, 22] + ([12, 18, 13, 13, 28, 14] if avec_fiches else [])
    for i, w in enumerate(largeurs, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def results_workbook(quiz, rows, stats=None, admission=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Résultats"

    # QCM : la note sur 20 au lieu du pourcentage (13 novembre 2026), et « Points » pour
    # les points bruts, pour ne pas lire deux colonnes « Note »
    qcm = quiz.est_qcm
    headers = ["Prénom", "Nom", "N° inscription", "Points" if qcm else "Note", "Sur",
               "Répondues", "Correctes", "Fausses", "Sans réponse",
               "Manuscrites à corriger", "Note sur 20" if qcm else "Pourcentage",
               "Questions non lues"]
    if admission:
        headers.append("Résultat")
    ws.append([f"{quiz.title} — {quiz.class_group.name}"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="1F4E79")
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center")

    for r in rows:
        s = r["student"]
        ligne = [s.first_name, s.last_name, s.student_number,
                 r["score"], r["max_score"], r["answered"], r["correct"],
                 r["wrong"], r["blank"], r["pending_open"],
                 r["note20"] if qcm else r["pct"] / 100, r["questions_manquantes"]]
        if admission:
            ligne.append(RESULTAT.get(r.get("resultat"), ""))
        ws.append(_sans_formule(ligne))
        ws.cell(row=ws.max_row, column=11).number_format = "0.00" if qcm else "0%"

    widths = [18, 22, 14, 8, 6, 11, 10, 8, 13, 20, 12, 12, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A3"

    if admission:
        # Liste des admis, par ordre de mérite : ce qui s'affiche ou se publie.
        wa = wb.create_sheet("Admis")
        sur20 = f", soit {admission['seuil20']:g} / 20" if qcm else ""
        wa.append([f"{quiz.title} — admis (note minimale "
                   f"{admission['seuil']:g} / {admission['max_score'] or 0:g}{sur20}) : "
                   f"{admission['n_admis']} sur {admission['n']}"])
        wa["A1"].font = Font(bold=True, size=12)
        entetes = ["Rang", "Prénom", "Nom", "N° inscription", "Points" if qcm else "Note",
                   "Sur", "Bonnes réponses", "Note sur 20" if qcm else "Pourcentage"]
        wa.append(entetes)
        for col in range(1, len(entetes) + 1):
            cell = wa.cell(row=2, column=col)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = head_fill
        for r in admission["admis"]:
            s = r["student"]
            wa.append([r["rang"], s.first_name, s.last_name, s.student_number,
                       r["score"], r["max_score"], r["correct"],
                       r["note20"] if qcm else r["pct"] / 100])
            wa.cell(row=wa.max_row, column=8).number_format = "0.00" if qcm else "0%"
        for i, w in enumerate([7, 18, 22, 14, 8, 6, 15, 12], start=1):
            wa.column_dimensions[get_column_letter(i)].width = w
        wa.freeze_panes = "A3"

    if stats:
        ws2 = wb.create_sheet("Statistiques")
        ws2.append(["Étudiants", stats["n"]])
        ws2.append(["Moyenne", stats["mean"]])
        ws2.append(["Médiane", stats["median"]])
        ws2.append(["Écart-type", stats["stdev"]])
        ws2.append(["Meilleure note", stats["best"]])
        ws2.append(["Note la plus basse", stats["worst"]])
        # QCM : notes sur 20 (le nombre de questions est dans la feuille « Résultats »)
        ws2.append(["Notes sur" if stats.get("sur20") else "Barème total", stats["max_score"]])
        if stats["suspectes"]:
            ws2.append(["Bonne réponse à vérifier (questions)",
                        ", ".join(str(n) for n in stats["suspectes"])])
        ws2.append([])

        entetes = ["Question", "Type", "Réussite %", "Justes", "Fausses",
                   "Sans réponse", "Copies notées", "Discrimination",
                   "Lecture", "Répartition des réponses"]
        ws2.append(entetes)
        # Position calculée et non écrite en dur : le résumé ci-dessus a déjà
        # gagné deux lignes, et en gagnera peut-être d'autres.
        ligne_entete = ws2.max_row
        for c in range(1, len(entetes) + 1):
            cell = ws2.cell(row=ligne_entete, column=c)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")

        for s in stats["questions"]:
            q = s["question"]
            indice = s.get("discrimination")
            lecture = s.get("discrimination_label", "")
            if s.get("corrige_suspect"):
                lecture = "suspecte — vérifier la bonne réponse"
            repartition = " · ".join(
                f"{c['lettre']} {c['n']}" for c in s.get("repartition", []))
            if q.qtype == "qcm":
                # une question manuscrite n'a ni juste, ni faux, ni vide :
                # on laisse les colonnes à blanc au lieu d'afficher des 0
                ws2.append([q.order, "QCM", s["success"], s["correct"],
                            s["wrong"], s["blank"], s["n"],
                            indice, lecture, repartition])
            else:
                ws2.append([q.order, "Manuscrite", s["success"], None, None,
                            None, s["graded"], None, "", ""])
        for i, w in enumerate([18, 12, 12, 9, 9, 13, 14, 14, 34, 30], start=1):
            ws2.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ETATS_CASE = {"admis": "Admis", "refuse": "Non admis", "en_attente": "En attente"}


def parcours_workbook(parcours):
    """Résultats d'un concours en étapes : une ligne par candidat, pour
    chaque épreuve la note, le pourcentage (QCM : la note sur 20) et le résultat,
    puis le résultat final (services.parcours_concours)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Concours"
    epreuves = parcours["epreuves"]
    headers = ["Prénom", "Nom", "N° inscription"]
    for q in epreuves:          # QCM : note sur 20 au lieu du pourcentage
        headers += [f"{q.title} — {'points' if q.est_qcm else 'note'}",
                    f"{q.title} — {'note sur 20' if q.est_qcm else '%'}",
                    f"{q.title} — résultat"]
    headers.append("Résultat final")
    ws.append([" → ".join(q.title for q in epreuves)])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(headers)
    fond = PatternFill("solid", fgColor="1F4E79")
    for col in range(1, len(headers) + 1):
        cellule = ws.cell(row=2, column=col)
        cellule.font = Font(bold=True, color="FFFFFF")
        cellule.fill = fond
        cellule.alignment = Alignment(horizontal="center", wrap_text=True)
    for c in parcours["candidats"]:
        s = c["etudiant"]
        ligne = [s.first_name, s.last_name, s.student_number]
        for case in c["cases"]:
            if case["etat"] == "note":
                r = case["row"]
                ligne += [r["score"], r["note20"] if r["est_qcm"] else r["pct"] / 100,
                          ETATS_CASE.get(case["resultat"], "")]
            elif case["etat"] == "non_note":
                ligne += [None, None, "Non noté"]
            else:
                ligne += [None, None, "Non convoqué"]
        ligne.append(c["final"])
        ws.append(_sans_formule(ligne))
        for k, q in enumerate(epreuves):
            ws.cell(row=ws.max_row, column=5 + 3 * k).number_format = \
                "0.00" if q.est_qcm else "0%"
    for i, largeur in enumerate([18, 22, 14] + [11, 8, 13] * len(epreuves) + [34],
                                start=1):
        ws.column_dimensions[get_column_letter(i)].width = largeur
    ws.freeze_panes = "D3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
