import logging
import re
import time
from abc import ABC
from operator import itemgetter
from time import sleep
from typing import Any, Literal

import openai
from django.utils import timezone
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.agents.openai_assistant.base import OpenAIAssistantFinish
from langchain.memory import ConversationBufferMemory
from langchain_core.load import Serializable
from langchain_core.memory import BaseMemory
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder, SystemMessagePromptTemplate
from langchain_core.runnables import (
    Runnable,
    RunnableConfig,
    RunnableLambda,
    RunnablePassthrough,
    RunnableSerializable,
    ensure_config,
)

from apps.annotations.models import Tag
from apps.channels.models import ChannelPlatform
from apps.chat.agent.tools import get_tools
from apps.chat.conversation import compress_chat_history
from apps.chat.models import ChatMessage, ChatMessageType
from apps.experiments.models import Experiment, ExperimentRoute, ExperimentSession
from apps.utils.time import pretty_date

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    pass


class GenerationCancelled(Exception):
    def __init__(self, output: "ChainOutput"):
        self.output = output


def create_experiment_runnable(experiment: Experiment, session: ExperimentSession) -> "BaseExperimentRunnable":
    """Create an experiment runnable based on the experiment configuration."""
    if experiment.assistant:
        return AssistantExperimentRunnable(experiment=experiment, session=session)

    assert experiment.llm, "Experiment must have an LLM model"
    assert experiment.llm_provider, "Experiment must have an LLM provider"
    if experiment.tools_enabled:
        return AgentExperimentRunnable(experiment=experiment, session=session)

    return SimpleExperimentRunnable(experiment=experiment, session=session)


