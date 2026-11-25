from datetime import timedelta

from django.db import models
from django.utils import timezone


class Platform(models.Model):
    """An LMS registration (one per issuer + client ID)."""

    name = models.CharField(max_length=120, help_text="A label, e.g. 'University VLE'.")
    issuer = models.CharField(max_length=255, help_text="The platform's 'iss' value.")
    client_id = models.CharField(max_length=255)
    deployment_ids = models.TextField(
        blank=True,
        help_text="Accepted deployment IDs, one per line. Leave blank to accept any.",
    )
    auth_login_url = models.URLField(help_text="Platform OIDC authorisation endpoint.")
    jwks_url = models.URLField(help_text="Platform public keyset URL.")
    access_token_url = models.URLField(blank=True, help_text="Platform OAuth2 token endpoint (optional).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("issuer", "client_id")]

    def __str__(self):
        return self.name

    def accepts_deployment(self, deployment_id):
        allowed = [line.strip() for line in self.deployment_ids.splitlines() if line.strip()]
        return not allowed or deployment_id in allowed


DEFAULT_AI_POLICY = """You may use generative AI tools (such as ChatGPT, Claude or Copilot) to help you learn: to explain ideas, to suggest sources to look up, or to give feedback on a draft. The work you hand in must be your own. Its ideas, analysis, structure and wording should come from you, and you must be able to explain and justify all of it.

Do not hand in text, code, data or images produced by an AI tool as your own work.

If you used AI in a way that shaped your work, say how in a short statement at the end of it.

Check anything an AI tool tells you. You are responsible for the accuracy of your work.

During the Aletheia check that follows, do not use AI tools, notes, other people or other materials. Answer from your own understanding.

Handing in work that is not your own, or using AI during the check, may be treated as academic misconduct."""


class Assignment(models.Model):
    """One LTI resource link placed in a course."""

    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, related_name="assignments")
    # Set only for assignments reached through a Canvas API token rather than LTI.
    canvas_course = models.ForeignKey(
        "CanvasCourse", on_delete=models.PROTECT, null=True, blank=True, related_name="assignments"
    )
    canvas_assignment_id = models.PositiveBigIntegerField(null=True, blank=True)
    deployment_id = models.CharField(max_length=255)
    resource_link_id = models.CharField(max_length=255)
    context_id = models.CharField(max_length=255, blank=True)
    context_title = models.CharField(max_length=255, blank=True)

    title = models.CharField(max_length=255, blank=True)
    brief = models.TextField(
        blank=True,
        help_text="What students were asked to do and what the questions should test.",
    )
    objective_count = models.PositiveSmallIntegerField(default=5)
    questions_per_objective = models.PositiveSmallIntegerField(
        default=1, help_text="Correct answers needed to clear each objective."
    )
    retries_per_objective = models.PositiveSmallIntegerField(
        default=1, help_text="Extra questions allowed on an objective after wrong answers."
    )
    seconds_per_question = models.PositiveSmallIntegerField(default=45)
    time_each_question = models.BooleanField(
        default=True, help_text="Give each question its own time limit. If off, the whole quiz has one time limit."
    )
    time_limit_minutes = models.PositiveSmallIntegerField(
        default=10, help_text="Time for the whole quiz when questions aren't timed individually."
    )
    max_attempts = models.PositiveSmallIntegerField(default=1, help_text="0 means unlimited.")
    room_word_required = models.BooleanField(
        default=False,
        help_text="Students type a rotating word shown in the room to start, and again at random points.",
    )
    ai_policy = models.TextField(
        blank=True,
        default=DEFAULT_AI_POLICY,
        help_text="Students must agree to this before handing in. Blank means no agreement is asked for.",
    )
    lineitem_url = models.URLField(
        max_length=500, blank=True, help_text="The LMS gradebook column for this assignment (from the launch)."
    )
    memberships_url = models.URLField(
        max_length=500, blank=True, help_text="The LMS class list service for this course (from the launch)."
    )
    ags_scopes = models.JSONField(default=list, blank=True, help_text="Grade service scopes the LMS granted.")
    code_viva = models.BooleanField(
        default=False,
        help_text="For programming work: accepts source files and .zip projects; questions can show code.",
    )
    room_secret = models.CharField(max_length=64, blank=True, editable=False)
    configured = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("platform", "deployment_id", "resource_link_id")]

    def __str__(self):
        return self.title or f"Assignment {self.pk}"

    def save(self, *args, **kwargs):
        if not self.room_secret:
            from .room import new_secret

            self.room_secret = new_secret()
        super().save(*args, **kwargs)


