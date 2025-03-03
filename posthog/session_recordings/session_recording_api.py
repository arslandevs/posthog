import json
import os
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from typing import Any, Optional, cast, Literal

from posthoganalytics.ai.openai import OpenAI
from urllib.parse import urlparse

import posthoganalytics
import requests
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from drf_spectacular.utils import extend_schema
from prometheus_client import Counter, Histogram
from pydantic import ValidationError, BaseModel
from rest_framework import exceptions, request, serializers, viewsets
from rest_framework.mixins import UpdateModelMixin
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder
from rest_framework.request import Request

import posthog.session_recordings.queries.session_recording_list_from_query
from ee.session_recordings.session_summary.summarize_session import summarize_recording
from posthog.api.person import MinimalPersonSerializer
from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.utils import action, safe_clickhouse_string
from posthog.auth import PersonalAPIKeyAuthentication, SharingAccessTokenAuthentication
from posthog.cloud_utils import is_cloud
from posthog.event_usage import report_user_action
from posthog.models import Team, User
from posthog.models.person.person import PersonDistinctId
from posthog.rate_limit import (
    ClickHouseBurstRateThrottle,
    ClickHouseSustainedRateThrottle,
    PersonalApiKeyRateThrottle,
)
from posthog.schema import HogQLQueryModifiers, QueryTiming, RecordingsQuery
from posthog.session_recordings.models.session_recording import SessionRecording
from posthog.session_recordings.models.session_recording_event import (
    SessionRecordingViewed,
)
from posthog.session_recordings.queries.session_recording_list_from_query import SessionRecordingListFromQuery
from posthog.session_recordings.queries.session_replay_events import SessionReplayEvents
from posthog.session_recordings.realtime_snapshots import (
    get_realtime_snapshots,
    publish_subscription,
)
from posthog.storage import object_storage
from posthog.session_recordings.ai_data.ai_filter_schema import AiFilterSchema
from posthog.session_recordings.ai_data.ai_regex_schema import AiRegexSchema
from posthog.session_recordings.ai_data.ai_regex_prompts import AI_REGEX_PROMPTS
from posthog.session_recordings.ai_data.ai_filter_prompts import AI_FILTER_INITIAL_PROMPT, AI_FILTER_PROPERTIES_PROMPT
from posthog.settings.session_replay import SESSION_REPLAY_AI_DEFAULT_MODEL, SESSION_REPLAY_AI_REGEX_MODEL
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
    ChatCompletionAssistantMessageParam,
)
from posthog.session_recordings.utils import clean_prompt_whitespace

SNAPSHOTS_BY_PERSONAL_API_KEY_COUNTER = Counter(
    "snapshots_personal_api_key_counter",
    "Requests for recording snapshots per personal api key",
    labelnames=["api_key", "source"],
)

SNAPSHOT_SOURCE_REQUESTED = Counter(
    "session_snapshots_requested_counter",
    "When calling the API and providing a concrete snapshot type to load.",
    labelnames=["source"],
)

GENERATE_PRE_SIGNED_URL_HISTOGRAM = Histogram(
    "session_snapshots_generate_pre_signed_url_histogram",
    "Time taken to generate a pre-signed URL for a session snapshot",
)

GET_REALTIME_SNAPSHOTS_FROM_REDIS = Histogram(
    "session_snapshots_get_realtime_snapshots_from_redis_histogram",
    "Time taken to get realtime snapshots from Redis",
)

STREAM_RESPONSE_TO_CLIENT_HISTOGRAM = Histogram(
    "session_snapshots_stream_response_to_client_histogram",
    "Time taken to stream a session snapshot to the client",
)


def filter_from_params_to_query(params: dict) -> RecordingsQuery:
    data_dict = query_as_params_to_dict(params)
    # we used to send `version` and it's not part of query, so we pop to make sure
    data_dict.pop("version", None)
    # we used to send `hogql_filtering` and it's not part of query, so we pop to make sure
    data_dict.pop("hogql_filtering", None)

    try:
        return RecordingsQuery.model_validate(data_dict)
    except ValidationError as pydantic_validation_error:
        raise exceptions.ValidationError(json.dumps(pydantic_validation_error.errors()))


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

    def to_openai_message(self) -> ChatCompletionUserMessageParam | ChatCompletionAssistantMessageParam:
        if self.role == "user":
            return ChatCompletionUserMessageParam(role="user", content=self.content)
        return ChatCompletionAssistantMessageParam(role="assistant", content=self.content)


class AiFilterRequest(BaseModel):
    messages: list[ChatMessage]


class SurrogatePairSafeJSONEncoder(JSONEncoder):
    def encode(self, o):
        return safe_clickhouse_string(super().encode(o), with_counter=False)


class SurrogatePairSafeJSONRenderer(JSONRenderer):
    """
    Blob snapshots are compressed data which we pass through from blob storage.
    Realtime snapshot API returns "bare" JSON from Redis.
    We can be sure that the "bare" data could contain surrogate pairs
    from the browser's console logs.

    This JSON renderer ensures that the stringified JSON does not have any unescaped surrogate pairs.

    Because it has to override the encoder, it can't use orjson.
    """

    encoder_class = SurrogatePairSafeJSONEncoder


