import mimetypes
import pathlib
from functools import wraps
from io import BytesIO

import openai
from openai import OpenAI
from openai.types.beta import Assistant

from apps.assistants.models import OpenAiAssistant, ToolResources
from apps.assistants.utils import get_assistant_tool_options
from apps.files.models import File
from apps.service_providers.models import LlmProvider
from apps.teams.models import Team


class OpenAiSyncError(Exception):
    pass


def wrap_openai_errors(fn):
    @wraps(fn)
    def _inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except openai.APIError as e:
            message = e.message
            if isinstance(e.body, dict):
                try:
                    message = e.body["message"]
                except KeyError | AttributeError:
                    pass

            raise OpenAiSyncError(message) from e

    return _inner


@wrap_openai_errors
def push_assistant_to_openai(assistant: OpenAiAssistant):
    """Pushes the assistant to OpenAI. If the assistant already exists, it will be updated."""
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    data = _ocs_assistant_to_openai_kwargs(assistant)
    data["tool_resources"] = _sync_tool_resources(assistant)
    if assistant.assistant_id:
        client.beta.assistants.update(assistant.assistant_id, **data)
    else:
        openai_assistant = client.beta.assistants.create(**data)
        assistant.assistant_id = openai_assistant.id
        assistant.save()


@wrap_openai_errors
def delete_file_from_openai(client: OpenAI, file: File):
    if not file.external_id or file.external_source != "openai":
        return

    try:
        client.files.delete(file.external_id)
    except openai.NotFoundError:
        pass
    file.external_id = ""
    file.external_source = ""


@wrap_openai_errors
def sync_from_openai(assistant: OpenAiAssistant):
    """Syncs the local assistant instance with the remote OpenAI assistant."""
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    openai_assistant = client.beta.assistants.retrieve(assistant.assistant_id)
    for key, value in _openai_assistant_to_ocs_kwargs(openai_assistant).items():
        setattr(assistant, key, value)
    assistant.save()
    _sync_tool_resources_from_openai(openai_assistant, assistant)


@wrap_openai_errors
def import_openai_assistant(assistant_id: str, llm_provider: LlmProvider, team: Team) -> OpenAiAssistant:
    client = llm_provider.get_llm_service().get_raw_client()
    openai_assistant = client.beta.assistants.retrieve(assistant_id)
    kwargs = _openai_assistant_to_ocs_kwargs(openai_assistant, team=team, llm_provider=llm_provider)
    assistant = OpenAiAssistant.objects.create(**kwargs)
    _sync_tool_resources_from_openai(openai_assistant, assistant)
    return assistant


@wrap_openai_errors
def delete_openai_assistant(assistant: OpenAiAssistant):
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    try:
        client.beta.assistants.delete(assistant.assistant_id)
    except openai.NotFoundError:
        pass

    for resource in assistant.tool_resources.all():
        if resource.tool_type == "file_search" and "vector_store_id" in resource.extra:
            vector_store_id = resource.extra.pop("vector_store_id")
            client.beta.vector_stores.delete(vector_store_id=vector_store_id)

        for file in resource.files.all():
            delete_file_from_openai(client, file)


def _fetch_file_from_openai(assistant: OpenAiAssistant, file_id: str) -> File:
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    openai_file = client.files.retrieve(file_id)
    filename = openai_file.filename
    try:
        filename = pathlib.Path(openai_file.filename).name
    except Exception:
        pass

    content_type = mimetypes.guess_type(filename)[0]
    file = File(
        team=assistant.team,
        name=filename,
        content_type=content_type,
        external_id=openai_file.id,
        external_source="openai",
    )
    # Can't retrieve content from openai assistant files
    # content = client.files.retrieve_content(openai_file.id)
    # file.file.save(filename, ContentFile(content.read()))
    file.save()
    return file


def _sync_tool_resources_from_openai(openai_assistant: Assistant, assistant: OpenAiAssistant):
    tools = {tool.type for tool in openai_assistant.tools}
    if "code_interpreter" in tools:
        ocs_code_interpreter, _ = ToolResources.objects.get_or_create(assistant=assistant, tool_type="code_interpreter")
        try:
            code_file_ids = openai_assistant.tool_resources.code_interpreter.file_ids
        except AttributeError:
            pass
        else:
            _sync_tool_resource_files_from_openai(code_file_ids, ocs_code_interpreter)

    if "file_search" in tools:
        ocs_file_search, _ = ToolResources.objects.get_or_create(assistant=assistant, tool_type="file_search")
        try:
            vector_store_id = openai_assistant.tool_resources.file_search.vector_store_ids[0]
        except AttributeError:
            pass
        else:
            if ocs_file_search.extra.get("vector_store_id") != vector_store_id:
                ocs_file_search.extra["vector_store_id"] = vector_store_id
                ocs_file_search.save()
            client = assistant.llm_provider.get_llm_service().get_raw_client()
            file_ids = (
                file.id
                for file in client.beta.vector_stores.files.list(
                    vector_store_id=vector_store_id  # there can only be one
                )
            )
            _sync_tool_resource_files_from_openai(file_ids, ocs_file_search)


