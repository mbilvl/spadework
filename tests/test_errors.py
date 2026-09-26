from spadework.errors import ExitCode, InvalidError, Problem, SpadeworkError, UsageError


def test_problem_locations() -> None:
    assert str(Problem("bad", "brief.md", 3)) == "brief.md:3: bad"
    assert str(Problem("bad", "brief.md")) == "brief.md: bad"
    assert str(Problem("bad")) == "bad"
    assert Problem("bad").location is None


def test_error_lines() -> None:
    one = InvalidError("x is not valid", "Fix x.", problems=[Problem("bad", "x", 1)])
    many = InvalidError(
        "x is not valid",
        "Fix x.",
        problems=[Problem("a", "x", 1), Problem("b", "x", 2)],
    )
    none = SpadeworkError("it broke", "Try again.")

    assert one.lines() == ["x:1: bad"]
    assert many.lines() == ["x is not valid", "  x:1: a", "  x:2: b"]
    assert none.lines() == ["it broke"]


def test_exit_codes() -> None:
    assert SpadeworkError("m", "h").exit_code == ExitCode.ERROR == 1
    assert UsageError("m", "h").exit_code == ExitCode.USAGE == 2
    assert InvalidError("m", "h").exit_code == ExitCode.INVALID == 3
