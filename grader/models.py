from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

# Hauteur maximale d'une zone de réponse manuscrite (mm) : au-delà, le bloc
# ne tient plus dans la zone imprimable d'une page A4 (voir layout.py).
MAX_OPEN_HEIGHT_MM = 200


class ClassGroup(models.Model):
    """Une classe / un groupe d'étudiants."""
    # SET_NULL et non CASCADE : quand un enseignant quitte l'établissement,
    # son compte est supprimé mais ses classes, ses épreuves et les notes de
    # ses élèves restent — ce sont les archives de l'établissement, pas sa
    # propriété. La classe devient sans titulaire et l'administration peut
    # la confier à quelqu'un d'autre depuis la fiche du nouveau compte.
    # (Avec CASCADE, Quiz.class_group étant protégé, la suppression d'un
    # enseignant ayant la moindre épreuve échouait purement et simplement.)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,
                              on_delete=models.SET_NULL,
                              null=True, related_name="class_groups",
                              verbose_name="enseignant")
    name = models.CharField("nom de la classe", max_length=120)
    created_at = models.DateTimeField("créée le", auto_now_add=True)

    class Meta:
        verbose_name = "classe"
        verbose_name_plural = "classes"
        ordering = ["name"]
        unique_together = [("owner", "name")]

    def __str__(self):
        return self.name


