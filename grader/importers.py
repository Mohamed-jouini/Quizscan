"""Import de fichiers existants : listes de candidats et questionnaires.

Formats acceptés (fiche technique : « Import Excel / PDF / Word ») :
  - Excel (.xlsx / .xlsm) : un tableau avec une ligne d'en-tête ;
  - Word (.docx)          : paragraphes et tableaux ;
  - PDF (.pdf)            : texte du document ; un PDF sans texte (scan,
                            impression « Microsoft Print to PDF ») est lu par
                            OCR, pour les questionnaires (ocr_pdf) ;
  - Texte (.txt / .csv), Markdown (.md) : puces, titres et gras sont ignorés.

Questionnaire au format texte (Word, PDF, .txt, .md) — une question numérotée,
puis ses choix précédés d'une lettre ; la bonne réponse est marquée d'un
astérisque ou donnée sur une ligne « Réponse : B » :

    1. Quelle est la capitale de la Tunisie ? (2 pts)
    A) Sfax
    B) Tunis *
    C) Sousse
    2. Expliquez le cycle de l'eau. [manuscrite]

Une question sans choix est importée comme question manuscrite. Une question à
choix sans bonne réponse indiquée est importée sans corrigé : il se renseigne
ensuite, à la main ou en scannant la fiche du corrigé (une bonne réponse écrite
mais invalide, elle, est refusée).
"""
import csv
import datetime
import io
import re
import unicodedata
import zipfile
from xml.etree import ElementTree as ET

from .models import ARABIC_LETTERS, MAX_CHOICES, MAX_OPEN_HEIGHT_MM

CANDIDATE_EXTS = (".xlsx", ".xlsm", ".csv", ".txt", ".docx", ".pdf")
QUESTION_EXTS = (".xlsx", ".xlsm", ".csv", ".txt", ".md", ".markdown", ".docx", ".pdf")


class ImportError_(ValueError):
    """Fichier illisible ou contenu non reconnu (message pour l'utilisateur)."""


class PdfSansTexte(ImportError_):
    """PDF lisible, mais sans texte : ses lettres sont des images ou des dessins."""


