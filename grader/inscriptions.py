"""Registre des inscriptions : la liste de référence des étudiants de
l'établissement, chacun avec son n° d'inscription et son QR code.

Trois usages :
  - inscrire un étudiant (fiche complète) et lui remettre sa carte
    d'inscription, dont le QR porte « QSI|<n°>|<NOM>|<Prénom> » ;
  - vérifier une liste de candidats importée (Excel…) contre le registre :
    même n°, même nom ? n° inconnu ? ;
  - ajouter directement des inscrits à un concours ou à une classe.

Le QR de la carte est aussi compris au scan des copies : collé dans
l'emplacement de l'étiquette, il identifie l'étudiant par son n°.
"""
import datetime
import io
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from . import journal
from . import qr as qr_mod
from .fonts import has_arabic, load_fonts
from .importers import _EXTRAS as COLONNES_FICHE
from .importers import chiffres_latins, lire_date
from .labels_pdf import _draw_text, _ellipsize
from .models import Inscrit, Student
from .omr import _forme

PREFIXE_QR = "QSI"


def numero_propre(numero):
    """N° d'inscription tel qu'on le compare : sans espaces autour ni dedans
    (« 104 523 » saisi au clavier = « 104523 » de la grille)."""
    return re.sub(r"\s+", "", chiffres_latins(numero))[:40]


def cin_propre(valeur):
    """CIN telle qu'on la compare : sans espaces, en capitales. Excel efface le
    zéro de tête d'un n° saisi comme nombre (09876543 s'y lit 9876543) : on le
    rend, une CIN compte 8 chiffres."""
    cin = "".join(chiffres_latins(valeur).split()).upper()
    return cin.zfill(8) if cin.isdigit() and 5 <= len(cin) < 8 else cin


def cin_valide(cin):
    """8 chiffres, ou un n° de passeport : lettres et chiffres."""
    return cin.isalnum() and 6 <= len(cin) <= 20


def meme_nom(nom_a, prenom_a, nom_b, prenom_b):
    """Même personne, aux majuscules, accents et espaces près, dans un ordre
    ou l'autre : un nom découpé de travers (« علي بن سالم », « محمد ») reste le
    même une fois remis bout à bout."""
    return bool({_forme(f"{nom_a} {prenom_a}"), _forme(f"{prenom_a} {nom_a}")}
                & {_forme(f"{nom_b} {prenom_b}"), _forme(f"{prenom_b} {nom_b}")})


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


def rattacher_existants(inscrit, groupes):
    """Relie au registre les étudiants déjà présents sous le même n° dans
    les classes ou concours donnés — ceux de l'utilisateur : inscrire un n°
    ne doit pas permettre de s'emparer des candidats d'un collègue (la
    fiche modifiée renommerait ensuite ses étudiants)."""
    return (Student.objects.filter(student_number=inscrit.numero, inscrit=None,
                                   class_group__in=groupes)
            .update(inscrit=inscrit))


# ------------------------------------------------------------ registre

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
    """Chaque mot doit se trouver dans le nom, le prénom, le n°, la CIN, le
    centre, la salle, l'établissement ou l'adresse e-mail."""
    from django.db.models import Q
    for mot in (recherche or "").split():
        qs = qs.filter(Q(nom__icontains=mot) | Q(prenom__icontains=mot)
                       | Q(numero__icontains=mot) | Q(cin__icontains=mot)
                       | Q(centre__icontains=mot) | Q(salle__icontains=mot)
                       | Q(etablissement__icontains=mot) | Q(email__icontains=mot))
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


# --------------------------------------------- une liste face au registre

ETATS_LIGNE = ("conforme", "ajoute", "nom_different", "non_inscrit", "sans_numero",
               "doublon")
# La fiche se complète sans écraser ce qui y figure, sauf là où on compose :
# chaque nouvelle liste apporte le centre et la salle de la session.
REMPLACES_PAR_LA_LISTE = ("centre", "salle")


def _porte_une_fiche(ligne):
    """La ligne dit plus que le nom et le n° ? Une liste simple (nom, prénom, n°)
    n'inscrit personne (demande du 30 octobre 2026) ; la liste officielle, avec
    la CIN, la date de naissance ou la salle, remplit le registre (5 novembre)."""
    return any(ligne.get(cle) for cle in COLONNES_FICHE)


def _date_lue(ligne, cle, libelle, remarques):
    brut = ligne.get(cle) or ""
    date = lire_date(brut)
    if brut and (date is None or not datetime.date(1900, 1, 1) <= date <= datetime.date.today()):
        remarques.append(f"{libelle} illisible : « {brut[:30]} » non retenue.")
        return None
    return date


