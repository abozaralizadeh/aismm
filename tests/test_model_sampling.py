"""Sampling parameters, and the models that refuse them.

Reported live: repointing AZURE_OPENAI_MODEL at `gpt-5.6-luna` made every run
fail with

    400 Unsupported parameter: 'temperature' is not supported with this model.

Reasoning-family models do their own sampling internally and REJECT the
parameter rather than ignoring it, so this is not a tuning question — it is the
difference between the app working and not working. Every agent therefore builds
its ModelSettings through `agent_model_settings`, so the rule lives in one place.
"""
import dataclasses

import pytest

from aismm import config as config_module
from aismm import llm


@pytest.fixture()
def model(monkeypatch):
    """Point settings at a named model, with no operator override."""
    def _use(name, *, override=None):
        patched = dataclasses.replace(
            config_module.settings,
            llm=dataclasses.replace(config_module.settings.llm, model=name,
                                    supports_temperature=override))
        monkeypatch.setattr(llm, "settings", patched)
        return patched
    return _use


# --- which models refuse it ------------------------------------------------------------ #

@pytest.mark.parametrize("name", ["gpt-5.6-luna", "gpt-5", "gpt-5-mini", "o1", "o1-preview",
                                  "o3", "o3-mini", "o4-mini"])
def test_reasoning_models_get_no_temperature(model, name):
    model(name)
    assert llm.supports_sampling(name) is False
    # None is what the SDK turns into `omit`, so the key never reaches the wire.
    assert llm.agent_model_settings(temperature=0.8).temperature is None


@pytest.mark.parametrize("name", ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo"])
def test_ordinary_models_keep_theirs(model, name):
    """gpt-4o must not be caught by the o-series pattern."""
    model(name)
    assert llm.supports_sampling(name) is True
    assert llm.agent_model_settings(temperature=0.8).temperature == 0.8


def test_other_settings_are_untouched_either_way(model):
    """Only the sampling knobs are conditional; max_tokens is not."""
    model("gpt-5.6-luna")
    settings = llm.agent_model_settings(temperature=0.5, max_tokens=400)
    assert settings.max_tokens == 400
    assert settings.temperature is None


def test_no_temperature_asked_for_means_nothing_to_strip(model):
    model("gpt-5.6-luna")
    assert llm.agent_model_settings(max_tokens=10).max_tokens == 10


# --- the operator override ------------------------------------------------------------- #
# On Azure the model name is the DEPLOYMENT name, chosen by the operator, so the
# guess above cannot always be right.

def test_an_operator_can_force_it_off_for_a_deployment_with_a_plain_name(model):
    """A gpt-5 deployment called "main" is invisible to the pattern."""
    model("main", override=False)
    assert llm.supports_sampling("main") is False
    assert llm.agent_model_settings(temperature=0.8).temperature is None


def test_an_operator_can_force_it_on(model):
    """A future model whose name matches the pattern but accepts sampling."""
    model("gpt-6-classic", override=True)
    assert llm.supports_sampling("gpt-6-classic") is True
    assert llm.agent_model_settings(temperature=0.8).temperature == 0.8


def test_unset_means_decide_for_me_not_false(monkeypatch):
    """A tri-state flag: the absence of the env var must not read as "off"."""
    monkeypatch.delenv("LLM_SUPPORTS_TEMPERATURE", raising=False)
    assert config_module._opt_bool("LLM_SUPPORTS_TEMPERATURE") is None
    monkeypatch.setenv("LLM_SUPPORTS_TEMPERATURE", "0")
    assert config_module._opt_bool("LLM_SUPPORTS_TEMPERATURE") is False
    monkeypatch.setenv("LLM_SUPPORTS_TEMPERATURE", "1")
    assert config_module._opt_bool("LLM_SUPPORTS_TEMPERATURE") is True


# --- no agent may bypass it ------------------------------------------------------------ #

def test_no_agent_constructs_model_settings_directly():
    """One forgotten call site is a 400 on every run of that agent."""
    from pathlib import Path

    offenders = []
    for path in Path("aismm").rglob("*.py"):
        if path.name == "llm.py":
            continue
        if "ModelSettings(" in path.read_text():
            offenders.append(str(path))
    assert offenders == []


def test_every_agent_still_gets_its_settings():
    """Routing them through the helper must not have dropped them."""
    from aismm.agent import manager_agent, memory, vision

    for module in (manager_agent, memory, vision):
        assert "agent_model_settings(" in module.__loader__.get_source(module.__name__)