def norm(s):
    """Minuscule sans accents, espaces réduits, pour reconnaître les en-têtes."""
    s = unicodedata.normalize("NFD", str(s or "").strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[°'’._\-/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)          # 12345.0 -> "12345"
    return str(v).strip()


# ------------------------------------------------------------------ lecture

MAX_LIGNES = 20000       # au-delà, ce n'est plus une liste de classe
MAX_COLONNES = 60


def _xlsx_rows(data):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"Fichier Excel illisible : {exc}") from exc
    ws = wb.active
    # Dimensions déclarées par le fichier lui-même : un petit .xlsx peut
    # annoncer des milliards de cellules, que la lecture remplirait de vide.
    ws.reset_dimensions()
    rows = []
    for r in ws.iter_rows(max_row=MAX_LIGNES, max_col=MAX_COLONNES, values_only=True):
        ligne = [_cell(c) for c in r]
        if any(ligne):
            rows.append(ligne)
    return rows


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_blocks(data):
    """Paragraphes et lignes de tableaux d'un .docx, dans l'ordre du document.
    Retourne une liste d'éléments : str (paragraphe) ou list[str] (ligne)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # taille décompressée vérifiée avant lecture (archive piégée)
            if z.getinfo("word/document.xml").file_size > 50 * 1024 * 1024:
                raise ImportError_("Document Word démesuré.")
            root = ET.fromstring(z.read("word/document.xml"))
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"Fichier Word illisible : {exc}") from exc

    def text_of(el):
        parts = []
        for node in el.iter():
            if node.tag == _W + "t" and node.text:
                parts.append(node.text)
            elif node.tag == _W + "tab":
                parts.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        return "".join(parts)

    body = root.find(_W + "body")
    blocks = []
    for el in (body if body is not None else []):
        if el.tag == _W + "p":
            for line in text_of(el).split("\n"):
                blocks.append(line)
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                blocks.append([text_of(tc).strip() for tc in tr.findall(_W + "tc")])
    return blocks


def _pdf_lines(data):
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"Fichier PDF illisible : {exc}") from exc
    lines = []
    for page in doc:
        lines.extend(page.get_text("text").splitlines())
    imprime = "print to pdf" in (doc.metadata.get("producer") or "").lower()
    doc.close()
    if not any(l.strip() for l in lines):
        # « Microsoft Print to PDF » dessine chaque lettre : rien à lire, pas même
        # par un copier-coller (questionnaire reçu de l'utilisateur, 6 novembre 2026)
        cause = ("Il a été imprimé avec « Microsoft Print to PDF », qui transforme les "
                 "lettres en dessins" if imprime
                 else "Ses lettres y sont des images ou des dessins (document scanné ?)")
        raise PdfSansTexte(f"Ce PDF ne contient pas de texte lisible. {cause}. Importez "
                           "plutôt le fichier d'origine (Word, texte ou Markdown .md), ou "
                           "un PDF créé depuis Word par « Enregistrer sous… › PDF ».")
    return lines


def _text_lines(data):
    for enc in ("utf-8-sig", "cp1256", "cp1252", "latin-1"):
        try:
            return data.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace").splitlines()


def _csv_rows(data):
    lines = _text_lines(data)
    sample = "\n".join(lines[:5])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    return [[c.strip() for c in r] for r in csv.reader(lines, dialect) if any(r)]


def _ext(name):
    name = (name or "").lower()
    return name[name.rfind("."):] if "." in name else ""


# ------------------------------------------------------------------ candidats

def _entetes(*textes):
    """Intitulés sous la forme où norm() les rend (« الإصدار » -> « الاصدار »)."""
    return {norm(t) for t in textes}


_LAST = _entetes("nom", "lastname", "last name", "last", "nom de famille", "اللقب")
_FIRST = _entetes("prenom", "firstname", "first name", "first", "الاسم")
_NUM = _entetes("numero", "numero d inscription", "n", "n inscription", "no", "num",
                "matricule", "inscription", "numero inscription", "id", "identifiant",
                "n d inscription", "رقم التسجيل", "الرقم", "عدد الترسيم", "رقم الترسيم",
                "ع الترسيم", "الترسيم", "عدد التسجيل")
# Colonnes de la fiche au registre, celles de la liste officielle de
# l'institut. Une colonne « CIN » seule (sans colonne de n° d'inscription)
# reste lue comme le n° : c'était le cas avant que la CIN ait son propre champ,
# les fichiers existants donnent le même résultat.
_CIN = _entetes("cin", "n cin", "numero cin", "num cin", "cin n", "carte d identite",
                "n carte d identite", "numero de cin", "رقم بطاقة التعريف",
                "بطاقة التعريف", "عدد ب ت و", "ع ب ت و", "ب ت و", "عدد بطاقة التعريف",
                "عدد بطاقة التعريف الوطنية", "رقم بطاقة التعريف الوطنية",
                "بطاقة التعريف الوطنية")
_CIN_LIEU = _entetes("مكانها", "مكان الإصدار", "مكان إصدارها", "جهة الإصدار", "lieu",
                     "lieu de delivrance", "lieu delivrance", "lieu cin", "delivree a")
_CIN_DATE = _entetes("تاريخ الإصدار", "تاريخ إصدارها", "تاريخها", "date de delivrance",
                     "date delivrance", "date cin", "delivree le")
_NAISSANCE = _entetes("date de naissance", "date naissance", "naissance", "ne le",
                      "nee le", "ne e le", "ddn", "تاريخ الولادة", "تاريخ الميلاد")
_CENTRE = _entetes("المركز", "مركز الامتحان", "مركز الاختبار", "centre",
                   "centre d examen", "centre examen")
_SALLE = _entetes("القاعة", "قاعة", "قاعة الامتحان", "salle", "salle d examen",
                  "n salle", "numero de salle")
# Plus d'établissement ni d'e-mail : retirés du registre le 7 novembre 2026, leurs
# colonnes sont ignorées comme toute colonne inconnue.
_EXTRAS = {"cin": _CIN, "cin_lieu": _CIN_LIEU, "cin_date": _CIN_DATE,
           "naissance": _NAISSANCE, "centre": _CENTRE, "salle": _SALLE}
_FULL = _entetes("nom et prenom", "nom prenom", "prenom et nom", "prenom nom",
                 "nom complet", "etudiant", "candidat")

_ARABE = re.compile(r"[؀-ۿ]")
# Mots qui appartiennent au nom de famille avec le mot qui les suit :
# « بن سالم », « بو علي », « عبد الله », « الحاج علي ».
_PARTICULES = _entetes("بن", "ابن", "بنت", "بو", "أبو", "عبد", "حاج", "الحاج")


def _colonne_nom_complet(entete):
    """Une seule colonne pour le nom et le prénom ? Tolère les fautes de frappe
    de l'intitulé (« الاسم ة اللقب » dans la liste de l'institut). Retourne
    « prenom » ou « nom » selon ce qui vient en premier, sinon None."""
    if "اسم" in entete and "لقب" in entete:
        return "prenom" if entete.index("اسم") < entete.index("لقب") else "nom"
    if entete in _FULL:
        return "prenom" if entete.startswith("prenom") else "nom"
    return None


def decouper_nom_complet(texte, prenom_d_abord=False):
    """Le contenu d'une colonne unique -> (nom, prénom, numéro éventuel).

    En arabe, pas de majuscules pour reconnaître le nom de famille : c'est le
    dernier mot quand la colonne donne le prénom d'abord (« الاسم و اللقب »), le
    premier sinon, avec ses particules. Toujours au moins un mot pour le
    prénom : « محمد علي بن سالم » -> « بن سالم », « محمد علي »."""
    if not _ARABE.search(texte) or any(sep in texte for sep in ";\t,"):
        return split_name_line(texte)
    mots = texte.split()
    numero = ""
    if len(mots) > 1 and re.fullmatch(r"\d{3,}", mots[-1]):
        numero = mots.pop()
    elif len(mots) > 1 and re.fullmatch(r"\d{3,}", mots[0]):
        numero = mots.pop(0)
    if len(mots) < 2:
        return " ".join(mots), "", numero
    if prenom_d_abord:
        debut = len(mots) - 1
        while debut > 1 and norm(mots[debut - 1]) in _PARTICULES:
            debut -= 1
        return " ".join(mots[debut:]), " ".join(mots[:debut]), numero
    fin = 1
    while fin < len(mots) - 1 and norm(mots[fin - 1]) in _PARTICULES:
        fin += 1
    return " ".join(mots[:fin]), " ".join(mots[fin:]), numero


_CHIFFRES_LATINS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_FORMATS_DATE = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d")


def chiffres_latins(texte):
    """« ٧٦١ » -> « 761 » : les chiffres saisis au clavier arabe."""
    return str(texte or "").translate(_CHIFFRES_LATINS)


def lire_date(texte):
    """Date d'une cellule (date d'Excel, « 17/12/2024 », « 2024-12-17 » ou n° de
    série d'Excel quand la cellule n'est pas au format date) -> date, ou None."""
    texte = chiffres_latins(texte).strip()
    if not texte:
        return None
    if re.fullmatch(r"\d{5}(\.0+)?", texte):
        return datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(texte)))
    # « 2003-01-03 00:00:00 » : une date d'Excel passée par _cell()
    texte = texte.split(" ")[0].split("T")[0]
    for forme in _FORMATS_DATE:
        try:
            return datetime.datetime.strptime(texte, forme).date()
        except ValueError:
            continue
    return None


def split_name_line(line):
    """Découpe une ligne libre en (nom, prénom, numéro).

    Séparateurs reconnus : « ; », « , », tabulation. Sinon : un numéro en fin
    (ou en début) de ligne est détaché, puis les mots en MAJUSCULES du début
    forment le nom (« BEN SALAH Ahmed »), à défaut le premier mot."""
    line = line.strip()
    for sep in (";", "\t", ","):
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            parts += ["", "", ""]
            return parts[0], parts[1], parts[2]
    words = line.split()
    number = ""
    if len(words) > 1 and re.fullmatch(r"\d{3,}", words[-1]):
        number = words.pop()
    elif len(words) > 1 and re.fullmatch(r"\d{3,}", words[0]):
        number = words.pop(0)
    upper = []
    for w in words:
        if any(c.isalpha() for c in w) and w == w.upper() and w != w.lower():
            upper.append(w)
        else:
            break
    if upper and len(upper) < len(words):
        return " ".join(upper), " ".join(words[len(upper):]), number
    return (words[0] if words else ""), " ".join(words[1:]), number


def rows_to_candidates(rows):
    """Lignes de tableau -> [(nom, prénom, numéro)], en-tête détecté."""
    return [(f["nom"], f["prenom"], f["numero"]) for f in rows_to_fiches(rows)]


def fiche_simple(nom, prenom, numero):
    """Une ligne qui ne porte que le nom et le n° (copier-coller, Word, PDF)."""
    return dict({"nom": nom, "prenom": prenom, "numero": numero},
                **{cle: "" for cle in _EXTRAS})


def _colonnes_de(ligne):
    """Colonnes reconnues d'une ligne d'intitulés : {clé: indice}. La première
    occurrence l'emporte si un intitulé revient."""
    colonnes = {}
    for i, cellule in enumerate(ligne):
        h = norm(cellule)
        if not h:
            continue
        cle = None
        if h in _LAST:
            cle = "last"
        elif h in _FIRST:
            cle = "first"
        elif h in _NUM:
            cle = "number"
        elif _colonne_nom_complet(h):
            cle = "full"
        else:
            cle = next((c for c, noms in _EXTRAS.items() if h in noms), None)
        if cle and cle not in colonnes:
            colonnes[cle] = i
            if cle == "full":
                colonnes["prenom_d_abord"] = _colonne_nom_complet(h) == "prenom"
    return colonnes


