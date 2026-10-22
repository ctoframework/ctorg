import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

import ctorg

SAMPLE = Path(__file__).with_name("data.json")


def write_people(tmp_path, people):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(people), encoding="utf-8")
    return path


def test_load_sample_data():
    people = ctorg.load(SAMPLE)
    assert len(people) == 15
    assert ctorg.build_tree(people)[0][0]["name"] == "Elena Marsh"


def test_build_tree_groups_children_under_manager():
    people = [
        {"name": "A", "role": "Lead"},
        {"name": "B", "role": "Dev", "manager": "A"},
        {"name": "C", "role": "Dev", "manager": "A"},
    ]
    roots, children = ctorg.build_tree(people)
    assert [p["name"] for p in roots] == ["A"]
    assert [p["name"] for p in children["A"]] == ["B", "C"]
    assert children["B"] == []


def test_ascii_tree_output():
    people = [
        {"name": "A", "role": "Lead", "team": "T"},
        {"name": "B", "role": "Dev", "seniority": "Junior", "team": "T", "manager": "A"},
        {"name": "C", "role": "Tester", "manager": "A"},
    ]
    roots, children = ctorg.build_tree(people)
    assert ctorg.ascii_tree(roots, children) == [
        "A — Lead (T)",
        "├── B — Junior Dev (T)",
        "└── C — Tester",
    ]


def test_ascii_tree_colours_people_by_team():
    people = [
        {"name": "A", "role": "Lead", "team": "T"},
        {"name": "B", "role": "Dev", "team": "U", "manager": "A"},
        {"name": "C", "role": "Tester", "manager": "A"},
    ]
    roots, children = ctorg.build_tree(people)
    lines = ctorg.ascii_tree(roots, children, ctorg.team_painter(people))
    assert lines == [
        "\033[36mA — Lead (T)\033[0m",
        "├── \033[32mB — Dev (U)\033[0m",
        "└── C — Tester",
    ]


def test_use_color_respects_tty_and_no_color(monkeypatch):
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert ctorg.use_color(Stream(True))
    assert not ctorg.use_color(Stream(False))
    monkeypatch.setenv("NO_COLOR", "1")
    assert not ctorg.use_color(Stream(True))


@pytest.mark.parametrize(
    "people, message",
    [
        ([{"name": "A"}], "'role' is required"),
        ([{"name": "A", "role": "Dev", "seniority": "Principal"}], "seniority must be one of"),
        ([{"name": "A", "role": "Dev"}, {"name": "A", "role": "Dev"}], "duplicate name 'A'"),
        ([{"name": "A", "role": "Dev", "manager": "Nobody"}], "unknown manager 'Nobody'"),
        (
            [
                {"name": "A", "role": "Dev", "manager": "B"},
                {"name": "B", "role": "Dev", "manager": "A"},
            ],
            "cycle",
        ),
    ],
)
def test_load_rejects_invalid_data(tmp_path, people, message):
    with pytest.raises(ctorg.DataError, match=message):
        ctorg.load(write_people(tmp_path, people))


def test_render_png_writes_image(tmp_path):
    people = ctorg.load(SAMPLE)
    roots, children = ctorg.build_tree(people)
    out = tmp_path / "chart.png"
    ctorg.render_png(people, roots, children, out)
    with Image.open(out) as image:
        assert image.size[0] > 0 and image.size[1] > 0


def test_main_defaults_to_ascii_on_terminal_without_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert ctorg.main([str(SAMPLE)]) == 0
    assert "Elena Marsh — Lead (Leadership)" in capsys.readouterr().out
    assert not any(tmp_path.iterdir())


def test_main_png_format_writes_default_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert ctorg.main([str(SAMPLE), "--format", "png"]) == 0
    assert (tmp_path / "orgchart.png").exists()
    assert "Wrote orgchart.png" in capsys.readouterr().out


def test_main_svg_format_writes_svg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ctorg.main([str(SAMPLE), "--format", "svg"]) == 0
    assert (tmp_path / "orgchart.svg").exists()


