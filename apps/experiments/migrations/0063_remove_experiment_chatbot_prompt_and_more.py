# Generated by Django 4.2.7 on 2024-02-09 09:21

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("experiments", "0062_experiment_input_formatter_experiment_prompt_text_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="experiment",
            name="chatbot_prompt",
        ),
        migrations.RemoveField(
            model_name="safetylayer",
            name="prompt",
        ),
        migrations.DeleteModel(
            name="Prompt",
        ),
    ]
