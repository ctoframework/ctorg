#!/usr/bin/env python3
"""ctorg: draw an org chart from a JSON file, as ASCII on the terminal and as a PNG."""

import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO, TypedDict, cast

import click
import svgwrite
from PIL import Image, ImageDraw, ImageFont


class _RequiredPerson(TypedDict):
    name: str
    role: str


class Person(_RequiredPerson, total=False):
    seniority: str
    team: str
    manager: str | None


Point = tuple[float, float]
Segment = tuple[Point, Point]
Tree = dict[str, list[Person]]
Positions = dict[str, Point]
Paint = Callable[[Person, str], str]

SENIORITIES = ("Junior", "Mid", "Senior")
DEFAULT_DATA = "data.json"
FORMATS = ("ascii", "png", "svg")
DEFAULT_OUTPUT = {"png": "orgchart.png", "svg": "orgchart.svg"}

NODE_W = 220
NODE_H = 80
H_GAP = 30
V_GAP = 60
MARGIN = 30
PADDING = 10
LINE_H = 20
EDGE = "#555555"
NO_TEAM = "#eeeeee"
PALETTE = ["#cfe8ff", "#d8f5d0", "#ffe3c2", "#f3d1f4", "#fff4b8", "#ffd1d1", "#d2f4f0", "#e0e0ff"]
ANSI_PALETTE = [36, 32, 33, 35, 94, 31, 96, 92]
ANSI_RESET = "\033[0m"


class DataError(Exception):
    pass


def load(path: Path | str) -> list[Person]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise DataError("expected a JSON array of people")

    people: list[Person] = []
    by_name: dict[str, Person] = {}
    for index, person in enumerate(data):
        if not isinstance(person, dict):
            raise DataError(f"entry {index} is not an object")
        for field in ("name", "role"):
            if not isinstance(person.get(field), str) or not person[field].strip():
                raise DataError(f"entry {index}: '{field}' is required")
        person = cast(Person, person)
        name = person["name"]
        if name in by_name:
            raise DataError(f"duplicate name '{name}'")
        seniority = person.get("seniority")
        if seniority is not None and seniority not in SENIORITIES:
            raise DataError(f"'{name}': seniority must be one of {', '.join(SENIORITIES)}")
        by_name[name] = person
        people.append(person)

    for person in people:
        manager = person.get("manager")
        if manager is not None and manager not in by_name:
            raise DataError(f"'{person['name']}': unknown manager '{manager}'")

    for person in people:
        seen: set[str] = set()
        current: str | None = person["name"]
        while current is not None:
            if current in seen:
                raise DataError(f"'{person['name']}': manager chain contains a cycle")
            seen.add(current)
            current = by_name[current].get("manager")

    return people


def build_tree(people: list[Person]) -> tuple[list[Person], Tree]:
    roots: list[Person] = []
    children: Tree = {person["name"]: [] for person in people}
    for person in people:
        manager = person.get("manager")
        if manager is None:
            roots.append(person)
        else:
            children[manager].append(person)
    return roots, children


def role_text(person: Person) -> str:
    return " ".join(filter(None, [person.get("seniority"), person["role"]]))


def label(person: Person) -> str:
    text = f"{person['name']} — {role_text(person)}"
    if person.get("team"):
        text += f" ({person['team']})"
    return text


def ascii_tree(roots: list[Person], children: Tree, paint: Paint | None = None) -> list[str]:
    lines: list[str] = []

    def walk(person: Person, prefix: str, connector: str, indent: str) -> None:
        text = label(person)
        if paint is not None:
            text = paint(person, text)
        lines.append(prefix + connector + text)
        kids = children[person["name"]]
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            walk(kid, indent, "└── " if last else "├── ", indent + ("    " if last else "│   "))

    for root in roots:
        walk(root, "", "", "")
    return lines


def team_painter(people: list[Person]) -> Paint:
    teams = sorted({person["team"] for person in people if person.get("team")})
    codes = {team: ANSI_PALETTE[i % len(ANSI_PALETTE)] for i, team in enumerate(teams)}

    def paint(person: Person, text: str) -> str:
        code = codes.get(person.get("team", ""))
        return text if code is None else f"\033[{code}m{text}{ANSI_RESET}"

    return paint


def use_color(stream: TextIO) -> bool:
    return stream.isatty() and "NO_COLOR" not in os.environ


def subtree_width(person: Person, children: Tree) -> float:
    kids = children[person["name"]]
    if not kids:
        return NODE_W
    kids_width = sum(subtree_width(kid, children) for kid in kids) + H_GAP * (len(kids) - 1)
    return max(NODE_W, kids_width)


