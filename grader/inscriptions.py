"""Registre des inscriptions : la liste de référence des étudiants de
l'établissement, chacun avec son n° d'inscription et son QR code.

Trois usages :
  - inscrire un étudiant (saisie ou fichier) et lui remettre sa carte
    d'inscription, dont le QR porte « QSI|<n°>|<NOM>|<Prénom> » ;
  - vérifier une liste de candidats importée (Excel…) contre le registre :
    même n°, même nom ? n° inconnu ? ;
  - ajouter directement des inscrits à un concours ou à une classe.

Le QR de la carte est aussi compris au scan des copies : collé dans
l'emplacement de l'étiquette, il identifie l'étudiant par son n°.
"""
import io
import re

from django.db import transaction
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from . import qr as qr_mod
from .fonts import has_arabic, load_fonts
from .labels_pdf import _draw_text, _ellipsize
from .models import Inscrit, Student
from .omr import _forme

PREFIXE_QR = "QSI"


def numero_propre(numero):
    """N° d'inscription tel qu'on le compare : sans espaces autour ni dedans
    (« 104 523 » saisi au clavier = « 104523 » de la grille)."""
    return re.sub(r"\s+", "", str(numero or ""))[:40]


def meme_nom(nom_a, prenom_a, nom_b, prenom_b):
    """Même personne, aux majuscules, accents et espaces près."""
    return (_forme(f"{nom_a} {prenom_a}") == _forme(f"{nom_b} {prenom_b}")
            or _forme(f"{nom_a} {prenom_a}") == _forme(f"{prenom_b} {nom_b}"))


def charge_qr(inscrit):
    """Contenu du QR de la carte : n°, NOM et prénom, lisibles par tout
    lecteur de QR (le « | » est réservé comme séparateur)."""
    def net(t):
        return str(t or "").replace("|", " ").strip()
    return f"{PREFIXE_QR}|{net(inscrit.numero)}|{net(inscrit.nom)}|{net(inscrit.prenom)}"


def lire_charge_qr(texte):
    """« QSI|n°|NOM|Prénom » -> n° d'inscription, ou None."""
    parties = (texte or "").split("|")
    if len(parties) >= 2 and parties[0] == PREFIXE_QR and parties[1].strip():
        return numero_propre(parties[1])
    return None


def qr_png(inscrit, **kw):
    return qr_mod.qr_png(charge_qr(inscrit), **kw)


def rattacher_existants(inscrit):
    """Relie au registre les étudiants déjà présents dans des classes ou
    concours sous le même n°."""
    return (Student.objects.filter(student_number=inscrit.numero, inscrit=None)
            .update(inscrit=inscrit))


# ------------------------------------------------------------ registre

@transaction.atomic
def importer(lignes, utilisateur=None):
    """Inscrit les lignes (nom, prénom, n°) d'un fichier ou d'un collage.

    Un n° déjà inscrit n'est jamais écrasé : s'il porte le même nom, la
    ligne est « déjà inscrite » ; sinon c'est un conflit, signalé pour que
    quelqu'un tranche. Retourne un rapport (listes de dicts)."""
    rapport = {"crees": [], "deja": [], "conflits": [], "sans_numero": []}
    vus = {}
    for nom, prenom, numero in lignes:
        nom, prenom = (nom or "").strip().upper()[:80], (prenom or "").strip()[:80]
        numero = numero_propre(numero)
        ligne = {"nom": nom, "prenom": prenom, "numero": numero}
        if not numero:
            rapport["sans_numero"].append(ligne)
            continue
        existant = vus.get(numero) or Inscrit.objects.filter(numero=numero).first()
        if existant is not None:
            if meme_nom(nom, prenom, existant.nom, existant.prenom):
                rapport["deja"].append(ligne)
            else:
                ligne["inscrit"] = existant.nom_complet
                rapport["conflits"].append(ligne)
            continue
        inscrit = Inscrit.objects.create(numero=numero, nom=nom, prenom=prenom,
                                         cree_par=utilisateur)
        rattacher_existants(inscrit)
        vus[numero] = inscrit
        rapport["crees"].append(ligne)
    return rapport


