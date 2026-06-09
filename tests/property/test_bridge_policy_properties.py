"""Property-based invariants for pure bridge-policy value spaces."""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from src.bridge_policy import (
    BridgePolicySnapshot,
    EffectivePolicyEntry,
    FederationPolicyReason,
    PolicyType,
    normalize_instance_subject,
    normalize_snowflake,
)


_dns_label = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=8,
)
_valid_host = st.lists(_dns_label, min_size=2, max_size=3).map(".".join)
_snowflake = st.integers(min_value=0, max_value=10**18).map(str)


def _entry(policy_type: PolicyType, subject: str) -> EffectivePolicyEntry:
    """Build one immutable effective entry for a pure snapshot property."""

    return EffectivePolicyEntry(policy_type, subject, "dynamic")


@given(host=_valid_host, also_allowed=st.booleans())
def test_blocked_domain_is_denied_regardless_of_allow_membership(
    host: str,
    also_allowed: bool,
) -> None:
    """Block precedence is absolute for every generated valid host."""

    entries = [_entry(PolicyType.FEDERATION_BLOCK, host)]
    if also_allowed:
        entries.append(_entry(PolicyType.FEDERATION_ALLOW, host))

    decision = BridgePolicySnapshot(tuple(entries)).federation_decision(host)

    assert decision.allowed is False
    assert decision.reason is FederationPolicyReason.BLOCKLISTED
    assert decision.host == host


@given(target=_valid_host, unrelated=_valid_host)
def test_unrelated_block_entry_does_not_change_open_target_decision(
    target: str,
    unrelated: str,
) -> None:
    """A distinct blocked host cannot affect an otherwise open target."""

    assume(target != unrelated)
    snapshot = BridgePolicySnapshot(
        (_entry(PolicyType.FEDERATION_BLOCK, unrelated),)
    )

    decision = snapshot.federation_decision(target)

    assert decision.allowed is True
    assert decision.reason is FederationPolicyReason.ALLOWED
    assert decision.host == target


@given(host=_valid_host, unrelated=_valid_host)
def test_entry_order_and_duplicates_do_not_change_decisions(
    host: str,
    unrelated: str,
) -> None:
    """Set-like policy semantics are invariant to ordering and duplicates."""

    assume(host != unrelated)
    base = (
        _entry(PolicyType.FEDERATION_ALLOW, host),
        _entry(PolicyType.FEDERATION_BLOCK, unrelated),
    )
    reordered_with_duplicates = (
        base[1],
        base[0],
        base[1],
        base[0],
    )

    first = BridgePolicySnapshot(base).federation_decision(host)
    second = BridgePolicySnapshot(reordered_with_duplicates).federation_decision(host)

    assert first.allowed is True
    assert first.reason is FederationPolicyReason.ALLOWED
    assert second == first


@given(host=_valid_host)
def test_canonical_host_variants_normalize_to_same_subject(host: str) -> None:
    """Case, trailing dot, and URL wrapping preserve canonical host identity."""

    expected = host.lower()
    variants = (
        host.upper(),
        f"{host}.",
        f"https://{host}/c/test",
        f"HTTPS://{host.upper()}/c/test",
    )

    assert {normalize_instance_subject(value) for value in variants} == {expected}


@given(host=_valid_host, whitespace=st.sampled_from([" ", "\t", "\n"]))
def test_whitespace_bearing_hosts_are_rejected(host: str, whitespace: str) -> None:
    """Whitespace is never silently normalized into a policy subject."""

    malformed = f"{host}{whitespace}suffix"
    decision = BridgePolicySnapshot(()).federation_decision(malformed)

    assert decision.allowed is False
    assert decision.reason is FederationPolicyReason.INVALID_HOST
    assert decision.host is None


@given(
    malformed=st.sampled_from(
        ["", ".example", "example..com", "https://", "http:///missing"]
    )
)
def test_structurally_malformed_hosts_are_rejected(malformed: str) -> None:
    """Empty labels and missing hosts fail the documented validation contract."""

    decision = BridgePolicySnapshot(()).federation_decision(malformed)

    assert decision.allowed is False
    assert decision.reason is FederationPolicyReason.INVALID_HOST


@given(value=_snowflake)
def test_decimal_discord_identifiers_normalize_without_change(value: str) -> None:
    """Every generated decimal snowflake is already canonical."""

    snapshot = BridgePolicySnapshot(
        (
            _entry(PolicyType.DISCORD_GUILD_ALLOW, value),
            _entry(PolicyType.BRIDGE_SUPER_ADMIN, value),
        )
    )

    assert normalize_snowflake(value) == value
    assert snapshot.is_discord_guild_allowed(value) is True
    assert snapshot.is_super_admin(value) is True


@given(
    value=st.one_of(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
        st.text(alphabet=" \t\n", min_size=1, max_size=4),
        st.from_regex(r"[0-9]+[a-z]+", fullmatch=True),
    )
)
def test_non_decimal_discord_identifiers_never_gain_access(value: str) -> None:
    """Invalid identifiers cannot be normalized, allowed, or made administrators."""

    snapshot = BridgePolicySnapshot(())

    try:
        normalize_snowflake(value)
    except ValueError:
        pass
    else:  # pragma: no cover - the generated strategy excludes decimal-only text.
        raise AssertionError(f"unexpected decimal-only value: {value!r}")
    assert snapshot.is_discord_guild_allowed(value) is False
    assert snapshot.is_super_admin(value) is False
