"""What the November failures on pocvm taught: each kind of failure gets its own answer.

* image SAFETY refusals are about the prompt: advice, and never the circuit breaker;
* image gateway TIMEOUTS are retried once at medium quality, not three times as-is;
* Instagram account insights ask for time-series-only metrics the right way;
* a transient Graph 500 on container creation is retried once;
* a reply cut off by the model provider's content filter is retried once, calmly.
"""
import asyncio
import base64
import dataclasses

import httpx
import pytest
from agents.exceptions import ModelBehaviorError

from aismm import config as config_module
from aismm.config import ImageSettings
from aismm.tools import image_tool


# --- images -------------------------------------------------------------------------- #

class _Status(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status_code = status


SAFETY = _Status(400, "Error code: 400 - {'error': {'message': 'Your request was rejected "
                      "by the safety system.', 'code': 'moderation_blocked'}}")
GATEWAY_TIMEOUT = _Status(500, "<html><title>500 - The request timed out.</title></html>")


def _response():
    class Datum:
        b64_json = base64.b64encode(b"image-bytes").decode()

    class Response:
        data = [Datum()]

    return Response()


@pytest.fixture()
def images(monkeypatch, tmp_path):
    """A scripted image client: each call pops the next outcome (an exception or OK)."""
    script, calls = [], []

    class FakeImages:
        async def generate(self, **kwargs):
            calls.append(kwargs)
            outcome = script.pop(0) if script else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            return _response()

    class FakeClient:
        images = FakeImages()

    monkeypatch.setattr(image_tool, "_client_for", lambda img: FakeClient())
    monkeypatch.setattr(image_tool, "save_bytes", lambda data, ext: str(tmp_path / f"o.{ext}"))
    monkeypatch.setattr(image_tool, "public_url", lambda p: f"https://host/{p}")
    monkeypatch.setattr(image_tool, "_RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(image_tool, "settings", dataclasses.replace(
        config_module.settings, image=ImageSettings(api_key="k", endpoint="https://e",
                                                    model="gpt-image-2.5-sunburst")))
    return script, calls


def _generate(state, **kwargs):
    return asyncio.run(image_tool.perform_generate_image(state, "a scene", **kwargs))


def test_failures_are_told_apart():
    assert image_tool.failure_kind(SAFETY) == "safety"
    assert image_tool.failure_kind(GATEWAY_TIMEOUT) == "timeout"
    assert image_tool.failure_kind(_Status(503, "Service Unavailable")) == "transient"
    assert image_tool.failure_kind(_Status(400, "invalid size")) == "other"


def test_a_safety_refusal_returns_advice_and_spares_the_circuit_breaker(images):
    script, calls = images
    script.append(SAFETY)
    state = {}
    result = _generate(state, quality="medium")
    assert result["error"] == "image_safety_rejected"
    assert "child" in result["advice"] and "metaphor" in result["advice"]
    assert len(calls) == 1                           # the same prompt is not resent
    assert state.get("image_failures", 0) == 0
    assert state["image_safety_refusals"] == 1


def test_a_gateway_timeout_is_retried_once_at_medium(images):
    script, calls = images
    script.append(GATEWAY_TIMEOUT)
    state = {}
    result = _generate(state, quality="high", size="1024x1280")
    assert result["asset_path"]
    assert [c.get("quality") for c in calls] == ["high", "medium"]
    assert "timed out" in result["adjustment"]
    assert state.get("image_failures", 0) == 0


def test_a_second_timeout_counts_as_one_failure(images):
    script, calls = images
    script.extend([GATEWAY_TIMEOUT, GATEWAY_TIMEOUT])
    state = {}
    result = _generate(state, quality="high")
    assert result["error"] == "image_generation_failed" and result["kind"] == "timeout"
    assert len(calls) == 2 and state["image_failures"] == 1


def test_a_transient_error_is_retried_once_as_is(images):
    script, calls = images
    script.append(_Status(503, "Service Unavailable"))
    assert _generate({}, quality="medium")["asset_path"]
    assert [c.get("quality") for c in calls] == ["medium", "medium"]


def test_a_bad_request_is_not_retried(images):
    script, calls = images
    script.append(_Status(400, "invalid parameter"))
    assert _generate({})["error"] == "image_generation_failed"
    assert len(calls) == 1


