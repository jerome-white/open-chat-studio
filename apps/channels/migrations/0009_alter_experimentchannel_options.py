# Generated by Django 4.2 on 2023-10-20 14:28

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("channels", "0008_alter_experimentchannel_platform"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="experimentchannel",
            options={"ordering": ["name"]},
        ),
    ]