def _trouver_entete(rows):
    """(rang de la ligne d'intitulés, colonnes). Un titre peut la précéder
    (« قائمة المترشحين ») : deux colonnes reconnues dans les 10 premières lignes
    suffisent, ou une seule en première ligne. (None, {}) : pas d'en-tête."""
    for rang, ligne in enumerate(rows[:10]):
        colonnes = _colonnes_de(ligne)
        if len(colonnes.keys() - {"prenom_d_abord"}) >= 2:
            return rang, colonnes
    colonnes = _colonnes_de(rows[0])
    return (0, colonnes) if colonnes else (None, {})


def _a_defaut(colonnes, cle, indice, nb_colonnes):
    """Une colonne de nom sans intitulé connu reste à sa place habituelle, si
    aucune autre colonne reconnue ne l'occupe (« Nom », « Prénoms », « N° »)."""
    if cle not in colonnes and indice < nb_colonnes and indice not in colonnes.values():
        colonnes[cle] = indice


def rows_to_fiches(rows):
    """Lignes de tableau -> [{nom, prenom, numero, cin, cin_lieu, cin_date,
    naissance, centre, salle}] ; les colonnes sont
    reconnues par leur intitulé (vides sinon). Sans intitulé : nom, prénom, n°.

    Avec des intitulés mais aucun pour le nom ni pour le n°, on refuse plutôt
    que de deviner : la liste de l'institut, dont seule la date de naissance
    était reconnue, donnait le n° pour nom et la CIN pour n°."""
    if not rows:
        return []
    rang, colonnes = _trouver_entete(rows)
    has_header = rang is not None
    donnees = rows[rang + 1:] if has_header else rows
    if not has_header:
        colonnes = {"last": 0, "first": 1, "number": 2}
    elif not colonnes.keys() & {"last", "first", "full", "number", "cin"}:
        raise ImportError_(
            "Colonnes non reconnues : la première ligne du fichier doit nommer au "
            "moins le n° d'inscription (عدد الترسيم) ou le nom (الاسم و اللقب).")
    if "full" not in colonnes and colonnes.keys() & {"last", "first"}:
        nb_colonnes = max(len(r) for r in rows)
        _a_defaut(colonnes, "last", 0, nb_colonnes)
        _a_defaut(colonnes, "first", 1, nb_colonnes)
        _a_defaut(colonnes, "number", 2, nb_colonnes)
    extras = {cle: colonnes.get(cle) for cle in _EXTRAS}
    if "cin" in colonnes and "number" not in colonnes:
        # ancien usage : la CIN tient lieu de n° d'inscription. Elle n'est pas
        # lue en plus comme CIN : sans vrai n°, pas de fiche au registre.
        colonnes["number"] = colonnes["cin"]
        extras["cin"] = None
    plein = colonnes.get("full") if not colonnes.keys() & {"last", "first"} else None
    out = []
    for r in donnees:
        def col(i):
            return r[i].strip() if i is not None and i < len(r) else ""
        if plein is not None:
            # une seule colonne « Nom et prénom »
            last, first, number = decouper_nom_complet(
                col(plein), colonnes.get("prenom_d_abord", False))
            number = col(colonnes.get("number")) or number
        elif not has_header and len([c for c in r if c]) == 1:
            last, first, number = split_name_line(next(c for c in r if c))
        else:
            last, first, number = (col(colonnes.get("last")), col(colonnes.get("first")),
                                   col(colonnes.get("number")))
        if last or first or number:
            fiche = {"nom": last, "prenom": first, "numero": number}
            for cle, indice in extras.items():
                fiche[cle] = col(indice)
            out.append(fiche)
    return out