def test_the_sdk_does_not_retry_on_its_own():
    client = image_tool._client_for(ImageSettings(api_key="k", endpoint="https://e",
                                                  model="gpt-image-2"))
    assert client.max_retries == 0


def test_at_most_two_images_render_at_once(images, monkeypatch):
    running, peak = 0, 0

    class SlowImages:
        async def generate(self, **kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return _response()

    class SlowClient:
        images = SlowImages()

    monkeypatch.setattr(image_tool, "_client_for", lambda img: SlowClient())

    async def three():
        state = {}
        await asyncio.gather(*(image_tool.perform_generate_image(state, "p")
                               for _ in range(3)))

    asyncio.run(three())
    assert peak == image_tool.MAX_PARALLEL_IMAGES == 2


def test_the_tool_stops_offering_photos_after_repeated_refusals():
    from aismm.tools.image_tool import MAX_SAFETY_REFUSALS, _make_generate_image

    state = {"image_settings": ImageSettings(api_key="k", endpoint="https://e",
                                             model="gpt-image-2"),
             "image_safety_refusals": MAX_SAFETY_REFUSALS}
    from agents.tool_context import ToolContext

    tool = _make_generate_image(state)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="1",
                      tool_arguments='{"prompt": "x"}')
    result = asyncio.run(tool.on_invoke_tool(ctx, '{"prompt": "x"}'))
    assert "image_safety_limit" in str(result)


# --- Instagram account insights -------------------------------------------------------- #

def _instagram():
    from aismm.models import PlatformName
    from aismm.platforms.registry import get_platform

    return get_platform(PlatformName.instagram)


def test_follower_count_is_asked_for_as_a_time_series(monkeypatch):
    from aismm.models import Account, PlatformName

    calls = []

    async def fake_get(self, token, path, params):
        calls.append(dict(params))
        name = params["metric"]
        if "metric_type" in params:
            return {"data": [{"name": name, "total_value": {"value": 10}}]}
        return {"data": [{"name": name, "values": [{"value": 1}, {"value": 3}]}]}

    ig = _instagram()
    monkeypatch.setattr(type(ig), "_graph_get", fake_get)
    rows = asyncio.run(ig.account_insights("t", Account(platform=PlatformName.instagram,
                                                        external_id="9")))
    assert {r["name"] for r in rows} == {"reach", "follower_count"}
    assert calls[0] == {"metric": "reach", "period": "day", "metric_type": "total_value"}
    assert calls[1] == {"metric": "follower_count", "period": "day"}


def test_a_metric_graph_newly_refuses_as_total_is_moved_not_fatal(monkeypatch):
    from aismm.models import Account, PlatformName

    calls = []

    async def fake_get(self, token, path, params):
        calls.append(dict(params))
        if "metric_type" in params and "online_followers" in params["metric"]:
            raise RuntimeError("Instagram Graph 400: (#100) The following metric "
                               "(online_followers) is incompatible with the metric type "
                               "(total_value) [code=100]")
        return {"data": [{"name": n} for n in params["metric"].split(",")]}

    ig = _instagram()
    monkeypatch.setattr(type(ig), "_graph_get", fake_get)
    rows = asyncio.run(ig.account_insights(
        "t", Account(platform=PlatformName.instagram, external_id="9"),
        metrics="reach,online_followers"))
    assert {r["name"] for r in rows} == {"reach", "online_followers"}
    assert calls[-1] == {"metric": "online_followers", "period": "day"}


# --- Instagram container creation ------------------------------------------------------ #

def _graph_response(status, body):
    return httpx.Response(status, json=body,
                          request=httpx.Request("POST", "https://graph.facebook.com/x/media"))


class _Client:
    def __init__(self, *responses):
        self.responses, self.posts = list(responses), 0

    async def post(self, url, data=None, headers=None):
        self.posts += 1
        return self.responses.pop(0)


@pytest.fixture()
def no_pause(monkeypatch):
    from aismm.platforms import instagram

    monkeypatch.setattr(instagram, "CONTAINER_RETRY_PAUSE", 0)


def test_a_transient_graph_500_on_container_create_is_retried(no_pause):
    client = _Client(_graph_response(500, {"error": {"message": "An unknown error occurred",
                                                     "code": 1, "error_subcode": 99}}),
                     _graph_response(200, {"id": "c1"}))
    assert asyncio.run(_instagram()._create_container(client, "9", "t", {})) == "c1"
    assert client.posts == 2