# context manager for gathering a sequence of server timings
class ServerTimingsGathered:
    def __init__(self):
        # Instance level dictionary to store timings
        self.timings_dict = {}

    def __call__(self, name):
        self.name = name
        return self

    def __enter__(self):
        # timings are assumed to be in milliseconds when reported
        # but are gathered by time.perf_counter which is fractional seconds 🫠
        # so each value is multiplied by 1000 at collection
        self.start_time = time.perf_counter() * 1000

    def __exit__(self, exc_type, exc_val, exc_tb):
        end_time = time.perf_counter() * 1000
        elapsed_time = end_time - self.start_time
        self.timings_dict[self.name] = elapsed_time

    def get_all_timings(self):
        return self.timings_dict


class SessionRecordingSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="session_id", read_only=True)
    recording_duration = serializers.IntegerField(source="duration", read_only=True)
    person = MinimalPersonSerializer(required=False)

    ongoing = serializers.SerializerMethodField()
    viewed = serializers.SerializerMethodField()
    viewers = serializers.SerializerMethodField()
    activity_score = serializers.SerializerMethodField()

    def get_ongoing(self, obj: SessionRecording) -> bool:
        # ongoing is a custom field that we add if loading from ClickHouse
        return getattr(obj, "ongoing", False)

    def get_viewed(self, obj: SessionRecording) -> bool:
        # viewed is a custom field that we load from PG Sql and merge into the model
        return getattr(obj, "viewed", False)

    def get_viewers(self, obj: SessionRecording) -> list[str]:
        return getattr(obj, "viewers", [])

    def get_activity_score(self, obj: SessionRecording) -> Optional[float]:
        return getattr(obj, "activity_score", None)

    class Meta:
        model = SessionRecording
        fields = [
            "id",
            "distinct_id",
            "viewed",
            "viewers",
            "recording_duration",
            "active_seconds",
            "inactive_seconds",
            "start_time",
            "end_time",
            "click_count",
            "keypress_count",
            "mouse_activity_count",
            "console_log_count",
            "console_warn_count",
            "console_error_count",
            "start_url",
            "person",
            "storage",
            "snapshot_source",
            "ongoing",
            "activity_score",
        ]

        read_only_fields = [
            "id",
            "distinct_id",
            "viewed",
            "recording_duration",
            "active_seconds",
            "inactive_seconds",
            "start_time",
            "end_time",
            "click_count",
            "keypress_count",
            "mouse_activity_count",
            "console_log_count",
            "console_warn_count",
            "console_error_count",
            "start_url",
            "storage",
            "snapshot_source",
            "ongoing",
            "activity_score",
        ]


class SessionRecordingSharedSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="session_id", read_only=True)
    recording_duration = serializers.IntegerField(source="duration", read_only=True)

    class Meta:
        model = SessionRecording
        fields = ["id", "recording_duration", "start_time", "end_time"]


class SessionRecordingPropertiesSerializer(serializers.Serializer):
    session_id = serializers.CharField()
    properties = serializers.DictField(required=False)

    def to_representation(self, instance):
        return {
            "id": instance["session_id"],
            "properties": instance["properties"],
        }


class SessionRecordingSnapshotsSourceSerializer(serializers.Serializer):
    source = serializers.CharField()  # type: ignore
    start_timestamp = serializers.DateTimeField(allow_null=True)
    end_timestamp = serializers.DateTimeField(allow_null=True)
    blob_key = serializers.CharField(allow_null=True)


class SessionRecordingSourcesSerializer(serializers.Serializer):
    sources = serializers.ListField(child=SessionRecordingSnapshotsSourceSerializer(), required=False)
    snapshots = serializers.ListField(required=False)


class SessionRecordingUpdateSerializer(serializers.Serializer):
    viewed = serializers.BooleanField(required=False)
    analyzed = serializers.BooleanField(required=False)
    player_metadata = serializers.JSONField(required=False)
    durations = serializers.JSONField(required=False)

    def validate(self, data):
        if not data.get("viewed") and not data.get("analyzed"):
            raise serializers.ValidationError("At least one of 'viewed' or 'analyzed' must be provided.")

        return data


def list_recordings_response(
    listing_result: tuple[list[SessionRecording], bool, dict], context: dict[str, Any]
) -> Response:
    (recordings, more_recordings_available, timings) = listing_result

    session_recording_serializer = SessionRecordingSerializer(recordings, context=context, many=True)
    results = session_recording_serializer.data

    response = Response(
        {"results": results, "has_next": more_recordings_available, "version": 4},
    )
    response.headers["Server-Timing"] = ", ".join(
        f"{key};dur={round(duration, ndigits=2)}" for key, duration in timings.items()
    )
    return response


def ensure_not_weak(etag: str) -> str:
    """
    minio at least doesn't like weak etags, so we need to strip the W/ prefix if it exists.
    we don't really care about the semantic difference between a strong and a weak etag here,
    so we can just strip it.
    """
    if etag.startswith("W/"):
        return etag[2:].lstrip('"').rstrip('"')
    return etag


