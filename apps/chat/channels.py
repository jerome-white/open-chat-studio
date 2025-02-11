import logging
from abc import abstractmethod
from enum import Enum
from functools import cached_property
from io import BytesIO
from typing import ClassVar

import requests
from django.conf import settings
from django.db import transaction
from telebot import TeleBot
from telebot.util import antiflood, smart_split

from apps.channels import audio
from apps.channels.models import ChannelPlatform, ExperimentChannel
from apps.chat.bots import TopicBot
from apps.chat.exceptions import AudioSynthesizeException, MessageHandlerException
from apps.chat.models import ChatMessage, ChatMessageType
from apps.events.models import StaticTriggerType
from apps.events.tasks import enqueue_static_triggers
from apps.experiments.models import (
    Experiment,
    ExperimentSession,
    Participant,
    SessionStatus,
    VoiceResponseBehaviours,
)
from apps.service_providers.llm_service.runnables import GenerationCancelled
from apps.service_providers.speech_service import SynthesizedAudio
from apps.slack.utils import parse_session_external_id
from apps.users.models import CustomUser

USER_CONSENT_TEXT = "1"
UNSUPPORTED_MESSAGE_BOT_PROMPT = """
Tell the user (in the language being spoken) that they sent an unsupported message.
You only support {supported_types} messages types. Respond only with the message for the user
"""


class MESSAGE_TYPES(Enum):
    TEXT = "text"
    VOICE = "voice"

    @staticmethod
    def is_member(value: str):
        return any(value == item.value for item in MESSAGE_TYPES)


