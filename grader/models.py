import logging

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.text import format_lazy, get_valid_filename
from django.utils.translation import gettext, gettext_lazy, pgettext_lazy

# Hauteur maximale d'une zone de réponse manuscrite (mm) : au-delà, le bloc
# ne tient plus dans la zone imprimable d'une page A4 (voir layout.py).
MAX_OPEN_HEIGHT_MM = 200

def nom_et_prenom(nom, prenom):
    """Nom complet affiché ou imprimé d'un seul tenant : le prénom d'abord, partout
    dans l'application (demande de l'utilisateur, 7 novembre 2026). C'est l'ordre de la
    liste officielle de l'institut, « الاسم واللقب » : « محمد أمين الجبالي », « Mokhtar
    ARBI ». Les tableaux mettent de même la colonne Prénom avant la colonne Nom."""
    return f"{(prenom or '').strip()} {(nom or '').strip()}".strip()


class ClassGroupQuerySet(models.QuerySet):
    """Les classes, et les listes de candidats des concours : les deux sont des ClassGroup.

    Chaque étape d'un concours reçoit à sa création sa propre liste de candidats
    (views.quiz_create) : ce n'est pas une classe. Demande de l'utilisateur du 23 novembre
    2026 (« lorsque fait un concours ne pas créer une classe ») : elle ne paraît ni ne compte
    parmi les classes (tableau de bord, épreuve de classe, administration). Une liste est
    celle d'un concours tant qu'une épreuve de concours (auto_enroll) s'y rattache."""

    def classes(self):
        return self.exclude(quizzes__auto_enroll=True)

    def de_concours(self):
        return self.filter(quizzes__auto_enroll=True).distinct()