def chercher(texte):
    """N° séparés par des espaces, virgules, points-virgules ou retours à la
    ligne -> (inscrits trouvés, n° inconnus), dans l'ordre de saisie."""
    numeros = [n for n in (numero_propre(t) for t in re.split(r"[\s,;]+", texte or ""))
               if n]
    connus = {i.numero: i for i in Inscrit.objects.filter(numero__in=numeros)}
    trouves, inconnus = [], []
    for n in dict.fromkeys(numeros):
        (trouves.append(connus[n]) if n in connus else inconnus.append(n))
    return trouves, inconnus


@transaction.atomic
def ajouter_a_classe(groupe, inscrits):
    """Ajoute des inscrits à une classe ou aux candidats d'un concours, sous
    leur identité du registre. Un étudiant déjà présent avec le même n° n'est
    pas recréé (il est seulement relié au registre). Retourne (ajoutés, déjà)."""
    ajoutes = deja = 0
    for i in inscrits:
        present = groupe.students.filter(student_number=i.numero).first()
        if present is not None:
            if present.inscrit_id != i.pk:
                present.inscrit = i
                present.save(update_fields=["inscrit"])
            deja += 1
            continue
        Student.objects.create(class_group=groupe, last_name=i.nom,
                               first_name=i.prenom, student_number=i.numero,
                               inscrit=i)
        ajoutes += 1
    return ajoutes, deja


def filtrer_inscrits(qs, recherche):
    """Chaque mot doit se trouver dans le nom, le prénom ou le n°."""
    from django.db.models import Q
    for mot in (recherche or "").split():
        qs = qs.filter(Q(nom__icontains=mot) | Q(prenom__icontains=mot)
                       | Q(numero__icontains=mot))
    return qs


def filtrer_etudiants(qs, recherche):
    """Même filtre, sur les étudiants d'une classe ou d'un concours."""
    from django.db.models import Q
    for mot in (recherche or "").split():
        qs = qs.filter(Q(last_name__icontains=mot) | Q(first_name__icontains=mot)
                       | Q(student_number__icontains=mot))
    return qs


@transaction.atomic
def copier_etudiants(groupe, etudiants):
    """Ajoute à une classe ou à un concours les étudiants d'un autre groupe
    (même nom, prénom, n° et fiche du registre). Déjà présent — même n°, ou
    même nom et prénom quand il n'y a pas de n° — : pas recréé.
    Retourne (ajoutés, déjà présents)."""
    ajoutes = deja = 0
    for e in etudiants:
        numero = (e.student_number or "").strip()
        present = (groupe.students.filter(student_number=numero) if numero
                   else groupe.students.filter(last_name=e.last_name,
                                               first_name=e.first_name)).exists()
        if present:
            deja += 1
            continue
        Student.objects.create(class_group=groupe, last_name=e.last_name,
                               first_name=e.first_name, student_number=numero,
                               inscrit_id=e.inscrit_id)
        ajoutes += 1
    return ajoutes, deja


# ------------------------------------------- vérification d'une liste

def verifier(lignes):
    """Confronte une liste de candidats (nom, prénom, n°) au registre.

    Chaque ligne reçoit un état :
      - « conforme »     : n° inscrit, même nom ;
      - « nom_different » : n° inscrit sous un autre nom — l'identité du
                            registre est retenue, la ligne est signalée ;
      - « non_inscrit »  : n° absent du registre ;
      - « sans_numero »  : pas de n° — impossible à vérifier.
    Retourne (lignes enrichies, décomptes)."""
    numeros = {numero_propre(n) for _, _, n in lignes} - {""}
    registre = {i.numero: i for i in Inscrit.objects.filter(numero__in=numeros)}
    resultat = []
    for nom, prenom, numero in lignes:
        numero = numero_propre(numero)
        inscrit = registre.get(numero) if numero else None
        if not numero:
            etat = "sans_numero"
        elif inscrit is None:
            etat = "non_inscrit"
        elif meme_nom(nom, prenom, inscrit.nom, inscrit.prenom):
            etat = "conforme"
        else:
            etat = "nom_different"
        resultat.append({"nom": (nom or "").strip(), "prenom": (prenom or "").strip(),
                         "numero": numero, "etat": etat, "inscrit": inscrit})
    decomptes = {e: sum(1 for r in resultat if r["etat"] == e)
                 for e in ("conforme", "nom_different", "non_inscrit", "sans_numero")}
    return resultat, decomptes


