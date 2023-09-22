# Generated by Django 4.2 on 2023-04-14 14:21

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("experiments", "0006_experiment_llm_experimentsession_llm"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompt",
            name="input_formatter",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Use the {input} variable somewhere to modify the user input before it reaches the bot. E.g. 'Safe or unsafe? {input}'",
            ),
        ),
        migrations.AlterField(
            model_name="prompt",
            name="description",
            field=models.TextField(
                blank=True, default="", verbose_name="A longer description of what the prompt does."
            ),
        ),
    ]
