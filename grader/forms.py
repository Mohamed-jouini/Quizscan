from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm

from . import importers, pieces
from .securite import FreinConnexion
from .models import (MAX_CHOICES, MAX_OPEN_HEIGHT_MM, POINTS_QCM, ClassGroup,
                     Communique, Inscrit, PieceJointe, Question, Quiz, choice_letter)


class DecimalField(forms.FloatField):
    """Nombre décimal acceptant la virgule française (« 1,5 ») comme le
    point (« 1.5 »)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal"}))
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return super().to_python(value)

    def prepare_value(self, value):
        # 1.0 -> « 1 », 1.5 -> « 1,5 »
        if isinstance(value, float):
            return f"{value:g}".replace(".", ",")
        return value


class ClassGroupForm(forms.ModelForm):
    class Meta:
        model = ClassGroup
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={
            "placeholder": "Nouvelle classe, ex. 3ème A — Sciences",
            "aria-label": "Nom de la nouvelle classe"})}


def _texte(exemple, **attrs):
    """Champ de texte du registre : sens d'écriture suivi de ce qui est saisi
    (les noms et les lieux s'écrivent souvent en arabe)."""
    return forms.TextInput(attrs={"placeholder": exemple, "autocomplete": "off",
                                  "dir": "auto", **attrs})


def _date_passee():
    # « max » : sans lui, Chrome laisse six chiffres à l'année au clavier.
    return forms.DateInput(attrs={"type": "date", "min": "1900-01-01", "max": "9999-12-31"},
                           format="%Y-%m-%d")


class InscritForm(forms.ModelForm):
    """Inscription d'un étudiant au registre de l'établissement : les colonnes
    de la liste officielle de l'institut (voir importers.modele_candidats_xlsx)."""

    class Meta:
        model = Inscrit
        fields = ["numero", "nom", "prenom", "cin", "cin_lieu", "cin_date",
                  "date_naissance", "centre", "salle", "email", "etablissement"]
        labels = {"cin_lieu": "Lieu de délivrance", "cin_date": "Date de délivrance"}
        widgets = {
            "numero": _texte("104523"),
            "nom": _texte("BEN SALEM"),
            "prenom": _texte("Samia"),
            "cin": _texte("09876543", inputmode="numeric"),
            "cin_lieu": _texte("Tunis"),
            "cin_date": _date_passee(),
            "date_naissance": _date_passee(),
            "centre": _texte("Lycée pilote de Sfax"),
            "salle": _texte("Salle 12"),
            "email": forms.EmailInput(attrs={"placeholder": "prenom.nom@exemple.tn",
                                             "autocomplete": "off"}),
            "etablissement": _texte("Faculté de droit de Tunis"),
        }
        help_texts = {"cin": "", "centre": "", "etablissement": ""}
        error_messages = {"numero": {
            "unique": "Ce n° d'inscription est déjà attribué (voir la liste)."}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # L'identité de la liste officielle est obligatoire (demande de
        # l'utilisateur, 5 novembre 2026 ; e-mail et établissement l'étaient
        # depuis le 30 octobre). Le centre et la salle viennent souvent après
        # l'inscription. Le modèle garde tout facultatif pour les fiches
        # inscrites avant, qui se complètent par « Modifier ».
        for nom in ("cin", "cin_lieu", "cin_date", "date_naissance"):
            self.fields[nom].required = True
            self.fields[nom].error_messages["required"] = "Champ obligatoire."

    def clean_numero(self):
        from .inscriptions import numero_propre
        numero = numero_propre(self.cleaned_data["numero"])
        if not numero:
            raise forms.ValidationError("Indiquez le n° d'inscription.")
        return numero

    def clean_nom(self):
        return " ".join(self.cleaned_data["nom"].split()).upper()

    def clean_prenom(self):
        return " ".join(self.cleaned_data["prenom"].split())

    def clean_cin(self):
        """CIN (8 chiffres) ou passeport : lettres et chiffres, sans espaces ;
        une même CIN ne peut appartenir qu'à un seul inscrit."""
        from .inscriptions import cin_propre, cin_valide
        cin = cin_propre(self.cleaned_data.get("cin", ""))
        if not cin_valide(cin):
            raise forms.ValidationError(
                "N° de CIN invalide : 8 chiffres (ou n° de passeport).")
        autre = Inscrit.objects.filter(cin=cin).exclude(pk=self.instance.pk).first()
        if autre is not None:
            raise forms.ValidationError(
                f"Cette CIN est déjà celle de {autre.nom_complet} (n° {autre.numero}).")
        return cin

    def _date_vraisemblable(self, champ, libelle):
        import datetime
        date = self.cleaned_data.get(champ)
        if date and not datetime.date(1900, 1, 1) <= date <= datetime.date.today():
            raise forms.ValidationError(f"{libelle} invraisemblable.")
        return date

    def clean_date_naissance(self):
        return self._date_vraisemblable("date_naissance", "Date de naissance")

    def clean_cin_date(self):
        return self._date_vraisemblable("cin_date", "Date de délivrance")

    def clean(self):
        donnees = super().clean()
        for champ in ("cin_lieu", "centre", "salle", "etablissement"):
            if donnees.get(champ):
                donnees[champ] = " ".join(donnees[champ].split())
        naissance, delivrance = donnees.get("date_naissance"), donnees.get("cin_date")
        if naissance and delivrance and delivrance < naissance:
            self.add_error("cin_date", "La délivrance ne peut pas précéder la naissance.")
        return donnees


class EnvoiMultiple(forms.FileInput):
    allow_multiple_selected = True


class FichiersField(forms.FileField):
    """Plusieurs fichiers dans un seul champ : la liste des fichiers envoyés."""
    widget = EnvoiMultiple

    def clean(self, data, initial=None):
        envoyes = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        return [super(FichiersField, self).clean(f, initial) for f in envoyes]


class CommuniqueForm(forms.ModelForm):
    """Avis publié sur l'espace public, avec ses pièces jointes (images,
    vidéos, documents : grader/pieces.py)."""
    fichiers = FichiersField(
        label="Pièces jointes", required=False,
        widget=EnvoiMultiple(attrs={"accept": pieces.ACCEPT}))
    retirer = forms.ModelMultipleChoiceField(
        label="Retirer", required=False, queryset=PieceJointe.objects.none(),
        widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = Communique
        fields = ["titre", "texte", "publie"]
        widgets = {"texte": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["retirer"].queryset = self.instance.pieces.all()

    def clean_fichiers(self):
        fichiers = self.cleaned_data.get("fichiers") or []
        erreurs = [e for e in (pieces.controler(f) for f in fichiers) if e]
        if erreurs:
            raise forms.ValidationError(erreurs)
        return fichiers

    def clean(self):
        donnees = super().clean()
        gardees = (self.instance.pieces.count() if self.instance.pk else 0) \
            - len(donnees.get("retirer") or [])
        if gardees + len(donnees.get("fichiers") or []) > pieces.MAX_PAR_COMMUNIQUE:
            self.add_error("fichiers", f"{pieces.MAX_PAR_COMMUNIQUE} pièces jointes "
                                       f"au plus par communiqué.")
        return donnees

    def enregistrer_pieces(self, avis):
        """Après avis.save() : retire les pièces cochées, ajoute les nouvelles."""
        for piece in self.cleaned_data.get("retirer") or []:
            piece.delete()
        for f in self.cleaned_data.get("fichiers") or []:
            PieceJointe.objects.create(
                communique=avis, fichier=f, nom=f.name[:200],
                genre=pieces.type_de(f.name)[0], taille=f.size)


class StudentImportForm(forms.Form):
    text = forms.CharField(
        label="Liste des étudiants",
        widget=forms.Textarea(attrs={
            "rows": 8,
            "placeholder": "Un étudiant par ligne : NOM;Prénom;N°  ou  NOM Prénom N°\n"
                           "Ex.\nBEN SALAH;Ahmed;104523\nTRABELSI Mariem 104524"}),
        help_text="Copier-coller depuis un tableur ou un document : « NOM;Prénom;N° », "
                  "colonnes séparées par des tabulations, ou « NOM Prénom N° » "
                  "(les mots en MAJUSCULES forment le nom). Le n° est optionnel.")

    def parse(self):
        return [importers.fiche_simple(*importers.split_name_line(line))
                for line in self.cleaned_data["text"].splitlines() if line.strip()]


class CandidateFileForm(forms.Form):
    """Import d'une liste de candidats depuis un fichier existant : la liste
    officielle de l'institut (Excel), ou Excel, CSV, Word, PDF à colonnes
    Nom, Prénom, N° d'inscription."""
    file = forms.FileField(
        label="Fichier (Excel, CSV, Word ou PDF)",
        widget=forms.ClearableFileInput(
            attrs={"accept": ",".join(importers.CANDIDATE_EXTS)}),
        help_text="Les colonnes se reconnaissent à leur intitulé, dans n'importe quel "
                  "ordre. Un titre peut précéder la ligne d'intitulés.")

    def clean_file(self):
        return _controler_import(self.cleaned_data["file"], importers.CANDIDATE_EXTS,
                                 ".xlsx, .csv, .docx, .pdf ou .txt")

    def parse(self):
        """Retourne les lignes du fichier : voir importers.rows_to_fiches."""
        f = self.cleaned_data["file"]
        return importers.parse_fiches_file(f.name, f.read())


# Ancien nom (import Excel seul) conservé pour compatibilité
CandidateXlsxForm = CandidateFileForm


class QuestionImportForm(forms.Form):
    """Import d'un questionnaire existant (Excel, CSV, Word, PDF, texte)."""
    file = forms.FileField(
        label="Questionnaire (Excel, Word, PDF ou texte)",
        widget=forms.ClearableFileInput(
            attrs={"accept": ",".join(importers.QUESTION_EXTS)}))

    def clean_file(self):
        return _controler_import(self.cleaned_data["file"], importers.QUESTION_EXTS,
                                 ", ".join(importers.QUESTION_EXTS))


class QuestionBankForm(forms.Form):
    """Banque de questions : reprendre les questions d'une épreuve précédente."""
    source = forms.ModelChoiceField(
        label="Reprendre les questions du quiz", queryset=Quiz.objects.none(),
        empty_label="— choisir un quiz —")
    orders = forms.CharField(
        label="Questions à reprendre (facultatif)", required=False,
        widget=forms.TextInput(attrs={"placeholder": "Ex. 1-5, 8, 12 — vide = toutes"}))

    def __init__(self, *args, quizzes=None, exclude=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = quizzes if quizzes is not None else Quiz.objects.all()
        if exclude is not None:
            qs = qs.exclude(pk=exclude.pk)
        self.fields["source"].queryset = qs.filter(questions__isnull=False).distinct()
        self.fields["source"].label_from_instance = \
            lambda q: f"{q.title} — {q.class_group.name} ({q.questions.count()} q.)"

    def clean_orders(self):
        raw = (self.cleaned_data.get("orders") or "").replace(" ", "")
        if not raw:
            return None
        wanted = set()
        for part in raw.split(","):
            if not part:
                continue
            try:
                if "-" in part:
                    a, b = (int(x) for x in part.split("-", 1))
                    wanted.update(range(min(a, b), max(a, b) + 1))
                else:
                    wanted.add(int(part))
            except ValueError:
                raise forms.ValidationError(
                    f"« {part} » n'est pas un numéro ou un intervalle valide.")
        return wanted


# Modes d'identification proposés pour un concours : la liste des candidats
# est importée, chacun reçoit son étiquette QR (ou noircit son n° d'inscription).
CONCOURS_ID_MODES = ["sticker", "grid"]

AIDE_ENTETE = ("Laissé vide : en-tête de l'institut (المعهد الأعلى للمحاماة). Sinon, une "
               "ligne par ligne : ministère, établissement… Imprimé dans le cartouche du sujet.")
# Le champ de date du navigateur n'accepte que ce format. Sans max, Chrome laisse
# 6 chiffres à l'année.
SAISIE_DATE = forms.DateInput(attrs={"type": "date", "min": "2000-01-01", "max": "9999-12-31"},
                              format="%Y-%m-%d")


def _champ_anonymat():
    """Présentation des copies nominatives : nom imprimé, talon à découper
    (solution 1) ou copie anonyme à n° de place (solution 2)."""
    return forms.ChoiceField(
        label="Présentation des copies", choices=Quiz.ANONYMATS, required=False,
        widget=forms.RadioSelect,
        help_text="Talon ou n° de place : une copie par étudiant, QR imprimé "
                  "(identification « QR code », même en concours). N° de place : "
                  "aucun nom sur la copie, une liste de placement et "
                  "d'émargement est imprimée pour le surveillant.")


def _appliquer_anonymat(form, data):
    """Reporte le choix sur l'épreuve (deux cases du modèle, exclusives)."""
    anonymat = data.get("anonymat") or ""
    form.instance.talon_nom = anonymat == "talon"
    form.instance.place_anonyme = anonymat == "place"
    if anonymat:
        data["id_mode"] = "qr"          # copies nominatives, QR imprimé


# Étapes d'un concours : QCM -> écrit -> oral. Le type qui précède chacune ; un
# QCM ouvre le concours, il ne suit aucune épreuve.
ETAPE_PRECEDENTE = {"ecrit": "qcm", "oral": "ecrit"}


class ChoixEpreuvePrecedente(forms.Select):
    """Chaque option porte le type de son épreuve : pages.js ne propose que
    l'étape qui précède le type choisi."""

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if value:
            option["attrs"]["data-type"] = value.instance.type_epreuve
        return option


class QuizForm(forms.ModelForm):
    """Création d'un quiz de classe ou d'un concours.

    Le nombre de choix se règle question par question (il suit les choix
    saisis) ; le mode concours est déterminé par la page d'origine (menu
    « Concours »), pas par une case à cocher."""

    anonymat = _champ_anonymat()

    class Meta:
        model = Quiz
        fields = ["title", "type_epreuve", "epreuve_precedente", "class_group",
                  "language", "note_sur",
                  "sheet_mode", "entete", "duree", "date_epreuve",
                  "id_mode", "id_digits", "anonymat", "grading_mode", "wrong_penalty"]
        widgets = {"grading_mode": forms.RadioSelect,
                   "type_epreuve": forms.RadioSelect,
                   "epreuve_precedente": ChoixEpreuvePrecedente,
                   "entete": forms.Textarea(attrs={"rows": 3}),
                   "date_epreuve": SAISIE_DATE}
        field_classes = {"wrong_penalty": DecimalField, "note_sur": DecimalField}
        labels = {"id_digits": "Nombre de chiffres du n° d'inscription"}
        help_texts = {"id_digits": "Taille de la grille à noircir sur la fiche "
                                   "(ex. 6 pour un n° comme 104523).",
                      "entete": AIDE_ENTETE}

    def __init__(self, *args, user=None, concours=False, precedentes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.concours = concours
        # Étape suivante d'un concours : les candidats sont les admis d'une
        # épreuve précédente (QCM -> écrit -> oral). Seules les épreuves de
        # l'enseignant qui ont une note minimale sont proposées.
        prec = self.fields["epreuve_precedente"]
        prec.queryset = (precedentes if precedentes is not None
                         else Quiz.objects.none()).filter(note_admission__isnull=False)
        prec.required = False
        prec.empty_label = "— aucune : tous les étudiants de la liste —"
        prec.label = "Candidats : admis de l'épreuve"
        prec.help_text = ("Concours en plusieurs étapes : les admis du QCM passent "
                          "à l'écrit, les admis de l'écrit passent à l'oral.")
        # Facultatifs à l'envoi : QCM sur 20 par défaut (formulaires et
        # scripts d'avant les épreuves écrites et orales).
        self.fields["type_epreuve"].required = False
        self.fields["note_sur"].required = False
        if concours:
            # un concours a sa propre liste de candidats, créée automatiquement
            del self.fields["class_group"]
            self.fields["id_mode"].choices = [
                c for c in Quiz.ID_MODES if c[0] in CONCOURS_ID_MODES]
            self.fields["id_mode"].initial = "sticker"
            # Chaque page demande une étiquette, et le questionnaire complet n'en
            # loge que 5 QCM : 50 QCM y feraient 10 pages, donc 10 étiquettes.
            self.fields["sheet_mode"].initial = "split"
        else:
            # quiz de classe : correction toujours immédiate
            del self.fields["grading_mode"]
            qs = self.fields["class_group"].queryset
            if user is not None and not user.is_superuser:
                qs = qs.filter(enseignants=user).distinct()
            self.fields["class_group"].queryset = qs
            self.fields["class_group"].empty_label = "— choisir une classe —"
            # avec une épreuve précédente, la liste des candidats (ses admis)
            # est créée pour cette épreuve : pas de classe à choisir
            self.fields["class_group"].required = not self.data.get("epreuve_precedente")
            self.fields["class_group"].help_text = (
                "Inutile avec « admis de l'épreuve » : la liste des admis est "
                "créée pour cette épreuve.")

    def _get_validation_exclusions(self):
        exclus = super()._get_validation_exclusions()
        if (getattr(self, "cleaned_data", {}) or {}).get("epreuve_precedente") is not None:
            exclus.add("class_group")       # renseignée par la vue
        return exclus

    def clean(self):
        data = super().clean()
        _appliquer_anonymat(self, data)
        precedente = data.get("epreuve_precedente")
        if precedente is not None and (precedente.type_epreuve
                                       != ETAPE_PRECEDENTE.get(data.get("type_epreuve"))):
            self.add_error("epreuve_precedente",
                           "Un QCM ne suit aucune épreuve, un écrit suit un QCM, "
                           "un oral suit un écrit.")
        return data

    def clean_type_epreuve(self):
        return self.cleaned_data.get("type_epreuve") or "qcm"

    def clean_note_sur(self):
        return self.cleaned_data.get("note_sur") or 20.0


class EpreuveEcritForm(forms.ModelForm):
    """Réglages d'une épreuve écrite : sujet, barème, copie à imprimer."""

    anonymat = _champ_anonymat()

    class Meta:
        model = Quiz
        fields = ["sujet_texte", "entete", "duree", "date_epreuve", "note_sur", "id_mode",
                  "id_digits", "anonymat", "pages_redaction", "format_impression"]
        widgets = {"sujet_texte": forms.Textarea(attrs={
                       "rows": 9, "placeholder": "Ex. Racontez un voyage qui vous a "
                                                 "marqué…\n\nVous décrirez les lieux "
                                                 "et vos impressions."}),
                   "entete": forms.Textarea(attrs={"rows": 3}),
                   "date_epreuve": SAISIE_DATE}
        field_classes = {"note_sur": DecimalField}
        labels = {"id_digits": "Nombre de chiffres du n° d'inscription",
                  "pages_redaction": "Pages lignées après la page de garde",
                  "format_impression": "Format des copies"}
        help_texts = {"entete": AIDE_ENTETE,
                      "format_impression": "A3 : la page de garde et 3 pages lignées sur une "
                                           "feuille, imprimée recto-verso sur le bord court, à "
                                           "100 %, puis pliée au milieu. Les pages lignées "
                                           "vont jusqu'à la fin de la feuille."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # facultatif à l'envoi : les formulaires d'avant ce choix gardent le format actuel
        self.fields["format_impression"].required = False
        if self.instance.auto_enroll:
            self.fields["id_mode"].choices = [
                c for c in Quiz.ID_MODES if c[0] in CONCOURS_ID_MODES]
        if self.instance.language == "ar":
            self.fields["sujet_texte"].widget.attrs["dir"] = "rtl"
        self.fields["anonymat"].initial = self.instance.anonymat

    def clean_format_impression(self):
        return self.cleaned_data.get("format_impression") or self.instance.format_impression

    def clean(self):
        data = super().clean()
        _appliquer_anonymat(self, data)
        return data


class EpreuveOralForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = ["note_sur"]
        field_classes = {"note_sur": DecimalField}


class QuestionForm(forms.ModelForm):
    # N° de la question : le suivant par défaut (à la fin) ; un n° déjà pris place la
    # question à ce rang (demande de l'utilisateur, 6 novembre 2026). Voir __init__.
    numero = forms.IntegerField(label="N° de la question", required=False, min_value=1)
    correct_letter = forms.ChoiceField(
        label="Bonne réponse (facultatif)", required=False,
        help_text="Laissez vide si vous préférez renseigner le corrigé plus "
                  "tard, à la main ou en scannant la fiche remplie avec les "
                  "bonnes réponses.")
    choices_text = forms.CharField(
        label="Choix de réponse (un par ligne)", required=False,
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "Ex.\nParis\nLyon\nMarseille\nToulouse"}),
        help_text="Le texte de chaque choix, imprimé à côté de sa case. Le "
                  "nombre de cases suit alors le nombre de lignes saisies. "
                  "Laissez vide pour n'imprimer qu'une rangée de cases "
                  "(fiche de réponses seule, sujet distribué à part).")

    # Pas de champ « Nombre de cases à imprimer » (retiré à la demande de
    # l'utilisateur, 11 octobre 2026) : le nombre de cases suit les choix
    # saisis ; sans texte de choix, c'est celui du quiz (Quiz.num_choices,
    # 4 par défaut). L'« Ajout rapide » garde son propre nombre de choix.

    class Meta:
        model = Question
        fields = ["numero", "qtype", "text", "choices_text", "correct_letter",
                  "points", "open_height_mm"]
        field_classes = {"points": DecimalField}
        widgets = {"open_height_mm": forms.NumberInput(
            attrs={"min": 10, "max": MAX_OPEN_HEIGHT_MM})}

    def __init__(self, *args, quiz=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quiz = quiz
        lang = quiz.language if quiz else "fr"
        # Le nombre de réponses possibles suit le nombre de choix saisis,
        # sinon celui du quiz.
        n = self._cases_par_defaut()
        if self.is_bound:
            raw = self.data.get(self.add_prefix("choices_text"), "")
            typed = [l for l in raw.splitlines() if l.strip()]
            if typed:
                n = len(typed)
            n = min(max(n, 2), MAX_CHOICES)
        self.fields["correct_letter"].choices = \
            [("", "—")] + [(str(i), choice_letter(i, lang)) for i in range(n)]
        # Une QCM vaut toujours 1 point : le barème ne concerne que les
        # réponses manuscrites (champ masqué pour une QCM).
        self.fields["points"].required = False
        self.fields["points"].label = "Barème de la réponse manuscrite (points)"
        self.fields["points"].help_text = "Une QCM vaut toujours 1 point."
        # N° : plus au choix dès qu'une copie est scannée. Une fiche imprimée associe
        # chaque rangée de cases à un n° de question (rien sur la fiche ne dit avec
        # quelle version des questions elle a été imprimée) : insérer une question
        # ferait lire décalées les copies qui restent à scanner.
        self.prochain_numero, self.insertion_possible = 1, True
        if quiz is not None and quiz.pk:
            dernier = quiz.questions.order_by("-order").values_list("order", flat=True).first()
            self.prochain_numero = (dernier or 0) + 1
            self.insertion_possible = not quiz.batches.exists()
        numero = self.fields["numero"]
        numero.initial = self.prochain_numero
        numero.widget.attrs.update({"min": 1, "max": self.prochain_numero})
        if self.insertion_possible:
            numero.help_text = (
                f"{self.prochain_numero} : à la fin. Un n° déjà pris place la question à "
                "ce rang, et les questions suivantes prennent un n° de plus.")
        else:
            numero.disabled = True
            numero.help_text = (
                "Des copies sont déjà scannées : la question se place à la fin, pour ne "
                "pas décaler la lecture des fiches déjà imprimées.")
        # Quiz en arabe : saisie de droite à gauche pour l'intitulé et les choix
        if quiz and quiz.language == "ar":
            for name in ("text", "choices_text"):
                self.fields[name].widget.attrs.update(
                    {"dir": "rtl", "style": "text-align:right"})
            self.fields["choices_text"].widget.attrs["placeholder"] = \
                "مثال:\nتونس\nصفاقس\nسوسة\nبنزرت"

    def clean_numero(self):
        numero = self.cleaned_data.get("numero")
        if numero is None or not self.insertion_possible:
            return self.prochain_numero
        if numero > self.prochain_numero:
            raise forms.ValidationError(
                f"Entre 1 et {self.prochain_numero} ({self.prochain_numero} : à la fin).")
        return numero

    def _cases_par_defaut(self):
        """Nombre de cases d'une QCM dont le texte des choix n'est pas saisi."""
        n = getattr(self.quiz, "num_choices", None) or 4
        return min(max(n, 2), MAX_CHOICES)

    def clean(self):
        data = super().clean()
        if data.get("qtype") == "qcm" or data.get("points") is None:
            data["points"] = POINTS_QCM
        if data.get("qtype") == "qcm":
            # La bonne réponse est FACULTATIVE : elle peut être renseignée
            # plus tard, à la main ou en scannant la fiche remplie avec le
            # corrigé. La correction des copies reste bloquée tant qu'il en
            # manque une (voir services.corrige_incomplet).
            sans_corrige = data.get("correct_letter") in ("", None)
            choices = [l.strip() for l in data.get("choices_text", "").splitlines()
                       if l.strip()]
            data["choices_list"] = choices
            data["num_choices"] = len(choices) or self._cases_par_defaut()
            if sans_corrige:
                return data
            if not choices and int(data["correct_letter"]) >= data["num_choices"]:
                raise forms.ValidationError(
                    "La bonne réponse dépasse le nombre de choix.")
            if choices:
                if len(choices) < 2:
                    raise forms.ValidationError("Saisissez au moins deux choix.")
                if len(choices) > MAX_CHOICES:
                    raise forms.ValidationError(
                        f"Maximum {MAX_CHOICES} choix par question.")
                if int(data["correct_letter"]) >= len(choices):
                    raise forms.ValidationError(
                        "La bonne réponse dépasse le nombre de choix saisis.")
        else:
            data["choices_list"] = []
            data["num_choices"] = None
        return data


class BulkQcmForm(forms.Form):
    answer_key = forms.CharField(
        label="Corrigé (une lettre par question)",
        widget=forms.TextInput(attrs={"placeholder": "Ex. ABCADBBC — crée 8 questions QCM"}),
        help_text="Saisissez la suite des bonnes réponses ; une question QCM est créée par lettre.")
    num_choices = forms.IntegerField(
        label="Nombre de choix par question", initial=4, min_value=2,
        max_value=MAX_CHOICES)
    def __init__(self, *args, quiz=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quiz = quiz

    # correspondance lettres arabes -> indices (saisie du corrigé en arabe acceptée)
    # Lettres arabes acceptées dans le corrigé (ordre alphabétique hijā'ī),
    # + tolérance pour l'ancien ordre abjadi (ج/د)
    ARABIC_MAP = {"أ": 0, "ا": 0, "ب": 1, "ت": 2, "ث": 3, "ج": 4, "ح": 5,
                  "خ": 6, "د": 7, "ذ": 8, "ر": 9}

    def clean(self):
        data = super().clean()
        if "answer_key" in data and data.get("num_choices"):
            data["answer_key"] = self._parse_key(data["answer_key"], data["num_choices"])
        return data

    def _parse_key(self, raw, n):
        raw = raw.replace(" ", "").replace("ـ", "").upper()
        key, bad = "", []
        for ch in raw:
            if ch in self.ARABIC_MAP and self.ARABIC_MAP[ch] < n:
                key += chr(ord("A") + self.ARABIC_MAP[ch])
            elif "A" <= ch < chr(ord("A") + n):
                key += ch
            else:
                bad.append(ch)
        if bad:
            self.add_error("answer_key",
                           f"Lettres non valides : {', '.join(sorted(set(bad)))} "
                           f"(choix possibles : A–{chr(ord('A') + n - 1)} ou أ، ب، ج…)")
        return key


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


# Scans acceptés (copies, corrigé) : PDF et images. Le fichier d'origine est
# conservé et resservi : jamais de HTML, de SVG ni d'autre type actif.
SCAN_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
TAILLE_MAX_SCAN = 500 * 1024 * 1024        # par fichier (un PDF de tout un lot)
TAILLE_MAX_IMPORT = 20 * 1024 * 1024       # liste d'étudiants, questionnaire


def _controler_import(f, extensions, formats):
    if not (f.name or "").lower().endswith(extensions):
        raise forms.ValidationError(f"Formats acceptés : {formats}.")
    if f.size > TAILLE_MAX_IMPORT:
        raise forms.ValidationError(
            f"Fichier trop lourd ({f.size // (1024 * 1024)} Mo) : "
            f"{TAILLE_MAX_IMPORT // (1024 * 1024)} Mo au plus.")
    return f


class MultiFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultiFileInput(
            attrs={"multiple": True, "accept": ",".join(SCAN_EXTS),
                   "data-max-fichiers": settings.DATA_UPLOAD_MAX_NUMBER_FILES}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if isinstance(data, (list, tuple)):
            fichiers = [super(MultiFileField, self).clean(d, initial) for d in data]
        else:
            fichiers = [super().clean(data, initial)]
        for f in fichiers:
            if f is None:
                continue
            if not (f.name or "").lower().endswith(SCAN_EXTS):
                raise forms.ValidationError(
                    f"« {f.name} » : seuls les PDF et les images (JPG, PNG, TIFF, "
                    f"BMP, WebP) sont acceptés.")
            if f.size > TAILLE_MAX_SCAN:
                raise forms.ValidationError(
                    f"« {f.name} » : {f.size // (1024 * 1024)} Mo, au-delà des "
                    f"{TAILLE_MAX_SCAN // (1024 * 1024)} Mo permis par fichier.")
        return fichiers


class UploadForm(forms.Form):
    label = forms.CharField(label="Libellé du lot", required=False, max_length=120,
                            widget=forms.TextInput(attrs={"placeholder": "Ex. Groupe A — 28/08"}))
    files = MultiFileField(
        label="Fichiers scannés (PDF ou images)",
        help_text="Vous pouvez sélectionner plusieurs fichiers : 10 000 au plus par "
                  "envoi, 500 Mo au plus chacun. Un PDF ou un TIFF multi-pages est "
                  "accepté (une copie par page). Au-delà, faites plusieurs envois.")


class ConnexionForm(FreinConnexion, AuthenticationForm):
    """Connexion enseignant, essais limités (grader/securite.py)."""


class AdminLoginForm(FreinConnexion, AuthenticationForm):
    """Connexion à l'espace d'administration.

    Refuse les comptes enseignants ordinaires : sans ce contrôle, un
    enseignant qui se trompe de page se verrait connecté puis renvoyé vers
    une interface d'administration qu'il n'a pas le droit d'ouvrir, sans
    comprendre pourquoi. Le message l'oriente vers la bonne page."""

    error_messages = dict(AuthenticationForm.error_messages, **{
        "not_admin": "Ce compte n'est pas un compte d'administration. "
                     "Utilisez la page « Connexion enseignant ».",
    })

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError(
                self.error_messages["not_admin"], code="not_admin")


class AnswerKeyUploadForm(forms.Form):
    """Téléversement de la fiche d'épreuve remplie avec les bonnes réponses.

    Elle se scanne comme une copie : mêmes repères, mêmes cases. Plusieurs
    fichiers ou un PDF multi-pages sont acceptés, pour une fiche recto-verso
    ou sur plusieurs pages."""
    files = MultiFileField(
        label="Fiche du corrigé scannée (PDF ou images)",
        help_text="Imprimez la fiche d'épreuve, noircissez les bonnes "
                  "réponses, scannez-la et déposez-la ici. Les cases lues "
                  "renseignent le corrigé ; la fiche est conservée avec "
                  "l'épreuve.")