def parse_candidates_file(name, data):
    """Fichier Excel / CSV / Word / PDF / texte -> [(nom, prénom, numéro)]."""
    return [(f["nom"], f["prenom"], f["numero"]) for f in parse_fiches_file(name, data)]


def modele_candidats_xlsx():
    """La liste modèle à remplir : les colonnes de la liste officielle de
    l'institut, de droite à gauche, avec deux lignes d'exemple inventées."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "المترشحون"
    ws.sheet_view.rightToLeft = True
    ws.append(["عدد الترسيم", "الاسم و اللقب", "عدد ب ت و", "مكانها", "تاريخ الإصدار",
               "تاريخ الولادة", "المركز", "القاعة"])
    for ligne in ((1001, "سامي بن تجربة", "01234567", "تونس", datetime.date(2020, 3, 2),
                   datetime.date(2001, 5, 14), "معهد المثال", "قاعة عدد 1"),
                  (1002, "ليلى المثال", "07654321", "صفاقس", datetime.date(2021, 9, 20),
                   datetime.date(2000, 11, 3), "معهد المثال", "قاعة عدد 2")):
        ws.append(list(ligne))
    for rang in range(2, ws.max_row + 1):
        ws.cell(rang, 3).number_format = "@"     # la CIN garde son zéro de tête
        for colonne in (5, 6):
            ws.cell(rang, colonne).number_format = "dd/mm/yyyy"
    for colonne, largeur in zip("ABCDEFGH", (12, 26, 12, 12, 14, 14, 26, 14)):
        ws.column_dimensions[colonne].width = largeur
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


def parse_fiches_file(name, data):
    """Comme parse_candidates_file, avec les colonnes de la fiche au registre
    (CIN, lieu et date de délivrance, naissance, centre, salle, établissement,
    e-mail) quand le fichier est un tableau : voir rows_to_fiches."""
    ext = _ext(name)
    if ext in (".xlsx", ".xlsm"):
        return rows_to_fiches(_xlsx_rows(data))
    if ext == ".csv":
        return rows_to_fiches(_csv_rows(data))
    if ext == ".docx":
        blocks = _docx_blocks(data)
        table = [b for b in blocks if isinstance(b, list) and any(b)]
        if table:
            return rows_to_fiches(table)
        lines = [b for b in blocks if isinstance(b, str)]
    elif ext == ".pdf":
        lines = _pdf_lines(data)
    elif ext == ".txt":
        lines = _text_lines(data)
    else:
        raise ImportError_("Format non pris en charge : utilisez un fichier "
                           "Excel, CSV, Word (.docx), PDF ou texte.")
    rows = [split_name_line(l) for l in lines if l.strip()]
    # une première ligne d'en-tête (« Nom Prénom N° ») est ignorée
    if rows and norm(rows[0][0]) in _LAST | _FULL:
        rows = rows[1:]
    return [fiche_simple(*r[:3]) for r in rows if r[0] or r[1]]


# ------------------------------------------------------------------ questions

_Q_START = re.compile(
    r"^\s*(?:(?:q(?:uestion)?|السؤال)\s*)?(\d{1,3})\s*[.)\-:–]\s*(.*)$", re.I)
_CHOICE = re.compile(
    r"^\s*[\[(]?\s*([A-Ja-j]|[" + "".join(ARABIC_LETTERS) + r"ا])\s*[).\]:\-–]\s*(.*)$")
_ANSWER = re.compile(
    r"^\s*(?:bonne\s+r[ée]ponse|r[ée]ponse(?:\s+correcte)?|corrig[ée]|answer|"
    r"الإجابة(?:\s+الصحيحة)?|الجواب)\s*[:=]\s*(.+)$", re.I)
_POINTS = re.compile(
    r"[(\[]\s*(\d+(?:[.,]\d+)?)\s*(?:pts?|points?|نقطة|نقاط)\s*[)\]]", re.I)
_POINTS_LINE = re.compile(
    r"^\s*(?:bar[èe]me|points?|pts|النقاط|العدد)\s*[:=]\s*(\d+(?:[.,]\d+)?)", re.I)
_OPEN_TAG = re.compile(r"[\[(]\s*(?:manuscrite|ouverte|r[ée]daction|open|"
                       r"مقالي|كتابي)\s*[\])]", re.I)
_MARK = re.compile(r"^\s*[*✓✔]\s*|\s*(?:[*✓✔]|\(\s*x\s*\)|\[\s*x\s*\])\s*$", re.I)

_AR_INDEX = {l: i for i, l in enumerate(ARABIC_LETTERS)}
_AR_INDEX["ا"] = 0


def letter_index(token):
    token = token.strip()
    if token in _AR_INDEX:
        return _AR_INDEX[token]
    if len(token) == 1 and token.isalpha() and token.isascii():
        return ord(token.upper()) - ord("A")
    return None


def _answer_index(value, choices):
    """« B », « b) », « 2 », « أ » ou le texte exact d'un choix -> index."""
    v = str(value or "").strip()
    if not v:
        return None
    m = re.match(r"^\s*([A-Ja-j]|[" + "".join(ARABIC_LETTERS) + r"ا])\s*[).]?\s*$", v)
    if m:
        return letter_index(m.group(1))
    if re.fullmatch(r"\d{1,2}", v):
        return int(v) - 1
    for i, c in enumerate(choices):
        if norm(c) == norm(v):
            return i
    return None


