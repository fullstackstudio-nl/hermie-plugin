"""The capability negotiation, from both sides.

These are the tests that matter for a plugin nobody updates: an app newer than
the plugin must degrade, and an app older than the plugin must ignore what it
does not know.
"""

import yaml
from pathlib import Path

from hermie_plugin import contract

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_version_matches_the_code():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["version"] == contract.PLUGIN_VERSION
    assert manifest["name"] == "hermie"


def test_advert_carries_only_what_was_offered():
    advert = contract.advert(modules={"push": "on"}, capabilities=["push.expo", "push.expo"], now=1000)
    assert advert["capabilities"] == ["push.expo"]
    assert advert["v"] == contract.CONTRACT_VERSION
    assert advert["updatedAt"] == 1000


def test_a_newer_contract_reads_as_nothing():
    """An app must not guess at a shape it does not know."""
    future = {"v": contract.CONTRACT_VERSION + 1, "capabilities": ["push.telepathy"]}
    assert contract.read_capabilities(future) == []


def test_an_older_plugin_simply_offers_less():
    """The app tests for a string; it never compares version numbers."""
    old = contract.advert(modules={"push": "on"}, capabilities=[contract.CAP_PUSH_EXPO])
    caps = contract.read_capabilities(old)
    assert contract.CAP_PUSH_EXPO in caps
    assert contract.CAP_PUSH_WEBPUSH not in caps


def test_absent_advert_is_absent_plugin():
    assert contract.read_capabilities(None) == []
    assert contract.read_capabilities({}) == []


def test_planned_modules_are_advertised_but_not_claimed():
    advert = contract.advert(
        modules={"push": "on", "presence": "planned"}, capabilities=[contract.CAP_PUSH_EXPO]
    )
    assert advert["modules"]["presence"] == "planned"
    assert "presence" not in " ".join(advert["capabilities"])