def _valeurs_lues(ligne, remarques):
    """Colonnes d'une ligne -> champs de la fiche. Une valeur illisible n'empêche
    pas le reste de la ligne : elle est écartée et signalée."""
    def texte(cle, longueur):
        return " ".join((ligne.get(cle) or "").split())[:longueur]

    cin = cin_propre(ligne.get("cin"))
    if cin and not cin_valide(cin):
        remarques.append(f"CIN invalide : « {cin[:30]} » non retenue.")
        cin = ""
    email = texte("email", 254)
    if email:
        try:
            validate_email(email)
        except ValidationError:
            remarques.append(f"E-mail invalide : « {email[:60]} » non retenu.")
            email = ""
    return {"cin": cin, "cin_lieu": texte("cin_lieu", 80),
            "cin_date": _date_lue(ligne, "cin_date", "Date de délivrance", remarques),
            "date_naissance": _date_lue(ligne, "naissance", "Date de naissance", remarques),
            "centre": texte("centre", 150), "salle": texte("salle", 60),
            "etablissement": texte("etablissement", 150), "email": email}


def _cin_libre(valeurs, numero, proprietaires, remarques):
    """Retire de la ligne une CIN déjà celle d'une autre fiche : une CIN
    n'appartient qu'à une personne, le formulaire le refuse aussi."""
    autre = proprietaires.get(valeurs["cin"]) if valeurs["cin"] else None
    if autre is not None and autre.numero != numero:
        remarques.append(f"CIN {valeurs['cin']} déjà celle de {autre.nom_complet} "
                         f"(n° {autre.numero}) : non retenue.")
        valeurs["cin"] = ""


def _completer(inscrit, valeurs, sortie, utilisateur, remarques):
    """Ajoute à une fiche existante ce que la liste apporte. Retourne les champs
    changés. Seuls son auteur et l'administrateur la modifient, comme à la main."""
    nom, prenom = sortie["nom"], sortie["prenom"]
    apports = {champ: valeur for champ, valeur in valeurs.items() if valeur}
    if not inscrit.nom_complet and (nom or prenom):
        apports.update(nom=nom.upper()[:80], prenom=prenom[:80])
    changes = {champ: valeur for champ, valeur in apports.items()
               if valeur != getattr(inscrit, champ)}
    if not changes:
        return []
    if not (utilisateur.is_superuser or inscrit.cree_par_id == utilisateur.pk):
        remarques.append("Fiche inscrite par un collègue : elle n'est pas modifiée.")
        return []
    retenus = []
    for champ, valeur in changes.items():
        actuel = getattr(inscrit, champ)
        if champ in REMPLACES_PAR_LA_LISTE or not actuel:
            setattr(inscrit, champ, valeur)
            retenus.append(champ)
        elif champ == "cin":
            remarques.append(f"Autre CIN au registre ({actuel}) : celle du registre est gardée.")
        elif champ == "date_naissance":
            remarques.append(f"Autre date de naissance au registre ({actuel:%d/%m/%Y}) : "
                             "celle du registre est gardée.")
    return retenus


