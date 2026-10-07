"""Génération des QR codes des candidats (étiquettes de concours).

Le QR d'un candidat encode son identité au format « QS|<groupe>|<id> ».
Ce format est déjà compris par le lecteur de scan (grader.omr.read_qr /
grader.services) qui retrouve le candidat par son identifiant — la colonne
QR et la planche d'étiquettes n'ont donc aucune incidence sur les feuilles
de questions / réponses, ni sur le pipeline de correction.
"""
import io

import qrcode


def payload(student):
    """Charge utile du QR d'un candidat (identifiant stable, indépendant du
    quiz : le scan retrouve le candidat par son id dans le conteneur)."""
    return f"QS|{student.class_group_id}|{student.pk}"


def qr_png(data, box_size=6, border=2):
    """Retourne les octets PNG d'un QR code."""
    qr = qrcode.QRCode(border=border, box_size=box_size,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf, format="PNG")
    return buf.getvalue()


def student_qr_png(student, **kw):
    return qr_png(payload(student), **kw)
