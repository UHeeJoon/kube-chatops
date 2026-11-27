"""로그 묶기: 숫자만 다른 줄은 한 묶음."""

from app.main import group_lines


def test_lines_that_differ_only_in_numbers_are_grouped():
    rows = [{"ts": 300, "pod": "a", "container": "app", "line": "GET /api/orders/17 500 in 12ms"},
            {"ts": 200, "pod": "b", "container": "app", "line": "GET /api/orders/3 500 in 9ms"},
            {"ts": 150, "pod": "a", "container": "app", "line": "connection refused"},
            {"ts": 100, "pod": "a", "container": "app", "line": "GET /api/orders/99 500 in 40ms"}]
    out = group_lines(rows)
    assert [g["count"] for g in out] == [3, 1]
    assert out[0] == {"line": "GET /api/orders/17 500 in 12ms", "count": 3, "last_ts": 300, "first_ts": 100, "pods": ["a", "b"]}
    assert "first_ts" not in out[1]


def test_group_count_is_capped():
    rows = [{"ts": i, "pod": "a", "container": "app", "line": f"line {'x' * i}"} for i in range(30, 0, -1)]
    assert len(group_lines(rows)) == 20