def _points(s):
    return float(s.replace(",", "."))


_MD_TRAIT = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MD_DEBUT = re.compile(r"^\s*(?:#{1,6}\s+|>\s?)")
_MD_PUCE = re.compile(r"^\s*[-*+•·▪●◦]\s+")


_FORMES_ARABES = re.compile("[ﭐ-﷿ﹰ-﻿]+")
_MARQUES_SENS = re.compile("[؜‍‎‏‪-‮⁦-⁩]")
_LETTRE_PARENTHESE = re.compile("^(\\s*[" + "".join(ARABIC_LETTERS) + "ا])\\s*\\(")


def texte_normalise(ligne):
    """Une ligne telle que la relit un PDF arabe, remise en texte ordinaire : formes de
    présentation (« ﺗﻮﻧﺲ ») en lettres (« تونس »), espaces insécables en espaces, marques
    de sens retirées, et la parenthèse d'un choix rendue retournée (« ب( ») remise à
    l'endroit. Sans effet sur un texte déjà ordinaire."""
    ligne = _FORMES_ARABES.sub(lambda m: unicodedata.normalize("NFKC", m.group()), ligne)
    ligne = _MARQUES_SENS.sub("", ligne.replace("\xa0", " ").replace(" ", " "))
    return _LETTRE_PARENTHESE.sub(r"\1)", ligne)