def test_a_real_error_on_container_create_is_not_retried(no_pause):
    client = _Client(_graph_response(400, {"error": {"message": "The caption was too long.",
                                                     "code": 36004}}))
    with pytest.raises(RuntimeError, match="too long"):
        asyncio.run(_instagram()._create_container(client, "9", "t", {}))
    assert client.posts == 1


def test_a_second_transient_failure_is_raised(no_pause):
    failure = {"error": {"message": "An unknown error occurred", "code": 1}}
    client = _Client(_graph_response(500, failure), _graph_response(500, failure))
    with pytest.raises(RuntimeError, match="unknown error"):
        asyncio.run(_instagram()._create_container(client, "9", "t", {}))
    assert client.posts == 2


# --- the model provider's content filter ----------------------------------------------- #

FILTERED = ModelBehaviorError("Responses stream ended with terminal event "
                              "`response.incomplete`. status=incomplete; incomplete_details="
                              "IncompleteDetails(reason='content_filter').")


def test_content_filter_is_recognised():
    from aismm.agent.manager_agent import content_filtered

    assert content_filtered(FILTERED)
    assert not content_filtered(ModelBehaviorError("something else"))
    assert not content_filtered(RuntimeError("content_filter"))


def test_the_retry_reuses_media_already_made():
    from aismm.agent.manager_agent import CONTENT_FILTER_NOTE, _content_filter_retry_input

    state = {"assets": [{"path": "/a/1.jpg", "kind": "image"}]}
    text = _content_filter_retry_input("kickoff", state)
    assert text.startswith("kickoff") and CONTENT_FILTER_NOTE in text and "/a/1.jpg" in text
    as_list = _content_filter_retry_input([{"role": "user", "content": "k"}], state)
    assert as_list[-1]["role"] == "user" and "/a/1.jpg" in as_list[-1]["content"]


def _agent_run(store, monkeypatch, outcomes):
    """run_for_account with a scripted Runner: each run pops an exception or a result."""
    from aismm.agent import manager_agent
    from aismm.models import Account, Instruction, PlatformName, Run, RunStatus

    inputs = []

    class FakeResult:
        def to_input_list(self):
            return []

    class FakeRunner:
        @staticmethod
        async def run(agent, agent_input, **kwargs):
            inputs.append(agent_input)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "publish":
                state = manager_agent._LAST_STATE
                state["result"] = {"mode": "dry_run"}
            return FakeResult()

    real_build_tools = manager_agent.build_tools

    def capture_state(state, enabled):
        manager_agent._LAST_STATE = state
        return real_build_tools(state, enabled)

    async def no_compact(*args, **kwargs):
        return None

    monkeypatch.setattr(manager_agent, "Runner", FakeRunner)
    monkeypatch.setattr(manager_agent, "build_tools", capture_state)
    monkeypatch.setattr(manager_agent, "_resolve_model", lambda instruction, store: "fake-model")
    monkeypatch.setattr(manager_agent, "maybe_compact", no_compact)
    account = store.upsert_account(Account(platform=PlatformName.instagram, handle="s"),
                                   access_token="t")
    instruction = store.upsert_instruction(Instruction(name="Psych", brief="b"))
    run = store.add_run(Run(instruction_id=instruction.id, account_id=account.id,
                            status=RunStatus.running))
    result = asyncio.run(manager_agent.run_for_account(account, instruction, store, run))
    return result, inputs, run


def test_a_filtered_run_is_retried_once_and_can_still_finish(store, monkeypatch):
    from aismm.agent.manager_agent import CONTENT_FILTER_NOTE

    result, inputs, run = _agent_run(store, monkeypatch, [FILTERED, "publish"])
    assert result == {"mode": "dry_run"}
    assert len(inputs) == 2 and CONTENT_FILTER_NOTE in str(inputs[1])
    assert "content filter stopped the first attempt" in run.log


def test_filtered_twice_fails_with_a_message_that_says_so(store, monkeypatch):
    from aismm.agent.manager_agent import ContentFiltered

    with pytest.raises(ContentFiltered, match="content filter"):
        _agent_run(store, monkeypatch, [FILTERED, FILTERED])
