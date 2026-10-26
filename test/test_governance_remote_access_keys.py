"""Remote-access governance keys: ``bind_address``, ``dashboard_origins``, the
``tailnets`` list on ``capabilities.tailnet_origin``, and ``computer_use``.

RFC ``rfc-crew-enterprise-edition``, appendix rows "Bind address and tunnel",
"Dashboard origins", "Allowed tailnets" and "Desktop control". In an enterprise
build each is at its minimal set until a policy names it (D3). In an ``oss`` build
with no policy naming the key, behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from kiro_crew.computer_use import enable_state
from kiro_crew.dashboard import tailnet, urls
from kiro_crew.platform import governance_profiles
from kiro_crew.platform.governance import SCOPE_CATALOG, compose_tier_ladder, parse_policy
from kiro_crew.platform.governance_parts.tiers import TIER_CENTRAL, TIER_HOME

_SUFFIX = "tail1234.ts.net"
_NAME = f"desk.{_SUFFIX}"
_STATUS = {"Self": {"DNSName": f"{_NAME}."}, "CurrentTailnet": {"MagicDNSSuffix": _SUFFIX}}


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture
def install_policy(monkeypatch: pytest.MonkeyPatch):
    """Install a composed context carrying *doc* (or no policy) and no profile."""

    def _install(doc: "dict | None", local: "dict | None" = None) -> None:
        """*doc* as the central tier and *local* as the home tier, folded for real."""
        from kiro_crew.platform import context as pc

        tiers = []
        for body, tier in ((doc, TIER_CENTRAL), (local, TIER_HOME)):
            if body is not None:
                parsed = parse_policy({"version": 1, "boot": {}, **body})
                tiers.append(dataclasses.replace(parsed, tier=tier))
        ceiling = compose_tier_ladder(*tiers) if tiers else None
        ctx = SimpleNamespace(governance=ceiling)
        monkeypatch.setattr(pc, "current_context", lambda: ctx)
        monkeypatch.setattr(pc, "installed_context", lambda: ctx)
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: None)

    _install(None)
    return _install


def _allow(*items: str) -> dict:
    return {"mode": "allow", "allow": list(items)}


def test_catalog_rows() -> None:
    assert SCOPE_CATALOG["bind_address"].matcher == "ip"
    assert SCOPE_CATALOG["dashboard_origins"].matcher == "origin"
    assert SCOPE_CATALOG["capabilities.computer_use"].kind == "capability"
    assert SCOPE_CATALOG["capabilities.tailnet_origin"].scope_matchers == {"tailnets": "host"}


# ── bind_address ────────────────────────────────────────────────────────


class TestBindAddress:
    def test_loopback_is_never_asked(self, enterprise, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_BIND", raising=False)
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits",
            lambda *a: pytest.fail("loopback must not be asked"),
        )
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"

    def test_oss_env_bind_still_applies(self, install_policy, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        assert urls.bind_address_for(local_only=False) == "0.0.0.0"

    def test_oss_policy_narrows_the_env_bind(self, install_policy, monkeypatch) -> None:
        install_policy({"bind_address": _allow("10.0.0.5")})
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"

    def test_enterprise_unnamed_binds_loopback(self, enterprise, install_policy, monkeypatch):
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        assert urls.bind_address_for(local_only=False) == "127.0.0.1"

    def test_enterprise_policy_widens(self, enterprise, install_policy, monkeypatch) -> None:
        install_policy({"bind_address": _allow("0.0.0.0", "::")})
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        install_policy({"bind_address": _allow("0:0:0:0:0:0:0:0")})
        monkeypatch.setenv("KIROCREW_BIND", "::")
        assert urls.bind_address_for(local_only=True) == "::"
        install_policy({"bind_address": _allow("0.0.0.0", "::")})
        # Asked as canonical IP text, so a long spelling matches the entry.
        monkeypatch.setenv("KIROCREW_BIND", "0:0:0:0:0:0:0:0")
        assert urls.bind_address_for(local_only=True) == "0:0:0:0:0:0:0:0"

    def test_the_bind_decision_is_audited(self, enterprise, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"
        assert [(r["scope"], r["item"], r["outcome"]) for r in records] == [
            ("bind_address", "0.0.0.0", "denied")
        ]

    def test_the_oss_shortcut_is_audited(self, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        install_policy({})
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"
        assert [(r["scope"], r["outcome"], r["layer"]) for r in records] == [
            ("bind_address", "allowed", "default")
        ]

    def test_a_profile_deny_does_not_narrow_the_bind(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"bind_address": _allow("0.0.0.0")})
        profile = governance_profiles.deny_all_profile("test")
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: profile)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "0.0.0.0"

    def test_a_governance_error_binds_loopback(self, install_policy, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("unreadable")

        install_policy({"bind_address": _allow("0.0.0.0")})
        monkeypatch.setattr("kiro_crew.platform.governance.resolve", _boom)
        monkeypatch.setenv("KIROCREW_BIND", "0.0.0.0")
        assert urls.bind_address_for(local_only=True) == "127.0.0.1"


# ── dashboard_origins ───────────────────────────────────────────────────

_FLOOR = {
    "http://127.0.0.1:5476",
    "http://localhost:5476",
    "http://[::1]:5476",
    "http://kirocrew.localhost:5476",
}


def _origins(monkeypatch, *, tailnet_host: str = "") -> set[str]:
    monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://other.example.com")
    monkeypatch.setenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", "8777")
    return urls.build_allowed_origins(
        5476,
        True,
        configured_host="crew.example.com",
        dashboard_url="https://crew.example.com",
        tailnet_host=tailnet_host,
    )


class TestDashboardOrigins:
    def test_oss_keeps_every_origin(self, install_policy, monkeypatch) -> None:
        got = _origins(monkeypatch)
        assert {"https://crew.example.com", "https://other.example.com"} <= got
        assert "http://localhost:8777" in got

    def test_no_extras_asks_nothing(self, enterprise, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KIROCREW_ALLOWED_LOOPBACK_PORTS", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(
            "kiro_crew.edition_locks.policy_permits",
            lambda *a: pytest.fail("the floor must not be asked"),
        )
        assert urls.build_allowed_origins(5476, True) == _FLOOR

    def test_enterprise_unnamed_keeps_this_machine_only(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        got = _origins(monkeypatch, tailnet_host=_NAME)
        assert got == _FLOOR | {f"https://{_NAME}"}
        assert urls.build_allowed_hosts(got) == {
            "localhost",
            "127.0.0.1",
            "::1",
            "kirocrew.localhost",
            _NAME,
        }

    def test_enterprise_policy_names_one(self, enterprise, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert _origins(monkeypatch) == _FLOOR | {"https://crew.example.com"}

    def test_an_ipv6_origin_matches_its_own_entry(self, install_policy, monkeypatch) -> None:
        v6 = "https://[2001:db8::1]:8443"
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", v6)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [v6]}})
        assert v6 not in urls.build_allowed_origins(5476, True)
        install_policy({"dashboard_origins": _allow(v6)})
        assert v6 in urls.build_allowed_origins(5476, True)

    @pytest.mark.parametrize(
        "spoof", ["https://evil.com/.example.com", "https://evil.com?.example.com"]
    )
    def test_a_glob_cannot_be_met_by_a_path(self, enterprise, install_policy, monkeypatch, spoof):
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", spoof)
        install_policy({"dashboard_origins": _allow("https://*.example.com")})
        got = urls.build_allowed_origins(5476, True)
        assert spoof not in got
        assert "evil.com" not in urls.build_allowed_hosts(got)

    def test_a_deny_judges_the_host_a_bad_port_would_trust(
        self, install_policy, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://evil.com:bad")
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["https://evil.com"]}})
        assert "https://evil.com:bad" not in urls.build_allowed_origins(5476, True)

    def test_a_deny_all_refuses_an_unusual_but_valid_host(
        self, install_policy, monkeypatch
    ) -> None:
        odd = "https://crew+test.example.com"
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", odd)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["*"]}})
        assert odd not in urls.build_allowed_origins(5476, True)

    def test_an_unmatchable_item_is_refused_while_governed(
        self, install_policy, monkeypatch
    ) -> None:
        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["https://x.example"]}})
        assert urls.origin_permitted("https://a b.example") is False
        records: list[dict] = []
        import kiro_crew.sel as sel_mod

        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        assert urls.origin_permitted("https://a b.example", audit_tool="t") is False
        assert [r["outcome"] for r in records] == ["denied"]
        install_policy(None)
        assert urls.origin_permitted("https://a b.example") is True

    @pytest.mark.parametrize("entry", ["https://evil.example.com/", "https://evil.example.com:443"])
    def test_a_deny_entry_in_another_spelling_still_denies(
        self, install_policy, monkeypatch, entry
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", "https://evil.example.com")
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [entry]}})
        assert "https://evil.example.com" not in urls.build_allowed_origins(5476, True)

    def test_port_80_loopback_stays_trusted(self, enterprise, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        req = _origin_request("http://localhost", "localhost")
        req.app = {"allowed_origins": urls.build_allowed_origins(80, True)}
        assert check_origin(req) is True

    @pytest.mark.parametrize(
        ("configured", "entry"),
        [
            ("https://[2001:db8::1]", "https://[2001:0db8:0:0:0:0:0:1]"),
            ("https://bücher.example", "https://xn--bcher-kva.example"),
            ("https://crew.example.com.", "https://crew.example.com"),
            ("HTTPS://Crew.Example.com:0443", "https://crew.example.com"),
        ],
    )
    def test_equal_spellings_meet_a_deny(self, install_policy, monkeypatch, configured, entry):
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", configured)
        install_policy({"dashboard_origins": {"mode": "deny", "deny": [entry]}})
        assert configured not in urls.build_allowed_origins(5476, True)

    def test_a_loopback_deny_in_long_form_refuses_the_fallback(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy(
            {"dashboard_origins": {"mode": "deny", "deny": ["http://[0:0:0:0:0:0:0:1]:8777"]}}
        )
        assert check_origin(_origin_request("http://[::1]:8777", "[::1]:8777")) is False

    def test_enterprise_refuses_a_deny_list_at_load(self, enterprise) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="dashboard_origins.*allow list"):
            parse_policy({"version": 1, "boot": {}, "dashboard_origins": {"mode": "deny"}})

    def test_oss_still_loads_a_deny_list(self) -> None:
        parse_policy({"version": 1, "boot": {}, "dashboard_origins": {"mode": "deny"}})

    @pytest.mark.parametrize(
        "spelling",
        ["https://Crew.Example.com", "https://crew.example.com.", "https://crew.example.com:443"],
    )
    def test_enterprise_allow_list_matches_canonical_spellings(
        self, enterprise, install_policy, monkeypatch, spelling
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", spelling)
        install_policy({"dashboard_origins": _allow("https://crew.example.com/")})
        assert spelling in urls.build_allowed_origins(5476, True)

    @pytest.mark.parametrize(
        "odd",
        [
            "https://crew.example.com.evil.com",
            "https://crew+x.example.com",
            "https://[2001:db8::1]",
            "https://evil.com/crew.example.com",
        ],
    )
    def test_enterprise_odd_spellings_outside_the_list_are_refused(
        self, enterprise, install_policy, monkeypatch, odd
    ) -> None:
        monkeypatch.setenv("KIROCREW_CORS_ORIGINS", odd)
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        got = urls.build_allowed_origins(5476, True)
        assert odd not in got
        assert got == _FLOOR

    def test_the_origin_matcher_refuses_a_non_origin(self) -> None:
        from kiro_crew.platform.governance_parts.matching import _match_origin

        assert _match_origin("https://a.example.com", "https://*.example.com")
        assert not _match_origin("https://evil.com/.example.com", "https://*.example.com")
        assert not _match_origin("https://u@a.example.com", "https://*")

    def test_oss_policy_narrows(self, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://crew.example.com")})
        assert "https://other.example.com" not in _origins(monkeypatch)


# ── the same-origin loopback fallback ───────────────────────────────────


def _origin_request(origin: str, host: str):
    from unittest.mock import MagicMock

    req = MagicMock()
    req.app = {"allowed_origins": set(_FLOOR)}
    req.headers = {"Origin": origin, "Host": host}
    req.remote = "127.0.0.1"
    return req


class TestLoopbackFallback:
    _FWD = ("http://localhost:8777", "localhost:8777")

    def test_oss_still_accepts_a_forwarded_loopback_port(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        assert check_origin(_origin_request(*self._FWD)) is True

    def test_a_policy_refusing_extras_refuses_it(self, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({"dashboard_origins": {"mode": "deny", "deny": ["*"]}})
        assert check_origin(_origin_request(*self._FWD)) is False
        # The bound port is the floor and stays trusted.
        assert check_origin(_origin_request("http://localhost:5476", "localhost:5476"))

    def test_the_bound_port_ipv6_loopback_stays_trusted(self, enterprise, install_policy):
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        assert check_origin(_origin_request("http://[::1]:5476", "[::1]:5476")) is True

    def test_a_long_form_bound_port_ipv6_stays_trusted(self, enterprise, install_policy):
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        req = _origin_request("http://[0:0:0:0:0:0:0:1]:5476", "[0:0:0:0:0:0:0:1]:5476")
        assert check_origin(req) is True
        req = _origin_request("http://[0:0:0:0:0:0:0:1]:8777", "[0:0:0:0:0:0:0:1]:8777")
        assert check_origin(req) is False

    def test_each_request_decision_is_audited(self, install_policy, monkeypatch) -> None:
        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.origin import check_origin

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        install_policy({"dashboard_origins": _allow("http://localhost:8777")})
        assert check_origin(_origin_request(*self._FWD)) is True
        assert [(r["item"], r["outcome"]) for r in records] == [
            ("http://localhost:8777", "allowed")
        ]

    def test_enterprise_unnamed_refuses_it(self, enterprise, install_policy) -> None:
        from kiro_crew.dashboard.origin import check_origin

        install_policy({})
        assert check_origin(_origin_request(*self._FWD)) is False


# ── the tunnel origin ───────────────────────────────────────────────────

_TUNNEL_URL = "https://crew-1.tunnel.example"


def _connect_tunnel(monkeypatch) -> set[str]:
    from kiro_crew.tunnel import setup as tunnel_setup

    hooks: dict = {}

    class _FakeManager:
        def __init__(self, port, **kwargs):
            hooks.update(kwargs)

        async def start(self) -> None:
            await hooks["on_connect"](_TUNNEL_URL)

    monkeypatch.setattr(tunnel_setup, "TunnelManager", _FakeManager)
    monkeypatch.setattr(tunnel_setup, "publish_disabled", lambda: False)
    monkeypatch.setattr(tunnel_setup, "set_tunnel_url", lambda url: None)
    origins = set(_FLOOR)
    auth_mw = SimpleNamespace(_is_token_auth=True)
    asyncio.run(
        tunnel_setup.setup_tunnel(
            middlewares=[auth_mw],
            allowed_origins=origins,
            tunnel_name_mode="auto",
            tunnel_name_override="",
            port=5476,
            log_api_access=lambda **kw: None,
        )
    )
    return origins


class TestTunnelOrigin:
    def test_oss_trusts_the_tunnel(self, install_policy, monkeypatch) -> None:
        assert _TUNNEL_URL in _connect_tunnel(monkeypatch)

    def test_enterprise_unnamed_does_not_trust_it(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({})
        assert _connect_tunnel(monkeypatch) == _FLOOR

    def test_oss_policy_can_refuse_it(self, install_policy, monkeypatch) -> None:
        install_policy({"dashboard_origins": _allow("https://other.example.com")})
        assert _connect_tunnel(monkeypatch) == _FLOOR


# ── tailnets ────────────────────────────────────────────────────────────


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailnet, "_run_json", lambda args: _STATUS)


def _tailnet_policy(body: dict) -> dict:
    return {"capabilities": {"tailnet_origin": body}}


class TestTailnets:
    def test_oss_without_a_list_derives_the_name(self, install_policy, daemon) -> None:
        assert tailnet.self_dns_name() == _NAME
        install_policy(_tailnet_policy({"enabled": True}))
        assert tailnet.self_dns_name() == _NAME

    def test_oss_list_refuses_another_tailnet(self, install_policy, daemon) -> None:
        install_policy(_tailnet_policy({"scopes": {"tailnets": _allow("other.ts.net")}}))
        assert tailnet.self_dns_name() is None

    def test_enterprise_unnamed_derives_nothing(self, enterprise, install_policy, daemon):
        assert tailnet.self_dns_name() is None
        # On, but the list unnamed: the list keeps its minimal set (none).
        install_policy(_tailnet_policy({"enabled": True}))
        assert tailnet.self_dns_name() is None

    def test_enterprise_local_tier_cannot_supply_the_list(
        self, enterprise, install_policy, daemon
    ) -> None:
        local = _tailnet_policy({"scopes": {"tailnets": _allow("*.ts.net")}})
        install_policy(_tailnet_policy({"enabled": True}), local)
        assert tailnet.self_dns_name() is None
        # With no central document the local tier only narrows the baseline (off).
        install_policy(None, local)
        assert tailnet.self_dns_name() is None

    def test_enterprise_named_tailnet(self, enterprise, install_policy, daemon) -> None:
        install_policy(_tailnet_policy({"scopes": {"tailnets": _allow("*.ts.net")}}))
        assert tailnet.self_dns_name() == _NAME
        install_policy(_tailnet_policy({"scopes": {"tailnets": _allow("other.ts.net")}}))
        assert tailnet.self_dns_name() is None

    def test_a_refusal_names_the_policy_not_magicdns(
        self, enterprise, install_policy, daemon, monkeypatch, caplog
    ) -> None:
        from kiro_crew.dashboard.handlers import tailnet_mobile

        monkeypatch.setattr(tailnet, "_cli_path", lambda: "/usr/bin/tailscale")
        monkeypatch.setattr(tailnet, "_run_json_detail", lambda args: (_STATUS, False))
        install_policy(_tailnet_policy({"enabled": True}))
        probe = tailnet.probe_daemon()
        assert probe.governance_refused is True
        assert "administrator" in probe.detail and "MagicDNS" not in probe.detail
        step = tailnet_mobile._derive_step(
            pinned=False,
            probe=probe,
            trusted=False,
            startup_host="",
            published=None,
            port_free=None,
        )
        assert step == "pinned"
        with caplog.at_level("WARNING"):
            assert asyncio.run(tailnet.resolve_tailnet_host(True)) == ""
        assert "tailscale status" not in caplog.text
        assert "not permitted by governance" in caplog.text

    def test_cli_up_refuses_before_publishing(
        self, enterprise, install_policy, daemon, monkeypatch, tmp_path, capsys
    ) -> None:
        import argparse
        import json

        from kiro_crew import cli_commands
        from kiro_crew.dashboard import tailnet_serve

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"dashboard": {"tailscale": {"enabled": True}}}))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(cli_commands, "_marker_port", lambda: 5476)
        monkeypatch.setattr(tailnet, "is_governance_pinned_off", lambda *a, **k: False)
        monkeypatch.setattr(
            tailnet_serve, "publish", lambda *a, **k: pytest.fail("must not publish")
        )
        import kiro_crew.sel as sel_mod

        records: list[dict] = []
        fake = SimpleNamespace(log_governance_decision=lambda **kw: records.append(kw))
        monkeypatch.setattr(sel_mod, "sel", lambda: fake)
        install_policy(_tailnet_policy({"enabled": True}))
        with pytest.raises(SystemExit) as exc:
            cli_commands._tailnet(argparse.Namespace(tailnet_action="up"))
        assert exc.value.code == 1
        assert [(r["tool_name"], r["outcome"]) for r in records] == [("tailnet_cli_up", "denied")]
        assert "does not permit this tailnet" in capsys.readouterr().err

    def test_unreadable_ceiling_derives_nothing(self, enterprise, monkeypatch, daemon) -> None:
        from kiro_crew.platform import context as pc

        def _boom():
            raise RuntimeError("no context")

        monkeypatch.setattr(pc, "current_context", _boom)
        assert tailnet.self_dns_name() is None


# ── computer_use ────────────────────────────────────────────────────────

_ON = {"enabled": True}


class TestComputerUse:
    def test_oss_opt_in_still_applies(self, install_policy, monkeypatch) -> None:
        # No landed policy names the key: nothing is evaluated at all.
        monkeypatch.setattr(
            governance_profiles,
            "governance_permits",
            lambda *a, **k: pytest.fail("oss must not evaluate an unnamed key"),
        )
        assert enable_state.is_enabled(_ON) is True
        assert enable_state.locked_by_policy() is False
        assert enable_state.is_enabled({}) is False

    def test_oss_policy_pins_it_off(self, install_policy) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": False}}})
        assert enable_state.is_enabled(_ON) is False
        assert enable_state.locked_by_policy() is True

    def test_enterprise_unnamed_is_off(self, enterprise, install_policy) -> None:
        assert enable_state.is_enabled(_ON) is False
        install_policy({})
        assert enable_state.is_enabled(_ON) is False
        assert enable_state.locked_by_policy() is True

    def test_enterprise_policy_turns_it_on(self, enterprise, install_policy) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": True}}})
        assert enable_state.is_enabled(_ON) is True
        # The operator's own opt-in still has to say yes.
        assert enable_state.is_enabled({}) is False

    def test_a_profile_deny_is_not_a_policy_lock(
        self, enterprise, install_policy, monkeypatch
    ) -> None:
        install_policy({"capabilities": {"computer_use": {"enabled": True}}})
        profile = governance_profiles.deny_all_profile("test")
        monkeypatch.setattr(governance_profiles, "resolve_active_scope", lambda *a, **k: profile)
        assert enable_state.is_enabled(_ON) is True