class ChannelBase:
    """
    This class defines a set of common functions that all channels
    must implement. It provides a blueprint for tuning the behavior of the handler to suit specific channels.

    Attributes:
        voice_replies_supported: Indicates whether the channel supports voice messages

    Args:
        experiment_channel: An optional ExperimentChannel object representing the channel associated with the handler.
        experiment_session: An optional ExperimentSession object representing the experiment session associated
            with the handler.

        Either one of these arguments must to be provided
    Raises:
        MessageHandlerException: If both 'experiment_channel' and 'experiment_session' arguments are not provided.

    Properties:
        chat_id: An abstract property that must be implemented in subclasses to return the unique identifier
            of the chat.
        message_content_type: An abstract property that must be implemented in subclasses to return the type
            of message content (e.g., text, voice).
        message_text: An abstract property that must be implemented in subclasses to return the text
            content of the message.

    Class variables:
        supported_message_types: A list of message content types that are supported by this channel

    Abstract methods:
        initialize: (Optional) Performs any necessary initialization
        send_voice_to_user: (Optional) An abstract method to send a voice message to the user. This must be implemented
            if voice_replies_supported is True
        send_text_to_user: Implementation of sending text to the user. Typically this is the reply from the bot
        get_message_audio: The method to retrieve the audio content of the message from the external channel
        transcription_started:A callback indicating that the transcription process has started
        transcription_finished: A callback indicating that the transcription process has finished.
        submit_input_to_llm: A callback indicating that the user input will be given to the language model
    Public API:
        new_user_message: Handles a message coming from the user.
        new_bot_message: Handles a message coming from the bot.
        get_chat_id_from_message: Returns the unique identifier of the chat from the message object.
    """

    voice_replies_supported: ClassVar[bool] = False
    supported_message_types: ClassVar[str] = []

    def __init__(
        self,
        experiment_channel: ExperimentChannel | None = None,
        experiment_session: ExperimentSession | None = None,
    ):
        if not experiment_channel and not experiment_session:
            raise MessageHandlerException("ChannelBase expects either")

        self.experiment_session = experiment_session
        self.experiment_channel = experiment_channel if experiment_channel else experiment_session.experiment_channel
        self.experiment = experiment_channel.experiment if experiment_channel else experiment_session.experiment
        self.message = None
        self._user_query = None
        self.initialize()

    @abstractmethod
    def initialize(self):
        pass

    @property
    def chat_id(self) -> str:
        if self.experiment_session and self.experiment_session.participant.identifier:
            return self.experiment_session.participant.identifier
        return self.get_chat_id_from_message(self.message)

    @abstractmethod
    def get_chat_id_from_message(self, message):
        raise NotImplementedError()

    @property
    @abstractmethod
    def message_content_type(self):
        raise NotImplementedError()

    @property
    @abstractmethod
    def message_text(self):
        raise NotImplementedError()

    @abstractmethod
    def send_voice_to_user(self, synthetic_voice: SynthesizedAudio):
        if self.voice_replies_supported:
            raise Exception(
                "Voice replies are supported but the method reply (`send_voice_to_user`) is not implemented"
            )
        pass

    @abstractmethod
    def send_text_to_user(self, text: str):
        """Channel specific way of sending text back to the user"""
        pass

    @abstractmethod
    def get_message_audio(self) -> BytesIO:
        pass

    @abstractmethod
    def transcription_started(self):
        """Callback indicating that the transcription process started"""
        pass

    @abstractmethod
    def transcription_finished(self, transcript: str):
        """Callback indicating that the transcription is finished"""
        pass

    @abstractmethod
    def submit_input_to_llm(self):
        """Callback indicating that the user input will now be given to the LLM"""
        pass

    def new_bot_message(self, bot_message: str):
        """Handles a message coming from the bot. Call this to send bot messages to the user"""
        self._send_message_to_user(bot_message)

    @staticmethod
    def from_experiment_session(experiment_session: ExperimentSession) -> "ChannelBase":
        """Given an `experiment_session` instance, returns the correct ChannelBase subclass to use"""
        platform = experiment_session.experiment_channel.platform

        if platform == "telegram":
            channel_cls = TelegramChannel
        elif platform == "web":
            channel_cls = WebChannel
        elif platform == "whatsapp":
            channel_cls = WhatsappChannel
        elif platform == "facebook":
            channel_cls = FacebookMessengerChannel
        elif platform == "api":
            channel_cls = ApiChannel
        elif platform == "slack":
            channel_cls = SlackChannel
        else:
            raise Exception(f"Unsupported platform type {platform}")
        return channel_cls(
            experiment_channel=experiment_session.experiment_channel, experiment_session=experiment_session
        )

    @property
    def user_query(self):
        """Returns the user query, extracted from whatever (supported) message type was used to convey the
        message
        """
        if not self._user_query:
            self._user_query = self._extract_user_query()
        return self._user_query

    def _add_message(self, message):
        """Adds the message to the handler in order to extract session information"""
        self._user_query = None
        self.message = message
        self._ensure_sessions_exists()

    def new_user_message(self, message) -> str:
        """Handles the message coming from the user. Call this to send bot messages to the user.
        The `message` here will probably be some object, depending on the channel being used.
        """
        try:
            return self._new_user_message(message)
        except GenerationCancelled:
            return ""

    def _new_user_message(self, message) -> str:
        self._add_message(message)

        if not self.is_message_type_supported():
            return self._handle_unsupported_message()

        if self.experiment_channel.platform != ChannelPlatform.WEB:
            if self._is_reset_conversation_request():
                # Webchats' statuses are updated through an "external" flow
                return

            if self.experiment.conversational_consent_enabled:
                if self._should_handle_pre_conversation_requirements():
                    self._handle_pre_conversation_requirements()
                    return
            else:
                # If `conversational_consent_enabled` is not enabled, we should just make sure that the session's status
                # is ACTIVE
                self.experiment_session.update_status(SessionStatus.ACTIVE)

        enqueue_static_triggers.delay(self.experiment_session.id, StaticTriggerType.NEW_HUMAN_MESSAGE)
        response = self._handle_supported_message()
        return response

    def _handle_pre_conversation_requirements(self):
        """Since external channels doesn't have nice UI, we need to ask users' consent and get them to fill in the
        pre-survey using the conversation thread. We use the session status and a rough state machine to achieve this.

        Here's a breakdown of the flow and the expected session status for each
        Session started -> status will be SETUP
        (Status==SETUP) First user message -> set status to PENDING

        (Status==PENDING) User gave consent -> set status to ACTIVE if there isn't a survey
        (Status==PENDING) User gave consent -> set status to PENDING_PRE_SURVEY if there is a survey

        (Status==PENDING_PRE_SURVEY) user indicated that they took the survey -> sett status to ACTIVE
        """
        # We manually add the message to the history here, since this doesn't follow the normal flow
        self._add_message_to_history(self.user_query, ChatMessageType.HUMAN)

        if self.experiment_session.status == SessionStatus.SETUP:
            self._chat_initiated()
        elif self.experiment_session.status == SessionStatus.PENDING:
            if self._user_gave_consent():
                if not self.experiment.pre_survey:
                    self.start_conversation()
                else:
                    self.experiment_session.update_status(SessionStatus.PENDING_PRE_SURVEY)
                    self._ask_user_to_take_survey()
            else:
                self._ask_user_for_consent()
        elif self.experiment_session.status == SessionStatus.PENDING_PRE_SURVEY:
            if self._user_gave_consent():
                self.start_conversation()
            else:
                self._ask_user_to_take_survey()

    def start_conversation(self):
        self.experiment_session.update_status(SessionStatus.ACTIVE)
        # This is technically the start of the conversation
        if self.experiment.seed_message:
            bot_response = self._generate_response_for_user(self.experiment.seed_message)
            self.new_bot_message(bot_response)

    def _chat_initiated(self):
        """The user initiated the chat and we need to get their consent before continuing the conversation"""
        self.experiment_session.update_status(SessionStatus.PENDING)
        self._ask_user_for_consent()

    def _ask_user_for_consent(self):
        consent_text = self.experiment.consent_form.consent_text
        confirmation_text = self.experiment.consent_form.confirmation_text
        bot_message = f"{consent_text}\n\n{confirmation_text}"
        self._add_message_to_history(bot_message, ChatMessageType.AI)
        self.send_text_to_user(bot_message)

    def _ask_user_to_take_survey(self):
        pre_survey_link = self.experiment_session.get_pre_survey_link()
        confirmation_text = self.experiment.pre_survey.confirmation_text
        bot_message = confirmation_text.format(survey_link=pre_survey_link)
        self._add_message_to_history(bot_message, ChatMessageType.AI)
        self.send_text_to_user(bot_message)

    def _should_handle_pre_conversation_requirements(self):
        """Checks to see if the user went through the pre-conversation formalities, such as giving consent and filling
        out the survey. Since we're using and updating the session's status during this flow, simply checking the
        session status should be enough.
        """
        return self.experiment_session.status in [
            SessionStatus.SETUP,
            SessionStatus.PENDING,
            SessionStatus.PENDING_PRE_SURVEY,
        ]

    def _user_gave_consent(self) -> bool:
        return self.user_query.strip() == USER_CONSENT_TEXT

    def _extract_user_query(self) -> str:
        if self.message_content_type == MESSAGE_TYPES.VOICE:
            try:
                return self._get_voice_transcript()
            except Exception as e:
                self._inform_user_of_error()
                raise e
        return self.message_text

    def _send_message_to_user(self, bot_message: str):
        """Sends the `bot_message` to the user. The experiment's config will determine which message type to use"""
        send_message_func = self.send_text_to_user
        user_sent_voice = self.message and self.message_content_type == MESSAGE_TYPES.VOICE

        if self.voice_replies_supported and self.experiment.synthetic_voice:
            voice_config = self.experiment.voice_response_behaviour
            if voice_config == VoiceResponseBehaviours.ALWAYS:
                send_message_func = self._reply_voice_message
            elif voice_config == VoiceResponseBehaviours.RECIPROCAL and user_sent_voice:
                send_message_func = self._reply_voice_message

        send_message_func(bot_message)

    def _handle_supported_message(self):
        response = self._get_llm_response(self.user_query)
        self._send_message_to_user(response)
        # Returning the response here is a bit of a hack to support chats through the web UI while trying to
        # use a coherent interface to manage / handle user messages
        return response

    def _handle_unsupported_message(self):
        return self.send_text_to_user(self._unsupported_message_type_response())

    def _reply_voice_message(self, text: str):
        voice_provider = self.experiment.voice_provider
        speech_service = voice_provider.get_speech_service()
        try:
            synthetic_voice_audio = speech_service.synthesize_voice(text, self.experiment.synthetic_voice)
            self.send_voice_to_user(synthetic_voice_audio)
        except AudioSynthesizeException as e:
            logging.exception(e)
            self.send_text_to_user(text)

    def _get_voice_transcript(self) -> str:
        # Indicate to the user that the bot is busy processing the message
        self.transcription_started()

        audio_file = self.get_message_audio()
        transcript = self._transcribe_audio(audio_file)
        self.transcription_finished(transcript)
        return transcript

    def _transcribe_audio(self, audio: BytesIO) -> str:
        llm_service = self.experiment.get_llm_service()
        if llm_service.supports_transcription:
            return llm_service.transcribe_audio(audio)
        elif self.experiment.voice_provider:
            speech_service = self.experiment.voice_provider.get_speech_service()
            if speech_service.supports_transcription:
                return speech_service.transcribe_audio(audio)

    def _get_llm_response(self, text: str) -> str:
        """
        Handles a user message by sending it for experiment response and replying with the answer.
        """
        self.submit_input_to_llm()

        return self._get_experiment_response(message=text)

    def _get_experiment_response(self, message: str) -> str:
        experiment_bot = TopicBot(self.experiment_session)
        answer = experiment_bot.process_input(message)
        self.experiment_session.no_activity_ping_count = 0
        self.experiment_session.save()
        return answer

    def _add_message_to_history(self, message: str, message_type: ChatMessageType):
        """Use this to update the chat history when not using the normal bot flow"""
        ChatMessage.objects.create(
            chat=self.experiment_session.chat,
            message_type=message_type,
            content=message,
        )

    def _ensure_sessions_exists(self):
        """
        Ensures an experiment session exists for the given experiment and chat ID.

        Checks if an experiment session already exists for the specified experiment and chat ID.
        If not, a new experiment session is created and associated with the chat.

        If the user requested a new session (by sending the reset command), this will create a new experiment
        session.
        """
        self.experiment_session = (
            ExperimentSession.objects.filter(
                experiment=self.experiment,
                participant__identifier=str(self.chat_id),
            )
            .order_by("-created_at")
            .first()
        )

        if not self.experiment_session:
            self._create_new_experiment_session()
            enqueue_static_triggers.delay(self.experiment_session.id, StaticTriggerType.PARTICIPANT_JOINED_EXPERIMENT)
        else:
            if self._is_reset_conversation_request() and self.experiment_session.user_already_engaged():
                self._reset_session()
            if not self.experiment_session.experiment_channel:
                # This branch will only be entered for channel sessions that were created by the data migration.
                # These sessions doesn't have experiment channels associated with them, so we need to make sure that
                # they have experiment channels here. For new chats/sessions, the channel is added when they're
                # created in _create_new_experiment_session.
                # See this PR: https://github.com/czue/gpt-playground/pull/67
                # If you see this comment in or after November 2023, you can remove this code. Do update the data
                # migration (apps/channels/migrations/0005_create_channel_sessions.py) to link experiment channels
                # to the channel sessions when removing this code
                self.experiment_session.experiment_channel = self.experiment_channel
                self.experiment_session.save()

    def _reset_session(self):
        """Resets the session by ending the current `experiment_session` and creating a new one"""
        self.experiment_session.end()
        self._create_new_experiment_session()

    def _create_new_experiment_session(self):
        """Creates a new experiment session. If one already exists, the participant will be transfered to the new
        session
        """
        if not self.experiment_session:
            participant, _ = Participant.objects.get_or_create(identifier=self.chat_id, team=self.experiment.team)
        else:
            participant = self.experiment_session.participant
        self.experiment_session = ExperimentSession.objects.create(
            team=self.experiment.team,
            participant=participant,
            experiment=self.experiment,
            llm=self.experiment.llm,
            experiment_channel=self.experiment_channel,
        )
        enqueue_static_triggers.delay(self.experiment_session.id, StaticTriggerType.CONVERSATION_START)

    def _is_reset_conversation_request(self):
        return self.user_query == ExperimentChannel.RESET_COMMAND

    def is_message_type_supported(self) -> bool:
        return self.message_content_type is not None and self.message_content_type in self.supported_message_types

    def _unsupported_message_type_response(self):
        """Generates a suitable response to the user when they send unsupported messages"""
        ChatMessage.objects.create(
            chat=self.experiment_session.chat,
            message_type=ChatMessageType.SYSTEM,
            content=f"The user sent an unsupported message type: {self.message.content_type_unparsed}",
        )
        return self._generate_response_for_user(
            UNSUPPORTED_MESSAGE_BOT_PROMPT.format(supported_types=self.supported_message_types)
        )

    def _inform_user_of_error(self):
        """Simply tells the user that something went wrong to keep them in the loop"""
        bot_message = self._generate_response_for_user(
            """
            Tell the user that something went wrong while processing their message and that they should
            try again later
            """
        )
        self.new_bot_message(bot_message)

    def _generate_response_for_user(self, prompt: str) -> str:
        """Generates a response based on the `prompt`."""
        topic_bot = TopicBot(self.experiment_session)
        return topic_bot.process_input(user_input=prompt, save_input_to_history=False)