def sans_markdown(ligne):
    """Une ligne de Markdown lue comme du texte : sans trait de séparation, titre,
    citation, puce de liste, ni gras « ** » / « __ ». Le « * » qui suit un choix
    (bonne réponse) reste : seul « * » suivi d'un espace en début de ligne est une puce."""
    if _MD_TRAIT.match(ligne):
        return ""
    ligne = _MD_PUCE.sub("", _MD_DEBUT.sub("", ligne))
    return ligne.replace("**", "").replace("__", "").strip()


_LETTRES_CHOIX = "A-Ja-j" + "".join(ARABIC_LETTERS) + "ا"
# Ce que l'OCR fait d'un choix : lettre perdue (« ) الأمازون »), doublée (« تث) النيل »),
# ou, devant un nombre, lettre perdue et ordre inversé (« أ) 5 » lu « 5) »).
_CHOIX_OCR = re.compile(rf"^\s*([{_LETTRES_CHOIX}]{{1,2}})?\s*[)(]+\s*(.*)$")
_NOMBRE_OCR = re.compile(r"^\s*(\d{1,4})\s*[)(]\s*$")
_Q_SANS_POINT = re.compile(r"^\s*(\d{1,3})\s+([^\W\d_].*)$")
_LETTRE_COLLEE = re.compile(r"^[ء-ي](\d+)$")      # « ة8 » : 8


A_RELIRE = "(à relire)"


def _choix_ocr(ligne, attendu):
    """(position, texte) du choix lu par l'OCR, ou None si la ligne n'en est pas un.
    Position : celle de la lettre lue, ou celle attendue (attendu : le nombre de choix
    déjà lus) quand l'OCR a perdu la lettre. Une lettre plus loin que l'attendue (deux
    au plus) : les choix d'entre-deux n'ont pas été lus."""
    if attendu >= MAX_CHOICES:
        return None
    nombre = _NOMBRE_OCR.match(ligne)
    if nombre:
        return attendu, nombre.group(1)
    m = _CHOIX_OCR.match(ligne)
    if not m or not m.group(2).strip():
        return None
    position = attendu
    if m.group(1):
        lues = {letter_index(l) for l in m.group(1)} - {None}
        if attendu not in lues:
            plus_loin = [p for p in lues if attendu < p <= attendu + 2 and p < MAX_CHOICES]
            if not plus_loin:
                return None
            position = min(plus_loin)
    texte = m.group(2).strip()
    collee = _LETTRE_COLLEE.match(texte)
    return position, (collee.group(1) if collee else texte)


def parse_questions_text(lines, ocr=False):
    """Texte libre -> (questions, erreurs). Voir le format en tête de module.
    Le Markdown d'un questionnaire (« **1.** … », « - أ) … ») se lit aussi.

    ocr : lignes lues par OCR (ocr_pdf). Le n° d'une question peut y perdre un
    chiffre (« 0. » pour 10.) : il ne sert que de repère, l'ordre est celui des
    lignes ; une question doit avoir un texte, et un choix se reconnaît même
    quand l'OCR en a perdu ou doublé la lettre (_choix_ocr)."""
    items, cur = [], None

    def close():
        if cur is not None:
            items.append(cur)

    for raw in lines:
        line = sans_markdown(texte_normalise(raw))
        if not line:
            continue
        m_ans = _ANSWER.match(line)
        if m_ans and cur is not None:
            cur["answer"] = m_ans.group(1).strip()
            continue
        m_pts = _POINTS_LINE.match(line)
        if m_pts and cur is not None:
            cur["points"] = _points(m_pts.group(1))
            continue
        m_q = _Q_START.match(line)
        if ocr and m_q and not re.search(r"[^\W\d_]", m_q.group(2)):
            m_q = None            # « 5) » : un choix dont l'OCR a perdu la lettre
        if ocr and not m_q and (cur is None or len(cur["choices"]) >= 2):
            # « 7 ما هي … ؟ » : l'OCR a perdu le point du n° ; une question (texte
            # terminé par « ؟ » ou de 4 mots et plus), après une question complète
            m_q = _Q_SANS_POINT.match(line)
            if m_q and not (m_q.group(2).rstrip().endswith(("؟", "?"))
                            or len(m_q.group(2).split()) >= 4):
                m_q = None
        if ocr and cur is not None and not m_q:
            lu = _choix_ocr(line, len(cur["choices"]))
            if lu is not None:
                position, texte_choix = lu
                while len(cur["choices"]) < position:      # choix illisible : sa place
                    cur["choices"].append(A_RELIRE)
                if _MARK.search(texte_choix):
                    texte_choix = _MARK.sub("", texte_choix).strip()
                    cur["correct"] = position
                cur["choices"].append(texte_choix)
                continue
        m_c = _CHOICE.match(line)
        if m_q:
            close()
            text = m_q.group(2).strip()
            cur = {"num": int(m_q.group(1)), "text": text, "choices": [],
                   "correct": None, "points": None, "answer": None,
                   "open": False}
            continue
        if cur is None:
            continue            # titre, consignes… avant la 1re question
        if m_c:
            label, body = m_c.group(1), m_c.group(2).strip()
            expected = len(cur["choices"])
            if letter_index(label) == expected:
                if _MARK.search(body):
                    body = _MARK.sub("", body).strip()
                    cur["correct"] = expected
                cur["choices"].append(body)
                continue
        # ligne de continuation : suite du dernier choix ou de l'intitulé
        if cur["choices"]:
            cur["choices"][-1] = f"{cur['choices'][-1]} {line}".strip()
        else:
            cur["text"] = f"{cur['text']} {line}".strip()
    close()
    return _finalize(items)


