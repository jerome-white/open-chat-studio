import dataclasses
from collections.abc import Sequence
from unittest.mock import patch

import freezegun
import pytest
from langchain_core.messages import BaseMessage, SystemMessage

from apps.chat.models import Chat, ChatMessage, ChatMessageType
from apps.experiments.models import AgentTools, ParticipantData, SourceMaterial
from apps.service_providers.llm_service.runnables import (
    AgentExperimentRunnable,
    ChainOutput,
    ExperimentRunnable,
    SimpleExperimentRunnable,
)
from apps.utils.factories.channels import ChannelPlatform, ExperimentChannelFactory
from apps.utils.factories.experiment import ExperimentSessionFactory
from apps.utils.langchain import FakeLlm, FakeLlmService


@pytest.fixture()
def fake_llm():
    return FakeLlm(responses=["this is a test message"], token_counts=[30, 20, 10])


@pytest.fixture()
def session(fake_llm):
    session = ExperimentSessionFactory()
    session.experiment.get_llm_service = lambda: FakeLlmService(llm=fake_llm)
    session.experiment.tools = [AgentTools.SCHEDULE_UPDATE]
    return session


@pytest.fixture()
def chat(team_with_users):
    chat = Chat.objects.create(team=team_with_users)
    ChatMessage.objects.create(chat=chat, content="Hello", message_type=ChatMessageType.HUMAN)
    return chat


@dataclasses.dataclass
class RunnableFixture:
    runnable: type[ExperimentRunnable]
    expect_tools: bool = False

    def build(self, *args, **kwargs):
        return self.runnable(*args, **kwargs)


runnables = {
    "simple": RunnableFixture(SimpleExperimentRunnable),
    "agent": RunnableFixture(AgentExperimentRunnable, expect_tools=True),
}


@pytest.fixture(params=list(runnables))
def runnable(request, session):
    return runnables[request.param]


@pytest.mark.django_db()
@freezegun.freeze_time("2024-02-08 13:00:08.877096+00:00")
def test_runnable(runnable, session, fake_llm):
    chain = runnable.build(experiment=session.experiment, session=session)
    result = chain.invoke("hi")
    assert result == ChainOutput(output="this is a test message", prompt_tokens=30, completion_tokens=20)
    assert len(fake_llm.get_calls()) == 1
    assert _messages_to_dict(fake_llm.get_call_messages()[0]) == [
        {
            "system": (
                "You are a helpful assistant\nThe current datetime is Thursday, 08 February 2024 13:00:08 "
                "UTC (timezone preserved)"
            )
        },
        {"human": "hi"},
    ]
    if runnable.expect_tools:
        assert "tools" in fake_llm.get_calls()[0].kwargs
    else:
        assert "tools" not in fake_llm.get_calls()[0].kwargs


@pytest.mark.django_db()
@freezegun.freeze_time("2024-02-08 13:00:08.877096+00:00")
def test_runnable_with_source_material(runnable, session, fake_llm):
    session.experiment.source_material = SourceMaterial(material="this is the source material")
    session.experiment.prompt_text = "System prompt with {source_material}"
    chain = runnable.build(experiment=session.experiment, session=session)
    result = chain.invoke("hi")
    assert result == ChainOutput(output="this is a test message", prompt_tokens=30, completion_tokens=20)
    expected_system__prompt = (
        "System prompt with this is the source material"
        + "\nThe current datetime is Thursday, 08 February 2024 13:00:08 UTC (timezone preserved)"
    )
    assert fake_llm.get_call_messages()[0][0] == SystemMessage(content=expected_system__prompt)


@pytest.mark.django_db()
@freezegun.freeze_time("2024-02-08 13:00:08.877096+00:00")
def test_runnable_with_source_material_missing(runnable, session, fake_llm):
    session.experiment.prompt_text = "System prompt with {source_material}"
    chain = runnable.build(experiment=session.experiment, session=session)
    result = chain.invoke("hi")
    assert result == ChainOutput(output="this is a test message", prompt_tokens=30, completion_tokens=20)
    expected_system__prompt = (
        "System prompt with " + "\nThe current datetime is Thursday, 08 February 2024 13:00:08 UTC (timezone preserved)"
    )
    assert fake_llm.get_call_messages()[0][0] == SystemMessage(content=expected_system__prompt)


