from django import forms
from django.contrib.auth.forms import AuthenticationForm

from . import importers
from .models import (MAX_CHOICES, MAX_OPEN_HEIGHT_MM, ClassGroup, Question,
                     Quiz, choice_letter)


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
        return [importers.split_name_line(line)
                for line in self.cleaned_data["text"].splitlines() if line.strip()]


class CandidateFileForm(forms.Form):
    """Import d'une liste de candidats depuis un fichier existant
    (Excel, CSV, Word ou PDF) — colonnes Nom, Prénom, N° d'inscription."""
    file = forms.FileField(
        label="Fichier (Excel, CSV, Word ou PDF)",
        widget=forms.ClearableFileInput(
            attrs={"accept": ",".join(importers.CANDIDATE_EXTS)}),
        help_text="Colonnes attendues : Nom, Prénom, N° d'inscription (voir "
                  "modele_candidats.xlsx). La première ligne peut être un en-tête "
                  "(détecté automatiquement). Word : un tableau, ou une ligne par "
                  "candidat ; PDF : une ligne par candidat.")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not (f.name or "").lower().endswith(importers.CANDIDATE_EXTS):
            raise forms.ValidationError(
                "Formats acceptés : .xlsx, .csv, .docx, .pdf ou .txt.")
        return f

    def parse(self):
        """Retourne une liste de tuples (nom, prénom, numéro)."""
        f = self.cleaned_data["file"]
        return importers.parse_candidates_file(f.name, f.read())


# Ancien nom (import Excel seul) conservé pour compatibilité
CandidateXlsxForm = CandidateFileForm


class QuestionImportForm(forms.Form):
    """Import d'un questionnaire existant (Excel, CSV, Word, PDF, texte)."""
    file = forms.FileField(
        label="Questionnaire (Excel, Word, PDF ou texte)",
        widget=forms.ClearableFileInput(
            attrs={"accept": ",".join(importers.QUESTION_EXTS)}))


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


class QuizForm(forms.ModelForm):
    """Création d'un quiz de classe ou d'un concours.

    Le nombre de choix se règle question par question (il suit les choix
    saisis) ; le mode concours est déterminé par la page d'origine (menu
    « Concours »), pas par une case à cocher."""

    class Meta:
        model = Quiz
        fields = ["title", "class_group", "language", "sheet_mode",
                  "id_mode", "id_digits", "grading_mode", "wrong_penalty"]
        widgets = {"grading_mode": forms.RadioSelect}
        field_classes = {"wrong_penalty": DecimalField}
        labels = {"id_digits": "Nombre de chiffres du n° d'inscription"}
        help_texts = {"id_digits": "Taille de la grille à noircir sur la fiche "
                                   "(ex. 6 pour un n° comme 104523)."}

    def __init__(self, *args, user=None, concours=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.concours = concours
        if concours:
            # un concours a sa propre liste de candidats, créée automatiquement
            del self.fields["class_group"]
            self.fields["id_mode"].choices = [
                c for c in Quiz.ID_MODES if c[0] in CONCOURS_ID_MODES]
            self.fields["id_mode"].initial = "sticker"
        else:
            # quiz de classe : correction toujours immédiate
            del self.fields["grading_mode"]
            qs = self.fields["class_group"].queryset
            if user is not None and not user.is_superuser:
                qs = qs.filter(owner=user)
            self.fields["class_group"].queryset = qs
            self.fields["class_group"].empty_label = "— choisir une classe —"
            self.fields["class_group"].required = True


class QuestionForm(forms.ModelForm):
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

    # Ce champ ne sert QUE lorsque le texte des choix n'est pas saisi — cas
    # de la « fiche de réponses seule », où la fiche ne porte qu'une rangée
    # de cases et où le sujet est distribué à part. Dès qu'on tape des choix,
    # le nombre de cases suit ce qui est tapé et le champ est masqué par le
    # formulaire (voir quiz_detail.html). C'est le seul réglage du nombre de
    # cases : Quiz.num_choices n'est exposé nulle part.
    num_choices = forms.IntegerField(
        label="Nombre de cases à imprimer", initial=4, min_value=2,
        max_value=MAX_CHOICES, required=False,
        help_text="Combien de cases A, B, C… imprimer devant cette question, "
                  "puisque vous ne saisissez pas le texte des choix.")

    class Meta:
        model = Question
        fields = ["qtype", "text", "choices_text", "num_choices", "correct_letter",
                  "points", "open_height_mm"]
        field_classes = {"points": DecimalField}
        widgets = {"open_height_mm": forms.NumberInput(
            attrs={"min": 10, "max": MAX_OPEN_HEIGHT_MM})}

    def __init__(self, *args, quiz=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.quiz = quiz
        lang = quiz.language if quiz else "fr"
        # Le nombre de réponses possibles suit le nombre de choix saisis,
        # sinon le « Nombre de choix » de la question.
        n = 4
        if self.is_bound:
            raw = self.data.get(self.add_prefix("choices_text"), "")
            typed = [l for l in raw.splitlines() if l.strip()]
            try:
                n = int(self.data.get(self.add_prefix("num_choices")) or 4)
            except ValueError:
                n = 4
            if typed:
                n = len(typed)
            n = min(max(n, 2), MAX_CHOICES)
        self.fields["correct_letter"].choices = \
            [("", "—")] + [(str(i), choice_letter(i, lang)) for i in range(n)]
        # Quiz en arabe : saisie de droite à gauche pour l'intitulé et les choix
        if quiz and quiz.language == "ar":
            for name in ("text", "choices_text"):
                self.fields[name].widget.attrs.update(
                    {"dir": "rtl", "style": "text-align:right"})
            self.fields["choices_text"].widget.attrs["placeholder"] = \
                "مثال:\nتونس\nصفاقس\nسوسة\nبنزرت"

    def clean(self):
        data = super().clean()
        if data.get("qtype") == "qcm":
            # La bonne réponse est FACULTATIVE : elle peut être renseignée
            # plus tard, à la main ou en scannant la fiche remplie avec le
            # corrigé. La correction des copies reste bloquée tant qu'il en
            # manque une (voir services.corrige_incomplet).
            sans_corrige = data.get("correct_letter") in ("", None)
            choices = [l.strip() for l in data.get("choices_text", "").splitlines()
                       if l.strip()]
            data["choices_list"] = choices
            data["num_choices"] = len(choices) or data.get("num_choices") or 4
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
    points = DecimalField(label="Barème par question", initial=1.0, min_value=0)

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


class MultiFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultiFileInput(attrs={"multiple": True}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        if isinstance(data, (list, tuple)):
            return [super(MultiFileField, self).clean(d, initial) for d in data]
        return [super().clean(data, initial)]


class UploadForm(forms.Form):
    label = forms.CharField(label="Libellé du lot", required=False,
                            widget=forms.TextInput(attrs={"placeholder": "Ex. Groupe A — 28/08"}))
    files = MultiFileField(
        label="Fichiers scannés (PDF ou images)",
        help_text="Vous pouvez sélectionner plusieurs fichiers. "
                  "Un PDF multi-pages est accepté (une copie par page), sans "
                  "limite de nombre : la correction démarre dès l'envoi.")


class AdminLoginForm(AuthenticationForm):
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
