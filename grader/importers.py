"""Import de fichiers existants : listes de candidats et questionnaires.

Formats acceptés (fiche technique : « Import Excel / PDF / Word ») :
  - Excel (.xlsx / .xlsm) : un tableau avec une ligne d'en-tête ;
  - Word (.docx)          : paragraphes et tableaux ;
  - PDF (.pdf)            : texte du document (PDF « texte », pas un scan) ;
  - Texte (.txt / .csv).

Questionnaire au format texte (Word, PDF, .txt) — une question numérotée,
puis ses choix précédés d'une lettre ; la bonne réponse est marquée d'un
astérisque ou donnée sur une ligne « Réponse : B » :

    1. Quelle est la capitale de la Tunisie ? (2 pts)
    A) Sfax
    B) Tunis *
    C) Sousse
    2. Expliquez le cycle de l'eau. [manuscrite]

Une question sans choix est importée comme question manuscrite.
"""
import csv
import io
import re
import unicodedata
import zipfile
from xml.etree import ElementTree as ET

from .models import ARABIC_LETTERS, MAX_CHOICES, MAX_OPEN_HEIGHT_MM

CANDIDATE_EXTS = (".xlsx", ".xlsm", ".csv", ".txt", ".docx", ".pdf")
QUESTION_EXTS = (".xlsx", ".xlsm", ".csv", ".txt", ".docx", ".pdf")


class ImportError_(ValueError):
    """Fichier illisible ou contenu non reconnu (message pour l'utilisateur)."""


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

def _xlsx_rows(data):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"Fichier Excel illisible : {exc}") from exc
    ws = wb.active
    rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
    return [r for r in rows if any(r)]


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_blocks(data):
    """Paragraphes et lignes de tableaux d'un .docx, dans l'ordre du document.
    Retourne une liste d'éléments : str (paragraphe) ou list[str] (ligne)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
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
    doc.close()
    if not any(l.strip() for l in lines):
        raise ImportError_("Ce PDF ne contient pas de texte (document scanné ?). "
                           "Utilisez un PDF exporté depuis Word, ou le fichier Word.")
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

_LAST = {"nom", "lastname", "last name", "last", "nom de famille", "اللقب"}
_FIRST = {"prenom", "firstname", "first name", "first", "الاسم"}
_NUM = {"numero", "numero d inscription", "n", "n inscription", "no", "num",
        "matricule", "inscription", "numero inscription", "id", "identifiant",
        "cin", "n d inscription", "رقم التسجيل", "الرقم"}
_FULL = {"nom et prenom", "nom prenom", "nom complet", "etudiant", "candidat",
         "الاسم واللقب", "اللقب والاسم"}


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
    if not rows:
        return []
    header = [norm(c) for c in rows[0]]
    idx = {"last": 0, "first": 1, "number": 2}
    full_col = None
    has_header = False
    for i, h in enumerate(header):
        if h in _LAST:
            idx["last"] = i; has_header = True
        elif h in _FIRST:
            idx["first"] = i; has_header = True
        elif h in _NUM:
            idx["number"] = i; has_header = True
        elif h in _FULL:
            full_col = i; has_header = True
    found = _found(header)
    out = []
    for r in (rows[1:] if has_header else rows):
        def col(i):
            return r[i].strip() if i is not None and i < len(r) else ""
        if full_col is not None and not ({"last", "first"} & found):
            # une seule colonne « Nom et prénom »
            last, first, number = split_name_line(col(full_col))
            if "number" in found:
                number = col(idx["number"]) or number
        elif not has_header and len([c for c in r if c]) == 1:
            last, first, number = split_name_line(next(c for c in r if c))
        else:
            last, first, number = col(idx["last"]), col(idx["first"]), col(idx["number"])
        if last or first or number:
            out.append((last, first, number))
    return out


def _found(header):
    found = set()
    for h in header:
        if h in _LAST:
            found.add("last")
        elif h in _FIRST:
            found.add("first")
        elif h in _NUM:
            found.add("number")
    return found


def parse_candidates_file(name, data):
    """Fichier Excel / CSV / Word / PDF / texte -> [(nom, prénom, numéro)]."""
    ext = _ext(name)
    if ext in (".xlsx", ".xlsm"):
        return rows_to_candidates(_xlsx_rows(data))
    if ext == ".csv":
        return rows_to_candidates(_csv_rows(data))
    if ext == ".docx":
        blocks = _docx_blocks(data)
        table = [b for b in blocks if isinstance(b, list) and any(b)]
        if table:
            return rows_to_candidates(table)
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
    return [r for r in rows if r[0] or r[1]]


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


def parse_questions_text(lines):
    """Texte libre -> (questions, erreurs). Voir le format en tête de module."""
    items, cur = [], None

    def close():
        if cur is not None:
            items.append(cur)

    for raw in lines:
        line = raw.strip()
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
        if it.get("answer"):
            correct = _answer_index(it["answer"], choices)
        if len(choices) < 2:
            errors.append(f"{label} : au moins deux choix sont nécessaires.")
            continue
        if len(choices) > MAX_CHOICES:
            errors.append(f"{label} : maximum {MAX_CHOICES} choix.")
            continue
        if correct is None or not (0 <= correct < len(choices)):
            errors.append(f"{label} : bonne réponse absente ou invalide "
                          "(marquez-la d'un * ou ajoutez « Réponse : B »).")
            continue
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
        correct = _answer_index(col("answer"), choices)
        # sans texte de choix (grille de réponses seule) : le nombre de cases
        # suit le réglage du quiz, vérifié à l'enregistrement
        n_choices = len(choices) or MAX_CHOICES
        if len(choices) == 1:
            errors.append(f"{label} : au moins deux choix sont nécessaires.")
            continue
        if correct is None or not (0 <= correct < n_choices):
            errors.append(f"{label} : bonne réponse absente ou invalide "
                          f"« {col('answer')} ».")
            continue
        questions.append({"qtype": "qcm", "text": text[:500], "choices": choices,
                          "correct": correct, "points": points,
                          "open_height_mm": 25})
    return questions, errors


def parse_questions_file(name, data):
    """Fichier Excel / CSV / Word / PDF / texte -> (questions, erreurs)."""
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
        return parse_questions_text(_pdf_lines(data))
    if ext == ".txt":
        return parse_questions_text(_text_lines(data))
    raise ImportError_("Format non pris en charge : utilisez un fichier "
                       "Excel, CSV, Word (.docx), PDF ou texte.")
