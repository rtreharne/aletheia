from django.core.management.base import BaseCommand

from tool.models import Platform


class Command(BaseCommand):
    help = "Register (or update) an LMS platform for LTI 1.3."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--issuer", required=True)
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--auth-login-url", required=True)
        parser.add_argument("--jwks-url", required=True)
        parser.add_argument("--access-token-url", default="")
        parser.add_argument("--deployment-id", action="append", default=[])

    def handle(self, *args, **o):
        platform, created = Platform.objects.update_or_create(
            issuer=o["issuer"],
            client_id=o["client_id"],
            defaults={
                "name": o["name"],
                "auth_login_url": o["auth_login_url"],
                "jwks_url": o["jwks_url"],
                "access_token_url": o["access_token_url"],
                "deployment_ids": "\n".join(o["deployment_id"]),
            },
        )
        self.stdout.write(f"{'Created' if created else 'Updated'} platform '{platform}'.")
