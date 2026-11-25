"""Add or update a Canvas API-token connection from the command line (the token is read from stdin, never echoed).

    grep '^CANVAS_API_TOKEN=' other/.env | cut -d= -f2- | \
        python manage.py canvas_connect "University of Liverpool" https://canvas.liverpool.ac.uk --courses 53523
"""

import sys

from django.core.management.base import BaseCommand, CommandError

from tool import canvas
from tool.models import CanvasConnection


class Command(BaseCommand):
    help = "Add or update a Canvas API-token connection, then check it."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("base_url")
        parser.add_argument("--courses", required=True, help="Allowed Canvas course IDs, comma-separated.")

    def handle(self, name, base_url, courses, **options):
        token = sys.stdin.readline().strip().strip('"').strip("'")
        if not token:
            raise CommandError("Pipe the token in on standard input.")
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise CommandError("Use the https:// address of the Canvas site.")
        connection = CanvasConnection.objects.filter(base_url=base_url).first() or CanvasConnection(base_url=base_url)
        connection.name = name
        connection.allowed_course_ids = courses
        canvas.set_token(connection, token)
        canvas.ensure_platform(connection)
        connection.save()
        result = canvas.check(connection)
        self.stdout.write(f"Token belongs to: {result.get('account', '?')}")
        for course_id, c in result.get("courses", {}).items():
            self.stdout.write(f"  course {course_id} {c.get('name', '')}: {'ready' if c.get('ok') else c}")
