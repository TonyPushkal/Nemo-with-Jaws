import importlib.util
from pathlib import Path

import pytest

from nemo.envfile import load_env_file, parse_env

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    spec = importlib.util.spec_from_file_location("probe_script", ROOT / "scripts" / "probe_provider.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_env_handles_quotes_comments_export():
    text = "# c\nA=1\nexport B='two words'\nC=\"x\"  \nD=val # note\n\nE=\nbad line\n1X=no\n"
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "x", "D": "val", "E": ""}


def test_load_env_file_does_not_override_or_load_empty(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TAVILY_API_KEY=from-file\nNEMO_MAX_USD_PER_RUN=\nOTHER=1\n")
    env = {"OTHER": "already"}
    assert load_env_file(f, env) == ["TAVILY_API_KEY"] and env == {"OTHER": "already", "TAVILY_API_KEY": "from-file"}
    assert load_env_file(tmp_path / "missing", env) == []


def test_dry_run_needs_no_key_and_never_prints_it(monkeypatch, capsys, tmp_path):
    script = load_script()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-SECRET")
    assert script.main(["--dry-run", "--env-file", str(tmp_path / "none")]) == 0
    out = capsys.readouterr().out
    assert "site:linkedin.com/jobs/view/ software engineer" in out and "tvly-SECRET" not in out
    assert "Paid calls: DISABLED" in out


def test_real_run_refused_without_key_or_budget_or_confirmation(monkeypatch, capsys, tmp_path):
    script = load_script()
    env_file = str(tmp_path / "none")
    for var in ("TAVILY_API_KEY", "NEMO_PAID_CALLS_ENABLED", "NEMO_MAX_USD_PER_RUN", "NEMO_MONTHLY_USD_CAP"):
        monkeypatch.delenv(var, raising=False)
    assert script.main(["--env-file", env_file, "--db", str(tmp_path / "l.sqlite")]) == 2      # no key
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert script.main(["--env-file", env_file, "--db", str(tmp_path / "l.sqlite")]) == 2      # no budget
    monkeypatch.setenv("NEMO_PAID_CALLS_ENABLED", "true")
    monkeypatch.setenv("NEMO_MAX_USD_PER_RUN", "0.1")
    assert script.main(["--env-file", env_file, "--db", str(tmp_path / "l.sqlite")]) == 2      # not interactive, no --yes
    err = capsys.readouterr().err
    assert "TAVILY_API_KEY is not set" in err and "Paid calls are disabled" in err and "without confirmation" in err
    assert not (tmp_path / "l.sqlite").exists()                                                # nothing was spent or ledgered
