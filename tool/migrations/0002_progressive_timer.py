from django.db import migrations, models


def use_new_defaults(apps, schema_editor):
    # Assignments that kept the old fixed 20s default move to the new 60s start.
    apps.get_model("tool", "Assignment").objects.filter(start_seconds=20).update(start_seconds=60)


class Migration(migrations.Migration):
    dependencies = [("tool", "0001_initial")]

    operations = [
        migrations.RenameField("assignment", "seconds_per_question", "start_seconds"),
        migrations.AlterField(
            "assignment",
            "start_seconds",
            models.PositiveSmallIntegerField(default=60, help_text="Time allowed for the first question."),
        ),
        migrations.AddField(
            "assignment",
            "seconds_step",
            models.PositiveSmallIntegerField(default=10, help_text="Seconds taken off after each correct answer."),
        ),
        migrations.AddField(
            "assignment",
            "min_seconds",
            models.PositiveSmallIntegerField(default=20, help_text="The time never drops below this."),
        ),
        migrations.RenameField("attempt", "seconds_per_question", "start_seconds"),
        migrations.AlterField("attempt", "start_seconds", models.PositiveSmallIntegerField(default=60)),
        migrations.AddField("attempt", "seconds_step", models.PositiveSmallIntegerField(default=10)),
        migrations.AddField("attempt", "min_seconds", models.PositiveSmallIntegerField(default=20)),
        migrations.AddField(
            "question",
            "seconds_allowed",
            models.PositiveSmallIntegerField(blank=True, null=True, help_text="Fixed when the question is shown."),
        ),
        migrations.RunPython(use_new_defaults, migrations.RunPython.noop),
    ]
