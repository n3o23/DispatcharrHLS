from django.db import migrations

def lock_or_create_profiles(apps, schema_editor):
    StreamProfile = apps.get_model("core", "StreamProfile")

    system_profiles = [
        {
            "name": "ffmpeg",
            "command": "ffmpeg",
            "parameters": "-i {streamUrl} -c:v copy -c:a copy -f mpegts pipe:1",
            "new_parameters": "-user_agent {userAgent} -i {streamUrl} -c copy -f mpegts pipe:1",
        },
        {
            "name": "streamlink",
            "command": "streamlink",
            "parameters": "{streamUrl} best --stdout",
            "new_parameters": "{streamUrl} --http-header {userAgent} best --stdout",
        },
    ]

    for profile_data in system_profiles:
        existing_profile = StreamProfile.objects.filter(
            profile_name=profile_data["name"],
            command=profile_data["command"],
            parameters=profile_data["parameters"],
        ).first()

        if existing_profile:
            existing_profile.locked = True
            existing_profile.parameters = profile_data["new_parameters"]
            existing_profile.save()
        else:
            if not StreamProfile.objects.filter(profile_name=profile_data["name"]).exists():
                StreamProfile.objects.create(
                    profile_name=profile_data["name"],
                    command=profile_data["command"],
                    parameters=profile_data["new_parameters"],
                    locked=True,
                )

def reverse_migration(apps, schema_editor):
    pass

class Migration(migrations.Migration):
    dependencies = [
        ('core', '0005_streamprofile_locked_alter_streamprofile_command_and_more'),
    ]

    operations = [
        migrations.RunPython(lock_or_create_profiles, reverse_code=reverse_migration),
    ]
