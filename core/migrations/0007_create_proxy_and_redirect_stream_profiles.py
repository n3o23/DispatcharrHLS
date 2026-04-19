from django.db import migrations

def create_proxy_stream_profile(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    UserAgent = apps.get_model("core", "UserAgent")

    # Get first available user agent (or None if not yet created)
    ua = UserAgent.objects.first()
    default_user_agent_id = ua.pk if ua else None

    if not StreamProfile.objects.filter(profile_name="Proxy").exists():
        StreamProfile.objects.create(
            profile_name="Proxy",
            command="",
            parameters="",
            locked=True,
            is_active=True,
            user_agent_id=default_user_agent_id,
        )

    if not StreamProfile.objects.filter(profile_name="Redirect").exists():
        StreamProfile.objects.create(
            profile_name="Redirect",
            command="",
            parameters="",
            locked=True,
            is_active=True,
            user_agent_id=default_user_agent_id,
        )

def reverse_migration(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")
    StreamProfile.objects.filter(profile_name__in=["Proxy", "Redirect"]).delete()

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_set_locked_stream_profiles'),
    ]

    operations = [
        migrations.RunPython(create_proxy_stream_profile, reverse_migration)
    ]