def submission_path(instance, filename):
    return f"submissions/{instance.assignment_id}/{instance.lti_user_id}/{filename}"


class Attempt(models.Model):
    DRAFT = "draft"  # uploaded, waiting for the student to check the file and confirm
    GENERATING = "generating"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (DRAFT, "Awaiting confirmation"),
        (GENERATING, "Generating"),
        (READY, "Ready"),
        (IN_PROGRESS, "In progress"),
        (COMPLETED, "Completed"),
        (FAILED, "Failed"),
    ]

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="attempts")
    lti_user_id = models.CharField(max_length=255, db_index=True)
    user_name = models.CharField(max_length=255, blank=True)
    number = models.PositiveSmallIntegerField(default=1)
    # Settings copied from the assignment when the attempt starts.
    seconds_per_question = models.PositiveSmallIntegerField(default=45)
    time_each_question = models.BooleanField(default=True)
    time_limit_minutes = models.PositiveSmallIntegerField(default=10)
    room_word_required = models.BooleanField(default=False)
    code_viva = models.BooleanField(default=False)
    objective_count = models.PositiveSmallIntegerField(default=5)
    questions_per_objective = models.PositiveSmallIntegerField(default=1)
    retries_per_objective = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=GENERATING)
    error = models.TextField(blank=True)

    file = models.FileField(upload_to=submission_path, max_length=500)
    original_filename = models.CharField(max_length=255)
    submission_text = models.TextField()

    # [{"text": "..."}], in order; questions refer to them by index.
    objectives = models.JSONField(default=list, blank=True)
    # Filled in on completion: [{"text", "correct", "total", "alignment"}].
    objective_alignment = models.JSONField(default=list, blank=True)
    overall_alignment = models.FloatField(null=True, blank=True)
    # Room word spot checks: [{"before": objective index, "status": pending|passed|failed, "started_at": iso}].
    room_checks = models.JSONField(default=list, blank=True)
    # Median seconds taken on questions answered before time ran out (instructor-only).
    # What the student confirmed before handing in.
    file_confirmed_at = models.DateTimeField(null=True, blank=True)
    ai_policy_text = models.TextField(blank=True, help_text="The policy as shown to the student.")
    ai_policy_agreed_at = models.DateTimeField(null=True, blank=True)

    LMS_PENDING = "pending"
    LMS_SENT = "sent"
    LMS_FAILED = "failed"
    LMS_CHOICES = [("", "Not sent"), (LMS_PENDING, "Sending"), (LMS_SENT, "Submitted"), (LMS_FAILED, "Failed")]
    lms_status = models.CharField(max_length=10, choices=LMS_CHOICES, blank=True, default="")
    lms_error = models.TextField(blank=True)

    handoff_nonce = models.CharField(
        max_length=64, blank=True, help_text="One-time key for opening the questions in a new tab."
    )
    ran_out_of_time = models.BooleanField(default=False, help_text="The whole-quiz time limit ended the attempt.")
    median_answer_seconds = models.FloatField(null=True, blank=True)
    # What generating this attempt cost (all Claude requests, including failed and unused ones).
    ai_requests = models.PositiveIntegerField(default=0)
    ai_input_tokens = models.PositiveIntegerField(default=0)
    ai_output_tokens = models.PositiveIntegerField(default=0)
    ai_cache_write_tokens = models.PositiveIntegerField(default=0)
    ai_cache_read_tokens = models.PositiveIntegerField(default=0)
    ai_cost_usd = models.FloatField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user_name or self.lti_user_id} · attempt {self.number}"

    def integrity_flag_count(self):
        return self.events.filter(kind__in=IntegrityEvent.FLAG_KINDS).count()