def layout(person: Person, children: Tree, left: float, depth: int, positions: Positions) -> float:
    width = subtree_width(person, children)
    kids = children[person["name"]]
    positions[person["name"]] = (left + width / 2, MARGIN + depth * (NODE_H + V_GAP))
    if kids:
        kids_width = sum(subtree_width(kid, children) for kid in kids) + H_GAP * (len(kids) - 1)
        x = left + (width - kids_width) / 2
        for kid in kids:
            layout(kid, children, x, depth + 1, positions)
            x += subtree_width(kid, children) + H_GAP
    return width


def load_fonts() -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, ImageFont.FreeTypeFont | ImageFont.ImageFont]:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", 16), ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        fallback = ImageFont.load_default()
        return fallback, fallback


def team_fills(people: list[Person]) -> dict[str, str]:
    teams = sorted({person["team"] for person in people if person.get("team")})
    return {team: PALETTE[i % len(PALETTE)] for i, team in enumerate(teams)}


def place(roots: list[Person], children: Tree) -> tuple[Positions, int, int]:
    positions: Positions = {}
    x: float = MARGIN
    for root in roots:
        x += layout(root, children, x, 0, positions) + H_GAP
    width = int(x - H_GAP + MARGIN)
    height = int(max(top for _, top in positions.values()) + NODE_H + MARGIN)
    return positions, width, height


def edge_segments(children: Tree, positions: Positions) -> list[Segment]:
    segments: list[Segment] = []
    for name, kids in children.items():
        if not kids:
            continue
        px, py = positions[name]
        bottom = py + NODE_H
        mid = bottom + V_GAP / 2
        kid_xs = [positions[kid["name"]][0] for kid in kids]
        segments.append(((px, bottom), (px, mid)))
        segments.append(((min(kid_xs), mid), (max(kid_xs), mid)))
        for kid, kx in zip(kids, kid_xs):
            segments.append(((kx, mid), (kx, positions[kid["name"]][1])))
    return segments


def render_png(people: list[Person], roots: list[Person], children: Tree, path: Path | str) -> None:
    positions, width, height = place(roots, children)
    fills = team_fills(people)

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    bold, regular = load_fonts()

    for start, end in edge_segments(children, positions):
        draw.line([start, end], fill=EDGE, width=2)

    for person in people:
        cx, top = positions[person["name"]]
        left = cx - NODE_W / 2
        fill = fills.get(person.get("team", ""), NO_TEAM)
        draw.rounded_rectangle(
            [left, top, left + NODE_W, top + NODE_H], radius=10, fill=fill, outline=EDGE, width=1
        )
        draw.text((left + PADDING, top + PADDING), person["name"], fill="black", font=bold)
        draw.text((left + PADDING, top + PADDING + LINE_H), role_text(person), fill="black", font=regular)
        if person.get("team"):
            draw.text((left + PADDING, top + PADDING + 2 * LINE_H), f"Team: {person['team']}", fill="#333333", font=regular)

    image.save(path)


def render_svg(people: list[Person], roots: list[Person], children: Tree, path: Path | str) -> None:
    positions, width, height = place(roots, children)
    fills = team_fills(people)

    drawing = svgwrite.Drawing(size=(width, height), profile="full")
    drawing.viewbox(0, 0, width, height)
    drawing["font-family"] = "DejaVu Sans, Verdana, sans-serif"
    drawing.add(drawing.rect(insert=(0, 0), size=(width, height), fill="white"))
    for start, end in edge_segments(children, positions):
        drawing.add(drawing.line(start=start, end=end, stroke=EDGE, stroke_width=2))

    for person in people:
        cx, top = positions[person["name"]]
        left = cx - NODE_W / 2
        fill = fills.get(person.get("team", ""), NO_TEAM)
        drawing.add(
            drawing.rect(
                insert=(left, top), size=(NODE_W, NODE_H), rx=10, ry=10, fill=fill, stroke=EDGE, stroke_width=1
            )
        )
        drawing.add(
            drawing.text(person["name"], insert=(left + PADDING, top + PADDING + 16), font_size=16, font_weight="bold")
        )
        drawing.add(
            drawing.text(role_text(person), insert=(left + PADDING, top + PADDING + LINE_H + 14), font_size=14)
        )
        if person.get("team"):
            drawing.add(
                drawing.text(
                    "Team: " + person["team"],
                    insert=(left + PADDING, top + PADDING + 2 * LINE_H + 14),
                    font_size=14,
                    fill="#333333",
                )
            )

    drawing.saveas(path, pretty=True)


