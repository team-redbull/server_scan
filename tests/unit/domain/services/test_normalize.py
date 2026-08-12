from app.domain.services.normalize import normalize_text


def test_lowercases_and_collapses_whitespace() -> None:
    assert normalize_text("  OCP-Dell   Worker  001 ") == "ocp-dell worker 001"


def test_none_and_empty_produce_empty_string() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""