class Question(models.Model):
    """One question on one objective. Generated ahead of need, so some are never served."""

    MCQ = "mcq"
    MAQ = "maq"
    KIND_CHOICES = [(MCQ, "Single answer"), (MAQ, "Multiple answer")]

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    STATUS_CHOICES = [(PENDING, "Generating"), (READY, "Ready"), (FAILED, "Failed")]

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="questions")
    objective_index = models.PositiveSmallIntegerField()
    position = models.PositiveSmallIntegerField(help_text="0 for an objective's first question, 1+ for follow-ups.")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    kind = models.CharField(max_length=3, choices=KIND_CHOICES)
    prompt = models.TextField(blank=True)
    code = models.TextField(blank=True, help_text="Optional code snippet shown with the question.")
    options = models.JSONField(default=list)
    correct = models.JSONField(default=list, help_text="Indices into options.")
    explanation = models.TextField(blank=True, help_text="Why the answer is right; shown after the attempt.")
    error = models.TextField(blank=True)
    tries = models.PositiveSmallIntegerField(default=0)
    requested_at = models.DateTimeField(default=timezone.now)

    order = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Sequence shown to the student.")
    seconds_allowed = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Fixed when the question is shown.")
    served_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    selected = models.JSONField(null=True, blank=True)
    is_correct = models.BooleanField(null=True)
    timed_out = models.BooleanField(default=False)

    class Meta:
        ordering = ["objective_index", "position"]
        unique_together = [("attempt", "objective_index", "position")]

    @property
    def answered(self):
        return self.answered_at is not None

    @property
    def answer_seconds(self):
        """How long the student took, or None if unanswered or time ran out."""
        if self.served_at and self.answered_at and not self.timed_out:
            return (self.answered_at - self.served_at).total_seconds()
        return None

    def overdue(self, grace_seconds, now=None):
        """True once the time limit plus a grace period has passed."""
        if not self.served_at:
            return False
        deadline = self.served_at + timedelta(seconds=self.seconds_allowed + grace_seconds)
        return (now or timezone.now()) > deadline

    def seconds_remaining(self, now=None):
        limit = self.seconds_allowed or self.attempt.seconds_per_question
        if not self.served_at:
            return float(limit)
        elapsed = ((now or timezone.now()) - self.served_at).total_seconds()
        return max(0.0, limit - elapsed)


