import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("es", ROOT / "scripts" / "exploratory_shortlist.py")
es = importlib.util.module_from_spec(spec)
spec.loader.exec_module(es)

L = "https://in.linkedin.com/jobs/view/{}?utm_campaign=google_jobs_apply"


def test_slug_parts_unquotes_and_splits():
    assert es.slug_parts(L.format("senior-data-platform-engineer-%E2%80%93-modelops-at-digikey-global-capability-center-4473205534")) == \
        ("senior data platform engineer – modelops", "digikey global capability center")


def test_consistent_listing_passes():
    job = {"title": "Senior Java DevOps Engineer", "company_name": "QualityAI", "location": "Bengaluru, Karnataka",
           "description": "Location: Bangalore"}
    assert es.check(job, L.format("senior-java-devops-engineer-at-qualityai-4472703192")) == []


def test_company_title_and_location_contradictions_are_flagged():
    job = {"title": "AI Ops Engineer", "company_name": "Circana", "location": "Bengaluru, Karnataka", "description": ""}
    assert any("company mismatch" in p for p in es.check(job, L.format("ai-ops-engineer-at-skit-ai-4344432903")))
    job = {"title": "Payroll Specialist", "company_name": "Acme", "location": "Bengaluru", "description": ""}
    assert any("title mismatch" in p for p in es.check(job, L.format("devops-engineer-at-acme-4400000000")))
    job = {"title": "DevOps Engineer", "company_name": "Acme", "location": "Bengaluru, Karnataka",
           "description": "This role is based in Pune."}
    assert any("location mismatch" in p for p in es.check(job, L.format("devops-engineer-at-acme-4400000000")))
    job = {"title": "DevOps Engineer", "company_name": "Acme", "location": "Bengaluru", "description": "Location: All over India"}
    assert es.check(job, L.format("devops-engineer-at-acme-4400000000")) == []