class ChainOutput(Serializable):
    output: str
    """String text."""
    prompt_tokens: int
    """Number of tokens in the prompt."""
    completion_tokens: int
    """Number of tokens in the completion."""

    type: Literal["OcsChainOutput"] = "ChainOutput"

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether this class is serializable."""
        return True

    @classmethod
    def get_lc_namespace(cls) -> list[str]:
        """Get the namespace of the langchain object."""
        return ["ocs", "schema", "chain_output"]


class BaseExperimentRunnable(RunnableSerializable[dict, ChainOutput], ABC):
    experiment: Experiment
    session: ExperimentSession
    input_key: str = "input"

    class Config:
        arbitrary_types_allowed = True

    @property
    def llm_service(self):
        return self.experiment.get_llm_service()

    @property
    def callback_handler(self):
        model = self.llm_service.get_chat_model(self.experiment.llm, self.experiment.temperature)
        return self.llm_service.get_callback_handler(model)

    def _save_message_to_history(self, message: str, type_: ChatMessageType):
        ChatMessage.objects.create(
            chat=self.session.chat,
            message_type=type_.value,
            content=message,
        )

    def format_input(self, input: dict):
        if self.experiment.input_formatter:
            input[self.input_key] = self.experiment.input_formatter.format(input=input[self.input_key])
        return input

    @property
    def is_unauthorized_participant(self):
        """Returns `true` if a participant is unauthorized. A participant is considered authorized when the
        following conditions are met:
        For web channels:
        - They are a platform user
        All other channels:
        - Always True, since the external channel handles authorization
        """
        return self.session.experiment_channel.platform == ChannelPlatform.WEB and self.session.participant.user is None

    @property
    def participant_data(self):
        if self.is_unauthorized_participant:
            return ""
        return self.session.get_participant_data(use_participant_tz=True) or ""


class ExperimentRunnable(BaseExperimentRunnable):
    memory: BaseMemory = ConversationBufferMemory(return_messages=True, output_key="output", input_key="input")
    cancelled: bool = False
    last_cancel_check: float | None = None
    check_every_ms: int = 1000

    class Config:
        arbitrary_types_allowed = True

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return False

    def invoke(self, input: str, config: RunnableConfig | None = None) -> ChainOutput:
        callback = self.callback_handler
        config = ensure_config(config)
        config["callbacks"] = config["callbacks"] or []
        config["callbacks"].append(callback)

        self._populate_memory()

        if config.get("configurable", {}).get("save_input_to_history", True):
            self._save_message_to_history(input, ChatMessageType.HUMAN)

        output = self._get_output_check_cancellation(input, config)
        result = ChainOutput(
            output=output, prompt_tokens=callback.prompt_tokens, completion_tokens=callback.completion_tokens
        )
        if self.cancelled:
            raise GenerationCancelled(result)

        if config.get("configurable", {}).get("save_output_to_history", True):
            self._save_message_to_history(
                output, ChatMessageType.AI, config.get("configurable", {}).get("add_experiment_tag", False)
            )
        return result

    def _get_output_check_cancellation(self, input, config):
        chain = self._build_chain()

        output = ""
        for token in chain.stream(input, config):
            output += self._parse_output(token)
            if self._chat_is_cancelled():
                return output
        return output

    def _parse_output(self, output):
        return output

    def _chat_is_cancelled(self):
        if self.cancelled:
            return True

        if self.last_cancel_check and self.check_every_ms:
            if self.last_cancel_check + self.check_every_ms > time.time():
                return False

        self.last_cancel_check = time.time()

        self.session.chat.refresh_from_db(fields=["metadata"])
        # temporary mechanism to cancel the chat
        # TODO: change this to something specific to the current chat message
        if self.session.chat.metadata.get("cancelled", False):
            self.cancelled = True

        return self.cancelled

    @property
    def source_material(self):
        return self.experiment.source_material.material if self.experiment.source_material else ""

    def _build_chain(self) -> Runnable[dict[str, Any], Any]:
        raise NotImplementedError

    @property
    def prompt(self):
        participant_tz = self.session.get_participant_timezone()
        current_datetime = pretty_date(timezone.now(), participant_tz)
        # The bot converts to UTC unless we tell it to preserve the given timezone
        prompt = self.experiment.prompt_text + f"\nThe current datetime is {current_datetime} (timezone preserved)"
        system_prompt = SystemMessagePromptTemplate.from_template(prompt)
        return ChatPromptTemplate.from_messages(
            [
                system_prompt,
                MessagesPlaceholder("history", optional=True),
                ("human", "{input}"),
            ]
        )

    def _populate_memory(self):
        # TODO: convert to use BaseChatMessageHistory object
        model = self.llm_service.get_chat_model(self.experiment.llm, self.experiment.temperature)
        messages = compress_chat_history(self.session.chat, model, self.experiment.max_token_limit)
        self.memory.chat_memory.messages = messages

    def _save_message_to_history(self, message: str, type_: ChatMessageType, add_experiment_tag: bool = False):
        chat_message = ChatMessage.objects.create(
            chat=self.session.chat,
            message_type=type_.value,
            content=message,
        )
        if add_experiment_tag:
            exp_route = ExperimentRoute.objects.filter(
                team=self.session.team, child=self.experiment.id, parent=self.session.experiment
            ).first()
            if not Tag.objects.filter(name=exp_route.keyword, team=self.session.team).exists() and exp_route:
                chat_message.tags.create(team=self.session.team, name=exp_route.keyword, is_system_tag=True)
            chat_message.add_tags([exp_route.keyword], team=self.session.team, added_by=None)


class SimpleExperimentRunnable(ExperimentRunnable):
    def _build_chain(self) -> Runnable[dict[str, Any], str]:
        model = self.llm_service.get_chat_model(self.experiment.llm, self.experiment.temperature)
        return (
            {"input": RunnablePassthrough()}
            | RunnablePassthrough.assign(source_material=RunnableLambda(lambda x: self.source_material))
            | RunnablePassthrough.assign(participant_data=RunnableLambda(lambda x: self.participant_data))
            | RunnablePassthrough.assign(
                history=RunnableLambda(self.memory.load_memory_variables) | itemgetter("history")
            )
            | RunnableLambda(self.format_input)
            | self.prompt
            | model
            | StrOutputParser()
        )


class AgentExperimentRunnable(ExperimentRunnable):
    def _parse_output(self, output):
        return output.get("output", "")

    def _build_chain(self) -> Runnable[dict[str, Any], dict]:
        assert self.experiment.tools_enabled
        model = self.llm_service.get_chat_model(self.experiment.llm, self.experiment.temperature)
        tools = get_tools(self.session)
        agent = (
            RunnablePassthrough.assign(source_material=RunnableLambda(lambda x: self.source_material))
            | RunnablePassthrough.assign(participant_data=RunnableLambda(lambda x: self.participant_data))
            | RunnableLambda(self.format_input)
            | create_tool_calling_agent(llm=model, tools=tools, prompt=self.prompt)
        )
        executor = AgentExecutor.from_agent_and_tools(
            agent=agent,
            tools=tools,
            memory=self.memory,
            max_execution_time=120,
        )
        return {"input": RunnablePassthrough()} | executor

    @property
    def prompt(self):
        prompt = super().prompt
        return ChatPromptTemplate.from_messages(prompt.messages + [MessagesPlaceholder("agent_scratchpad")])


class AssistantExperimentRunnable(BaseExperimentRunnable):
    input_key = "content"

    class Config:
        arbitrary_types_allowed = True

    @property
    def chat(self):
        return self.session.chat

    def invoke(self, input: str, config: RunnableConfig | None = None) -> ChainOutput:
        callback = self.callback_handler
        config = ensure_config(config)
        config["callbacks"] = config["callbacks"] or []
        config["callbacks"].append(callback)

        input_dict = {"content": input}

        if config.get("configurable", {}).get("save_input_to_history", True):
            self._save_message_to_history(input, ChatMessageType.HUMAN)

        # Note: if this is not a new chat then the history won't be persisted to the thread
        thread_id = self.chat.get_metadata(self.chat.MetadataKeys.OPENAI_THREAD_ID)
        if thread_id:
            input_dict["thread_id"] = thread_id

        # Langchain doesn't support the `additional_instructions` parameter that the API specifies, so we have to
        # override the instructions if we want to pass in dynamic data.
        # https://github.com/langchain-ai/langchain/blob/cccc8fbe2fe59bde0846875f67aa046aeb1105a3/libs/langchain/langchain/agents/openai_assistant/base.py#L491
        new_instructions = self.experiment.assistant.instructions.format(participant_data=self.participant_data)
        input_dict["instructions"] = new_instructions

        response = self._get_response_with_retries(config, input_dict, thread_id)
        if not thread_id:
            self.chat.set_metadata(self.chat.MetadataKeys.OPENAI_THREAD_ID, response.thread_id)

        output = response.return_values["output"]
        self._save_message_to_history(output, ChatMessageType.AI)

        return ChainOutput(output=response.return_values["output"], prompt_tokens=0, completion_tokens=0)

    def _get_response_with_retries(self, config, input_dict, thread_id):
        assistant = self.experiment.assistant.get_assistant()
        assistant_runnable = RunnableLambda(self.format_input) | assistant
        for i in range(3):
            try:
                response: OpenAIAssistantFinish = assistant_runnable.invoke(input_dict, config)
            except openai.BadRequestError as e:
                self._handle_api_error(thread_id, assistant, e)
            except ValueError as e:
                if re.search(r"cancelling|cancelled", str(e)):
                    raise GenerationCancelled(ChainOutput(output="", prompt_tokens=0, completion_tokens=0))
            else:
                return response
        raise GenerationError("Failed to get response after 3 retries")

    def _handle_api_error(self, thread_id, assistant, exc):
        """Handle OpenAI API errors.
        This should either raise an exception or return if the error was handled and the run should be retried.
        """
        message = exc.body.get("message") or ""
        match = re.match(r".*(thread_[\w]+) while a run (run_[\w]+) is active.*", message)
        if not match:
            raise exc

        error_thread_id, run_id = match.groups()
        if error_thread_id != thread_id:
            raise GenerationError(f"Thread ID mismatch: {error_thread_id} != {thread_id}", exc)

        self._cancel_run(assistant, thread_id, run_id)

    def _cancel_run(self, assistant, thread_id, run_id):
        logger.info("Cancelling run %s in thread %s", run_id, thread_id)
        run = assistant.client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run_id)
        cancelling = run.status == "cancelling"
        while cancelling:
            run = assistant.client.beta.threads.runs.retrieve(run_id, thread_id=thread_id)
            cancelling = run.status == "cancelling"
            if cancelling:
                sleep(assistant.check_every_ms / 1000)
