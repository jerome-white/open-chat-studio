# Generated by Django 4.2.11 on 2024-05-28 13:25

import django.contrib.postgres.fields
from django.db import migrations, models

def _create_agent_tool_resouces(apps, schema_editor):
    Experiment = apps.get_model("experiments", "Experiment")
    from apps.experiments.models import AgentTools

    for experiment in Experiment.objects.filter(tools_enabled=True):
        experiment.tools = [AgentTools.RECURRING_REMINDER, AgentTools.ONE_OFF_REMINDER]
        experiment.save()
        

def _remove_agent_tool_resouces(apps, schema_editor):
    Experiment = apps.get_model("experiments", "Experiment")
    Experiment.objects.all.update(tools=[])


class Migration(migrations.Migration):

    dependencies = [
        ('experiments', '0079_alter_participant_unique_together_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='experiment',
            name='tools',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=128), blank=True, default=list, size=None),
        ),
        migrations.RunPython(_create_agent_tool_resouces, _remove_agent_tool_resouces),
        migrations.RemoveField(
            model_name='experiment',
            name='tools_enabled',
        ),
    ]