@contextmanager
def stream_from(url: str, headers: dict | None = None) -> Generator[requests.Response, None, None]:
    """
    Stream data from a URL using optional headers.

    Tricky: mocking the requests library, so we can control the response here is a bit of a pain.
    the mocks are complex to write, so tests fail when the code actually works
    by wrapping this interaction we can mock this method
    instead of trying to mock the internals of the requests library
    """
    if headers is None:
        headers = {}

    session = requests.Session()

    try:
        response = session.get(url, headers=headers, stream=True)
        yield response
    finally:
        session.close()


class SnapshotsBurstRateThrottle(PersonalApiKeyRateThrottle):
    scope = "snapshots_burst"
    rate = "120/minute"


class SnapshotsSustainedRateThrottle(PersonalApiKeyRateThrottle):
    scope = "snapshots_sustained"
    rate = "600/hour"


def query_as_params_to_dict(params_dict: dict) -> dict:
    """
    before (if ever) we convert this to a query runner that takes a post
    we need to convert to a valid dict from the data that arrived in query params
    """
    converted = {}
    for key in params_dict:
        try:
            converted[key] = json.loads(params_dict[key]) if isinstance(params_dict[key], str) else params_dict[key]
        except JSONDecodeError:
            converted[key] = params_dict[key]

    converted.pop("as_query", None)

    return converted


def clean_referer_url(current_url: str | None) -> str:
    try:
        parsed_url = urlparse(current_url)
        path = str(parsed_url.path) if parsed_url.path else "unknown"

        path = re.sub(r"^/?project/\d+", "", path)

        path = re.sub(r"^/?person/.*$", "person-page", path)

        path = re.sub(r"^/?insights/[^/]+/edit$", "insight-edit", path)

        path = re.sub(r"^/?insights/[^/]+$", "insight", path)

        path = re.sub(r"^/?data-management/events/[^/]+$", "data-management-events", path)
        path = re.sub(r"^/?data-management/actions/[^/]+$", "data-management-actions", path)

        path = re.sub(r"^/?replay/[a-fA-F0-9-]+$", "replay-direct", path)
        path = re.sub(r"^/?replay/playlists/.+$", "replay-playlists-direct", path)

        # remove leading and trailing slashes
        path = re.sub(r"^/+|/+$", "", path)
        path = re.sub("/", "-", path)
        return path or "unknown"
    except Exception as e:
        posthoganalytics.capture_exception(e, distinct_id="clean_referer_url", properties={"current_url": current_url})
        return "unknown"


