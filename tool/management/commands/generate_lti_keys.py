from django.conf import settings
from django.core.management.base import BaseCommand

from tool import lti


class Command(BaseCommand):
    help = "Create the tool's RSA key pair for LTI 1.3 if it does not exist."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Replace existing keys.")

    def handle(self, *args, force=False, **options):
        if lti.generate_keys(force=force):
            self.stdout.write(f"Created keys at {settings.LTI_PRIVATE_KEY_PATH.parent}")
        else:
            self.stdout.write("LTI keys already exist.")
