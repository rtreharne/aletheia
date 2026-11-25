from django import forms
from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from . import canvas
from .models import (
    Assignment, Attempt, CanvasConnection, CanvasCourse, CanvasMember, Enquiry, IntegrityEvent, Platform, Question,
)


@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    list_display = ("name", "issuer", "client_id", "created_at")
    search_fields = ("name", "issuer", "client_id")


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ("title", "context_title", "platform", "objective_count", "questions_per_objective", "retries_per_objective", "seconds_per_question", "max_attempts", "configured")
    list_filter = ("platform", "configured")
    search_fields = ("title", "context_title", "resource_link_id")


class QuestionInline(admin.TabularInline):
    model = Question
    extra = 0
    fields = ("objective_index", "position", "status", "order", "seconds_allowed", "kind", "prompt", "options", "correct", "selected", "is_correct", "timed_out")
    readonly_fields = fields
    can_delete = False


class IntegrityEventInline(admin.TabularInline):
    model = IntegrityEvent
    extra = 0
    fields = ("created_at", "kind", "question")
    readonly_fields = fields
    can_delete = False


@admin.register(Attempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("user_name", "assignment", "number", "status", "overall_alignment", "ai_cost", "created_at")
    list_filter = ("status", "assignment")
    search_fields = ("user_name", "lti_user_id")
    readonly_fields = ("objectives", "objective_alignment", "overall_alignment", "median_answer_seconds", "error",
                       "ai_requests", "ai_input_tokens", "ai_output_tokens", "ai_cache_write_tokens",
                       "ai_cache_read_tokens", "ai_cost_usd")

    @admin.display(description="AI cost", ordering="ai_cost_usd")
    def ai_cost(self, obj):
        return f"${obj.ai_cost_usd:.3f}"
    exclude = ("submission_text",)
    inlines = [QuestionInline, IntegrityEventInline]


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "name", "email", "institution", "meeting_start", "status")
    list_filter = ("status",)
    search_fields = ("name", "email", "institution", "message")
    readonly_fields = ("token", "created_at", "confirmed_at")


# ---------------------------------------------------------------- Canvas API-token route

class CanvasConnectionForm(forms.ModelForm):
    token = forms.CharField(
        required=False, strip=True, widget=forms.PasswordInput(render_value=False),
        help_text="The API token of the account that is a teacher in each course. Stored encrypted; never shown again. "
                  "Leave blank to keep the current one.",
    )

    class Meta:
        model = CanvasConnection
        fields = ("name", "base_url", "allowed_course_ids")

    def clean_base_url(self):
        url = self.cleaned_data["base_url"].rstrip("/")
        if not url.startswith("https://"):
            raise forms.ValidationError("Use the https:// address of the Canvas site.")
        return url

    def clean(self):
        data = super().clean()
        if not self.instance.pk and not data.get("token"):
            self.add_error("token", "A token is needed for a new connection.")
        return data


@admin.register(CanvasConnection)
class CanvasConnectionAdmin(admin.ModelAdmin):
    form = CanvasConnectionForm
    list_display = ("name", "base_url", "sender_name", "token_status", "checked_at", "check_ok")
    readonly_fields = ("token_status", "sender_name", "check_summary", "checked_at")
    actions = ["check_connections", "end_pilot"]

    @admin.display(description="Token")
    def token_status(self, obj):
        return f"…{obj.token_hint}" if obj.token_encrypted else "None (removed)"

    @admin.display(description="Check", boolean=True)
    def check_ok(self, obj):
        return obj.check_result.get("ok") if obj.check_result else None

    @admin.display(description="Last check")
    def check_summary(self, obj):
        r = obj.check_result or {}
        if not r:
            return "Not checked yet."
        if r.get("error"):
            return r["error"]
        rows = []
        for course_id, c in r.get("courses", {}).items():
            if c.get("error"):
                status = c["error"]
            elif c.get("ok"):
                status = "Ready"
            else:
                gaps = ([] if c.get("teacher") else ["isn't a teacher in this course"]) + [f"can't {m}" for m in c.get("missing", [])]
                status = "The account " + "; ".join(gaps)
            rows.append((course_id, c.get("name", ""), status))
        return format_html(
            "Token belongs to <strong>{}</strong>.<br>{}", r.get("account", "?"),
            format_html_join(format_html("<br>"), "Course {} {}: {}", rows),
        )

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("token"):
            canvas.set_token(obj, form.cleaned_data["token"])
        canvas.ensure_platform(obj)
        super().save_model(request, obj, form, change)
        self._check(request, obj)

    def _check(self, request, obj):
        result = canvas.check(obj)
        if result.get("ok"):
            self.message_user(request, f"{obj}: connected as {result.get('account')}; every course is ready.")
        else:
            self.message_user(request, f"{obj}: not ready. See Last check for what's missing.", messages.WARNING)

    @admin.action(description="Check connection")
    def check_connections(self, request, queryset):
        for obj in queryset:
            self._check(request, obj)

    @admin.action(description="End pilot: remove Aletheia from Canvas and delete the token")
    def end_pilot(self, request, queryset):
        if request.POST.get("confirmed") != "yes":
            return render(request, "admin/tool/end_pilot.html", {
                "connections": queryset, "action": "end_pilot", "opts": self.model._meta,
                "linked": Assignment.objects.filter(canvas_course__connection__in=queryset).count(),
            })
        for obj in queryset:
            problems = canvas.offboard(obj)
            if problems:
                self.message_user(request, f"{obj}: token kept, because some assignments couldn't be updated: "
                                  + "; ".join(problems), messages.ERROR)
            else:
                self.message_user(request, f"{obj}: Aletheia removed from its assignments and the token deleted.")