def test_main_ascii_with_output_writes_text(tmp_path, capsys):
    out = tmp_path / "chart.txt"
    assert ctorg.main([str(SAMPLE), "--format", "ascii", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines()[0] == "Elena Marsh — Lead (Leadership)"
    assert "\033[" not in out.read_text(encoding="utf-8")


def test_main_rejects_unknown_format(capsys):
    assert ctorg.main([str(SAMPLE), "--format", "pdf"]) == 2
    assert "is not one of" in capsys.readouterr().err


def test_render_svg_writes_valid_svg(tmp_path):
    people = ctorg.load(SAMPLE)
    roots, children = ctorg.build_tree(people)
    out = tmp_path / "chart.svg"
    ctorg.render_svg(people, roots, children, out)
    root = ET.parse(out).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    texts = [node.text for node in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "Elena Marsh" in texts
    rects = list(root.iter("{http://www.w3.org/2000/svg}rect"))
    assert len(rects) == 1 + len(people)  # background plus one card per person


def test_render_svg_escapes_text(tmp_path):
    people = [{"name": "A & <B>", "role": "Dev"}]
    roots, children = ctorg.build_tree(people)
    out = tmp_path / "chart.svg"
    ctorg.render_svg(people, roots, children, out)
    assert ET.parse(out).getroot() is not None
    assert "A &amp; &lt;B&gt;" in out.read_text(encoding="utf-8")


def test_main_reports_data_error(tmp_path, capsys):
    path = write_people(tmp_path, [{"name": "A"}])
    assert ctorg.main([str(path)]) == 1
    assert "'role' is required" in capsys.readouterr().err


def test_stats_lines_counts_structure():
    people = [
        {"name": "A", "role": "Lead", "team": "T", "seniority": "Senior"},
        {"name": "B", "role": "Dev", "team": "T", "manager": "A"},
        {"name": "C", "role": "Dev", "manager": "A"},
        {"name": "D", "role": "Dev", "team": "U", "seniority": "Junior", "manager": "B"},
    ]
    roots, children = ctorg.build_tree(people)
    lines = ctorg.stats_lines(people, roots, children)
    assert lines[:5] == [
        "People: 4",
        "Top-level people: 1",
        "Levels: 3",
        "Managers: 2",
        "Span of control: avg 1.5, max 2",
    ]
    assert "  T: 2" in lines
    assert "  (none): 1" in lines
    assert "  Dev: 3" in lines


def test_stats_lines_without_managers_omits_span():
    people = [{"name": "A", "role": "Dev"}]
    roots, children = ctorg.build_tree(people)
    lines = ctorg.stats_lines(people, roots, children)
    assert "Levels: 1" in lines
    assert not any(line.startswith("Span of control") for line in lines)


def test_stats_prints_counts_for_sample(capsys):
    assert ctorg.main(["stats", str(SAMPLE)]) == 0
    captured = capsys.readouterr().out
    assert "People: 15" in captured
    assert "Levels: 3" in captured
    assert "Span of control: avg 3.5, max 4" in captured
    assert "  Platform: 5" in captured
    assert "  QA: 4" in captured


def test_stats_reports_data_error(tmp_path, capsys):
    path = write_people(tmp_path, [{"name": "A"}])
    assert ctorg.main(["stats", str(path)]) == 1
    assert "'role' is required" in capsys.readouterr().err


def test_stats_does_not_write_png(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ctorg.main(["stats", str(SAMPLE)]) == 0
    assert not any(tmp_path.iterdir())


def test_list_prints_everyone_without_filters(capsys):
    assert ctorg.main(["list", str(SAMPLE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 15
    assert lines[0] == "Elena Marsh — Lead (Leadership)"


def test_list_filters_by_role_case_insensitively(capsys):
    assert ctorg.main(["list", str(SAMPLE), "--role", "developer"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all(" Developer" in line for line in lines)


def test_list_filters_by_seniority(capsys):
    assert ctorg.main(["list", str(SAMPLE), "--seniority", "Senior"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all(" — Senior " in line for line in lines)


def test_list_filters_by_team(capsys):
    assert ctorg.main(["list", str(SAMPLE), "--team", "platform"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all("(Platform)" in line for line in lines)


def test_list_combines_filters(tmp_path, capsys):
    people = [
        {"name": "A", "role": "Dev", "seniority": "Senior", "team": "T"},
        {"name": "B", "role": "Dev", "seniority": "Junior", "team": "T"},
        {"name": "C", "role": "Tester", "seniority": "Senior", "team": "T"},
        {"name": "D", "role": "Dev", "seniority": "Senior", "team": "U"},
    ]
    path = write_people(tmp_path, people)
    assert ctorg.main(["list", str(path), "--role", "Dev", "--seniority", "Senior", "--team", "T"]) == 0
    assert capsys.readouterr().out.splitlines() == ["A — Senior Dev (T)"]


def test_list_no_match_prints_nothing(capsys):
    assert ctorg.main(["list", str(SAMPLE), "--role", "Nonexistent"]) == 0
    assert capsys.readouterr().out == ""


def test_list_rejects_unknown_seniority(capsys):
    assert ctorg.main(["list", str(SAMPLE), "--seniority", "Principal"]) == 2
    assert "is not one of" in capsys.readouterr().err


def test_list_reports_data_error(tmp_path, capsys):
    path = write_people(tmp_path, [{"name": "A"}])
    assert ctorg.main(["list", str(path)]) == 1
    assert "'role' is required" in capsys.readouterr().err
