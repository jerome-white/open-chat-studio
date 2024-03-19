import django_tables2 as tables
from django.conf import settings
from django.urls import reverse
from django.utils.html import format_html

from apps.events.models import EventActionType, StaticTriggerType
from apps.utils.time import seconds_to_human


class ActionsColumn(tables.Column):
    def render(self, value, record):
        trigger_type = "timeout" if record["type"] == "__timeout__" else "static"
        view_log_url = reverse(
            f"experiments:events:{trigger_type}_logs_view",
            kwargs={
                "trigger_id": record["id"],
                "experiment_id": record["experiment_id"],
                "team_slug": record["team_slug"],
            },
        )
        edit_url = reverse(
            f"experiments:events:{trigger_type}_event_edit",
            kwargs={
                "trigger_id": record["id"],
                "experiment_id": record["experiment_id"],
                "team_slug": record["team_slug"],
            },
        )
        delete_url = reverse(
            f"experiments:events:{trigger_type}_event_delete",
            kwargs={
                "trigger_id": record["id"],
                "experiment_id": record["experiment_id"],
                "team_slug": record["team_slug"],
            },
        )
        return format_html(
            """
            <a class="btn btn-sm btn-outline btn-primary" href="{}">View Logs</a>
            <a class="btn btn-sm btn-outline btn-primary" href="{}">Edit</a>
            <a class="btn btn-sm btn-outline btn-primary" href="{}">Delete</a>""",
            view_log_url,
            edit_url,
            delete_url,
        )


class EventsTable(tables.Table):
    type = tables.Column(accessor="type", verbose_name="When...")
    action_type = tables.Column(accessor="action__action_type", verbose_name="Then...")
    action_params = tables.JSONColumn(accessor="action__action_params", verbose_name="With these parameters...")
    total_num_triggers = tables.Column(accessor="total_num_triggers", verbose_name="Repeat")
    actions = ActionsColumn(empty_values=())

    def render_type(self, value, record):
        if value == "__timeout__":
            return f"No response for {seconds_to_human(record['delay'])}"
        else:
            return StaticTriggerType(value).label

    def render_action_type(self, value):
        return EventActionType(value).label

    def render_total_num_triggers(self, value):
        return f"{value} times"

    class Meta:
        orderable = False
        row_attrs = {
            **settings.DJANGO_TABLES2_ROW_ATTRS,
            "id": lambda record: f"record-{record['type']}-{record['id']}",
        }
        fields = (
            "type",
            "action_type",
            "action_params",
        )