def levels(roots: list[Person], children: Tree) -> int:
    def depth(person: Person) -> int:
        return 1 + max((depth(kid) for kid in children[person["name"]]), default=0)

    return max((depth(root) for root in roots), default=0)


def count_lines(title: str, values: Iterable[str]) -> list[str]:
    lines = [title]
    for value, n in sorted(Counter(values).items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"  {value}: {n}")
    return lines


def stats_lines(people: list[Person], roots: list[Person], children: Tree) -> list[str]:
    managers = [person for person in people if children[person["name"]]]
    spans = [len(children[person["name"]]) for person in managers]
    lines = [
        f"People: {len(people)}",
        f"Top-level people: {len(roots)}",
        f"Levels: {levels(roots, children)}",
        f"Managers: {len(managers)}",
    ]
    if spans:
        lines.append(f"Span of control: avg {sum(spans) / len(spans):.1f}, max {max(spans)}")
    lines += [""] + count_lines("By team:", [person.get("team") or "(none)" for person in people])
    lines += [""] + count_lines("By seniority:", [person.get("seniority") or "(none)" for person in people])
    lines += [""] + count_lines("By role:", [person["role"] for person in people])
    return lines


def load_or_report(path: str) -> list[Person] | None:
    try:
        return load(path)
    except (OSError, json.JSONDecodeError, DataError) as err:
        print(f"ctorg: {err}", file=sys.stderr)
        return None


@click.group()
def cli() -> None:
    """Draw an org chart from a JSON file, or list and count its people."""


@cli.command("chart", epilog="Run 'ctorg stats --help' for team, seniority and role counts.")
@click.argument("data", default=DEFAULT_DATA)
@click.option("--format", "fmt", type=click.Choice(FORMATS), default="ascii", show_default=True, help="Export format (ascii is printed to the terminal).")
@click.option("-o", "--output", help="Output path (default: orgchart.png or orgchart.svg; with ascii, writes the text there instead of printing).")
def chart_cmd(data: str, fmt: str, output: str | None) -> int:
    """Draw an org chart from a JSON file."""
    people = load_or_report(data)
    if people is None:
        return 1

    roots, children = build_tree(people)
    if fmt == "ascii":
        if output is None:
            paint = team_painter(people) if use_color(sys.stdout) else None
            print("\n".join(ascii_tree(roots, children, paint)))
        else:
            with open(output, "w", encoding="utf-8") as f:
                f.write("\n".join(ascii_tree(roots, children)) + "\n")
            print(f"Wrote {output}")
        return 0

    output = output or DEFAULT_OUTPUT[fmt]
    renderer = render_png if fmt == "png" else render_svg
    renderer(people, roots, children, output)
    print(f"Wrote {output}")
    return 0


@cli.command("stats")
@click.argument("data", default=DEFAULT_DATA)
def stats_cmd(data: str) -> int:
    """Print counts and structure stats for a people JSON file."""
    people = load_or_report(data)
    if people is None:
        return 1

    roots, children = build_tree(people)
    print("\n".join(stats_lines(people, roots, children)))
    return 0


def matches(person: Person, role: str | None = None, seniority: str | None = None, team: str | None = None) -> bool:
    if role is not None and person["role"].casefold() != role.casefold():
        return False
    if seniority is not None and person.get("seniority") != seniority:
        return False
    if team is not None and (person.get("team") or "").casefold() != team.casefold():
        return False
    return True


@cli.command("list")
@click.argument("data", default=DEFAULT_DATA)
@click.option("--role", help="Only people with this role (case-insensitive).")
@click.option("--seniority", type=click.Choice(SENIORITIES), help="Only people with this seniority.")
@click.option("--team", help="Only people on this team (case-insensitive).")
def list_cmd(data: str, role: str | None, seniority: str | None, team: str | None) -> int:
    """List people, optionally filtered by role, seniority or team."""
    people = load_or_report(data)
    if people is None:
        return 1

    for person in people:
        if matches(person, role=role, seniority=seniority, team=team):
            print(label(person))
    return 0


def main(argv: Sequence[str] | None = None) -> Any:
    args = sys.argv[1:] if argv is None else list(argv)
    # Without a subcommand name, the bare form draws the chart, as it always has.
    # Top-level --help is left to the group so it lists the subcommands.
    if args[:1] != ["--help"] and (not args or args[0] not in cli.commands):
        args = ["chart", *args]
    try:
        return cli.main(args=args, prog_name="ctorg", standalone_mode=False)
    except click.ClickException as err:
        err.show()
        return err.exit_code


if __name__ == "__main__":
    sys.exit(main())