@pytest.mark.django_db()
def test_runnable_runnable_format_input(runnable, session, fake_llm):
    chain = runnable.build(experiment=session.experiment, session=session)
    session.experiment.input_formatter = "foo {input} bar"
    result = chain.invoke("hi")
    assert result == ChainOutput(output="this is a test message", prompt_tokens=30, completion_tokens=20)
    assert len(fake_llm.get_calls()) == 1
    assert _messages_to_dict(fake_llm.get_call_messages()[0])[1] == {"human": "foo hi bar"}


@pytest.mark.django_db()
def test_runnable_save_input_to_history(runnable, session, chat, fake_llm):
    chain = runnable.build(experiment=session.experiment, session=session)
    session.chat = chat
    assert chat.messages.count() == 1

    result = chain.invoke("hi", config={"configurable": {"save_input_to_history": False}})

    assert result.output == "this is a test message"
    assert len(fake_llm.get_calls()) == 1
    assert chat.messages.count() == 2


@pytest.mark.django_db()
@freezegun.freeze_time("2024-02-08 13:00:08.877096+00:00")
def test_runnable_with_history(runnable, session, chat, fake_llm):
    experiment = session.experiment
    experiment.max_token_limit = 0  # disable compression
    session.chat = chat
    assert chat.messages.count() == 1
    chain = runnable.build(experiment=experiment, session=session)
    result = chain.invoke("hi")
    assert result == ChainOutput(output="this is a test message", prompt_tokens=30, completion_tokens=20)
    assert len(fake_llm.get_calls()) == 1
    assert _messages_to_dict(fake_llm.get_call_messages()[0]) == [
        {
            "system": experiment.prompt_text
            + "\nThe current datetime is Thursday, 08 February 2024 13:00:08 UTC (timezone preserved)"
        },
        {"human": "Hello"},
        {"human": "hi"},
    ]
    assert chat.messages.count() == 3


@pytest.mark.django_db()
@freezegun.freeze_time("2024-02-08 13:00:08.877096+00:00")
@pytest.mark.parametrize(
    ("participant_with_user", "is_web_session", "considered_authorized"),
    [(True, True, True), (False, True, False), (True, False, True), (False, False, True)],
)
@patch("apps.channels.models._set_telegram_webhook")
def test_runnable_with_participant_data(
    _set_telegram_webhook, participant_with_user, is_web_session, considered_authorized, runnable, session, fake_llm
):
    """Participant data should be included in the prompt only for authorized users"""
    session.experiment_channel = ExperimentChannelFactory(
        experiment=session.experiment, platform=ChannelPlatform.WEB if is_web_session else ChannelPlatform.TELEGRAM
    )
    session.save()

    participant = session.participant
    if participant_with_user:
        participant.user = session.experiment.owner
    else:
        participant.user = None
    participant.save()

    ParticipantData.objects.create(
        team=session.team, content_object=session.experiment, participant=participant, data={"name": "Tester"}
    )
    session.experiment.prompt_text = "System prompt with {participant_data}"
    chain = runnable.build(experiment=session.experiment, session=session)
    chain.invoke("hi")

    if considered_authorized:
        expected_prompt = (
            "System prompt with {'name': 'Tester'}\nThe current datetime is Thursday, 08 February 2024 13:00:08 UTC"
            " (timezone preserved)"
        )
    else:
        expected_prompt = (
            "System prompt with \nThe current datetime is Thursday, 08 February 2024 13:00:08 UTC"
            " (timezone preserved)"
        )
    assert fake_llm.get_call_messages()[0][0] == SystemMessage(content=expected_prompt)


def _messages_to_dict(messages: Sequence[BaseMessage]) -> list[dict]:
    return [{message.type: message.content} for message in messages]
