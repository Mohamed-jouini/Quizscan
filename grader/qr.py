"""Génération des QR codes des candidats (étiquettes de concours).

Le QR d'un candidat encode son identité au format « QS|<groupe>|<id> ».
Ce format est déjà compris par le lecteur de scan (grader.omr.read_qr /
grader.services) qui retrouve le candidat par son identifiant — la colonne
QR et la planche d'étiquettes n'ont donc aucune incidence sur les feuilles
de questions / réponses, ni sur le pipeline de correction.
"""
import io
import secrets

import qrcode

# Code d'anonymat d'une copie à talon (demande du 13 novembre 2026) : imprimé sur le
# talon et sous le QR de la copie, il rapproche à l'œil le talon détaché de sa copie.
# Ni le n° d'inscription ni la CIN : la copie corrigée le montre, l'anonymat tomberait.
# Tiré au hasard (ni l'ordre de la liste ni la salle ne s'y lisent), sans caractères
# que l'on confond à la lecture ou à la main : 0/O, 1/I/L, 2/Z, 5/S, 6/G, 8/B.
ALPHABET_CODE = "ACDEFHJKMNPRTUVWXY3479"
LONGUEUR_CODE = 6


def nouveau_code(pris):
    """Un code qui n'est pas dans « pris » (22⁶ : 113 millions de codes)."""
    while True:
        code = "".join(secrets.choice(ALPHABET_CODE) for _ in range(LONGUEUR_CODE))
        if code not in pris:
            return code


def code_lisible(code):
    """« K7MR9Q » -> « K7M-R9Q » : en deux groupes, comme il s'imprime et se lit."""
    return f"{code[:3]}-{code[3:]}" if code and len(code) == LONGUEUR_CODE else (code or "")


def payload(student):
    """Charge utile du QR d'un candidat (identifiant stable, indépendant du
    quiz : le scan retrouve le candidat par son id dans le conteneur)."""
    return f"QS|{student.class_group_id}|{student.pk}"


def qr_png(data, box_size=6, border=2):
    """Retourne les octets PNG d'un QR code. Correction H (30 %) depuis le 9 novembre
    2026 : une étiquette tachée, rayée ou au coin arraché reste lisible."""
    qr = qrcode.QRCode(border=border, box_size=box_size,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf, format="PNG")
    return buf.getvalue()


def student_qr_png(student, **kw):
    return qr_png(payload(student), **kw)