class WebChannel(ChannelBase):
    """Message Handler for the UI"""

    voice_replies_supported = False
    supported_message_types = [MESSAGE_TYPES.TEXT]

    def get_chat_id_from_message(self, message):
        return message.chat_id

    @property
    def message_content_type(self):
        return MESSAGE_TYPES.TEXT

    @property
    def message_text(self):
        return self.message.message_text

    def new_bot_message(self, bot_message: str):
        # Simply adding a new AI message to the chat history will cause it to be sent to the UI
        pass

    def _ensure_sessions_exists(self):
        if not self.experiment_session:
            raise MessageHandlerException("WebChannel requires an existing session")

    @staticmethod
    def start_new_session(
        experiment: Experiment,
        experiment_channel: ExperimentChannel,
        participant_identifier: str,
        participant_user: CustomUser | None = None,
        session_status: SessionStatus = SessionStatus.ACTIVE,
        timezone: str | None = None,
    ):
        session = _start_experiment_session(
            experiment, experiment_channel, participant_identifier, participant_user, session_status, timezone
        )
        WebChannel.check_and_process_seed_message(session)
        return session

    @staticmethod
    def check_and_process_seed_message(session: ExperimentSession):
        from apps.experiments.tasks import get_response_for_webchat_task

        if session.experiment.seed_message:
            session.seed_task_id = get_response_for_webchat_task.delay(
                session.id, message_text=session.experiment.seed_message
            ).task_id
            session.save()
        return session


