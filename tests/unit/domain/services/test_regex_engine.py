import pytest

from app.domain.ports.regex_engine import RegexTimeout, RegexUnsafeError
from app.domain.services.regex_engine import RegexModuleEngine

ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


def test_search_finds_a_match() -> None:
    match = ENGINE.search(
        r"^ocp-.*", "ocp-dell-worker-001", ignore_case=True, multiline=False, dotall=False
    )
    assert match is not None
    assert match.start == 0


def test_search_returns_none_when_no_match() -> None:
    match = ENGINE.search(
        r"^upi-.*", "ocp-dell-worker-001", ignore_case=True, multiline=False, dotall=False
    )
    assert match is None


def test_ignore_case_flag_is_respected() -> None:
    assert (
        ENGINE.search("OCP", "ocp-x", ignore_case=True, multiline=False, dotall=False) is not None
    )
    assert ENGINE.search("OCP", "ocp-x", ignore_case=False, multiline=False, dotall=False) is None


def test_validate_rejects_pattern_over_max_length() -> None:
    engine = RegexModuleEngine(max_pattern_length=10, match_timeout_seconds=0.25)
    with pytest.raises(RegexUnsafeError):
        engine.validate("a" * 11, ignore_case=True, multiline=False, dotall=False)


def test_validate_rejects_invalid_pattern_syntax() -> None:
    with pytest.raises(RegexUnsafeError):
        ENGINE.validate("(unclosed", ignore_case=True, multiline=False, dotall=False)


def test_validate_accepts_a_reasonable_pattern() -> None:
    ENGINE.validate(
        r"^ocp-dell-.*$", ignore_case=True, multiline=False, dotall=False
    )  # should not raise


def test_catastrophic_backtracking_pattern_times_out_on_search() -> None:
    # The `regex` module's backtracking is measurably more resistant to
    # this classic pathological shape than stdlib `re` — empirically, a
    # short (~40-char) pathological subject completes in under a
    # millisecond here, where it would hang stdlib `re` immediately.
    # A few thousand characters still blows it up exponentially; this is
    # exactly why `_CANARY_INPUTS` (in regex_engine.py) uses a 2000-char
    # canary, not a 40-char one — a canary set tuned to stdlib `re`'s
    # much lower blowup threshold would give this engine a false pass.
    tight_engine = RegexModuleEngine(max_pattern_length=2100, match_timeout_seconds=0.25)
    with pytest.raises(RegexTimeout):
        tight_engine.search(
            r"(a+)+$", "a" * 2000 + "!", ignore_case=True, multiline=False, dotall=False
        )


def test_catastrophic_backtracking_pattern_rejected_at_validate_time() -> None:
    tight_engine = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.05)
    with pytest.raises(RegexUnsafeError):
        tight_engine.validate(r"(a+)+$", ignore_case=True, multiline=False, dotall=False)


def test_compiled_pattern_is_cached_across_calls() -> None:
    engine = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)
    engine.search(r"^ocp-.*", "ocp-a", ignore_case=True, multiline=False, dotall=False)
    engine.search(r"^ocp-.*", "ocp-b", ignore_case=True, multiline=False, dotall=False)
    info = engine._compile.cache_info()  # type: ignore[attr-defined]
    assert info.hits >= 1