def _finalize(items):
    questions, errors = [], []
    for it in items:
        text = it["text"]
        if _OPEN_TAG.search(text):
            it["open"] = True
            text = _OPEN_TAG.sub("", text).strip()
        m = _POINTS.search(text)
        points = it.get("points")
        if m:
            if points is None:
                points = _points(m.group(1))
            text = (text[:m.start()] + text[m.end():]).strip()
        points = 1.0 if points is None else points
        choices = [c for c in it["choices"] if c]
        label = f"Question {it['num']}"
        if it["open"] or not choices:
            questions.append({"qtype": "open", "text": text[:500], "choices": [],
                              "correct": None, "points": points,
                              "open_height_mm": 25})
            continue
        correct = it["correct"]
        reponse = it.get("answer")
        if reponse:
            correct = _answer_index(reponse, choices)
        if len(choices) < 2:
            errors.append(f"{label} : au moins deux choix sont nécessaires.")
            continue
        if len(choices) > MAX_CHOICES:
            errors.append(f"{label} : maximum {MAX_CHOICES} choix.")
            continue
        if reponse and (correct is None or not 0 <= correct < len(choices)):
            errors.append(f"{label} : bonne réponse « {reponse[:20]} » invalide "
                          "(la lettre d'un choix : A, B… ou أ، ب…).")
            continue
        # Sans bonne réponse indiquée (« version sans réponses ») : importée quand
        # même, à renseigner ensuite à la main ou par le scan du corrigé ; la
        # correction attend qu'elle le soit (services.corrige_incomplet).
        questions.append({"qtype": "qcm", "text": text[:500], "choices": choices,
                          "correct": correct, "points": points,
                          "open_height_mm": 25})
    return questions, errors


_H_TEXT = {"question", "questions", "intitule", "enonce", "libelle", "السؤال"}
_H_TYPE = {"type", "النوع"}
_H_ANSWER = {"reponse", "bonne reponse", "corrige", "reponse correcte",
             "answer", "correct", "الإجابة", "الاجابة", "الإجابة الصحيحة"}
_H_POINTS = {"bareme", "points", "point", "pts", "note", "النقاط", "العدد"}
_H_HEIGHT = {"hauteur", "hauteur zone", "hauteur mm", "hauteur zone mm"}


def _choice_col(h):
    """Index du choix d'une colonne d'en-tête (« A », « Choix B », « Choix 3 »)."""
    h = h.replace("choix", "").replace("choice", "").replace("proposition", "").strip()
    if not h:
        return None
    if re.fullmatch(r"[a-j]", h):
        return ord(h) - ord("a")
    if re.fullmatch(r"\d{1,2}", h) and 1 <= int(h) <= MAX_CHOICES:
        return int(h) - 1
    if h in _AR_INDEX:
        return _AR_INDEX[h]
    return None