class Student(models.Model):
    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE,
                                    related_name="students", verbose_name="classe")
    last_name = models.CharField("nom", max_length=80)
    first_name = models.CharField("prénom", max_length=80)
    student_number = models.CharField("n° d'inscription", max_length=40, blank=True)

    class Meta:
        verbose_name = "étudiant"
        verbose_name_plural = "étudiants"
        ordering = ["last_name", "first_name"]

    @property
    def full_name(self):
        return f"{self.last_name} {self.first_name}"

    def __str__(self):
        return self.full_name


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
    LANGUAGES = [("fr", "Français"), ("ar", "العربية (arabe)")]
    SHEET_MODES = [
        ("full", "Questionnaire complet — les questions et leurs choix sont imprimés sur la fiche"),
        ("split", "Sujet séparé + feuille de réponses — deux documents (questions d'un côté, grille de l'autre)"),
        ("grid", "Fiche de réponses seule — grille compacte, le sujet est distribué à part"),
    ]
    # SET_NULL, pour la même raison que ClassGroup.owner : une épreuve et
    # ses copies survivent au compte qui les a créées.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL,
                              on_delete=models.SET_NULL,
                              null=True, related_name="quizzes",
                              verbose_name="enseignant")
    title = models.CharField("titre", max_length=200)
    class_group = models.ForeignKey(ClassGroup, on_delete=models.PROTECT,
                                    related_name="quizzes", verbose_name="classe")
    ID_MODES = [
        ("name", "OCR du nom manuscrit (fiches identiques pour toute la classe)"),
        ("grid", "Grille de n° d'inscription à noircir (fiches identiques, lecture exacte)"),
        ("qr", "QR code — fiches nominatives pré-imprimées (une fiche par étudiant, "
               "identification instantanée et infaillible)"),
        ("sticker", "QR code — étiquette autocollante par candidat (planche d'étiquettes "
                    "à coller dans l'emplacement réservé ; lue automatiquement au scan)"),
    ]
    id_mode = models.CharField(
        "identification des copies", max_length=8, choices=ID_MODES, default="name")
    id_digits = models.PositiveSmallIntegerField(
        "chiffres du n° d'inscription", default=6,
        help_text="Utilisé uniquement avec la grille : nombre de chiffres "
                  "du numéro d'inscription (ex. 6).")
    auto_enroll = models.BooleanField(
        "mode concours : créer automatiquement les candidats", default=False,
        help_text="Aucune saisie préalable des étudiants : chaque copie scannée "
                  "crée son candidat à partir du numéro lu (grille de n° ou QR de "
                  "l'étiquette). La fiche concours ne demande aucune écriture au "
                  "candidat ; importez ensuite une liste « numéro;NOM;Prénom » "
                  "pour attacher les noms officiels aux numéros.")
    language = models.CharField(
        "langue du questionnaire", max_length=2, choices=LANGUAGES, default="fr",
        help_text="Détermine la langue de la fiche de réponses (sens d'écriture, "
                  "lettres des choix) et de la lecture OCR du nom.")
    GRADING_MODES = [
        ("immediate", "Correction immédiate — chaque lot est corrigé dès son téléversement"),
        ("deferred", "Correction après scan — on téléverse toutes les copies, puis on "
                     "clique sur « Lancer la correction »"),
    ]
    grading_mode = models.CharField(
        "correction des copies", max_length=10, choices=GRADING_MODES,
        default="immediate")
    sheet_mode = models.CharField(
        "type de fiche", max_length=6, choices=SHEET_MODES, default="full")
    num_choices = models.PositiveSmallIntegerField(
        "nombre de choix par question QCM", default=4,
        validators=[MinValueValidator(2), MaxValueValidator(MAX_CHOICES)],
        help_text=f"Ex. 4 pour A, B, C, D (2 à {MAX_CHOICES})")
    wrong_penalty = models.FloatField(
        "pénalité par mauvaise réponse", default=0.0,
        help_text="Points retirés pour chaque mauvaise réponse QCM (0 = pas de pénalité)")
    # Disposition de la fiche (coordonnées en mm), figée à la génération du PDF
    layout_json = models.JSONField(null=True, blank=True, editable=False)
    sheet_pdf = models.FileField("fiche de réponses (PDF)", upload_to="sheets/",
                                 null=True, blank=True)
    # Sujet séparé (questions seules) — mode "split"
    subject_pdf = models.FileField("sujet — questions (PDF)", upload_to="sheets/",
                                   null=True, blank=True)
    created_at = models.DateTimeField("créée le", auto_now_add=True)

    class Meta:
        verbose_name = "quiz"
        verbose_name_plural = "quiz"
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
        return sum(q.points for q in self.questions.all())

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
    TYPE_CHOICES = [("qcm", "QCM (lecture automatique)"),
                    ("open", "Réponse manuscrite (correction manuelle)")]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    order = models.PositiveSmallIntegerField("n°")
    qtype = models.CharField("type", max_length=4, choices=TYPE_CHOICES, default="qcm")
    text = models.CharField("intitulé de la question", max_length=500, blank=True)
    # Textes des choix de réponse (questionnaire complet) : liste de chaînes
    choices = models.JSONField("choix de réponse", default=list, blank=True)
    points = models.FloatField("barème (points)", default=1.0)
    # Pour les QCM : index de la bonne réponse (0=A, 1=B, ...)
    correct_choice = models.PositiveSmallIntegerField("bonne réponse", null=True, blank=True)
    # Nombre de cases quand le texte des choix n'est pas saisi (grille seule)
    num_choices = models.PositiveSmallIntegerField(
        "nombre de choix", null=True, blank=True,
        validators=[MinValueValidator(2), MaxValueValidator(MAX_CHOICES)],
        help_text="Nombre de cases A, B, C… de cette question (si le texte "
                  f"des choix n'est pas saisi) ; 2 à {MAX_CHOICES}.")
    # Hauteur de la zone de réponse manuscrite sur la fiche (mm)
    open_height_mm = models.PositiveSmallIntegerField(
        "hauteur zone réponse (mm)", default=25,
        validators=[MinValueValidator(10), MaxValueValidator(MAX_OPEN_HEIGHT_MM)],
        help_text=f"10 à {MAX_OPEN_HEIGHT_MM} mm (une zone plus haute ne "
                  "tiendrait pas sur une page A4).")

    class Meta:
        verbose_name = "question"
        verbose_name_plural = "questions"
        ordering = ["order"]
        unique_together = [("quiz", "order")]

    def __str__(self):
        return f"Q{self.order} ({self.get_qtype_display()})"

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
    STATUS = [("pending", "En attente de correction"),
              ("processing", "Correction en cours"),
              ("done", "Terminé"),
              ("error", "Erreur")]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,
                             related_name="batches", verbose_name="épreuve")
    label = models.CharField("libellé", max_length=120, blank=True)
    status = models.CharField("état", max_length=12, choices=STATUS, default="done")
    total_pages = models.PositiveIntegerField("pages à traiter", default=0)
    processed_pages = models.PositiveIntegerField("pages traitées", default=0)
    error_message = models.TextField("message d'erreur", blank=True)
    # fichiers d'origine conservés pour la reprise d'un traitement interrompu
    source_files = models.JSONField(default=list, blank=True, editable=False)
    created_at = models.DateTimeField("reçu le", auto_now_add=True)
    updated_at = models.DateTimeField("mis à jour le", auto_now=True)

    class Meta:
        verbose_name = "lot de scans"
        verbose_name_plural = "lots de scans"
        ordering = ["-created_at"]

    def __str__(self):
        return self.label or f"Lot du {self.created_at:%d/%m/%Y %H:%M}"

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
    STATUS = [("pending", "En attente"),
              ("ok", "Lu avec succès"),
              ("no_markers", "Repères non détectés"),
              ("no_match", "Étudiant non identifié"),
              ("error", "Erreur")]
    batch = models.ForeignKey(ScanBatch, on_delete=models.CASCADE,
                              related_name="sheets", verbose_name="lot")
    page_index = models.PositiveSmallIntegerField("page de la fiche", default=0)
    source_name = models.CharField("fichier d'origine", max_length=200, blank=True)
    image = models.ImageField("image scannée", upload_to="scans/")
    warped_image = models.ImageField("image redressée", upload_to="warped/",
                                     null=True, blank=True)
    overlay_image = models.ImageField("image de contrôle", upload_to="overlays/",
                                      null=True, blank=True)
    name_crop = models.ImageField("zone du nom", upload_to="crops/",
                                  null=True, blank=True)
    status = models.CharField("état", max_length=12, choices=STATUS,
                              default="pending")
    id_read = models.CharField("n° d'inscription lu", max_length=20, blank=True)
    ocr_name_raw = models.CharField("nom lu (OCR)", max_length=200, blank=True)
    match_score = models.FloatField("score de rapprochement", default=0.0)
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="sheets", verbose_name="étudiant")
    student_confirmed = models.BooleanField("identification confirmée", default=False)
    error_message = models.TextField("message d'erreur", blank=True)
    created_at = models.DateTimeField("reçue le", auto_now_add=True)

    class Meta:
        verbose_name = "page scannée"
        verbose_name_plural = "pages scannées"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.source_name} (page {self.page_index + 1})"


