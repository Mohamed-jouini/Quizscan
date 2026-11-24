"""Catalogues de traduction (grader/locale/<langue>/LC_MESSAGES/django.po -> django.mo).

« manage.py compilemessages » de Django demande msgfmt (GNU gettext), absent des postes
Windows et de l'image : ce module lit le .po et écrit le .mo en Python, au format que lit le
module gettext de Python (le même que msgfmt, sans table de hachage). Le .mo est versionné :
rien à compiler au déploiement. Après chaque modification d'un .po :
    python manage.py traductions
test_e2e_langue.py vérifie que le .mo suit le .po.
"""
import array
import ast
import struct
import sys
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent / "locale"


def lire_po(texte):
    """Entrées d'un .po : [(contexte | None, msgid, msgid_plural | None, [msgstr…])], sans
    les entrées « fuzzy » (traduction à revoir). L'en-tête est l'entrée de msgid vide."""
    entrees = []
    courante, champ, fuzzy_suivante = None, None, False

    def clore():
        if courante is not None and courante["msgid"] is not None and not courante["fuzzy"]:
            strs = [courante["msgstr"][k] for k in sorted(courante["msgstr"])]
            entrees.append((courante["msgctxt"], courante["msgid"], courante["msgid_plural"],
                            strs))

    def nouvelle():
        nonlocal courante, fuzzy_suivante
        clore()
        courante = {"msgctxt": None, "msgid": None, "msgid_plural": None, "msgstr": {},
                    "fuzzy": fuzzy_suivante}
        fuzzy_suivante = False

    for numero, ligne in enumerate(texte.splitlines(), start=1):
        ligne = ligne.strip()
        if not ligne:
            continue
        if ligne.startswith("#"):
            if ligne.startswith("#,") and "fuzzy" in ligne:
                fuzzy_suivante = True
            continue
        if ligne.startswith('"'):
            mot, reste = None, ligne
        else:
            mot, _, reste = ligne.partition(" ")
        try:
            valeur = ast.literal_eval(reste.strip())
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"ligne {numero} du .po illisible : {ligne}") from exc
        if not isinstance(valeur, str):
            raise ValueError(f"ligne {numero} du .po : chaîne attendue")
        if mot is None:                         # suite d'une chaîne sur plusieurs lignes
            if courante is None or champ is None:
                raise ValueError(f"ligne {numero} du .po : suite sans début")
            if champ[0] == "msgstr":
                courante["msgstr"][champ[1]] += valeur
            else:
                courante[champ[0]] += valeur
        elif mot == "msgctxt":
            nouvelle()
            courante["msgctxt"], champ = valeur, ("msgctxt", None)
        elif mot == "msgid":
            if courante is None or courante["msgid"] is not None:
                nouvelle()
            courante["msgid"], champ = valeur, ("msgid", None)
        elif mot == "msgid_plural" and courante is not None:
            courante["msgid_plural"], champ = valeur, ("msgid_plural", None)
        elif mot == "msgstr" and courante is not None:
            courante["msgstr"][0], champ = valeur, ("msgstr", 0)
        elif mot.startswith("msgstr[") and mot.endswith("]") and courante is not None:
            k = int(mot[7:-1])
            courante["msgstr"][k], champ = valeur, ("msgstr", k)
        else:
            raise ValueError(f"ligne {numero} du .po : « {mot} » inattendu")
    clore()
    return entrees


def compiler(entrees):
    """Contenu du .mo. Une entrée sans traduction n'y entre pas : le texte d'origine (le
    français) s'affiche alors, comme avec msgfmt."""
    messages = {}
    for contexte, msgid, pluriel, strs in entrees:
        if msgid and not (strs and all(strs)):
            continue
        cle = msgid if pluriel is None else msgid + "\0" + pluriel
        if contexte is not None:
            cle = contexte + "\x04" + cle
        messages[cle.encode("utf-8")] = "\0".join(strs).encode("utf-8")
    cles = sorted(messages)
    ids = strs = b""
    positions = []
    for cle in cles:
        positions.append((len(ids), len(cle), len(strs), len(messages[cle])))
        ids += cle + b"\0"
        strs += messages[cle] + b"\0"
    debut_cles = 7 * 4 + 16 * len(cles)
    debut_valeurs = debut_cles + len(ids)
    index_cles, index_valeurs = [], []
    for o1, l1, o2, l2 in positions:
        index_cles += [l1, o1 + debut_cles]
        index_valeurs += [l2, o2 + debut_valeurs]
    entete = struct.pack("<Iiiiiii", 0x950412DE, 0, len(cles), 7 * 4, 7 * 4 + len(cles) * 8,
                         0, 0)
    index = array.array("i", index_cles + index_valeurs)
    if sys.byteorder != "little":
        index.byteswap()                        # le .mo est écrit en petit-boutiste
    return entete + index.tobytes() + ids + strs


def catalogues():
    """Les .po du projet, chacun avec son .mo."""
    return [(po, po.with_suffix(".mo"))
            for po in sorted(DOSSIER.glob("*/LC_MESSAGES/django.po"))]


def compiler_po(po):
    return compiler(lire_po(po.read_text(encoding="utf-8")))
