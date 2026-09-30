from conftest import FILLED
from nemo.brief import parse_brief
from nemo.plan import plan_queries


def test_plan_is_deterministic_and_uses_only_brief_terms():
    b = parse_brief(FILLED)
    q1, dropped = plan_queries(b)
    assert q1 == plan_queries(b)[0] and dropped == 0
    texts = [q.text for q in q1]
    assert "Widget Engineer jobs Testville" in texts
    assert "Widget Engineer jobs remote Freedonia" in texts
    assert "Gadget Developer jobs Testville" in texts
    assert "build widgets jobs" in texts
    assert "Acme careers Widget Engineer" in texts


def test_no_locations_means_no_suffix():
    b = parse_brief("version: 1\nsearch: {target_titles: [X]}\nhard: {}\npreferred: {}\n")
    assert [q.text for q in plan_queries(b)[0]] == ["X jobs"]


def test_cap_reserves_room_for_variations_and_reports_dropped():
    b = parse_brief(FILLED + "budgets: {max_queries: 6, max_query_variations: 2}\n")
    queries, dropped = plan_queries(b)
    assert len(queries) == 4 and dropped > 0
    assert queries[0].purpose == "target_title"  # priority order kept


def test_duplicates_removed_case_insensitively():
    b = parse_brief("version: 1\nsearch: {target_titles: [X, x]}\nhard: {}\npreferred: {}\n")
    assert len(plan_queries(b)[0]) == 1