class ClassGroup(models.Model):
    """Une classe / un groupe d'étudiants (ou la liste de candidats d'un concours)."""
    # SET_NULL et non CASCADE : quand un enseignant quitte l'établissement,
    # son compte est supprimé mais ses classes, ses épreuves et les notes de
    # ses élèves restent — ce sont les archives de l'établissement, pas sa
    # propriété. La classe devient sans titulaire et l'administration peut
    # la confier à quelqu'un d'autre depuis la fiche du nouveau compte.
    # (Avec CASCADE, Quiz.class_group étant protégé, la suppression d'un
    # enseignant ayant la moindre épreuve échouait purement et simplement.)
    # Qui a créé la classe : un repère, plus un droit d'accès. L'accès suit
    # « enseignants » ci-dessous — l'administration peut retirer une classe
    # à son créateur.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,
                              on_delete=models.SET_NULL,
                              null=True, related_name="class_groups",
                              verbose_name=gettext_lazy("créée par"))
    # Une classe peut être tenue par plusieurs enseignants, et un enseignant
    # tenir plusieurs classes. Ce sont eux qui la voient, ainsi que ses
    # épreuves, copies et résultats.
    enseignants = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="classes_attribuees", blank=True,
        verbose_name=gettext_lazy("enseignants"))
    name = models.CharField(gettext_lazy("nom de la classe"), max_length=120)
    created_at = models.DateTimeField(gettext_lazy("créée le"), auto_now_add=True)

    objects = ClassGroupQuerySet.as_manager()

    class Meta:
        verbose_name = gettext_lazy("classe")
        verbose_name_plural = gettext_lazy("classes")
        ordering = ["name"]
        unique_together = [("owner", "name")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Qui crée une classe en est d'emblée l'un des enseignants, quel que
        # soit le chemin (application, administration, import) ; ensuite
        # seule la liste « enseignants » compte, l'administration peut l'en
        # retirer.
        nouvelle = self._state.adding
        super().save(*args, **kwargs)
        if nouvelle and self.owner_id:
            self.enseignants.add(self.owner_id)


class Inscrit(models.Model):
    """Registre des inscriptions de l'établissement : une personne, un
    n° d'inscription, quelles que soient les classes ou concours où elle
    figure ensuite. C'est la référence contre laquelle une liste de
    candidats importée est vérifiée (même n°, même nom ?)."""
    numero = models.CharField(gettext_lazy("n° d'inscription"), max_length=40, unique=True)
    nom = models.CharField(pgettext_lazy("personne", "nom"), max_length=80)
    prenom = models.CharField(gettext_lazy("prénom"), max_length=80)
    # Identité complémentaire (facultative). La CIN n'est pas dans le QR de
    # la carte : donnée personnelle, elle reste dans l'application.
    cin = models.CharField(gettext_lazy("n° de CIN"), max_length=20, blank=True, db_index=True,
                           help_text=gettext_lazy("Carte d'identité nationale (8 chiffres) ou "
                                     "n° de passeport."))
    cin_lieu = models.CharField(gettext_lazy("lieu de délivrance de la CIN"), max_length=80, blank=True)
    cin_date = models.DateField(gettext_lazy("date de délivrance de la CIN"), null=True, blank=True)
    date_naissance = models.DateField(gettext_lazy("date de naissance"), null=True, blank=True)
    # Là où il compose : remplacés par chaque nouvelle liste importée.
    centre = models.CharField(gettext_lazy("centre"), max_length=150, blank=True,
                              help_text=gettext_lazy("Centre d'examen."))
    salle = models.CharField(gettext_lazy("salle"), max_length=60, blank=True)
    etablissement = models.CharField(gettext_lazy("établissement"), max_length=150, blank=True,
                                     help_text=gettext_lazy("Établissement d'origine."))
    email = models.EmailField(gettext_lazy("adresse e-mail"), blank=True)
    cree_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="+",
                                 verbose_name=gettext_lazy("inscrit par"))
    cree_le = models.DateTimeField(gettext_lazy("inscrit le"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("inscription")
        verbose_name_plural = gettext_lazy("inscriptions")
        ordering = ["nom", "prenom"]

    @property
    def nom_complet(self):
        return nom_et_prenom(self.nom, self.prenom)

    @property
    def fiche_complete(self):
        """L'identité de la liste officielle renseignée : le nom, la CIN avec
        son lieu et sa date de délivrance, la date de naissance. Le centre et
        la salle peuvent venir plus tard ; l'e-mail et l'établissement ne sont
        pas dans cette liste (facultatifs depuis le 5 novembre 2026)."""
        return all((self.nom, self.prenom, self.cin, self.cin_lieu, self.cin_date,
                    self.date_naissance))

    def __str__(self):
        return f"{self.numero} — {self.nom_complet}"


class Student(models.Model):
    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE,
                                    related_name="students", verbose_name=gettext_lazy("classe"))
    last_name = models.CharField(pgettext_lazy("personne", "nom"), max_length=80)
    first_name = models.CharField(gettext_lazy("prénom"), max_length=80)
    student_number = models.CharField(gettext_lazy("n° d'inscription"), max_length=40, blank=True)
    # Fiche du registre des inscriptions (même n°), quand elle existe.
    inscrit = models.ForeignKey(Inscrit, on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="places",
                                verbose_name=gettext_lazy("inscription"))

    class Meta:
        verbose_name = gettext_lazy("étudiant")
        verbose_name_plural = gettext_lazy("étudiants")
        ordering = ["last_name", "first_name"]

    @property
    def full_name(self):
        return nom_et_prenom(self.last_name, self.first_name)

    @property
    def salle(self):
        """Salle d'examen, d'après la fiche du registre (vide sans fiche ni salle)."""
        fiche = self.inscrit if self.inscrit_id else None
        return " ".join((fiche.salle or "").split()) if fiche else ""

    @property
    def cin(self):
        """N° de CIN, d'après la fiche du registre (vide sans fiche ni CIN)."""
        fiche = self.inscrit if self.inscrit_id else None
        return (fiche.cin or "") if fiche else ""

    def __str__(self):
        return self.full_name


# Chaque réponse juste à une QCM vaut UN point (règle de l'établissement,
# 14 octobre 2026) : la note se lit alors en nombre de bonnes réponses.
# Seules les réponses manuscrites gardent un barème libre.
POINTS_QCM = 1.0

# Ordre alphabétique arabe (hijā'ī) : أ ب ت ث ج ح خ د ذ ر ...
ARABIC_LETTERS = ["أ", "ب", "ت", "ث", "ج", "ح", "خ", "د", "ذ", "ر"]

# Nombre maximum de choix par question QCM : la limite vient des lettres
# arabes disponibles (voir ARABIC_LETTERS), pour que la même question
# s'affiche en français comme en arabe.
MAX_CHOICES = len(ARABIC_LETTERS)


def choice_letter(index, language="fr"):
    """Lettre d'un choix (0 -> « A » / « أ »). Chaîne vide hors limites
    plutôt qu'une exception : une donnée ancienne ou saisie par
    l'administration ne doit pas casser l'affichage d'une copie."""
    if index is None or not (0 <= index < MAX_CHOICES):
        return ""
    if language == "ar":
        return ARABIC_LETTERS[index]
    return chr(ord("A") + index)


class Quiz(models.Model):
    LANGUAGES = [("fr", gettext_lazy("Français")), ("ar", gettext_lazy("العربية (arabe)"))]
    SHEET_MODES = [
        ("full", gettext_lazy("Questionnaire complet — les questions et leurs choix sont imprimés sur la fiche")),
        ("split", gettext_lazy("Sujet séparé + feuille de réponses — deux documents (questions d'un côté, grille de l'autre)")),
        ("grid", gettext_lazy("Fiche de réponses seule — grille compacte, le sujet est distribué à part")),
    ]
    # SET_NULL, pour la même raison que ClassGroup.owner : une épreuve et
    # ses copies survivent au compte qui les a créées.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,
                              on_delete=models.SET_NULL,
                              null=True, related_name="quizzes",
                              verbose_name=gettext_lazy("enseignant"))
    title = models.CharField(gettext_lazy("titre"), max_length=200)
    # Nature de l'épreuve. QCM : cases noircies, lues au scan. Écrit : copie
    # rédigée, corrigée à la main ; le correcteur noircit la note dans la
    # grille de la page de garde, lue au scan avec l'identité (QR ou n°).
    # Oral : notes importées d'un fichier Excel, seule voie depuis le 13 novembre 2026
    # (libellé du choix ci-dessous inchangé : forms.QuizForm l'affiche à jour).
    TYPES = [
        ("qcm", gettext_lazy("QCM — cases à noircir, corrigées automatiquement")),
        ("ecrit", gettext_lazy("Écrit (rédaction) — copie corrigée à la main, note "
                  "noircie par le correcteur puis scannée")),
        ("oral", gettext_lazy("Oral — notes saisies à l'écran")),
    ]
    type_epreuve = models.CharField(gettext_lazy("type d'épreuve"), max_length=6, choices=TYPES,
                                    default="qcm")
    note_sur = models.FloatField(
        gettext_lazy("note sur"), default=20.0,
        validators=[MinValueValidator(1), MaxValueValidator(999)],
        help_text=gettext_lazy("Écrit et oral : barème de la note (ex. 20)."))
    sujet_texte = models.TextField(
        gettext_lazy("énoncé du sujet"), blank=True,
        help_text=gettext_lazy("Écrit : texte du sujet, imprimé dans le document « Sujet ». "
                  "Une ligne vide sépare deux paragraphes."))
    pages_redaction = models.PositiveSmallIntegerField(
        gettext_lazy("pages de rédaction"), default=1,
        validators=[MaxValueValidator(6)],
        help_text=gettext_lazy("Écrit : feuilles lignées imprimées après la page de garde "
                  "(0 à 6). Elles ne se scannent pas."))
    # Une feuille A3 pliée au milieu porte quatre pages A4 : les copies d'un écrit,
    # le sujet d'un QCM. La feuille de réponses d'un QCM reste en A4 : sur une feuille
    # A3 de deux pages à lire, le scan n'en retrouverait qu'une.
    FORMATS = [("a4", "A4"), ("a3", gettext_lazy("A3 recto-verso, plié au milieu"))]
    format_impression = models.CharField(
        gettext_lazy("format d'impression"), max_length=2, choices=FORMATS, default="a4",
        help_text=gettext_lazy("Écrit : les copies. QCM : le sujet."))
    # Copie nominative dont le nom est imprimé sur un TALON, en haut de la
    # page, hors du cadre de lecture : découpé avant la correction, il rend
    # la copie anonyme ; le QR (dans le cadre) identifie toujours l'étudiant.
    talon_nom = models.BooleanField(
        gettext_lazy("nom sur un talon à découper"), default=False,
        help_text=gettext_lazy("Chaque copie porte le nom et le QR de l'étudiant ; le nom est "
                  "sur un talon à découper (massicot) avant la correction. La "
                  "copie reste lisible au scan, et anonyme pour le correcteur."))
    # Autre choix (exclusif du talon) : AUCUN nom sur la copie, seulement un
    # n° de place et le QR ; la liste de placement et d'émargement dit au
    # surveillant qui s'assoit où (bulletin.generate_liste_emargement).
    place_anonyme = models.BooleanField(
        gettext_lazy("copie anonyme à n° de place"), default=False,
        help_text=gettext_lazy("Chaque copie porte un n° de place et le QR de l'étudiant, "
                  "jamais son nom ; une liste de placement et d'émargement est "
                  "imprimée pour le surveillant."))
    # Concours en plusieurs étapes (QCM -> écrit -> oral) : les candidats de
    # cette épreuve sont les admis de l'épreuve précédente.
    epreuve_precedente = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="epreuves_suivantes",
        verbose_name=gettext_lazy("candidats : admis de l'épreuve"),
        help_text=gettext_lazy("Étape suivante d'un concours : seuls les admis de cette "
                  "épreuve sont candidats."))
    class_group = models.ForeignKey(ClassGroup, on_delete=models.PROTECT,
                                    related_name="quizzes", verbose_name=gettext_lazy("classe"))
    ID_MODES = [
        ("name", gettext_lazy("OCR du nom manuscrit (fiches identiques pour toute la classe)")),
        ("grid", gettext_lazy("Grille de n° d'inscription à noircir (fiches identiques, lecture exacte)")),
        ("qr", gettext_lazy("QR code — fiches nominatives pré-imprimées (une fiche par étudiant, "
               "identification instantanée et infaillible)")),
        ("sticker", gettext_lazy("QR code — étiquette autocollante par candidat (planche d'étiquettes "
                    "à coller dans l'emplacement réservé ; lue automatiquement au scan)")),
    ]
    id_mode = models.CharField(
        gettext_lazy("identification des copies"), max_length=8, choices=ID_MODES, default="name")
    id_digits = models.PositiveSmallIntegerField(
        gettext_lazy("chiffres du n° d'inscription"), default=6,
        help_text=gettext_lazy("Utilisé uniquement avec la grille : nombre de chiffres "
                  "du numéro d'inscription (ex. 6)."))
    auto_enroll = models.BooleanField(
        gettext_lazy("mode concours : créer automatiquement les candidats"), default=False,
        help_text=gettext_lazy("Aucune saisie préalable des étudiants : chaque copie scannée "
                  "crée son candidat à partir du numéro lu (grille de n° ou QR de "
                  "l'étiquette). La fiche concours ne demande aucune écriture au "
                  "candidat ; importez ensuite une liste « numéro;NOM;Prénom » "
                  "pour attacher les noms officiels aux numéros."))
    # Moyenne finale d'un concours (المعدل النهائي, demande de l'utilisateur, 13 novembre
    # 2026) : la moyenne des notes sur 20 de ses étapes (QCM, écrit, oral), pondérée par ce
    # coefficient de chacune ; réglé sur la page des résultats du concours.
    coefficient_final = models.FloatField(
        gettext_lazy("coefficient dans la moyenne finale"), default=1.0,
        validators=[MinValueValidator(0), MaxValueValidator(99)],
        help_text=gettext_lazy("Concours : poids de l'étape dans la moyenne finale (0 : elle n'y compte "
                  "pas)."))
    # Année d'un concours de l'institut, choisie à sa création (demande de l'utilisateur,
    # 13 novembre 2026) et commune à ses étapes. Le code d'anonymat des copies à talon
    # commence par X en 1re année, par Y en 2e (qr.premiere_lettre). Vide : épreuve de
    # classe, ou concours créé avant ce choix.
    ANNEES = [("1", "السنة الأولى"), ("2", "السنة الثانية")]
    annee = models.CharField(
        gettext_lazy("année"), max_length=1, choices=ANNEES, blank=True, default="",
        help_text=gettext_lazy("Concours : 1re ou 2e année. Le code d'anonymat des copies à talon "
                  "commence par X en 1re année, par Y en 2e."))
    language = models.CharField(
        gettext_lazy("langue du questionnaire"), max_length=2, choices=LANGUAGES, default="fr",
        help_text=gettext_lazy("Détermine la langue de la fiche de réponses (sens d'écriture, "
                  "lettres des choix) et de la lecture OCR du nom."))
    GRADING_MODES = [
        ("immediate", gettext_lazy("Correction immédiate — chaque lot est corrigé dès son téléversement")),
        ("deferred", gettext_lazy("Correction après scan — on téléverse toutes les copies, puis on "
                     "clique sur « Lancer la correction »")),
    ]
    grading_mode = models.CharField(
        gettext_lazy("correction des copies"), max_length=10, choices=GRADING_MODES,
        default="immediate")
    sheet_mode = models.CharField(
        gettext_lazy("type de fiche"), max_length=6, choices=SHEET_MODES, default="full")
    # QCM en deux versions (demande de l'utilisateur, 23 novembre 2026) : la version B a
    # ses questions et, dans chacune, ses choix dans un autre ordre ; la version est
    # imprimée sur la feuille et lue au scan (layout.VERSIONS)
    VERSIONS = [
        (1, gettext_lazy("Une seule version")),
        (2, gettext_lazy("Deux versions, A et B — questions et choix dans un autre ordre")),
    ]
    versions = models.PositiveSmallIntegerField(
        gettext_lazy("versions du sujet"), choices=VERSIONS, default=1,
        help_text=gettext_lazy("QCM : la version B mélange les questions et leurs choix ; chaque copie "
                  "porte sa version et se corrige selon elle."))
    num_choices = models.PositiveSmallIntegerField(
        gettext_lazy("nombre de choix par question QCM"), default=4,
        validators=[MinValueValidator(2), MaxValueValidator(MAX_CHOICES)],
        help_text=format_lazy(gettext_lazy("Ex. 4 pour A, B, C, D (2 à {n})"), n=MAX_CHOICES))
    wrong_penalty = models.FloatField(
        gettext_lazy("pénalité par mauvaise réponse"), default=0.0,
        help_text=gettext_lazy("Points retirés pour chaque mauvaise réponse QCM (0 = pas de pénalité)"))
    # Seuil de réussite (concours : note d'admission), sur le barème de
    # l'épreuve. Sans lui, QuizScan ne peut pas dire qui a réussi.
    note_admission = models.FloatField(
        gettext_lazy("note minimale de réussite"), null=True, blank=True,
        validators=[MinValueValidator(0)],
        help_text=gettext_lazy("Sur le barème de l'épreuve (ex. 10 pour 10 / 20). Concours : "
                  "note d'admission. Vide : pas de liste des admis."))
    # Disposition de la fiche (coordonnées en mm), figée à la génération du PDF
    layout_json = models.JSONField(null=True, blank=True, editable=False)
    sheet_pdf = models.FileField(gettext_lazy("fiche de réponses (PDF)"), upload_to="sheets/",
                                 null=True, blank=True)
    # Sujet séparé (questions seules) — mode "split"
    subject_pdf = models.FileField(gettext_lazy("sujet — questions (PDF)"), upload_to="sheets/",
                                   null=True, blank=True)
    # Cartouche du sujet, à la manière des sujets de concours : institutions
    # dans une case, intitulé et durée au centre. Facultatifs.
    entete = models.TextField(
        gettext_lazy("en-tête du sujet"), blank=True,
        help_text=gettext_lazy("Une ligne par ligne : ministère, établissement… "
                  "Imprimé dans le cartouche du sujet."))
    duree = models.CharField(
        gettext_lazy("durée de l'épreuve"), max_length=60, blank=True,
        help_text=gettext_lazy("Ex. « 1 h 30 » ou « ساعة ونصف »."))
    date_epreuve = models.DateField(
        gettext_lazy("date de l'épreuve"), null=True, blank=True,
        help_text=gettext_lazy("Imprimée dans le cartouche du sujet, la durée dessous."))
    # Espace public : chaque étudiant y consulte sa note avec son
    # n° d'inscription ; la liste des admis peut y être affichée en plus.
    resultats_publies = models.BooleanField(
        gettext_lazy("résultats publiés"), default=False,
        help_text=gettext_lazy("Chaque étudiant consulte sa note sur l'espace public avec "
                  "son n° d'inscription."))
    admis_publies = models.BooleanField(
        gettext_lazy("liste des admis publiée"), default=False,
        help_text=gettext_lazy("La liste des admis (rang, n°, nom) est affichée sur "
                  "l'espace public."))
    publie_le = models.DateTimeField(gettext_lazy("publié le"), null=True, blank=True)
    created_at = models.DateTimeField(gettext_lazy("créée le"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("quiz")
        verbose_name_plural = gettext_lazy("quiz")
        ordering = ["-created_at"]
        # Les deux droits que l'administration accorde compte par compte.
        # Ils sont portés par Quiz parce que c'est l'objet central, mais ils
        # valent pour tout ce qui en découle : questions, fiches, copies.
        permissions = [
            ("creer_epreuve",
             "Créer et modifier des épreuves (quiz, concours, questions)"),
            ("corriger_copies",
             "Téléverser les copies, lancer la correction et la vérifier"),
        ]

    def __str__(self):
        return self.title

    @property
    def max_score(self):
        if self.matieres:
            return self.note_sur        # moyenne pondérée des matières, sur le barème
        return sum(q.points for q in self.questions.all())

    @property
    def matieres(self):
        """Écrit ou oral en plusieurs matières (13 novembre 2026) : ses questions notées, une
        par matière, dans l'ordre. Liste vide pour une épreuve d'une seule note, et au QCM."""
        if self.type_epreuve not in ("ecrit", "oral") or not self.pk:
            return []
        notees = list(self.questions.filter(qtype="open").order_by("order"))
        return notees if len(notees) > 1 else []

    @property
    def est_qcm(self):
        return self.type_epreuve == "qcm"

    @property
    def corrige_apres_scan(self):
        """Concours corrigé « après scan » : les lots attendent « Lancer la correction »,
        bouton de la page de l'épreuve (QCM ou écrit)."""
        return self.auto_enroll and self.grading_mode == "deferred"

    # Présentation des copies nominatives (QR imprimé) : nom imprimé, nom sur
    # un talon à découper, ou copie anonyme à n° de place.
    ANONYMATS = [
        ("", gettext_lazy("Standard — selon l'identification choisie")),
        ("talon", gettext_lazy("Nom sur un talon à découper avant la correction")),
        ("place", gettext_lazy("Copie anonyme : n° de place + liste des candidats")),
    ]

    @property
    def anonymat(self):
        return "talon" if self.talon_nom else "place" if self.place_anonyme else ""

    @property
    def type_court(self):
        """Nom court du type d'épreuve : une étape dans la liste des concours. Traduit dans
        l'espace public en arabe (24 novembre 2026) ; en français ailleurs."""
        nom = {"qcm": "QCM", "ecrit": "Écrit", "oral": "Oral"}.get(self.type_epreuve, "")
        return gettext(nom) if nom else ""

    @property
    def de_type(self):
        """Une étape dans une phrase : « admis du QCM », « admis de l'écrit »."""
        return {"qcm": gettext("du QCM"), "ecrit": gettext("de l'écrit"),
                "oral": gettext("de l'oral")}.get(self.type_epreuve, "")

    @property
    def copies_nominatives(self):
        """Talon ou n° de place : une copie par étudiant, QR imprimé (même en
        concours, où l'on imprime sinon des fiches anonymes à étiquette)."""
        return self.talon_nom or self.place_anonyme

    @property
    def exemplaires_anonymes(self):
        """Fiches anonymes en nombre choisi (ancien concours en « qr ») : c'est le seul cas
        où le nombre se choisit. Les autres modes QR impriment une copie par étudiant."""
        return self.id_mode == "qr" and self.auto_enroll and not self.copies_nominatives

    @cached_property
    def a_des_qcm(self):
        """Des QCM à afficher en « bonnes réponses » ? (pas à l'écrit ni à
        l'oral, dont la note est portée par une seule question « Note »)."""
        return self.questions.filter(qtype="qcm").exists()

    @cached_property
    def note_distincte(self):
        """La note diffère du nombre de bonnes réponses (une QCM = 1 point)
        seulement avec une pénalité ou des réponses manuscrites : sinon, on
        n'affiche pas deux fois le même chiffre."""
        return bool(self.wrong_penalty) or self.questions.filter(qtype="open").exists()

    @property
    def questions_sans_corrige(self):
        """Questions QCM dont la bonne réponse n'est pas encore connue.

        La bonne réponse n'est plus obligatoire à la saisie : elle peut être
        renseignée plus tard à la main, ou lue en scannant une fiche remplie
        avec le corrigé. Tant qu'il en manque une, la correction des copies
        ne peut pas être lancée — elle compterait ces questions comme fausses."""
        return self.questions.filter(qtype="qcm", correct_choice__isnull=True)

    @property
    def corrige_complet(self):
        return not self.questions_sans_corrige.exists()

    @property
    def choice_letter_list(self):
        n = max(min(self.num_choices, MAX_CHOICES), 1)
        return [(i, choice_letter(i, self.language)) for i in range(n)]


class Question(models.Model):
    TYPE_CHOICES = [("qcm", gettext_lazy("QCM (lecture automatique)")),
                    ("open", gettext_lazy("Réponse manuscrite (correction manuelle)"))]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveSmallIntegerField(gettext_lazy("n°"))
    qtype = models.CharField(gettext_lazy("type"), max_length=4, choices=TYPE_CHOICES, default="qcm")
    text = models.CharField(gettext_lazy("intitulé de la question"), max_length=500, blank=True)
    # Textes des choix de réponse (questionnaire complet) : liste de chaînes
    choices = models.JSONField(gettext_lazy("choix de réponse"), default=list, blank=True)
    points = models.FloatField(
        gettext_lazy("barème (points)"), default=1.0,
        help_text=gettext_lazy("Une QCM vaut toujours 1 point ; barème libre pour une "
                  "réponse manuscrite."))
    # Pour les QCM : index de la bonne réponse (0=A, 1=B, ...)
    correct_choice = models.PositiveSmallIntegerField(gettext_lazy("bonne réponse"), null=True, blank=True)
    # Nombre de cases quand le texte des choix n'est pas saisi (grille seule)
    num_choices = models.PositiveSmallIntegerField(
        gettext_lazy("nombre de choix"), null=True, blank=True,
        validators=[MinValueValidator(2), MaxValueValidator(MAX_CHOICES)],
        help_text=format_lazy(gettext_lazy("Nombre de cases A, B, C… de cette question (si le "
                                           "texte des choix n'est pas saisi) ; 2 à {n}."),
                              n=MAX_CHOICES))
    # Hauteur de la zone de réponse manuscrite sur la fiche (mm)
    open_height_mm = models.PositiveSmallIntegerField(
        gettext_lazy("hauteur zone réponse (mm)"), default=25,
        validators=[MinValueValidator(10), MaxValueValidator(MAX_OPEN_HEIGHT_MM)],
        help_text=format_lazy(gettext_lazy("10 à {n} mm (une zone plus haute ne tiendrait pas "
                                           "sur une page A4)."), n=MAX_OPEN_HEIGHT_MM))
    # Écrit en plusieurs matières (demande de l'utilisateur, 13 novembre 2026 : à l'écrit
    # d'un concours, قانون مدني، قانون جزائي، الحريات الأساسية) : chaque matière est une
    # question notée de l'épreuve, son intitulé est le nom de la matière. Elle a sa page de
    # garde, son sujet, sa durée et sa date ; la note de l'écrit est la moyenne des
    # matières pondérée par leurs coefficients (services.compute_results).
    coefficient = models.FloatField(
        gettext_lazy("coefficient"), default=1.0,
        validators=[MinValueValidator(0.1), MaxValueValidator(99)],
        help_text=gettext_lazy("Écrit en matières : poids de la matière dans la moyenne de l'écrit."))
    sujet = models.TextField(
        gettext_lazy("énoncé du sujet"), blank=True,
        help_text=gettext_lazy("Écrit en matières : le sujet de la matière. Une ligne vide sépare "
                  "deux paragraphes."))
    duree = models.CharField(gettext_lazy("durée"), max_length=60, blank=True)
    date_epreuve = models.DateField(gettext_lazy("date"), null=True, blank=True)

    class Meta:
        verbose_name = gettext_lazy("question")
        verbose_name_plural = gettext_lazy("questions")
        ordering = ["order"]
        unique_together = [("quiz", "order")]

    def __str__(self):
        return f"Q{self.order} ({self.get_qtype_display()})"

    def save(self, *args, **kwargs):
        # Quel que soit le chemin (formulaire, import, banque, administration),
        # une QCM vaut un point.
        if self.qtype == "qcm":
            self.points = POINTS_QCM
        super().save(*args, **kwargs)

    @property
    def correct_letter(self):
        return choice_letter(self.correct_choice, self.quiz.language)

    @property
    def letter_options(self):
        """(index, lettre) des choix possibles, pour les listes déroulantes."""
        return [(i, choice_letter(i, self.quiz.language))
                for i in range(self.num_bubbles)]

    @property
    def num_bubbles(self):
        """Nombre de cases de cette question sur la fiche : le nombre de
        choix saisis, sinon le nombre de choix réglé pour la question
        (anciennes questions : valeur par défaut du quiz). Borné à
        MAX_CHOICES — au-delà, les choix n'auraient plus de lettre."""
        if self.qtype == "qcm" and self.choices:
            n = len(self.choices)
        else:
            n = self.num_choices or self.quiz.num_choices
        return max(min(n, MAX_CHOICES), 1)


class ScanBatch(models.Model):
    """Un lot de pages scannées téléversées pour un quiz.

    La correction se fait en arrière-plan dès l'envoi : `total_pages` et
    `processed_pages` alimentent le bandeau de progression du lot."""
    STATUS = [("pending", gettext_lazy("En attente de correction")),
              ("processing", gettext_lazy("Correction en cours")),
              ("done", gettext_lazy("Terminé")),
              ("error", gettext_lazy("Erreur"))]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,
                             related_name="batches", verbose_name=gettext_lazy("épreuve"))
    label = models.CharField(gettext_lazy("libellé"), max_length=120, blank=True)
    status = models.CharField(gettext_lazy("état"), max_length=12, choices=STATUS, default="done")
    total_pages = models.PositiveIntegerField(gettext_lazy("pages à traiter"), default=0)
    processed_pages = models.PositiveIntegerField(gettext_lazy("pages traitées"), default=0)
    error_message = models.TextField(gettext_lazy("message d'erreur"), blank=True)
    # fichiers d'origine conservés pour la reprise d'un traitement interrompu
    source_files = models.JSONField(default=list, blank=True, editable=False)
    created_at = models.DateTimeField(gettext_lazy("reçu le"), auto_now_add=True)
    updated_at = models.DateTimeField(gettext_lazy("mis à jour le"), auto_now=True)

    class Meta:
        verbose_name = gettext_lazy("lot de scans")
        verbose_name_plural = gettext_lazy("lots de scans")
        ordering = ["-created_at"]

    def __str__(self):
        # à l'heure de Tunis, comme la colonne « Date » (en UTC, le nom retardait d'une heure)
        return self.label or gettext("Lot du %(date)s") % {
            "date": f"{timezone.localtime(self.created_at):%d/%m/%Y %H:%M}"}

    @property
    def identified_count(self):
        return self.sheets.exclude(student=None).count()

    @property
    def is_processing(self):
        return self.status == "processing"

    @property
    def progress_pct(self):
        if not self.total_pages:
            return 0
        return min(round(self.processed_pages / self.total_pages * 100), 100)


class SheetScan(models.Model):
    STATUS = [("pending", gettext_lazy("En attente")),
              ("ok", gettext_lazy("Lu avec succès")),
              ("no_markers", gettext_lazy("Repères non détectés")),
              # écrit : page de rédaction scannée avec la copie (sans repères)
              ("ignoree", gettext_lazy("Page de rédaction, ignorée")),
              ("no_match", gettext_lazy("Étudiant non identifié")),
              ("error", gettext_lazy("Erreur"))]
    batch = models.ForeignKey(ScanBatch, on_delete=models.CASCADE,
                              related_name="sheets", verbose_name=gettext_lazy("lot"))
    page_index = models.PositiveSmallIntegerField(gettext_lazy("page de la fiche"), default=0)
    source_name = models.CharField(gettext_lazy("fichier d'origine"), max_length=200, blank=True)
    image = models.ImageField(gettext_lazy("image scannée"), upload_to="scans/")
    warped_image = models.ImageField(gettext_lazy("image redressée"), upload_to="warped/",
                                     null=True, blank=True)
    overlay_image = models.ImageField(gettext_lazy("image de contrôle"), upload_to="overlays/",
                                      null=True, blank=True)
    name_crop = models.ImageField(gettext_lazy("zone du nom"), upload_to="crops/",
                                  null=True, blank=True)
    status = models.CharField(gettext_lazy("état"), max_length=12, choices=STATUS,
                              default="pending")
    id_read = models.CharField(gettext_lazy("n° d'inscription lu"), max_length=20, blank=True)
    ocr_name_raw = models.CharField(gettext_lazy("nom lu (OCR)"), max_length=200, blank=True)
    match_score = models.FloatField(gettext_lazy("score de rapprochement"), default=0.0)
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="sheets", verbose_name=gettext_lazy("étudiant"))
    student_confirmed = models.BooleanField(gettext_lazy("identification confirmée"), default=False)
    # Page non identifiée dont l'alerte a été fermée (« × ») sur la page des
    # résultats : elle reste « à identifier » dans son lot, elle n'est
    # simplement plus signalée. Une nouvelle page non identifiée l'est.
    alerte_masquee = models.BooleanField(gettext_lazy("alerte masquée"), default=False)
    error_message = models.TextField(gettext_lazy("message d'erreur"), blank=True)
    created_at = models.DateTimeField(gettext_lazy("reçue le"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("page scannée")
        verbose_name_plural = gettext_lazy("pages scannées")
        ordering = ["created_at"]

    def __str__(self):
        return gettext("%(fichier)s (page %(n)s)") % {"fichier": self.source_name,
                                                     "n": self.page_index + 1}


class AnswerKeySheet(models.Model):
    """Page d'un corrigé scanné : la fiche d'épreuve remplie avec les bonnes
    réponses, passée au scanner comme une copie ordinaire.

    Chaque page téléversée est conservée avec le quiz — image d'origine et
    image de contrôle — pour garder la trace de l'origine du barème : c'est
    elle qui fait foi en cas de contestation après l'épreuve."""
    STATUS = [("ok", gettext_lazy("Corrigé lu")),
              ("partial", gettext_lazy("Corrigé lu partiellement")),
              ("no_markers", gettext_lazy("Repères non détectés")),
              ("error", gettext_lazy("Erreur"))]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,
                             related_name="answer_key_sheets",
                             verbose_name=gettext_lazy("épreuve"))
    page_index = models.PositiveSmallIntegerField(gettext_lazy("page de la fiche"), default=0)
    source_name = models.CharField(gettext_lazy("fichier d'origine"), max_length=200, blank=True)
    image = models.ImageField(gettext_lazy("image scannée"), upload_to="corriges/")
    overlay_image = models.ImageField(gettext_lazy("image de contrôle"), upload_to="corriges/",
                                      null=True, blank=True)
    status = models.CharField(gettext_lazy("état"), max_length=12, choices=STATUS, default="ok")
    # {numéro de question: index de la bonne réponse lue}
    detected = models.JSONField(gettext_lazy("réponses lues"), default=dict, blank=True)
    # numéros des questions illisibles sur cette page (vide ou plusieurs cases)
    unreadable = models.JSONField(gettext_lazy("questions illisibles"), default=list, blank=True)
    error_message = models.TextField(gettext_lazy("message d'erreur"), blank=True)
    created_at = models.DateTimeField(gettext_lazy("scanné le"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("corrigé scanné")
        verbose_name_plural = gettext_lazy("corrigés scannés")
        ordering = ["-created_at", "page_index"]

    def __str__(self):
        return gettext("%(fichier)s (page %(n)s)") % {"fichier": self.source_name,
                                                     "n": self.page_index + 1}

    @property
    def nb_lues(self):
        return len(self.detected or {})


class Answer(models.Model):
    sheet = models.ForeignKey(SheetScan, on_delete=models.CASCADE,
                              related_name="answers", verbose_name=gettext_lazy("copie"))
    question = models.ForeignKey(Question, on_delete=models.CASCADE,
                                 related_name="answers", verbose_name=gettext_lazy("question"))
    # QCM
    detected_choice = models.SmallIntegerField(  # 0=A ... ; None = vide
        gettext_lazy("case lue"), null=True, blank=True)
    is_multiple = models.BooleanField(gettext_lazy("plusieurs cases cochées"), default=False)
    fill_ratios = models.JSONField(gettext_lazy("taux de remplissage"), null=True, blank=True)
    # Manuscrit
    open_crop = models.ImageField(gettext_lazy("zone manuscrite"), upload_to="crops/",
                                  null=True, blank=True)
    # Notation
    points_awarded = models.FloatField(gettext_lazy("points attribués"), null=True, blank=True)
    manually_set = models.BooleanField(gettext_lazy("note saisie à la main"), default=False)

    class Meta:
        verbose_name = gettext_lazy("réponse")
        verbose_name_plural = gettext_lazy("réponses")
        unique_together = [("sheet", "question")]
        ordering = ["question__order"]

    @property
    def detected_letter(self):
        if self.detected_choice is None:
            return "—"
        if self.is_multiple:
            return "✱"
        return choice_letter(self.detected_choice, self.question.quiz.language)

    def __str__(self):
        # Sans cela, l'administration affichait « Answer object (5099) ».
        return gettext("%(copie)s — question %(n)s") % {"copie": self.sheet,
                                                       "n": self.question.order}

    @property
    def is_blank(self):
        return self.detected_choice is None and not self.is_multiple

    @property
    def is_correct(self):
        q = self.question
        if q.qtype != "qcm" or self.is_blank or self.is_multiple:
            return False
        return self.detected_choice == q.correct_choice


class Modification(models.Model):
    """Journal des gestes qui changent une note — en lecture seule.

    Une épreuve se conteste. Pour répondre, il faut savoir qui a modifié une
    note, l'attribution d'une copie ou une bonne réponse, quand, et de quoi
    à quoi. Les entrées ne se modifient ni ne s'effacent, même depuis
    l'administration (voir JournalAdmin).

    Les liens vers l'épreuve, la copie et l'auteur sont facultatifs
    (SET_NULL) et doublés d'un libellé en clair : une entrée doit survivre
    à la suppression de ce qu'elle décrit — c'est justement le cas d'un lot
    supprimé, ou d'un enseignant parti, qu'on voudra retrouver.
    """
    ACTIONS = [
        ("note", gettext_lazy("Note d'une réponse")),
        ("case_qcm", gettext_lazy("Case de QCM rectifiée")),
        ("identification", gettext_lazy("Attribution de la copie")),
        ("bonne_reponse", gettext_lazy("Bonne réponse")),
        ("bareme", gettext_lazy("Barème d'une question")),
        ("penalite", gettext_lazy("Pénalité par mauvaise réponse")),
        ("question_supprimee", gettext_lazy("Question supprimée")),
        ("copie_supprimee", gettext_lazy("Copie supprimée")),
        ("lot_supprime", gettext_lazy("Lot de copies supprimé")),
        ("corrige_supprime", gettext_lazy("Corrigé scanné supprimé")),
        ("titre", gettext_lazy("Titre de l'épreuve")),
        ("etudiant", gettext_lazy("Identité d'un étudiant noté")),
        ("note_admission", gettext_lazy("Note minimale d'admission")),
        ("publication", gettext_lazy("Publication des résultats")),
    ]
    quand = models.DateTimeField(gettext_lazy("date"), auto_now_add=True, db_index=True)
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+",
                               verbose_name=gettext_lazy("compte"))
    auteur_nom = models.CharField(gettext_lazy("auteur"), max_length=150)
    action = models.CharField(gettext_lazy("geste"), max_length=20, choices=ACTIONS)
    quiz = models.ForeignKey(Quiz, on_delete=models.SET_NULL, null=True,
                             blank=True, related_name="modifications",
                             verbose_name=gettext_lazy("épreuve"))
    quiz_titre = models.CharField(gettext_lazy("épreuve (titre)"), max_length=200, blank=True)
    copie = models.ForeignKey(SheetScan, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name="modifications",
                              verbose_name=gettext_lazy("copie"))
    objet = models.CharField(gettext_lazy("objet"), max_length=200)
    avant = models.CharField(gettext_lazy("avant"), max_length=200, blank=True)
    apres = models.CharField(gettext_lazy("après"), max_length=200, blank=True)

    class Meta:
        verbose_name = gettext_lazy("modification")
        # « modifications » et non « journal des modifications » : Django
        # compte avec ce mot (« 5 modifications »). Le titre « Journal des
        # modifications » est posé par l'administration (JournalAdmin).
        verbose_name_plural = gettext_lazy("modifications")
        ordering = ["-quand", "-pk"]

    def __str__(self):
        return f"{self.get_action_display()} — {self.objet}"


class Communique(models.Model):
    """Avis publié sur l'espace public : dates d'examen, convocations,
    salles, consignes… Visible de tous dès qu'il est publié."""
    titre = models.CharField(gettext_lazy("titre"), max_length=200)
    texte = models.TextField(gettext_lazy("texte"))
    publie = models.BooleanField(
        gettext_lazy("publié"), default=True,
        help_text=gettext_lazy("Décoché : le communiqué est gardé en brouillon, invisible "
                  "du public."))
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+",
                               verbose_name=gettext_lazy("auteur"))
    cree_le = models.DateTimeField(gettext_lazy("date"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("communiqué")
        verbose_name_plural = gettext_lazy("communiqués")
        ordering = ["-cree_le"]

    def __str__(self):
        return self.titre

    # Pièces jointes par genre (lues dans pieces.all() : un prefetch_related
    # sur « pieces » suffit pour toute une liste de communiqués).
    def _pieces(self, genre):
        return [p for p in self.pieces.all() if p.genre == genre]

    @property
    def images(self):
        return self._pieces("image")

    @property
    def videos(self):
        return self._pieces("video")

    @property
    def documents(self):
        return self._pieces("document")

    @property
    def resume_pieces(self):
        """« 2 images · 1 vidéo · 1 document », ou "" sans pièce jointe. En arabe (espace
        public, 24 novembre 2026) : un libellé pour un, un autre pour plusieurs."""
        parts = []
        for n, un, plusieurs in ((len(self.images), "%(n)s image", "%(n)s images"),
                                 (len(self.videos), "%(n)s vidéo", "%(n)s vidéos"),
                                 (len(self.documents), "%(n)s document", "%(n)s documents")):
            if n:
                parts.append(gettext(un if n == 1 else plusieurs) % {"n": n})
        return " · ".join(parts)


class PieceJointe(models.Model):
    """Fichier joint à un communiqué : image (affichée dans la page), vidéo
    (lue dans la page) ou document (à télécharger). Servi par
    views_public.piece_jointe — au public dès que le communiqué est publié —
    et jamais par /media/, réservé au personnel (voir grader/pieces.py)."""
    GENRES = [("image", gettext_lazy("image")), ("video", gettext_lazy("vidéo")), ("document", gettext_lazy("document"))]
    communique = models.ForeignKey(Communique, on_delete=models.CASCADE,
                                   related_name="pieces", verbose_name=gettext_lazy("communiqué"))
    fichier = models.FileField(gettext_lazy("fichier"), upload_to="communiques/%Y/%m/", max_length=255)
    nom = models.CharField(gettext_lazy("nom du fichier"), max_length=200)
    genre = models.CharField(gettext_lazy("genre"), max_length=10, choices=GENRES)
    taille = models.PositiveBigIntegerField(gettext_lazy("taille (octets)"), default=0)
    ajoute_le = models.DateTimeField(gettext_lazy("ajoutée le"), auto_now_add=True)

    class Meta:
        verbose_name = gettext_lazy("pièce jointe")
        verbose_name_plural = gettext_lazy("pièces jointes")
        ordering = ["pk"]

    def __str__(self):
        return self.nom

    @property
    def url(self):
        """Adresse publique ; le nom n'y est qu'un repère (enregistrer sous)."""
        try:
            nom = get_valid_filename(self.nom)
        except Exception:                     # nom vide ou réduit à « . »
            nom = "fichier"
        return reverse("public_piece", args=[self.pk, nom])


@receiver(post_delete, sender=PieceJointe)
def _effacer_piece(sender, instance, **kwargs):
    """Le fichier part avec sa pièce jointe (retirée, ou communiqué
    supprimé), une fois la suppression validée."""
    nom, stockage = instance.fichier.name, instance.fichier.storage

    def effacer():
        # la suppression est déjà enregistrée : pas d'erreur 500 pour un fichier
        # impossible à effacer, il reste seulement sur le disque
        try:
            stockage.delete(nom)
        except OSError:
            logging.getLogger(__name__).warning("Fichier impossible à effacer : %s", nom)

    if nom:
        transaction.on_commit(effacer)