class TelegramChannel(ChannelBase):
    voice_replies_supported = True
    supported_message_types = [MESSAGE_TYPES.TEXT, MESSAGE_TYPES.VOICE]

    def initialize(self):
        self.telegram_bot = TeleBot(self.experiment_channel.extra_data["bot_token"], threaded=False)

    def get_chat_id_from_message(self, message):
        return message.chat_id

    @property
    def message_content_type(self):
        return self.message.content_type

    @property
    def message_text(self):
        return self.message.body

    def send_voice_to_user(self, synthetic_voice: SynthesizedAudio):
        antiflood(
            self.telegram_bot.send_voice, self.chat_id, voice=synthetic_voice.audio, duration=synthetic_voice.duration
        )

    def send_text_to_user(self, text: str):
        for message_text in smart_split(text):
            antiflood(self.telegram_bot.send_message, self.chat_id, text=message_text)

    def get_message_audio(self) -> BytesIO:
        file_url = self.telegram_bot.get_file_url(self.message.media_id)
        ogg_audio = BytesIO(requests.get(file_url).content)
        return audio.convert_audio(ogg_audio, target_format="wav", source_format="ogg")

    # Callbacks

    def submit_input_to_llm(self):
        # Indicate to the user that the bot is busy processing the message
        self.telegram_bot.send_chat_action(chat_id=self.chat_id, action="typing")

    def transcription_started(self):
        self.telegram_bot.send_chat_action(chat_id=self.chat_id, action="upload_voice")

    def transcription_finished(self, transcript: str):
        self.telegram_bot.send_message(
            self.chat_id, text=f"I heard: {transcript}", reply_to_message_id=self.message.message_id
        )


