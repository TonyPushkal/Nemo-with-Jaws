from conftest import FILLED
from nemo.cli import main


def test_init_writes_empty_template_and_refuses_overwrite(tmp_path, capsys):
    p = tmp_path / "briefs" / "brief.yaml"
    assert main(["init", "--path", str(p)]) == 0 and p.exists()
    assert "target_titles: []" in p.read_text()
    assert main(["init", "--path", str(p)]) == 1
    assert main(["init", "--path", str(p), "--force"]) == 0


def test_check_on_fresh_template_fails_with_clear_error(tmp_path, capsys):
    p = tmp_path / "b.yaml"
    main(["init", "--path", str(p)])
    assert main(["brief", "check", "--brief", str(p)]) == 1
    out = capsys.readouterr().out
    assert "target_titles is empty" in out and "Paid calls: DISABLED" in out


def test_check_filled_brief_lists_queries_and_criteria(tmp_path, capsys):
    p = tmp_path / "b.yaml"
    p.write_text(FILLED)
    assert main(["brief", "check", "--brief", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Widget Engineer jobs Testville" in out
    assert "must skills: Python" in out and "skills: Rust" in out
    assert "unknown hard values: flag" in out


def test_check_invalid_brief_and_missing_file(tmp_path, capsys):
    p = tmp_path / "b.yaml"
    p.write_text("version: 1\nhard: {}\n")
    assert main(["brief", "check", "--brief", str(p)]) == 1
    assert main(["brief", "check", "--brief", str(tmp_path / "missing.yaml")]) == 1


def test_search_is_not_implemented(capsys):
    assert main(["search"]) == 2
