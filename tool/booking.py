"""15-minute calls booked from the landing page: available slots, confirmation emails and calendar invites."""

import secrets
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from .models import Enquiry

HOLD = timedelta(hours=2)  # an unconfirmed request keeps its slot this long
CONFIRM_WITHIN = timedelta(hours=24)


def zone():
    return ZoneInfo(settings.MEETING_TIME_ZONE)


def new_token():
    return secrets.token_urlsafe(24)


def _taken(now):
    held = Enquiry.objects.filter(meeting_start__isnull=False).filter(
        status=Enquiry.CONFIRMED
    ) | Enquiry.objects.filter(meeting_start__isnull=False, status=Enquiry.PENDING, created_at__gt=now - HOLD)
    return set(held.values_list("meeting_start", flat=True))


def all_slots(now=None):
    """Every bookable start time (aware, UTC) from tomorrow, before excluding booked ones."""
    now = now or timezone.now()
    tz = zone()
    today = now.astimezone(tz).date()
    step = timedelta(minutes=settings.MEETING_MINUTES)
    slots = []
    for offset in range(1, settings.MEETING_DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        start = datetime.combine(day, time(*settings.MEETING_DAY_START), tz)
        end = datetime.combine(day, time(*settings.MEETING_DAY_END), tz)
        while start + step <= end:
            slots.append(start.astimezone(ZoneInfo("UTC")))
            start += step
    return slots


def available_slots(now=None):
    now = now or timezone.now()
    taken = _taken(now)
    return [slot for slot in all_slots(now) if slot not in taken]


def is_available(start, now=None, ignore=None):
    now = now or timezone.now()
    if start not in all_slots(now):
        return False
    clash = Enquiry.objects.filter(meeting_start=start).exclude(pk=getattr(ignore, "pk", None))
    return not clash.filter(status=Enquiry.CONFIRMED).exists() and not clash.filter(
        status=Enquiry.PENDING, created_at__gt=now - HOLD
    ).exists()


def describe(start):
    """'Tuesday 2 December 2026, 10:15–10:30 UK time'."""
    local = start.astimezone(zone())
    end = local + timedelta(minutes=settings.MEETING_MINUTES)
    return f"{local:%A} {local.day} {local:%B %Y}, {local:%H:%M}–{end:%H:%M} UK time"


def _ics(enquiry, method="REQUEST"):
    start = enquiry.meeting_start.astimezone(ZoneInfo("UTC"))
    end = start + timedelta(minutes=settings.MEETING_MINUTES)
    fmt = "%Y%m%dT%H%M%SZ"
    link = settings.MEETING_URL
    description = f"Video call: {link}" if link else "We'll email the video link before the call."
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aletheia//Booking//EN", f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:aletheia-call-{enquiry.pk}@aletheia",
        f"DTSTAMP:{timezone.now().astimezone(ZoneInfo('UTC')):{fmt}}",
        f"DTSTART:{start:{fmt}}", f"DTEND:{end:{fmt}}",
        "SUMMARY:Aletheia: 15-minute call",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(link or 'Online')}",
        f"ORGANIZER;CN=Aletheia:mailto:{settings.CONTACT_EMAIL}",
        f"ATTENDEE;CN={_ics_escape(enquiry.name)};RSVP=FALSE:mailto:{enquiry.email}",
        "STATUS:CANCELLED" if method == "CANCEL" else "STATUS:CONFIRMED",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def _ics_escape(text):
    return str(text).replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def _send(subject, body, to, ics=None, reply_to=None):
    message = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, to, reply_to=reply_to)
    if ics:
        message.attach("invite.ics", ics, "text/calendar; method=" + ics.split("METHOD:")[1].split("\r\n")[0])
    message.send(fail_silently=False)


def _summary(enquiry):
    lines = [f"Name: {enquiry.name}", f"Email: {enquiry.email}"]
    if enquiry.institution:
        lines.append(f"Institution: {enquiry.institution}")
    if enquiry.meeting_start:
        lines.append(f"Call: {describe(enquiry.meeting_start)} ({enquiry.get_status_display().lower()})")
    if enquiry.message:
        lines += ["", enquiry.message]
    return "\n".join(lines)


def notify_new(enquiry):
    """Tell Aletheia about a new enquiry (a requested call is confirmed later, by the visitor)."""
    _send(f"Aletheia enquiry from {enquiry.name}", _summary(enquiry), [settings.CONTACT_EMAIL], reply_to=[enquiry.email])


def ask_to_confirm(enquiry, confirm_url):
    body = (
        f"Hello {enquiry.name},\n\n"
        f"Thank you for your interest in Aletheia. Please confirm your 15-minute call:\n\n"
        f"    {describe(enquiry.meeting_start)}\n\n"
        f"Confirm here: {confirm_url}\n\n"
        f"We'll hold this time for {int(HOLD.total_seconds() // 3600)} hours. If you didn't ask for this call, "
        f"you can ignore this email.\n\nAletheia\n{settings.CONTACT_EMAIL}\n"
    )
    _send("Please confirm your call with Aletheia", body, [enquiry.email])


def send_confirmed(enquiry, cancel_url):
    link = settings.MEETING_URL
    joining = f"Join the call: {link}" if link else "We'll email you the video link before the call."
    body = (
        f"Hello {enquiry.name},\n\nYour call with Aletheia is booked:\n\n"
        f"    {describe(enquiry.meeting_start)}\n\n{joining}\n\n"
        f"A calendar invitation is attached. If you need to cancel: {cancel_url}\n\n"
        f"Aletheia\n{settings.CONTACT_EMAIL}\n"
    )
    ics = _ics(enquiry)
    _send("Your call with Aletheia is booked", body, [enquiry.email], ics=ics)
    _send(f"Call booked: {enquiry.name}, {describe(enquiry.meeting_start)}", _summary(enquiry),
          [settings.CONTACT_EMAIL], ics=ics, reply_to=[enquiry.email])


def send_cancelled(enquiry):
    ics = _ics(enquiry, method="CANCEL")
    _send("Your call with Aletheia is cancelled",
          f"Hello {enquiry.name},\n\nYour call on {describe(enquiry.meeting_start)} is cancelled.\n\n"
          f"Aletheia\n{settings.CONTACT_EMAIL}\n", [enquiry.email], ics=ics)
    _send(f"Call cancelled: {enquiry.name}, {describe(enquiry.meeting_start)}", _summary(enquiry),
          [settings.CONTACT_EMAIL], ics=ics, reply_to=[enquiry.email])