def rapport_session(resultat, decomptes):
    """Version enregistrable en session (sans objets) des anomalies."""
    anomalies = [{"nom": r["nom"], "prenom": r["prenom"], "numero": r["numero"],
                  "etat": r["etat"],
                  "registre": r["inscrit"].nom_complet if r["inscrit"] else ""}
                 for r in resultat if r["etat"] != "conforme"]
    return {"decomptes": decomptes, "anomalies": anomalies[:500],
            "n_anomalies": len(anomalies), "total": len(resultat)}


# ------------------------------------------------ cartes d'inscription

CARTE_W, CARTE_H = 85.6, 54.0          # format carte bancaire (mm)
CARTE_COLS, CARTE_ROWS = 2, 5
CARTE_MARGE_X = (210 - CARTE_COLS * CARTE_W) / 2
CARTE_MARGE_Y = (297 - CARTE_ROWS * CARTE_H) / 2
CARTE_QR = 30.0


def cartes_pdf(inscrits, etablissement=""):
    """Planche A4 de cartes d'inscription (10 par page) : NOM, prénom,
    n° et QR code. À découper ; chaque étudiant garde la sienne."""
    load_fonts()
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Cartes d'inscription")
    page_h = 297.0

    def y(mm_haut):
        return (page_h - mm_haut) * mm

    par_page = CARTE_COLS * CARTE_ROWS
    for n, i in enumerate(inscrits):
        case = n % par_page
        if n and case == 0:
            c.showPage()
        x0 = CARTE_MARGE_X + (case % CARTE_COLS) * CARTE_W
        y0 = CARTE_MARGE_Y + (case // CARTE_COLS) * CARTE_H
        # bord de découpe
        c.setStrokeGray(0.7)
        c.setLineWidth(0.4)
        c.setDash(2, 2)
        c.rect(x0 * mm, y(y0 + CARTE_H), CARTE_W * mm, CARTE_H * mm)
        c.setDash()
        # bandeau
        c.setFillColorRGB(0.12, 0.42, 0.27)
        c.rect((x0 + 3) * mm, y(y0 + 11), (CARTE_W - 6) * mm, 8 * mm, stroke=0, fill=1)
        c.setFillGray(1.0)
        _draw_text(c, _ellipsize(etablissement or "Carte d'inscription", 8.5,
                                 CARTE_W - 12, bold=True),
                   (x0 + 6) * mm, y(y0 + 8.2), 8.5, bold=True)
        c.setFillGray(0.0)
        # QR à droite
        qx = x0 + CARTE_W - 4 - CARTE_QR
        qy = y0 + 14
        image = ImageReader(Image.open(io.BytesIO(qr_png(i, box_size=8, border=1))))
        c.drawImage(image, qx * mm, y(qy + CARTE_QR), CARTE_QR * mm, CARTE_QR * mm)
        # identité à gauche
        tx, largeur = x0 + 5, qx - x0 - 8
        rtl = has_arabic(f"{i.nom} {i.prenom}")
        ax, align = ((qx - 3), "right") if rtl else (tx, "left")
        c.setFillGray(0.45)
        _draw_text(c, "N° d'inscription", tx * mm, y(y0 + 19), 7)
        c.setFillGray(0.0)
        _draw_text(c, _ellipsize(i.numero, 13, largeur, bold=True),
                   tx * mm, y(y0 + 25.5), 13, bold=True)
        _draw_text(c, _ellipsize(i.nom.upper(), 11, largeur, bold=True),
                   ax * mm, y(y0 + 34), 11, bold=True, align=align)
        _draw_text(c, _ellipsize(i.prenom, 10.5, largeur),
                   ax * mm, y(y0 + 40.5), 10.5, align=align)
        c.setFillGray(0.5)
        _draw_text(c, "Résultats : espace public, avec ce n°", tx * mm,
                   y(y0 + CARTE_H - 4.5), 6.5)
        c.setFillGray(0.0)
    c.showPage()
    c.save()
    return buf.getvalue()
