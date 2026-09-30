from nemo.identity import Candidate, MergeKind, decide_merge, text_similarity
from nemo.urlnorm import canonical_url, is_linkedin

DESC = "We are hiring a widget engineer to design build and test widgets for our customers across the region"


def kind(a, b):
    return decide_merge(a, b).kind


def test_same_ats_key_merges_even_if_titles_differ():
    a = Candidate("Acme", "Widget Eng", ats_key="greenhouse:acme:1")
    b = Candidate("Acme Inc", "Widget Engineer II", ats_key="greenhouse:acme:1")
    assert kind(a, b) is MergeKind.MERGE


def test_different_ats_key_or_requisition_is_distinct():
    a = Candidate("Acme", "Widget Engineer", "Testville", ats_key="g:1", text=DESC)
    b = Candidate("Acme", "Widget Engineer", "Testville", ats_key="g:2", text=DESC)
    assert kind(a, b) is MergeKind.DISTINCT
    c = Candidate("Acme", "Widget Engineer", "Testville", requisition_id="R1", text=DESC)
    d = Candidate("Acme", "Widget Engineer", "Testville", requisition_id="R2", text=DESC)
    assert kind(c, d) is MergeKind.DISTINCT


def test_same_requisition_same_company_merges():
    a = Candidate("Acme", "X", requisition_id="R9")
    b = Candidate("acme", "Y", requisition_id="R9")
    assert kind(a, b) is MergeKind.MERGE


def test_same_canonical_apply_url_merges():
    a = Candidate("A", "T", apply_url="http://www.example.com/jobs/1/?utm_source=x#top")
    b = Candidate("B", "T2", apply_url="https://example.com/jobs/1")
    assert kind(a, b) is MergeKind.MERGE


def test_title_company_location_with_similar_full_text_merges():
    a = Candidate("Acme", "Widget Engineer", "Testville", text=DESC)
    b = Candidate("ACME", "widget engineer", "testville", text=DESC + " today")
    assert text_similarity(DESC, DESC + " today") >= 0.9
    assert kind(a, b) is MergeKind.MERGE


def test_snippet_only_never_merges_on_title_company():  # A13
    a = Candidate("Acme", "Widget Engineer", "Testville", text=DESC)
    b = Candidate("Acme", "Widget Engineer", "Testville")  # snippet-only lead
    assert decide_merge(a, b).kind is MergeKind.POSSIBLE_DUPLICATE


def test_dissimilar_text_is_only_possible_duplicate():
    a = Candidate("Acme", "Widget Engineer", "Testville", text=DESC)
    b = Candidate("Acme", "Widget Engineer", "Testville", text="Completely different responsibilities about sales")
    assert kind(a, b) is MergeKind.POSSIBLE_DUPLICATE


def test_unknown_location_is_only_possible_duplicate():
    a = Candidate("Acme", "Widget Engineer", "", text=DESC)
    b = Candidate("Acme", "Widget Engineer", "Testville", text=DESC)
    assert kind(a, b) is MergeKind.POSSIBLE_DUPLICATE


def test_different_location_or_title_is_distinct():
    a = Candidate("Acme", "Widget Engineer", "Testville", text=DESC)
    assert kind(a, Candidate("Acme", "Widget Engineer", "Elsewhere", text=DESC)) is MergeKind.DISTINCT
    assert kind(a, Candidate("Acme", "Widget Manager", "Testville", text=DESC)) is MergeKind.DISTINCT


def test_canonical_url():
    assert canonical_url("HTTP://WWW.Example.com:443/a/b/?b=2&utm_medium=x&a=1#frag") == "https://example.com/a/b?a=1&b=2"
    assert canonical_url("https://example.com/") == "https://example.com/"
    assert is_linkedin("https://www.linkedin.com/jobs/view/123") and not is_linkedin("https://notlinkedin.com/x")