@transaction.atomic
def rapprocher(lignes, utilisateur):
    """Confronte une liste au registre et complète celui-ci, sans une requête
    par ligne (20 000 lignes possibles). Chaque ligne reçoit un état :
      - « conforme »     : n° inscrit, même nom ;
      - « ajoute »       : n° absent, la ligne porte une fiche : inscrit ;
      - « nom_different » : n° inscrit sous un autre nom — l'identité du
                            registre est retenue, la ligne est signalée ;
      - « non_inscrit »  : n° absent, liste simple : personne n'est inscrit ;
      - « sans_numero »  : pas de n° — impossible à vérifier ;
      - « doublon »      : n° déjà lu plus haut dans le fichier : ligne écartée.
    et des remarques sur ce qui en a été écarté. Retourne (lignes enrichies,
    décomptes, fiches créées)."""
    numeros = {numero_propre(l["numero"]) for l in lignes} - {""}
    registre = {i.numero: i for i in Inscrit.objects.filter(numero__in=numeros)}
    cins = {cin_propre(l.get("cin")) for l in lignes} - {""}
    proprietaires = {i.cin: i for i in Inscrit.objects.filter(cin__in=cins)}
    proprietaires.update({i.cin: i for i in registre.values() if i.cin})

    resultat, vus, a_creer, modifiees, renommees = [], set(), [], {}, []
    for ligne in lignes:
        numero = numero_propre(ligne["numero"])
        nom = " ".join((ligne["nom"] or "").split())
        prenom = " ".join((ligne["prenom"] or "").split())
        sortie = {"nom": nom, "prenom": prenom, "numero": numero, "inscrit": None,
                  "etat": "sans_numero", "remarques": [], "avec_fiche": False}
        resultat.append(sortie)
        remarques = sortie["remarques"]
        if not numero:
            continue
        if numero in vus:
            sortie["etat"] = "doublon"
            remarques.append("N° déjà lu plus haut dans le fichier : ligne écartée.")
            continue
        vus.add(numero)
        fiche = sortie["avec_fiche"] = _porte_une_fiche(ligne)
        valeurs = _valeurs_lues(ligne, remarques) if fiche else {}
        if fiche:
            _cin_libre(valeurs, numero, proprietaires, remarques)
        inscrit = registre.get(numero)
        if inscrit is not None:
            if (not inscrit.nom_complet or not (nom or prenom)
                    or meme_nom(nom, prenom, inscrit.nom, inscrit.prenom)):
                sortie["etat"] = "conforme"
            else:
                sortie["etat"] = "nom_different"
            champs = _completer(inscrit, valeurs, sortie, utilisateur, remarques)
            if champs:
                modifiees.setdefault(inscrit.pk, (inscrit, set()))[1].update(champs)
                if "cin" in champs:
                    proprietaires[inscrit.cin] = inscrit
                if "nom" in champs:
                    renommees.append(inscrit)
        elif fiche:
            inscrit = Inscrit(numero=numero, nom=nom.upper()[:80], prenom=prenom[:80],
                              cree_par=utilisateur, **valeurs)
            a_creer.append(inscrit)
            registre[numero] = inscrit
            if inscrit.cin:
                proprietaires[inscrit.cin] = inscrit
            sortie["etat"] = "ajoute"
        else:
            sortie["etat"] = "non_inscrit"
        sortie["inscrit"] = inscrit
        if not (nom or prenom) and not (inscrit and inscrit.nom_complet):
            remarques.append("Nom manquant dans le fichier.")

    Inscrit.objects.bulk_create(a_creer, batch_size=1000)
    for champs in {frozenset(c) for _, c in modifiees.values()}:
        Inscrit.objects.bulk_update(
            [i for i, c in modifiees.values() if frozenset(c) == champs],
            list(champs), batch_size=1000)
    for inscrit in renommees:      # le registre fait foi : les listes reliées suivent
        for etudiant in Student.objects.filter(inscrit=inscrit):
            avant = journal.identite(etudiant)
            etudiant.last_name, etudiant.first_name = inscrit.nom, inscrit.prenom
            etudiant.save(update_fields=["last_name", "first_name"])
            journal.etudiant_modifie(utilisateur, etudiant, avant)

    decomptes = {e: sum(1 for r in resultat if r["etat"] == e) for e in ETATS_LIGNE}
    # Une liste simple ne dit rien des fiches anciennes : elle n'en signale aucune.
    decomptes["a_completer"] = sum(
        1 for r in resultat if r["avec_fiche"] and not r["inscrit"].fiche_complete)
    return resultat, decomptes, a_creer


def rattacher_nouvelles(inscrits, groupes):
    """Comme rattacher_existants, pour toutes les fiches d'un import."""
    par_numero = {i.numero: i for i in inscrits}
    etudiants = list(Student.objects.filter(student_number__in=list(par_numero), inscrit=None,
                                            class_group__in=groupes))
    for etudiant in etudiants:
        etudiant.inscrit = par_numero[etudiant.student_number]
    Student.objects.bulk_update(etudiants, ["inscrit"], batch_size=1000)
    return len(etudiants)


def rapport_session(resultat, decomptes):
    """Version enregistrable en session (sans objets) des anomalies."""
    anomalies = [{"nom": r["nom"], "prenom": r["prenom"], "numero": r["numero"],
                  "etat": r["etat"], "remarques": r["remarques"],
                  "registre": r["inscrit"].nom_complet if r["inscrit"] else ""}
                 for r in resultat if r["etat"] not in ("conforme", "ajoute") or r["remarques"]]
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
        bandeau = (f"Carte d'inscription — {i.etablissement}" if i.etablissement
                   else etablissement or "Carte d'inscription")
        _draw_text(c, _ellipsize(bandeau, 8.5, CARTE_W - 12, bold=True),
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
        # le prénom d'abord, puis le NOM (demande de l'utilisateur, 7 novembre 2026)
        _draw_text(c, _ellipsize(i.prenom, 10.5, largeur),
                   ax * mm, y(y0 + 34), 10.5, align=align)
        _draw_text(c, _ellipsize(i.nom.upper(), 11, largeur, bold=True),
                   ax * mm, y(y0 + 40.5), 11, bold=True, align=align)
        identite = " · ".join(t for t in (
            f"CIN {i.cin}" if i.cin else "",
            f"né(e) le {i.date_naissance:%d/%m/%Y}" if i.date_naissance else "") if t)
        if identite:
            c.setFillGray(0.3)
            _draw_text(c, _ellipsize(identite, 7.5, largeur), tx * mm, y(y0 + 45.5), 7.5)
            c.setFillGray(0.0)
        c.setFillGray(0.5)
        _draw_text(c, "Résultats : espace public, avec ce n°", tx * mm,
                   y(y0 + CARTE_H - 4.5), 6.5)
        c.setFillGray(0.0)
    c.showPage()
    c.save()
    return buf.getvalue()
