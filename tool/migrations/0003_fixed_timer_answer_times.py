from django.db import migrations, models


def use_45_seconds(apps, schema_editor):
    apps.get_model("tool", "Assignment").objects.update(seconds_per_question=45)


class Migration(migrations.Migration):
    dependencies = [("tool", "0002_progressive_timer")]

    operations = [
        migrations.RenameField("assignment", "start_seconds", "seconds_per_question"),
        migrations.AlterField("assignment", "seconds_per_question", models.PositiveSmallIntegerField(default=45)),
        migrations.RemoveField("assignment", "seconds_step"),
        migrations.RemoveField("assignment", "min_seconds"),
        migrations.RenameField("attempt", "start_seconds", "seconds_per_question"),
        migrations.AlterField("attempt", "seconds_per_question", models.PositiveSmallIntegerField(default=45)),
        migrations.RemoveField("attempt", "seconds_step"),
        migrations.RemoveField("attempt", "min_seconds"),
        migrations.AddField("attempt", "median_answer_seconds", models.FloatField(blank=True, null=True)),
        migrations.RunPython(use_45_seconds, migrations.RunPython.noop),
    ]