def parse_questions_rows(rows):
    """Tableau (Excel / CSV / Word) -> (questions, erreurs).

    En-têtes reconnus : Question, Type, A / B / C… (ou Choix 1, Choix 2…),
    Réponse, Barème, Hauteur."""
    if not rows:
        return [], ["Le fichier est vide."]
    header = [norm(c) for c in rows[0]]
    cols = {"choices": {}}
    for i, h in enumerate(header):
        if h in _H_TEXT:
            cols["text"] = i
        elif h in _H_TYPE:
            cols["type"] = i
        elif h in _H_ANSWER:
            cols["answer"] = i
        elif h in _H_POINTS:
            cols["points"] = i
        elif h in _H_HEIGHT:
            cols["height"] = i
        else:
            k = _choice_col(h)
            if k is not None:
                cols["choices"][k] = i
    if "text" not in cols:
        return [], ["En-tête non reconnu : la première ligne doit contenir au "
                    "moins les colonnes « Question », « A », « B »…, "
                    "« Réponse » et « Barème » (voir modele_questions.xlsx)."]

    questions, errors = [], []
    for n, r in enumerate(rows[1:], start=2):
        def col(key):
            i = cols.get(key)
            return r[i].strip() if i is not None and i < len(r) else ""
        text = col("text")
        choices = []
        for k in sorted(cols["choices"]):
            i = cols["choices"][k]
            choices.append(r[i].strip() if i < len(r) else "")
        while choices and not choices[-1]:
            choices.pop()
        if not text and not any(choices):
            continue
        label = f"Ligne {n}"
        try:
            points = _points(col("points")) if col("points") else 1.0
        except ValueError:
            errors.append(f"{label} : barème invalide « {col('points')} ».")
            continue
        qtype = norm(col("type"))
        is_open = qtype.startswith(("manus", "ouvert", "redac", "open", "مقال", "كتاب"))
        if is_open or (not any(choices) and not col("answer")):
            try:
                height = int(float(col("height"))) if col("height") else 25
            except ValueError:
                height = 25
            questions.append({"qtype": "open", "text": text[:500], "choices": [],
                              "correct": None, "points": points,
                              "open_height_mm": max(10, min(height, MAX_OPEN_HEIGHT_MM))})
            continue
        if any(not c for c in choices):
            errors.append(f"{label} : un choix est vide entre deux choix remplis.")
            continue
        reponse = col("answer")
        correct = _answer_index(reponse, choices) if reponse else None
        # sans texte de choix (grille de réponses seule) : le nombre de cases
        # suit le réglage du quiz, vérifié à l'enregistrement
        n_choices = len(choices) or MAX_CHOICES
        if len(choices) == 1:
            errors.append(f"{label} : au moins deux choix sont nécessaires.")
            continue
        # réponse vide : à renseigner ensuite (voir _finalize) ; écrite mais fausse : refusée
        if reponse and (correct is None or not 0 <= correct < n_choices):
            errors.append(f"{label} : bonne réponse « {reponse} » invalide.")
            continue
        questions.append({"qtype": "qcm", "text": text[:500], "choices": choices,
                          "correct": correct, "points": points,
                          "open_height_mm": 25})
    return questions, errors


def _questions_du_pdf(data, langue):
    """Questions d'un PDF : son texte ; s'il n'en a pas (imprimé avec « Microsoft
    Print to PDF », scanné), ce que l'OCR y lit. Chaque question lue par OCR porte
    « ocr » : à relire. Sans Tesseract, le message dit quoi envoyer à la place."""
    try:
        return parse_questions_text(_pdf_lines(data))
    except PdfSansTexte as sans_texte:
        import pymupdf

        from . import ocr_pdf
        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if doc.page_count > ocr_pdf.PAGES_MAX:
                    raise ImportError_(
                        f"{sans_texte} La lecture par OCR s'arrête à {ocr_pdf.PAGES_MAX} "
                        f"pages ; ce PDF en a {doc.page_count}.") from None
                lignes = ocr_pdf.lignes_du_pdf(doc, langue)
        except ocr_pdf.OcrIndisponible:
            raise sans_texte from None
        questions, erreurs = parse_questions_text(lignes, ocr=True)
        if not questions and not erreurs:
            raise ImportError_(f"{sans_texte} La lecture par reconnaissance de caractères "
                               "(OCR) n'y a trouvé aucune question numérotée.") from None
        for q in questions:
            q["ocr"] = True
        return questions, erreurs


def parse_questions_file(name, data, langue="fr"):
    """Fichier Excel / CSV / Word / PDF / texte -> (questions, erreurs).
    langue : celle du quiz, pour l'OCR d'un PDF sans texte."""
    ext = _ext(name)
    if ext in (".xlsx", ".xlsm"):
        return parse_questions_rows(_xlsx_rows(data))
    if ext == ".csv":
        return parse_questions_rows(_csv_rows(data))
    if ext == ".docx":
        blocks = _docx_blocks(data)
        table = [b for b in blocks if isinstance(b, list) and any(b)]
        if table and norm(table[0][0]) in _H_TEXT:
            return parse_questions_rows(table)
        lines = []
        for b in blocks:
            lines.extend([b] if isinstance(b, str) else ["\t".join(b)])
        return parse_questions_text(lines)
    if ext == ".pdf":
        return _questions_du_pdf(data, langue)
    if ext in (".txt", ".md", ".markdown"):
        return parse_questions_text(_text_lines(data))
    raise ImportError_("Format non pris en charge : utilisez un fichier "
                       "Excel, CSV, Word (.docx), PDF, texte ou Markdown (.md).")