class WhatsappChannel(ChannelBase):
    def initialize(self):
        self.messaging_service = self.experiment_channel.messaging_provider.get_messaging_service()

    def send_text_to_user(self, text: str):
        from_number = self.experiment_channel.extra_data.get("number")
        to_number = self.chat_id
        self.messaging_service.send_text_message(
            text, from_=from_number, to=to_number, platform=ChannelPlatform.WHATSAPP
        )

    def get_chat_id_from_message(self, message):
        return message.chat_id

    @property
    def voice_replies_supported(self) -> bool:
        # TODO: Update turn-python library to support this
        return bool(settings.AWS_ACCESS_KEY_ID) and self.messaging_service.voice_replies_supported

    @property
    def supported_message_types(self):
        return self.messaging_service.supported_message_types

    @property
    def message_content_type(self):
        return self.message.content_type

    @property
    def message_text(self):
        return self.message.message_text

    def get_message_audio(self) -> BytesIO:
        return self.messaging_service.get_message_audio(message=self.message)

    def transcription_finished(self, transcript: str):
        self.send_text_to_user(f'I heard: "{transcript}"')

    def send_voice_to_user(self, synthetic_voice: SynthesizedAudio):
        """
        Uploads the synthesized voice to AWS and send the public link to twilio
        """
        from_number = self.experiment_channel.extra_data["number"]
        to_number = self.chat_id
        self.messaging_service.send_voice_message(
            synthetic_voice, from_=from_number, to=to_number, platform=ChannelPlatform.WHATSAPP
        )