class CanvasMemberInline(admin.TabularInline):
    model = CanvasMember
    extra = 0
    fields = ("name", "role", "active", "canvas_user_id")
    readonly_fields = fields
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False


class CanvasCourseForm(forms.ModelForm):
    class Meta:
        model = CanvasCourse
        fields = ("connection", "canvas_course_id")

    def clean(self):
        data = super().clean()
        conn, course_id = data.get("connection"), data.get("canvas_course_id")
        if conn and course_id and not conn.course_allowed(course_id):
            raise forms.ValidationError("Add this course ID to the connection's allowed courses first.")
        return data


@admin.register(CanvasCourse)
class CanvasCourseAdmin(admin.ModelAdmin):
    form = CanvasCourseForm
    list_display = ("__str__", "connection", "canvas_course_id", "member_count", "synced_at", "sync_error")
    readonly_fields = ("name", "synced_at", "sync_error", "assignments_link")
    inlines = [CanvasMemberInline]
    actions = ["sync_class_lists"]

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("connection", "canvas_course_id") if obj else ())

    @admin.display(description="Class list")
    def member_count(self, obj):
        return obj.members.filter(active=True).count()

    @admin.display(description="Aletheia assignments")
    def assignments_link(self, obj):
        if not obj.pk:
            return "Save the course first."
        return format_html('<a class="button" href="{}">Link or unlink assignments</a>',
                           reverse("admin:tool_canvascourse_assignments", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._sync(request, obj)

    def _sync(self, request, obj):
        try:
            count = canvas.sync_course(obj)
            self.message_user(request, f"{obj}: class list synced ({count} people).")
        except canvas.CanvasError as exc:
            self.message_user(request, f"{obj}: {exc}", messages.ERROR)

    @admin.action(description="Sync class list from Canvas")
    def sync_class_lists(self, request, queryset):
        for obj in queryset:
            self._sync(request, obj)

    def get_urls(self):
        return [
            path("<int:pk>/assignments/", self.admin_site.admin_view(self.assignments_view),
                 name="tool_canvascourse_assignments"),
        ] + super().get_urls()

    def assignments_view(self, request, pk):
        course = get_object_or_404(CanvasCourse, pk=pk)
        here = reverse("admin:tool_canvascourse_assignments", args=[pk])

        def frame_url(assignment):
            return request.build_absolute_uri(reverse("canvas_frame", args=[assignment.pk]))

        if request.method == "POST":
            try:
                if "unlink" in request.POST:
                    assignment = get_object_or_404(Assignment, pk=request.POST["unlink"], canvas_course=course)
                    canvas.unlink_assignment(assignment)
                    self.message_user(request, f"Aletheia removed from \u201c{assignment.title}\u201d in Canvas.")
                elif "link" in request.POST:
                    assignment = canvas.link_assignment(course, frame_url, canvas_assignment_id=int(request.POST["link"]))
                    self.message_user(request, f"Aletheia added to \u201c{assignment.title}\u201d. "
                                      "Its settings are chosen by a teacher the first time they open it.")
                elif request.POST.get("new_name", "").strip():
                    assignment = canvas.link_assignment(course, frame_url, new_name=request.POST["new_name"].strip())
                    self.message_user(request, f"Created \u201c{assignment.title}\u201d (unpublished) with Aletheia in it.")
            except canvas.CanvasError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            return redirect(here)
        try:
            remote = canvas.list_assignments(course)
            error = ""
        except canvas.CanvasError as exc:
            remote, error = [], str(exc)
        linked = {a.canvas_assignment_id: a for a in course.assignments.all()}
        for item in remote:
            item["aletheia"] = linked.get(item["id"])
        return render(request, "admin/tool/canvas_assignments.html", {
            **self.admin_site.each_context(request), "course": course, "assignments": remote, "error": error,
            "opts": self.model._meta, "title": f"Aletheia in {course}",
        })