# NOTE: Could we put the sharing stuff in the shared mixin :thinking:
class SessionRecordingViewSet(TeamAndOrgViewSetMixin, viewsets.GenericViewSet, UpdateModelMixin):
    scope_object = "session_recording"
    scope_object_read_actions = ["list", "retrieve", "snapshots"]
    throttle_classes = [ClickHouseBurstRateThrottle, ClickHouseSustainedRateThrottle]
    serializer_class = SessionRecordingSerializer
    # We don't use this
    queryset = SessionRecording.objects.none()

    sharing_enabled_actions = ["retrieve", "snapshots", "snapshot_file"]

    def get_serializer_class(self) -> type[serializers.Serializer]:
        if isinstance(self.request.successful_authenticator, SharingAccessTokenAuthentication):
            return SessionRecordingSharedSerializer
        else:
            return SessionRecordingSerializer

    def safely_get_object(self, queryset) -> SessionRecording:
        recording = SessionRecording.get_or_build(session_id=self.kwargs["pk"], team=self.team)

        if recording.deleted:
            raise exceptions.NotFound("Recording not found")

        return recording

    def list(self, request: request.Request, *args: Any, **kwargs: Any) -> Response:
        query = filter_from_params_to_query(request.GET.dict())

        self._maybe_report_recording_list_filters_changed(request, team=self.team)
        return list_recordings_response(
            list_recordings_from_query(query, cast(User, request.user), team=self.team),
            context=self.get_serializer_context(),
        )

    @extend_schema(
        exclude=True,
        description="""
        Gets a list of event ids that match the given session recording filter.
        The filter must include a single session ID.
        And must include at least one event or action filter.
        This API is intended for internal use and might have unannounced breaking changes.""",
    )
    @action(methods=["GET"], detail=False)
    def matching_events(self, request: request.Request, *args: Any, **kwargs: Any) -> JsonResponse:
        data_dict = query_as_params_to_dict(request.GET.dict())
        query = RecordingsQuery.model_validate(data_dict)

        if not query.session_ids or len(query.session_ids) != 1:
            raise exceptions.ValidationError(
                "Must specify exactly one session_id",
            )

        if not query.events and not query.actions:
            raise exceptions.ValidationError(
                "Must specify at least one event or action filter",
            )

        distinct_id = str(cast(User, request.user).distinct_id)
        modifiers = safely_read_modifiers_overrides(distinct_id, self.team)
        results, _, timings = (
            posthog.session_recordings.queries.session_recording_list_from_query.ReplayFiltersEventsSubQuery(
                query=query, team=self.team, hogql_query_modifiers=modifiers
            ).get_event_ids_for_session()
        )

        response = JsonResponse(data={"results": results})

        response.headers["Server-Timing"] = ", ".join(
            f"{key};dur={round(duration, ndigits=2)}"
            for key, duration in _generate_timings(timings, ServerTimingsGathered()).items()
        )
        return response

    # Returns metadata about the recording
    def retrieve(self, request: request.Request, *args: Any, **kwargs: Any) -> Response:
        recording = self.get_object()

        loaded = recording.load_metadata()

        if not loaded:
            raise exceptions.NotFound("Recording not found")

        recording.load_person()
        if not request.user.is_anonymous:
            viewed = current_user_viewed([str(recording.session_id)], cast(User, request.user), self.team)
            other_viewers = _other_users_viewed([str(recording.session_id)], cast(User, request.user), self.team)

            recording.viewed = str(recording.session_id) in viewed
            recording.viewers = other_viewers.get(str(recording.session_id), [])

        serializer = self.get_serializer(recording)

        return Response(serializer.data)

    def update(self, request: request.Request, *args: Any, **kwargs: Any) -> Response:
        recording = self.get_object()
        loaded = recording.load_metadata()

        if recording is None or recording.deleted or not loaded:
            raise exceptions.NotFound("Recording not found")

        serializer = SessionRecordingUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        current_url = request.headers.get("Referer")
        session_id = request.headers.get("X-Posthog-Session-Id")
        durations = serializer.validated_data.get("durations", {})
        player_metadata = serializer.validated_data.get("player_metadata", {})

        event_properties = {
            "$current_url": current_url,
            "cleaned_replay_path": clean_referer_url(current_url),
            "$session_id": session_id,
            "snapshots_load_time": durations.get("snapshots"),
            "metadata_load_time": durations.get("metadata"),
            "events_load_time": durations.get("events"),
            "first_paint_load_time": durations.get("firstPaint"),
            "duration": player_metadata.get("duration"),
            "recording_id": player_metadata.get("sessionRecordingId"),
            "start_time": player_metadata.get("start"),
            "end_time": player_metadata.get("end"),
            "page_change_events_length": player_metadata.get("pageChangeEventsLength"),
            "recording_width": player_metadata.get("recordingWidth"),
            "load_time": durations.get(
                "firstPaint", 0
            ),  # TODO: DEPRECATED field. Keep around so dashboards don't break
            # older recordings did not store this and so "null" is equivalent to web
            # but for reporting we want to distinguish between not loaded and no value to load
            "snapshot_source": player_metadata.get("snapshotSource", "unknown"),
        }
        user: User | None | AnonymousUser = request.user

        if isinstance(user, User) and not user.is_anonymous:
            if "viewed" in serializer.validated_data:
                recording.check_viewed_for_user(user, save_viewed=True)
                report_user_action(
                    user=user,
                    event="recording viewed",
                    properties=event_properties,
                    team=self.team,
                )

            if "analyzed" in serializer.validated_data:
                report_user_action(
                    user=user,
                    event="recording analyzed",
                    properties=event_properties,
                    team=self.team,
                )

        return Response({"success": True})

    def destroy(self, request: request.Request, *args: Any, **kwargs: Any) -> Response:
        recording = self.get_object()

        if recording.deleted:
            raise exceptions.NotFound("Recording not found")

        recording.deleted = True
        recording.save()

        return Response({"success": True}, status=204)

    @extend_schema(exclude=True)
    @action(methods=["POST"], detail=True)
    def persist(self, request: request.Request, *args: Any, **kwargs: Any) -> Response:
        recording = self.get_object()

        if not settings.EE_AVAILABLE:
            raise exceptions.ValidationError("LTS persistence is only available in the full version of PostHog")

        # Indicates it is not yet persisted
        # "Persistence" is simply saving a record in the DB currently - the actual save to S3 is done on a worker
        if recording.storage == "object_storage":
            recording.save()

        return Response({"success": True})

    @extend_schema(exclude=True)
    @action(
        methods=["GET"],
        detail=True,
        renderer_classes=[SurrogatePairSafeJSONRenderer],
        throttle_classes=[SnapshotsBurstRateThrottle, SnapshotsSustainedRateThrottle],
    )
    def snapshots(self, request: request.Request, **kwargs):
        """
        Snapshots can be loaded from multiple places:
        1. From S3 if the session is older than our ingestion limit. This will be multiple files that can be streamed to the client
        2. or from Redis if the session is newer than our ingestion limit.

        Clients need to call this API twice.
        First without a source parameter to get a list of sources supported by the given session.
        And then once for each source in the returned list to get the actual snapshots.

        NB version 1 of this API has been deprecated and ClickHouse stored snapshots are no longer supported.
        """

        recording = self.get_object()

        if not SessionReplayEvents().exists(session_id=str(recording.session_id), team=self.team):
            raise exceptions.NotFound("Recording not found")

        source = request.GET.get("source")

        event_properties = {
            "team_id": self.team.pk,
            "request_source": source,
            "session_being_loaded": recording.session_id,
        }

        if request.headers.get("X-POSTHOG-SESSION-ID"):
            event_properties["$session_id"] = request.headers["X-POSTHOG-SESSION-ID"]

        posthoganalytics.capture(
            self._distinct_id_from_request(request),
            "v2 session recording snapshots viewed",
            event_properties,
        )

        if source:
            SNAPSHOT_SOURCE_REQUESTED.labels(source=source).inc()

        personal_api_key = PersonalAPIKeyAuthentication.find_key_with_source(request)
        if personal_api_key:
            SNAPSHOTS_BY_PERSONAL_API_KEY_COUNTER.labels(api_key=personal_api_key, source=source).inc()

        if not source:
            return self._gather_session_recording_sources(recording)
        elif source == "realtime":
            return self._send_realtime_snapshots_to_client(recording, request, event_properties)
        elif source == "blob":
            return self._stream_blob_to_client(recording, request, event_properties)
        else:
            raise exceptions.ValidationError("Invalid source must be one of [realtime, blob]")

    def _maybe_report_recording_list_filters_changed(self, request: request.Request, team: Team):
        """
        If the applied filters were modified by the user, capture only the partial filters
        applied (not the full filters object, since that's harder to search through in event props).
        Take each key from the filter and change it to `partial_filter_chosen_{key}`
        """
        user_modified_filters = request.GET.get("user_modified_filters")
        if user_modified_filters:
            user_modified_filters_obj = json.loads(user_modified_filters)
            partial_filters = {
                f"partial_filter_chosen_{key}": value for key, value in user_modified_filters_obj.items()
            }
            current_url = request.headers.get("Referer")
            session_id = request.headers.get("X-POSTHOG-SESSION-ID")

            report_user_action(
                user=cast(User, request.user),
                event="recording list filters changed",
                properties={"$current_url": current_url, "$session_id": session_id, **partial_filters},
                team=team,
            )

    def _gather_session_recording_sources(self, recording: SessionRecording) -> Response:
        might_have_realtime = True
        newest_timestamp = None
        response_data = {}
        sources: list[dict] = []
        blob_keys: list[str] | None = None
        blob_prefix = ""

        if recording.object_storage_path:
            blob_prefix = recording.object_storage_path
            blob_keys = object_storage.list_objects(cast(str, blob_prefix))
            might_have_realtime = False
        else:
            blob_prefix = recording.build_blob_ingestion_storage_path()
            blob_keys = object_storage.list_objects(blob_prefix)

        if blob_keys:
            for full_key in blob_keys:
                # Keys are like 1619712000-1619712060
                blob_key = full_key.replace(blob_prefix.rstrip("/") + "/", "")
                blob_key_base = blob_key.split(".")[0]  # Remove the extension if it exists
                time_range = [datetime.fromtimestamp(int(x) / 1000, tz=UTC) for x in blob_key_base.split("-")]

                sources.append(
                    {
                        "source": "blob",
                        "start_timestamp": time_range[0],
                        "end_timestamp": time_range.pop(),
                        "blob_key": blob_key,
                    }
                )
        if sources:
            sources = sorted(sources, key=lambda x: x["start_timestamp"])
            oldest_timestamp = min(sources, key=lambda k: k["start_timestamp"])["start_timestamp"]
            newest_timestamp = min(sources, key=lambda k: k["end_timestamp"])["end_timestamp"]

            if might_have_realtime:
                might_have_realtime = oldest_timestamp + timedelta(hours=24) > datetime.now(UTC)
        if might_have_realtime:
            sources.append(
                {
                    "source": "realtime",
                    "start_timestamp": newest_timestamp,
                    "end_timestamp": None,
                }
            )
            # the UI will use this to try to load realtime snapshots
            # so, we can publish the request for Mr. Blobby to start syncing to Redis now
            # it takes a short while for the subscription to be sync'd into redis
            # let's use the network round trip time to get started
            publish_subscription(team_id=str(self.team.pk), session_id=str(recording.session_id))
        response_data["sources"] = sources
        serializer = SessionRecordingSourcesSerializer(response_data)
        return Response(serializer.data)

    @staticmethod
    def _validate_blob_key(blob_key: Any) -> None:
        if not blob_key:
            raise exceptions.ValidationError("Must provide a snapshot file blob key")

        if not isinstance(blob_key, str):
            raise exceptions.ValidationError("Invalid blob key: " + blob_key)

        # blob key should be a string of the form 1619712000-1619712060
        if not all(x.isdigit() for x in blob_key.split("-")):
            raise exceptions.ValidationError("Invalid blob key: " + blob_key)

    @staticmethod
    def _distinct_id_from_request(request):
        if isinstance(request.user, AnonymousUser):
            return request.GET.get("sharing_access_token") or "anonymous"
        elif isinstance(request.user, User):
            return str(request.user.distinct_id)
        else:
            return "anonymous"

    @extend_schema(exclude=True)
    @action(methods=["POST"], detail=True)
    def summarize(self, request: request.Request, **kwargs):
        if not request.user.is_authenticated:
            raise exceptions.NotAuthenticated()

        user = cast(User, request.user)

        cache_key = f"summarize_recording_{self.team.pk}_{self.kwargs['pk']}"
        # Check if the response is cached
        cached_response = cache.get(cache_key)
        if cached_response is not None:
            return Response(cached_response)

        recording = self.get_object()

        if not SessionReplayEvents().exists(session_id=str(recording.session_id), team=self.team):
            raise exceptions.NotFound("Recording not found")

        environment_is_allowed = settings.DEBUG or is_cloud()
        has_openai_api_key = bool(os.environ.get("OPENAI_API_KEY"))
        if not environment_is_allowed or not has_openai_api_key:
            raise exceptions.ValidationError("session summary is only supported in PostHog Cloud")

        if not posthoganalytics.feature_enabled("ai-session-summary", str(user.distinct_id)):
            raise exceptions.ValidationError("session summary is not enabled for this user")

        summary = summarize_recording(recording, user, self.team)
        timings = summary.pop("timings", None)
        cache.set(cache_key, summary, timeout=30)

        posthoganalytics.capture(event="session summarized", distinct_id=str(user.distinct_id), properties=summary)

        # let the browser cache for half the time we cache on the server
        r = Response(summary, headers={"Cache-Control": "max-age=15"})
        if timings:
            r.headers["Server-Timing"] = ", ".join(
                f"{key};dur={round(duration, ndigits=2)}" for key, duration in timings.items()
            )
        return r

    def _stream_blob_to_client(
        self, recording: SessionRecording, request: request.Request, event_properties: dict
    ) -> HttpResponse:
        blob_key = request.GET.get("blob_key", "")
        self._validate_blob_key(blob_key)

        # very short-lived pre-signed URL
        with GENERATE_PRE_SIGNED_URL_HISTOGRAM.time():
            if recording.object_storage_path:
                if recording.storage_version == "2023-08-01":
                    file_key = f"{recording.object_storage_path}/{blob_key}"
                else:
                    raise NotImplementedError(
                        f"Unknown session replay object storage version {recording.storage_version}"
                    )
            else:
                blob_prefix = settings.OBJECT_STORAGE_SESSION_RECORDING_BLOB_INGESTION_FOLDER
                file_key = f"{recording.build_blob_ingestion_storage_path(root_prefix=blob_prefix)}/{blob_key}"
            url = object_storage.get_presigned_url(file_key, expiration=60)
            if not url:
                raise exceptions.NotFound("Snapshot file not found")

        event_properties["source"] = "blob"
        event_properties["blob_key"] = blob_key
        posthoganalytics.capture(
            self._distinct_id_from_request(request),
            "session recording snapshots v2 loaded",
            event_properties,
        )

        with STREAM_RESPONSE_TO_CLIENT_HISTOGRAM.time():
            # streams the file from S3 to the client
            # will not decompress the possibly large file because of `stream=True`
            #
            # we pass some headers through to the client
            # particularly we should signal the content-encoding
            # to help the client know it needs to decompress
            #
            # if the client provides an e-tag we can use it to check if the file has changed
            # object store will respect this and send back 304 if the file hasn't changed,
            # and we don't need to send the large file over the wire

            if_none_match = request.headers.get("If-None-Match")
            headers = {}
            if if_none_match:
                headers["If-None-Match"] = ensure_not_weak(if_none_match)

            with stream_from(url=url, headers=headers) as streaming_response:
                streaming_response.raise_for_status()

                response = HttpResponse(content=streaming_response.raw, status=streaming_response.status_code)

                etag = streaming_response.headers.get("ETag")
                if etag:
                    response["ETag"] = ensure_not_weak(etag)

                # blobs are immutable, _really_ we can cache forever
                # but let's cache for an hour since people won't re-watch too often
                # we're setting cache control and ETag which might be considered overkill,
                # but it helps avoid network latency from the client to PostHog, then to object storage, and back again
                # when a client has a fresh copy
                response["Cache-Control"] = streaming_response.headers.get("Cache-Control") or "max-age=3600"

                response["Content-Type"] = "application/json"
                response["Content-Disposition"] = "inline"

                return response

    def _send_realtime_snapshots_to_client(
        self, recording: SessionRecording, request: request.Request, event_properties: dict
    ) -> HttpResponse | Response:
        version = request.GET.get("version", "og")

        with GET_REALTIME_SNAPSHOTS_FROM_REDIS.time():
            snapshot_lines = (
                get_realtime_snapshots(
                    team_id=self.team.pk,
                    session_id=str(recording.session_id),
                )
                or []
            )

        event_properties["source"] = "realtime"
        event_properties["snapshots_length"] = len(snapshot_lines)
        posthoganalytics.capture(
            self._distinct_id_from_request(request),
            "session recording snapshots v2 loaded",
            event_properties,
        )

        if version == "og":
            # originally we returned a list of dictionaries
            # under a snapshot key
            # we keep doing this here for a little while
            # so that existing browser sessions, that don't know about the new format
            # can carry on working until the next refresh
            serializer = SessionRecordingSourcesSerializer({"snapshots": [json.loads(s) for s in snapshot_lines]})
            return Response(serializer.data)
        elif version == "2024-04-30":
            response = HttpResponse(
                # convert list to a jsonl response
                content=("\n".join(snapshot_lines)),
                content_type="application/json",
            )
            # the browser is not allowed to cache this at all
            response["Cache-Control"] = "no-store"
            return response
        else:
            raise exceptions.ValidationError(f"Invalid version: {version}")

    @extend_schema(
        description="Generate session recording filters using AI. This is in development and likely to change, you should not depend on this API."
    )
    @action(methods=["POST"], detail=False, url_path="ai/filters")
    def ai_filters(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not request.user.is_authenticated:
            raise exceptions.NotAuthenticated()

        try:
            # Validate request data against schema
            request_data = AiFilterRequest(messages=[ChatMessage(**msg) for msg in request.data.get("messages", [])])
        except ValidationError:
            raise exceptions.ValidationError(
                "Invalid message format. Messages must be a list of objects with 'role' (either 'user' or 'assistant') and 'content' fields."
            )

        # Create system prompt by combining the initial and properties prompts
        system_message = ChatCompletionSystemMessageParam(
            role="system", content=clean_prompt_whitespace(AI_FILTER_INITIAL_PROMPT + AI_FILTER_PROPERTIES_PROMPT)
        )

        # Convert messages to OpenAI format and combine with system message
        messages: list[ChatCompletionMessageParam] = [system_message]
        for msg in request_data.messages:
            if msg.role == "user":
                messages.append(
                    ChatCompletionUserMessageParam(role="user", content=clean_prompt_whitespace(msg.content))
                )
            else:
                messages.append(
                    ChatCompletionAssistantMessageParam(role="assistant", content=clean_prompt_whitespace(msg.content))
                )

        client = _get_openai_client()

        completion = client.beta.chat.completions.parse(
            model=SESSION_REPLAY_AI_DEFAULT_MODEL,
            messages=messages,
            response_format=AiFilterSchema,
            # need to type ignore before, this will be a WrappedParse
            # but the type detection can't figure that out
            posthog_distinct_id=self._distinct_id_from_request(request),  # type: ignore
            posthog_properties={
                "ai_product": "session_replay",
                "ai_feature": "ai_filters",
            },
        )

        if not completion.choices or not completion.choices[0].message.content:
            raise exceptions.ValidationError("Invalid response from OpenAI")

        try:
            response_data = json.loads(completion.choices[0].message.content)
        except JSONDecodeError:
            raise exceptions.ValidationError("Invalid JSON response from OpenAI")

        return Response(response_data)

    @extend_schema(
        description="Generate regex patterns using AI. This is in development and likely to change, you should not depend on this API."
    )
    @action(methods=["POST"], detail=False, url_path="ai/regex")
    def ai_regex(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        if not request.user.is_authenticated:
            raise exceptions.NotAuthenticated()

        if "regex" not in request.data:
            raise exceptions.ValidationError("Missing required field: regex")

        messages = create_openai_messages(
            system_content=clean_prompt_whitespace(AI_REGEX_PROMPTS),
            user_content=clean_prompt_whitespace(request.data["regex"]),
        )

        client = _get_openai_client()

        completion = client.beta.chat.completions.parse(
            model=SESSION_REPLAY_AI_REGEX_MODEL,
            messages=messages,
            response_format=AiRegexSchema,
            # need to type ignore before, this will be a WrappedParse
            # but the type detection can't figure that out
            posthog_distinct_id=self._distinct_id_from_request(request),  # type: ignore
            posthog_properties={
                "ai_product": "session_replay",
                "ai_feature": "ai_regex",
            },
        )

        if not completion.choices or not completion.choices[0].message.content:
            raise exceptions.ValidationError("Invalid response from OpenAI")

        try:
            response_data = json.loads(completion.choices[0].message.content)
        except JSONDecodeError:
            raise exceptions.ValidationError("Invalid JSON response from OpenAI")

        return Response(response_data)


# TODO i guess this becomes the query runner for our _internal_ use of RecordingsQuery
def list_recordings_from_query(
    query: RecordingsQuery, user: User | None, team: Team
) -> tuple[list[SessionRecording], bool, dict]:
    """
    As we can store recordings in S3 or in Clickhouse we need to do a few things here

    A. If filter.session_ids is specified:
      1. We first try to load them directly from Postgres if they have been persisted to S3 (they might have fell out of CH)
      2. Any that couldn't be found are then loaded from Clickhouse
    B. Otherwise we just load all values from Clickhouse
      2. Once loaded we convert them to SessionRecording objects in case we have any other persisted data

      In the context of an API call we'll always have user, but from Celery we might be processing arbitrary filters for a team and there won't be a user
    """
    all_session_ids = query.session_ids

    recordings: list[SessionRecording] = []
    more_recordings_available = False
    hogql_timings: list[QueryTiming] | None = None

    timer = ServerTimingsGathered()

    if all_session_ids:
        with timer("load_persisted_recordings"):
            # If we specify the session ids (like from pinned recordings) we can optimise by only going to Postgres
            sorted_session_ids = sorted(all_session_ids)

            persisted_recordings_queryset = SessionRecording.objects.filter(
                team=team, session_id__in=sorted_session_ids
            ).exclude(object_storage_path=None)

            persisted_recordings = persisted_recordings_queryset.all()

            recordings = recordings + list(persisted_recordings)

            remaining_session_ids = list(set(all_session_ids) - {x.session_id for x in persisted_recordings})
            query.session_ids = remaining_session_ids

    if (all_session_ids and query.session_ids) or not all_session_ids:
        modifiers = safely_read_modifiers_overrides(str(user.distinct_id), team) if user else None

        with timer("load_recordings_from_hogql"):
            (ch_session_recordings, more_recordings_available, hogql_timings) = SessionRecordingListFromQuery(
                query=query, team=team, hogql_query_modifiers=modifiers
            ).run()

        with timer("build_recordings"):
            recordings_from_clickhouse = SessionRecording.get_or_build_from_clickhouse(team, ch_session_recordings)
            recordings = recordings + recordings_from_clickhouse

            recordings = [x for x in recordings if not x.deleted]

            # If we have specified session_ids we need to sort them by the order they were specified
            if all_session_ids:
                recordings = sorted(
                    recordings,
                    key=lambda x: cast(list[str], all_session_ids).index(x.session_id),
                )

    if user and not user.is_authenticated:  # for mypy
        raise exceptions.NotAuthenticated()

    recording_ids_in_list: list[str] = [str(r.session_id) for r in recordings]
    # Update the viewed status for all loaded recordings
    with timer("load_viewed_recordings"):
        viewed_session_recordings = current_user_viewed(recording_ids_in_list, user, team)

    with timer("load_other_viewers_by_recording"):
        other_viewers = _other_users_viewed(recording_ids_in_list, user, team)

    with timer("load_persons"):
        # Get the related persons for all the recordings
        distinct_ids = sorted([x.distinct_id for x in recordings])
        person_distinct_ids = PersonDistinctId.objects.filter(distinct_id__in=distinct_ids, team=team).select_related(
            "person"
        )

    with timer("process_persons"):
        distinct_id_to_person = {}
        for person_distinct_id in person_distinct_ids:
            person_distinct_id.person._distinct_ids = [
                person_distinct_id.distinct_id
            ]  # Stop the person from loading all distinct ids
            distinct_id_to_person[person_distinct_id.distinct_id] = person_distinct_id.person

        for recording in recordings:
            recording.viewed = recording.session_id in viewed_session_recordings
            recording.viewers = other_viewers.get(recording.session_id, [])
            person = distinct_id_to_person.get(recording.distinct_id)
            if person:
                recording.person = person

    return recordings, more_recordings_available, _generate_timings(hogql_timings, timer)


def _other_users_viewed(recording_ids_in_list: list[str], user: User | None, team: Team) -> dict[str, list[str]]:
    if not user:
        return {}

    # we're looping in python
    # but since we limit the number of session recordings in the results set
    # it shouldn't be too bad
    other_viewers: dict[str, list[str]] = {str(x): [] for x in recording_ids_in_list}
    queryset = (
        SessionRecordingViewed.objects.filter(team=team, session_id__in=recording_ids_in_list)
        .exclude(user=user)
        .values_list("session_id", "user__email")
    )
    for session_id, user_email in queryset:
        other_viewers[session_id].append(str(user_email))

    return other_viewers


def current_user_viewed(recording_ids_in_list: list[str], user: User | None, team: Team) -> set[str]:
    if not user:
        return set()

    viewed_session_recordings = set(
        SessionRecordingViewed.objects.filter(team=team, user=user)
        .filter(session_id__in=recording_ids_in_list)
        .values_list("session_id", flat=True)
    )
    return viewed_session_recordings


def safely_read_modifiers_overrides(distinct_id: str, team: Team) -> HogQLQueryModifiers:
    modifiers = HogQLQueryModifiers()

    try:
        groups = {"organization": str(team.organization.id)}
        flag_key = "HOG_QL_ORG_QUERY_OVERRIDES"
        flags_n_bags = posthoganalytics.get_all_flags_and_payloads(
            distinct_id,
            groups=groups,
        )
        # this loads nothing whereas the payload is available
        # modifier_overrides = posthoganalytics.get_feature_flag_payload(
        #     flag_key,
        #     distinct_id,
        #     groups=groups,
        # )
        modifier_overrides = (flags_n_bags or {}).get("featureFlagPayloads", {}).get(flag_key, None)
        if modifier_overrides:
            modifiers.optimizeJoinedFilters = json.loads(modifier_overrides).get("optimizeJoinedFilters", None)
    except:
        # be extra safe
        pass

    return modifiers


def _generate_timings(hogql_timings: list[QueryTiming] | None, timer: ServerTimingsGathered) -> dict[str, float]:
    timings_dict = timer.get_all_timings()
    hogql_timings_dict = {}
    for key, value in hogql_timings or {}:
        new_key = f"hogql_{key[1].lstrip('./').replace('/', '_')}"
        # HogQL query timings are in seconds, convert to milliseconds
        hogql_timings_dict[new_key] = value[1] * 1000
    all_timings = {**timings_dict, **hogql_timings_dict}
    return all_timings


def _get_openai_client() -> OpenAI:
    """Get configured OpenAI client or raise appropriate error."""
    if not settings.DEBUG and not is_cloud():
        raise exceptions.ValidationError("AI features are only available in PostHog Cloud")

    if not os.environ.get("OPENAI_API_KEY"):
        raise exceptions.ValidationError("OpenAI API key is not configured")

    client = posthoganalytics.default_client
    if not client:
        raise exceptions.ValidationError("PostHog analytics client is not configured")

    return OpenAI(posthog_client=client)


def create_openai_messages(system_content: str, user_content: str) -> list[ChatCompletionMessageParam]:
    """Helper function to create properly typed OpenAI messages."""
    return [
        ChatCompletionSystemMessageParam(role="system", content=system_content),
        ChatCompletionUserMessageParam(role="user", content=user_content),
    ]