class FacebookMessengerChannel(ChannelBase):
    def initialize(self):
        self.messaging_service = self.experiment_channel.messaging_provider.get_messaging_service()

    def send_text_to_user(self, text: str):
        from_ = self.experiment_channel.extra_data.get("page_id")
        self.messaging_service.send_text_message(text, from_=from_, to=self.chat_id, platform=ChannelPlatform.FACEBOOK)

    def get_chat_id_from_message(self, message):
        return message.chat_id

    @property
    def voice_replies_supported(self) -> bool:
        return bool(settings.AWS_ACCESS_KEY_ID) and self.messaging_service.voice_replies_supported

    @property
    def supported_message_types(self):
        return self.messaging_service.supported_message_types

    @property
    def message_content_type(self):
        return self.message.content_type

    @property
    def message_text(self):
        return self.message.message_text

    def get_message_audio(self) -> BytesIO:
        return self.messaging_service.get_message_audio(message=self.message)

    def transcription_finished(self, transcript: str):
        self.send_text_to_user(f'I heard: "{transcript}"')

    def send_voice_to_user(self, synthetic_voice: SynthesizedAudio):
        """
        Uploads the synthesized voice to AWS and send the public link to twilio
        """
        from_ = self.experiment_channel.extra_data["page_id"]
        self.messaging_service.send_voice_message(
            synthetic_voice, from_=from_, to=self.chat_id, platform=ChannelPlatform.FACEBOOK
        )


