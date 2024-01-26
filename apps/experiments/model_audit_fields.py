from django.contrib.auth.models import AbstractUser

PROMPT_FIELDS = ["owner", "name", "description", "prompt", "input_formatter", "team"]

EXPERIMENT_FIELDS = [
    "owner",
    "name",
    "llm_provider",
    "llm",
    "temperature",
    "chatbot_prompt",
    "safety_layers",
    "is_active",
    "tools_enabled",
    "source_material",
    "seed_message",
    "pre_survey",
    "post_survey",
    "consent_form",
    "voice_provider",
    "synthetic_voice",
    "no_activity_config",
    "conversational_consent_enabled",
    "team",
]

SOURCE_MATERIAL_FIELDS = ["owner", "topic", "description", "material", "team"]
SAFETY_LAYER_FIELDS = ["prompt", "messages_to_review", "default_response_to_user", "prompt_to_bot", "team"]
CONSENT_FORM_FIELDS = [
    "name",
    "consent_text",
    "capture_identifier",
    "identifier_label",
    "identifier_type",
    "confirmation_text",
    "team",
]

TEAM_FIELDS = ["name", "slug", "members"]
MEMBERSHIP_FIELDS = ["team", "user", "role"]
EXPERIMENT_CHANNEL_FIELDS = [
    "name",
    "experiment",
    "active",
    "extra_data",
    "platform",
    "messaging_provider",
]

NO_ACTIVITY_CONFIG_FIELDS = ["message_for_bot", "name", "max_pings", "ping_after", "team"]
MESSAGING_PROVIDER_FIELDS = ["type", "name", "team"]
VOICE_PROVIDER_FIELDS = ["type", "name", "team"]
LLM_PROVIDER_FIELDS = ["team", "type", "name", "llm_models"]

# The auditing library struggles with dates. Let's ignore them for now
CUSTOM_USER_FIELDS = [f.attname for f in AbstractUser._meta.fields if f.attname not in ["last_login", "date_joined"]]