class IntegrityEvent(models.Model):
    KINDS = [
        ("hidden", "Left the page"),
        ("visible", "Returned to the page"),
        ("blur", "Window lost focus"),
        ("focus", "Window regained focus"),
        ("copy", "Copy"),
        ("cut", "Cut"),
        ("paste", "Paste"),
        ("contextmenu", "Right-click"),
        ("room_start_wrong", "Room word wrong at start"),
        ("room_passed", "Room word check passed"),
        ("room_failed", "Room word check failed"),
    ]
    FLAG_KINDS = ["hidden", "blur", "copy", "cut", "paste", "room_failed"]
    LEFT_PAGE_KINDS = ["hidden", "blur"]  # the question page logs one of these per time away

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="events")
    question = models.ForeignKey(Question, on_delete=models.SET_NULL, null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


class CanvasConnection(models.Model):
    """An institution's Canvas, reached with an API token instead of LTI (for pilots that can't install LTI yet).

    The token belongs to a dedicated account that is a teacher in each linked course. It is stored encrypted
    and never shown again. Aletheia only touches the courses listed in `allowed_course_ids`.
    """

    name = models.CharField(max_length=120, help_text="A label, e.g. 'University of Liverpool'.")
    base_url = models.URLField(help_text="The Canvas address, e.g. https://canvas.liverpool.ac.uk")
    token_encrypted = models.TextField(editable=False)
    token_hint = models.CharField(max_length=8, editable=False, help_text="The token's last four characters.")
    allowed_course_ids = models.TextField(
        help_text="Canvas course IDs Aletheia may touch, one per line or comma-separated. Every other course is refused.",
    )
    platform = models.OneToOneField(Platform, on_delete=models.PROTECT, related_name="canvas_connection", editable=False)
    sender_name = models.CharField(max_length=255, blank=True, editable=False, help_text="Whose name sign-in messages come from.")
    check_result = models.JSONField(default=dict, blank=True, editable=False)
    checked_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def origin(self):
        return self.base_url.rstrip("/")

    def course_allowed(self, course_id):
        allowed = {c.strip() for c in self.allowed_course_ids.replace(",", "\n").split() if c.strip()}
        return str(course_id) in allowed


class CanvasCourse(models.Model):
    connection = models.ForeignKey(CanvasConnection, on_delete=models.CASCADE, related_name="courses")
    canvas_course_id = models.PositiveBigIntegerField()
    name = models.CharField(max_length=255, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)

    class Meta:
        unique_together = [("connection", "canvas_course_id")]
        ordering = ["name"]

    def __str__(self):
        return self.name or f"Canvas course {self.canvas_course_id}"

    @property
    def url(self):
        return f"{self.connection.origin}/courses/{self.canvas_course_id}"


class CanvasMember(models.Model):
    """One person in a linked course's class list, as last synced from Canvas."""

    STUDENT, TEACHER, TA, DESIGNER = "student", "teacher", "ta", "designer"
    ROLE_CHOICES = [(STUDENT, "Student"), (TEACHER, "Teacher"), (TA, "Teaching assistant"), (DESIGNER, "Designer")]
    STAFF_ROLES = (TEACHER, TA, DESIGNER)

    course = models.ForeignKey(CanvasCourse, on_delete=models.CASCADE, related_name="members")
    canvas_user_id = models.PositiveBigIntegerField()
    name = models.CharField(max_length=255)
    sortable_name = models.CharField(max_length=255, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    active = models.BooleanField(default=True, help_text="Still enrolled at the last sync.")

    class Meta:
        unique_together = [("course", "canvas_user_id")]
        ordering = ["sortable_name", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

    @property
    def is_staff(self):
        return self.role in self.STAFF_ROLES

    @property
    def lti_user_id(self):
        """How attempts identify this person, alongside LTI users' own IDs."""
        return f"canvas:{self.canvas_user_id}"


class CanvasSignIn(models.Model):
    """One sign-in through the Canvas inbox (API-token route).

    The inbox link works once, for an hour. Opening it creates a temporary Canvas page whose iframe carries a
    one-time code (valid 10 minutes); exchanging the code signs the person in to the iframe for 12 hours.
    Only hashes of the link token and the code are stored.
    """

    LINK_LIFETIME = timedelta(hours=1)
    CODE_LIFETIME = timedelta(minutes=10)
    SESSION_LIFETIME = timedelta(hours=12)

    member = models.ForeignKey(CanvasMember, on_delete=models.CASCADE, related_name="sign_ins")
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="canvas_sign_ins")
    link_hash = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=64, blank=True, db_index=True)
    page_slug = models.CharField(max_length=255, blank=True, help_text="The temporary Canvas page, until deleted.")
    created_at = models.DateTimeField(default=timezone.now)
    link_used_at = models.DateTimeField(null=True, blank=True)
    code_used_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def link_usable(self, now=None):
        now = now or timezone.now()
        return not (self.link_used_at or self.superseded_at) and now < self.created_at + self.LINK_LIFETIME

    def code_usable(self, now=None):
        now = now or timezone.now()
        return bool(self.link_used_at) and not self.code_used_at and now < self.link_used_at + self.CODE_LIFETIME


class Invitation(models.Model):
    """An instructor's invitation for one student to try again: one more attempt, beyond the limit.

    It is pending until the student hands in a new attempt. Nothing is sent to the student or the
    LMS; they see the invitation the next time they open the assignment.
    """

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="invitations")
    lti_user_id = models.CharField(max_length=255)
    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="invitations",
                                help_text="The attempt the instructor was reviewing.")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


class Enquiry(models.Model):
    """A message from the landing page's contact form, optionally with a 15-minute call."""

    RECEIVED = "received"
    PENDING = "pending"  # call requested; waiting for the visitor to confirm by email
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (RECEIVED, "Message received"),
        (PENDING, "Call awaiting confirmation"),
        (CONFIRMED, "Call confirmed"),
        (CANCELLED, "Call cancelled"),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField()
    institution = models.CharField(max_length=200, blank=True)
    message = models.TextField(blank=True)
    meeting_start = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=RECEIVED)
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "enquiries"

    def __str__(self):
        return f"{self.name} <{self.email}>"
