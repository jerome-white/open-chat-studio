# Generated by Django 4.2 on 2023-11-07 12:52

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("service_providers", "0008_messagingprovider"),
        ("channels", "0011_delete_channelsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="experimentchannel",
            name="messaging_provider",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="service_providers.messagingprovider",
                verbose_name="Messaging Provider",
            ),
        ),
    ]
