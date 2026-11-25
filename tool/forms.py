from django import forms

from .models import Assignment
from .utils import MAX_UPLOAD_BYTES, allowed_extensions, extension

LIMITS = {
    "objective_count": (1, 10),
    "questions_per_objective": (1, 5),
    "retries_per_objective": (0, 3),
    "seconds_per_question": (5, 300),
    "time_limit_minutes": (1, 180),
    "max_attempts": (0, 20),
}


class SettingsForm(forms.ModelForm):
    class Meta:
        model = Assignment
        fields = [
            "title",
            "brief",
            "objective_count",
            "questions_per_objective",
            "retries_per_objective",
            "time_each_question",
            "seconds_per_question",
            "time_limit_minutes",
            "max_attempts",
            "room_word_required",
            "code_viva",
            "ai_policy",
        ]
        labels = {
            "brief": "Brief",
            "objective_count": "Learning objectives",
            "questions_per_objective": "Questions per objective",
            "retries_per_objective": "Retries per objective",
            "seconds_per_question": "Seconds per question",
            "time_each_question": "Time each question",
            "time_limit_minutes": "Quiz time limit (minutes)",
            "max_attempts": "Attempts allowed",
            "room_word_required": "Require room word",
            "code_viva": "Code viva",
            "ai_policy": "Generative AI policy",
        }
        help_texts = {
            "ai_policy": "Students must tick to say they have read and agree to this before handing in. Leave blank to skip it.",
            "brief": "What students were asked to do, and what the objectives should focus on.",
            "questions_per_objective": "Correct answers needed to clear an objective.",
            "retries_per_objective": "Extra questions after a wrong answer.",
            "max_attempts": "0 for unlimited.",
            "time_each_question": "Each question has its own countdown and moves on when it runs out. Turn off to give one time limit for the whole quiz instead.",
            "time_limit_minutes": "When it runs out, the current answer is submitted and any objectives not reached score 0.",
            "code_viva": "For programming work. Students can upload source files, notebooks or a .zip of their project, and questions can show short code snippets. A question with a snippet gets 2 extra seconds per line shown (up to 30).",
            "room_word_required": "For invigilated sessions. Show the room word on a screen; students type it to start and again once or twice at random.",
        }
        widgets = {"brief": forms.Textarea(attrs={"rows": 6}), "ai_policy": forms.Textarea(attrs={"rows": 10})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].required = True
        # Hidden while questions are timed individually, so it may be left out: keep the current value.
        self.fields["time_limit_minutes"].required = False
        for name, (low, high) in LIMITS.items():
            self.fields[name].widget.attrs.update(min=low, max=high)

    def clean_time_limit_minutes(self):
        value = self.cleaned_data.get("time_limit_minutes")
        return value if value is not None else (self.instance.time_limit_minutes or 10)

    def clean(self):
        cleaned = super().clean()
        for name, (low, high) in LIMITS.items():
            value = cleaned.get(name)
            if value is not None and not low <= value <= high:
                self.add_error(name, f"Choose between {low} and {high}.")
        return cleaned


class UploadForm(forms.Form):
    file = forms.FileField(label="Your work")

    def __init__(self, *args, code=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.code = code
        self.allowed = allowed_extensions(code)
        self.fields["file"].widget.attrs["accept"] = ",".join(sorted(self.allowed))
        if code:
            self.fields["file"].label = "Your code"

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if extension(upload.name) not in self.allowed:
            raise forms.ValidationError(
                "Upload source files as a .zip, or a single source file, notebook, PDF, Word or text file."
                if self.code else "Upload a PDF, Word (.docx) or text file."
            )
        if upload.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("That file is larger than 20 MB.")
        return upload


class ContactForm(forms.Form):
    name = forms.CharField(max_length=120)
    email = forms.EmailField(max_length=254)
    institution = forms.CharField(max_length=200, required=False)
    message = forms.CharField(max_length=4000, required=False, widget=forms.Textarea)
    meeting = forms.DateTimeField(required=False, input_formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"])
    website = forms.CharField(required=False)  # left empty by people; bots fill it in

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("message", "").strip() and not cleaned.get("meeting"):
            raise forms.ValidationError("Write a message, or choose a time for a call.")
        return cleaned
