"""Pièces jointes des communiqués : images, vidéos et documents.

Liste fermée de types, reconnus à l'extension (jamais au type annoncé par le
navigateur) : pas de HTML ni de SVG, qui exécuteraient du script sur le site.
Les fichiers ne passent pas par /media/ (réservé au personnel, par épreuve) :
views_public.piece_jointe les sert, au public dès que le communiqué est
publié. Les vidéos se lisent par morceaux (en-tête Range) : sans cela,
Safari ne les lit pas et on ne peut pas avancer dans la lecture.
"""
import os

from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header

IMAGE, VIDEO, DOCUMENT = "image", "video", "document"
MO = 1024 * 1024

# extension -> (genre, type MIME envoyé au navigateur)
TYPES = {
    ".jpg": (IMAGE, "image/jpeg"), ".jpeg": (IMAGE, "image/jpeg"),
    ".png": (IMAGE, "image/png"), ".gif": (IMAGE, "image/gif"),
    ".webp": (IMAGE, "image/webp"),
    ".mp4": (VIDEO, "video/mp4"), ".m4v": (VIDEO, "video/mp4"),
    ".webm": (VIDEO, "video/webm"), ".mov": (VIDEO, "video/quicktime"),
    ".pdf": (DOCUMENT, "application/pdf"),
    ".doc": (DOCUMENT, "application/msword"),
    ".docx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xls": (DOCUMENT, "application/vnd.ms-excel"),
    ".xlsx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".ppt": (DOCUMENT, "application/vnd.ms-powerpoint"),
    ".pptx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".odt": (DOCUMENT, "application/vnd.oasis.opendocument.text"),
    ".ods": (DOCUMENT, "application/vnd.oasis.opendocument.spreadsheet"),
    ".odp": (DOCUMENT, "application/vnd.oasis.opendocument.presentation"),
    ".txt": (DOCUMENT, "text/plain; charset=utf-8"),
    ".csv": (DOCUMENT, "text/csv; charset=utf-8"),
    ".zip": (DOCUMENT, "application/zip"),
}
TAILLE_MAX = {IMAGE: 10 * MO, VIDEO: 100 * MO, DOCUMENT: 25 * MO}
NOMS = {IMAGE: "une image", VIDEO: "une vidéo", DOCUMENT: "un document"}
MAX_PAR_COMMUNIQUE = 10
# ce que le sélecteur de fichiers propose d'emblée
ACCEPT = ",".join(sorted(TYPES))
# affichés dans la page plutôt que téléchargés
EN_LIGNE = {IMAGE, VIDEO}


def type_de(nom):
    """(genre, type MIME) d'après l'extension ; None si le type est refusé."""
    return TYPES.get(os.path.splitext(nom or "")[1].lower())


def controler(fichier):
    """Message d'erreur (str) si le fichier envoyé est refusé, sinon None."""
    connu = type_de(fichier.name)
    if connu is None:
        return (f"« {fichier.name} » : type de fichier non accepté (images JPG, "
                f"PNG, GIF, WebP ; vidéos MP4, WebM, MOV ; documents PDF, Word, "
                f"Excel, PowerPoint, OpenDocument, texte, ZIP).")
    genre = connu[0]
    if fichier.size > TAILLE_MAX[genre]:
        return (f"« {fichier.name} » : {fichier.size / MO:.1f} Mo, au-delà des "
                f"{TAILLE_MAX[genre] // MO} Mo permis pour {NOMS[genre]}.")
    if genre == IMAGE:
        from PIL import Image
        try:
            with Image.open(fichier) as img:
                img.verify()
        except Exception:
            return f"« {fichier.name} » n'est pas une image lisible."
        finally:
            fichier.seek(0)
    return None


# ------------------------------------------------------------ envoi

def _plage(entete, taille):
    """En-tête « Range: bytes=début-fin » -> (début, fin) inclus ; None
    s'il est absent ou multiple (fichier entier) ; False si hors du fichier."""
    if not entete.startswith("bytes=") or "," in entete:
        return None
    debut, _, fin = entete[6:].strip().partition("-")
    try:
        if debut == "":                       # « bytes=-500 » : les 500 derniers
            n = int(fin)
            if n <= 0:
                return False
            return max(taille - n, 0), taille - 1
        debut = int(debut)
        fin = int(fin) if fin else taille - 1
    except ValueError:
        return None
    if debut >= taille or fin < debut:
        return False
    return debut, min(fin, taille - 1)


def _morceaux(f, reste, bloc=256 * 1024):
    try:
        while reste > 0:
            donnees = f.read(min(bloc, reste))
            if not donnees:
                break
            reste -= len(donnees)
            yield donnees
    finally:
        f.close()


def servir(request, piece):
    """Réponse HTTP du fichier d'une pièce jointe : affiché dans la page
    (image, vidéo, PDF) ou téléchargé (autres documents), par morceaux si
    le navigateur le demande."""
    genre, mime = type_de(piece.nom) or (DOCUMENT, "application/octet-stream")
    try:
        f = piece.fichier.open("rb")
        taille = piece.fichier.size
    except (FileNotFoundError, OSError, ValueError):
        raise Http404("Fichier introuvable")
    en_ligne = genre in EN_LIGNE or mime == "application/pdf"
    plage = _plage(request.META.get("HTTP_RANGE", ""), taille)
    if plage is False:
        f.close()
        resp = HttpResponse(status=416)
        resp["Content-Range"] = f"bytes */{taille}"
        return resp
    if plage:
        debut, fin = plage
        f.seek(debut)
        resp = StreamingHttpResponse(_morceaux(f, fin - debut + 1), status=206,
                                     content_type=mime)
        resp["Content-Range"] = f"bytes {debut}-{fin}/{taille}"
        resp["Content-Length"] = str(fin - debut + 1)
        resp["Content-Disposition"] = content_disposition_header(
            not en_ligne, piece.nom)
    else:
        resp = FileResponse(f, content_type=mime, as_attachment=not en_ligne,
                            filename=piece.nom)
    resp["Accept-Ranges"] = "bytes"
    resp["X-Content-Type-Options"] = "nosniff"
    # court : un communiqué dépublié ne doit pas rester longtemps en cache
    resp["Cache-Control"] = "max-age=300"
    return resp