class ApiChannel(ChannelBase):
    """Message Handler for the API"""

    voice_replies_supported = False
    supported_message_types = [MESSAGE_TYPES.TEXT]

    def get_chat_id_from_message(self, message):
        return message.chat_id

    @property
    def message_content_type(self):
        return MESSAGE_TYPES.TEXT

    @property
    def message_text(self):
        return self.message.message_text

    def new_bot_message(self, bot_message: str):
        # The bot cannot send messages to this client, since it wouldn't know where to send it to
        pass


class SlackChannel(ChannelBase):
    voice_replies_supported = False
    supported_message_types = [MESSAGE_TYPES.TEXT]

    def __init__(
        self,
        experiment_channel: ExperimentChannel | None = None,
        experiment_session: ExperimentSession | None = None,
        send_response_to_user: bool = True,
    ):
        """
        Args:
            send_response_to_user: A boolean indicating whether the handler should send the response to the user.
                This is useful when the message sending happens as part of the slack event handler
                (e.g., in a slack event listener)
        """
        super().__init__(experiment_channel, experiment_session)
        self.send_response_to_user = send_response_to_user

    @cached_property
    def messaging_service(self):
        return self.experiment_channel.messaging_provider.get_messaging_service()

    @property
    def message_content_type(self):
        return MESSAGE_TYPES.TEXT

    @property
    def message_text(self):
        return self.message.message_text

    def send_text_to_user(self, text: str):
        if not self.send_response_to_user:
            return

        if not self.message:
            channel_id, thread_ts = parse_session_external_id(self.experiment_session.external_id)
        else:
            channel_id = self.message.channel_id
            thread_ts = self.message.thread_ts

        self.messaging_service.send_text_message(
            text,
            from_="",
            to=channel_id,
            platform=ChannelPlatform.SLACK,
            thread_ts=thread_ts,
        )

    def _ensure_sessions_exists(self):
        if not self.experiment_session:
            raise MessageHandlerException("WebChannel requires an existing session")

    @staticmethod
    def start_new_session(
        experiment: Experiment, experiment_channel: ExperimentChannel, participant_identifier: str, external_id: str
    ):
        return _start_experiment_session(
            experiment, experiment_channel, participant_identifier, session_external_id=external_id
        )


def _start_experiment_session(
    experiment: Experiment,
    experiment_channel: ExperimentChannel,
    participant_identifier: str,
    participant_user: CustomUser | None = None,
    session_status: SessionStatus = SessionStatus.ACTIVE,
    timezone: str | None = None,
    session_external_id: str | None = None,
) -> ExperimentSession:
    if not participant_identifier and not participant_user:
        raise ValueError("Either participant_identifier or participant_user must be specified!")

    if participant_user and participant_identifier != participant_user.email:
        # This should technically never happen, since we disable the input for logged in users
        raise Exception(f"User {participant_user.email} cannot impersonate participant {participant_identifier}")

    with transaction.atomic():
        try:
            participant = Participant.objects.get(team=experiment.team, identifier=participant_identifier)
            if participant_user and participant.user is None:
                # If a participant becomes a user, we must reconcile the user and participant
                participant.user = participant_user
                participant.save()
        except Participant.DoesNotExist:
            participant = Participant.objects.create(
                user=participant_user,
                identifier=participant_identifier,
                team=experiment.team,
            )

        session = ExperimentSession.objects.create(
            team=experiment.team,
            experiment=experiment,
            llm=experiment.llm,
            experiment_channel=experiment_channel,
            status=session_status,
            participant=participant,
            external_id=session_external_id,
        )

        # Record the participant's timezone
        if timezone:
            participant.update_memory(data={"timezone": timezone})

    if participant.experimentsession_set.count() == 1:
        enqueue_static_triggers.delay(session.id, StaticTriggerType.PARTICIPANT_JOINED_EXPERIMENT)
    enqueue_static_triggers.delay(session.id, StaticTriggerType.CONVERSATION_START)
    return session