class AnswerKeySheet(models.Model):
    """Page d'un corrigé scanné : la fiche d'épreuve remplie avec les bonnes
    réponses, passée au scanner comme une copie ordinaire.

    Chaque page téléversée est conservée avec le quiz — image d'origine et
    image de contrôle — pour garder la trace de l'origine du barème : c'est
    elle qui fait foi en cas de contestation après l'épreuve."""
    STATUS = [("ok", "Corrigé lu"),
              ("partial", "Corrigé lu partiellement"),
              ("no_markers", "Repères non détectés"),
              ("error", "Erreur")]
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE,
                             related_name="answer_key_sheets",
                             verbose_name="épreuve")
    page_index = models.PositiveSmallIntegerField("page de la fiche", default=0)
    source_name = models.CharField("fichier d'origine", max_length=200, blank=True)
    image = models.ImageField("image scannée", upload_to="corriges/")
    overlay_image = models.ImageField("image de contrôle", upload_to="corriges/",
                                      null=True, blank=True)
    status = models.CharField("état", max_length=12, choices=STATUS, default="ok")
    # {numéro de question: index de la bonne réponse lue}
    detected = models.JSONField("réponses lues", default=dict, blank=True)
    # numéros des questions illisibles sur cette page (vide ou plusieurs cases)
    unreadable = models.JSONField("questions illisibles", default=list, blank=True)
    error_message = models.TextField("message d'erreur", blank=True)
    created_at = models.DateTimeField("scanné le", auto_now_add=True)

    class Meta:
        verbose_name = "corrigé scanné"
        verbose_name_plural = "corrigés scannés"
        ordering = ["-created_at", "page_index"]

    def __str__(self):
        return f"{self.source_name} (page {self.page_index + 1})"

    @property
    def nb_lues(self):
        return len(self.detected or {})


class Answer(models.Model):
    sheet = models.ForeignKey(SheetScan, on_delete=models.CASCADE,
                              related_name="answers", verbose_name="copie")
    question = models.ForeignKey(Question, on_delete=models.CASCADE,
                                 related_name="answers", verbose_name="question")
    # QCM
    detected_choice = models.SmallIntegerField(  # 0=A ... ; None = vide
        "case lue", null=True, blank=True)
    is_multiple = models.BooleanField("plusieurs cases cochées", default=False)
    fill_ratios = models.JSONField("taux de remplissage", null=True, blank=True)
    # Manuscrit
    open_crop = models.ImageField("zone manuscrite", upload_to="crops/",
                                  null=True, blank=True)
    # Notation
    points_awarded = models.FloatField("points attribués", null=True, blank=True)
    manually_set = models.BooleanField("note saisie à la main", default=False)

    class Meta:
        verbose_name = "réponse"
        verbose_name_plural = "réponses"
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
        return f"{self.sheet} — question {self.question.order}"

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
        ("note", "Note d'une réponse"),
        ("case_qcm", "Case de QCM rectifiée"),
        ("identification", "Attribution de la copie"),
        ("bonne_reponse", "Bonne réponse"),
        ("bareme", "Barème d'une question"),
        ("penalite", "Pénalité par mauvaise réponse"),
        ("question_supprimee", "Question supprimée"),
        ("copie_supprimee", "Copie supprimée"),
        ("lot_supprime", "Lot de copies supprimé"),
    ]
    quand = models.DateTimeField("date", auto_now_add=True, db_index=True)
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="+",
                               verbose_name="compte")
    auteur_nom = models.CharField("auteur", max_length=150)
    action = models.CharField("geste", max_length=20, choices=ACTIONS)
    quiz = models.ForeignKey(Quiz, on_delete=models.SET_NULL, null=True,
                             blank=True, related_name="modifications",
                             verbose_name="épreuve")
    quiz_titre = models.CharField("épreuve (titre)", max_length=200, blank=True)
    copie = models.ForeignKey(SheetScan, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name="modifications",
                              verbose_name="copie")
    objet = models.CharField("objet", max_length=200)
    avant = models.CharField("avant", max_length=200, blank=True)
    apres = models.CharField("après", max_length=200, blank=True)

    class Meta:
        verbose_name = "modification"
        # « modifications » et non « journal des modifications » : Django
        # compte avec ce mot (« 5 modifications »). Le titre « Journal des
        # modifications » est posé par l'administration (JournalAdmin).
        verbose_name_plural = "modifications"
        ordering = ["-quand", "-pk"]

    def __str__(self):
        return f"{self.get_action_display()} — {self.objet}"
