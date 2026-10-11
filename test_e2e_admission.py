"""Note minimale d'admission : nombre d'admis et liste par ordre de mérite.

Sans seuil, QuizScan ne pouvait pas dire qui avait réussi un concours. On
règle la note minimale, puis on vérifie :
1. le décompte admis / non admis / en attente — une copie sous le seuil dont
   une réponse manuscrite reste à noter n'est pas « non admise » : elle peut
   encore passer ;
2. le classement par note décroissante, rang partagé en cas d'égalité ;
3. la page des résultats (carte d'admission, liste des admis, colonne
   « Résultat ») et l'export Excel (colonne et feuille « Admis ») ;
4. le réglage depuis la page du concours, et le retour à « pas de seuil ».
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from io import BytesIO  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from grader import services  # noqa: E402
from grader.models import (Answer, ClassGroup, Question, Quiz,  # noqa: E402
                           ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet  # noqa: E402

# nom : (QCM justes sur 4, points de la question manuscrite ou None = à noter)
CANDIDATS = {
    "AMRI": (4, 4.0),        # 24 -> admis, 1er
    "BEN SALEM": (3, 0.0),   # 15 -> admis, 2e ex aequo
    "CHAABANE": (3, 0.0),    # 15 -> admis, 2e ex aequo
    "ESSID": (2, 1.0),       # 11 -> non admis (copie entièrement notée)
    "DRISS": (2, None),      # 10 -> en attente (manuscrite pas encore notée)
    "FERCHICHI": (1, 0.0),   #  5 -> non admis
}
SEUIL = 12.0


def _concours():
    User = get_user_model()
    Quiz.objects.filter(owner__username="adm_prof").delete()
    ClassGroup.objects.filter(name="ADM concours").delete()
    User.objects.filter(username="adm_prof").delete()
    prof = enseignant_complet(User.objects.create_user("adm_prof", password="x"))
    groupe = ClassGroup.objects.create(name="ADM concours", owner=prof)
    quiz = Quiz.objects.create(title="ADM concours", class_group=groupe,
                               owner=prof, auto_enroll=True, id_mode="sticker",
                               sheet_mode="grid")
    qcm = [Question.objects.create(quiz=quiz, order=n, qtype="qcm", points=5,
                                   num_choices=4, correct_choice=0)
           for n in range(1, 5)]
    redaction = Question.objects.create(quiz=quiz, order=5, qtype="open",
                                        points=4)
    lot = ScanBatch.objects.create(quiz=quiz, label="ADM lot", status="done",
                                   total_pages=len(CANDIDATS))
    for i, (nom, (justes, manuscrite)) in enumerate(CANDIDATS.items()):
        cand = Student.objects.create(class_group=groupe, last_name=nom,
                                      first_name="X", student_number=f"70{i:02d}")
        copie = SheetScan.objects.create(batch=lot, source_name=f"{nom}.jpg",
                                         status="ok", student=cand)
        for n, q in enumerate(qcm, start=1):
            juste = n <= justes
            Answer.objects.create(sheet=copie, question=q,
                                  detected_choice=0 if juste else 1,
                                  points_awarded=5.0 if juste else 0.0)
        Answer.objects.create(sheet=copie, question=redaction,
                              points_awarded=manuscrite,
                              manually_set=manuscrite is not None)
    return quiz, prof


def main():
    quiz, prof = _concours()
    rows = services.compute_results(quiz)

    # ---------------------------------------------------- 1. sans seuil
    assert services.admissions(quiz, rows) is None
    client = Client()
    client.force_login(prof)
    page = client.get(f"/quiz/{quiz.pk}/resultats/").content.decode()
    assert "indiquez la <b>note minimale d'admission</b>" in page, \
        "sans seuil, la page doit dire comment obtenir la liste des admis"
    assert "<th>Résultat</th>" not in page

    # ------------------------------------- 2. réglage depuis la page du quiz
    r = client.post(f"/quiz/{quiz.pk}/", {
        "update_settings": "1", "sheet_mode": quiz.sheet_mode,
        "id_mode": quiz.id_mode, "id_digits": quiz.id_digits,
        "wrong_penalty": "0", "grading_mode": quiz.grading_mode,
        "note_admission": "12,0"})
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.note_admission == SEUIL, quiz.note_admission
    print("  réglage        note minimale d'admission saisie sur la page du concours")

    # ------------------------------------------- 3. décompte et classement
    rows = services.compute_results(quiz)
    adm = services.admissions(quiz, rows)
    assert (adm["n"], adm["n_admis"], adm["n_refuses"], adm["n_attente"]) == \
        (6, 3, 2, 1), adm
    assert adm["pct"] == 50 and adm["max_score"] == 24
    rangs = [(r["student"].last_name, r["rang"], r["score"])
             for r in adm["classement"]]
    assert rangs == [("AMRI", 1, 24.0), ("BEN SALEM", 2, 15.0),
                     ("CHAABANE", 2, 15.0), ("ESSID", 4, 11.0),
                     ("DRISS", 5, 10.0), ("FERCHICHI", 6, 5.0)], rangs
    resultats = {r["student"].last_name: r["resultat"] for r in rows}
    assert resultats["DRISS"] == "en_attente", "manuscrite à noter : pas encore refusé"
    assert resultats["ESSID"] == "refuse" and resultats["AMRI"] == "admis"
    assert [r["student"].last_name for r in adm["admis"]] == \
        ["AMRI", "BEN SALEM", "CHAABANE"]
    print("  classement     3 admis sur 6 (50 %), ex aequo au même rang, "
          "1 en attente de notation")

    # ----------------------------------------------- 4. page des résultats
    page = client.get(f"/quiz/{quiz.pk}/resultats/").content.decode()
    for attendu in ("Liste des admis", "note minimale 12 / 24", "<th>Rang</th>",
                    "<th>Résultat</th>", "Non admis", "En attente"):
        assert attendu in page, f"« {attendu} » absent de la page des résultats"
    liste = page.split("Liste des admis", 1)[1].split("</table>", 1)[0]
    assert liste.index("AMRI") < liste.index("BEN SALEM") < liste.index("CHAABANE")
    assert "ESSID" not in liste and "DRISS" not in liste, "non admis dans la liste"
    print("  page           carte d'admission, liste par ordre de mérite, "
          "colonne « Résultat »")

    # ----------------------------------------------------- 5. export Excel
    wb = load_workbook(BytesIO(client.get(
        f"/quiz/{quiz.pk}/resultats.xlsx").content))
    assert "Admis" in wb.sheetnames, wb.sheetnames
    feuille = wb["Admis"]
    noms = [feuille.cell(row=i, column=2).value for i in range(3, feuille.max_row + 1)]
    assert noms == ["AMRI", "BEN SALEM", "CHAABANE"], noms
    principale = wb["Résultats"]
    entetes = [c.value for c in principale[2]]
    assert entetes[-1] == "Résultat", entetes
    col = {principale.cell(row=i, column=1).value: principale.cell(row=i, column=len(entetes)).value
           for i in range(3, principale.max_row + 1)}
    assert col["DRISS"] == "En attente" and col["ESSID"] == "Non admis"
    print("  export Excel   colonne « Résultat » et feuille « Admis »")

    # ------------------------------------------- 6. bulletins des admis
    import pymupdf
    assert f"/quiz/{quiz.pk}/bulletins.pdf?admis=1" in page.replace("&amp;", "&") \
        or "?admis=1" in page, "bouton « Bulletins des admis » absent"
    assert "Bulletins des admis (3)" in page
    pdf = client.get(f"/quiz/{quiz.pk}/bulletins.pdf?admis=1")
    assert pdf.status_code == 200 and "bulletins_admis" in pdf["Content-Disposition"]
    doc = pymupdf.open(stream=pdf.content, filetype="pdf")
    pages = [p.get_text() for p in doc]
    noms = [n for t in pages for n in CANDIDATS if n in t]
    assert noms == ["AMRI", "BEN SALEM", "CHAABANE"], noms   # ordre de mérite
    assert "ADMIS — rang 1 sur 6" in pages[0], pages[0][:300]
    tous = "".join(p.get_text() for p in pymupdf.open(
        stream=client.get(f"/quiz/{quiz.pk}/bulletins.pdf").content, filetype="pdf"))
    for n in CANDIDATS:
        assert n in tous, f"{n} absent des bulletins de tous"
    assert "NON ADMIS — rang 4 sur 6" in tous and "EN ATTENTE" in tous
    print("  bulletins      « Bulletins des admis » : 3 relevés par ordre de mérite ; "
          "résultat et rang sur chaque relevé")

    # ----------------------------------------- 6. retour à « pas de seuil »
    client.post(f"/quiz/{quiz.pk}/", {
        "update_settings": "1", "sheet_mode": quiz.sheet_mode,
        "id_mode": quiz.id_mode, "id_digits": quiz.id_digits,
        "wrong_penalty": "0", "grading_mode": quiz.grading_mode,
        "note_admission": ""})
    quiz.refresh_from_db()
    assert quiz.note_admission is None
    # sans note minimale : pas de bouton, et l'adresse directe renvoie aux
    # résultats avec un message plutôt que de produire un PDF vide
    assert "?admis=1" not in client.get(f"/quiz/{quiz.pk}/resultats/").content.decode()
    r = client.get(f"/quiz/{quiz.pk}/bulletins.pdf?admis=1")
    assert r.status_code == 302, r.status_code

    Quiz.objects.filter(owner=prof).delete()
    print("\n✅ TEST DE LA NOTE D'ADMISSION RÉUSSI")


if __name__ == "__main__":
    main()