def _sync_tool_resource_files_from_openai(file_ids, ocs_resource):
    resource_files = ocs_resource.files.all()
    unused_files = {file.id for file in resource_files}
    existing_files = {file.external_id: file for file in resource_files if file.external_id}
    for file_id in file_ids:
        try:
            file = existing_files.pop(file_id)
            unused_files.remove(file.id)
        except KeyError:
            ocs_resource.files.add(_fetch_file_from_openai(ocs_resource.assistant, file_id))
    if unused_files:
        File.objects.filter(id__in=unused_files).delete()


def _sync_vector_store_files_to_openai(client, vector_store_id, files_ids: list[str]):
    vector_store_files = (file.id for file in client.beta.vector_stores.files.list(vector_store_id=vector_store_id))
    to_delete_remote = []
    for file_id in vector_store_files:
        try:
            files_ids.remove(file_id)
        except ValueError:
            to_delete_remote.append(file_id)

    for file_id in to_delete_remote:
        client.beta.vector_stores.files.delete(vector_store_id=vector_store_id, file_id=file_id)

    if files_ids:
        client.beta.vector_stores.file_batches.create(vector_store_id=vector_store_id, file_ids=files_ids)


def _ocs_assistant_to_openai_kwargs(assistant: OpenAiAssistant) -> dict:
    return {
        "instructions": assistant.instructions,
        "name": assistant.name,
        "tools": assistant.formatted_tools,
        "model": assistant.llm_model,
        "temperature": assistant.temperature,
        "top_p": assistant.top_p,
        "metadata": {
            "ocs_assistant_id": str(assistant.id),
        },
        "tool_resources": {},
    }


def _sync_tool_resources(assistant):
    resource_data = {}
    resources = {resource.tool_type: resource for resource in assistant.tool_resources.all()}
    if code_interpreter := resources.get("code_interpreter"):
        file_ids = _create_files_remote(assistant, code_interpreter.files.all())
        resource_data["code_interpreter"] = {"file_ids": file_ids}

    if file_search := resources.get("file_search"):
        file_ids = _create_files_remote(assistant, file_search.files.all())
        store_id = file_search.extra.get("vector_store_id")
        updated_store_id = _update_or_create_vector_store(
            assistant, f"{assistant.name} - File Search", store_id, file_ids
        )
        if store_id != updated_store_id:
            file_search.extra["vector_store_id"] = updated_store_id
            file_search.save()
        resource_data["file_search"] = {"vector_store_ids": [updated_store_id]}

    return resource_data


def _update_or_create_vector_store(assistant, name, vector_store_id, file_ids) -> str:
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    if vector_store_id:
        try:
            client.beta.vector_stores.retrieve(vector_store_id)
        except openai.NotFoundError:
            vector_store_id = None

    if not vector_store_id and assistant.assistant_id:
        # check if there is a vector store attached to this assistant that we don't know about
        openai_assistant = client.beta.assistants.retrieve(assistant.assistant_id)
        try:
            vector_store_id = openai_assistant.tool_resources.file_search.vector_store_ids[0]
        except (AttributeError, IndexError):
            pass

    if vector_store_id:
        _sync_vector_store_files_to_openai(client, vector_store_id, file_ids)
        return vector_store_id

    vector_store = client.beta.vector_stores.create(name=name, file_ids=file_ids)
    return vector_store.id


def _openai_assistant_to_ocs_kwargs(assistant: Assistant, team=None, llm_provider=None) -> dict:
    builtin_tools = dict(get_assistant_tool_options())
    kwargs = {
        "assistant_id": assistant.id,
        "name": assistant.name or "Untitled Assistant",
        "instructions": assistant.instructions or "",
        "builtin_tools": [tool.type for tool in assistant.tools if tool.type in builtin_tools],
        # What if the model isn't one of the ones configured for the LLM Provider?
        "llm_model": assistant.model,
        "temperature": assistant.temperature,
        "top_p": assistant.top_p,
    }
    if team:
        kwargs["team"] = team
    if llm_provider:
        kwargs["llm_provider"] = llm_provider
    return kwargs


def _create_files_remote(assistant, files):
    file_ids = []
    for file in files:
        if not file.external_id:
            _push_file_to_openai(assistant, file)
        file_ids.append(file.external_id)
    return file_ids


def _push_file_to_openai(assistant: OpenAiAssistant, file: File):
    client = assistant.llm_provider.get_llm_service().get_raw_client()
    with file.file.open("rb") as fh:
        bytesio = BytesIO(fh.read())
    openai_file = client.files.create(
        file=(file.name, bytesio),
        purpose="assistants",
    )
    file.external_id = openai_file.id
    file.external_source = "openai"
    file.save()
