"""Tests for demo_pkg.utils."""

from demo_pkg.utils import greet, add


class TestGreet:
    """Tests for the greet function."""

    def test_greet_returns_hello_string(self) -> None:
        """greet should return 'Hello, <name>!'."""
        result = greet("Alice")
        assert result == "Hello, Alice!"

    def test_greet_with_empty_string(self) -> None:
        """greet should handle an empty name."""
        result = greet("")
        assert result == "Hello, !"


class TestAdd:
    """Tests for the add function."""

    def test_add_two_positives(self) -> None:
        """add should return the sum of two positive integers."""
        assert add(2, 3) == 5

    def test_add_positive_and_negative(self) -> None:
        """add should handle a mix of positive and negative integers."""
        assert add(5, -2) == 3

    def test_add_zeros(self) -> None:
        """add should return zero when both arguments are zero."""
        assert add(0, 0) == 0
