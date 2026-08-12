"""Manifest conformance for the end-user plugin under ``plugin/``.

``plugin/`` is the shippable End-User AI Scaffold (``spec/AI_PLUGIN.md``), distributed as a
single-plugin marketplace whose entry is the repo-relative source ``./plugin``. Nothing else
under ``tests/`` reads it, so the ways it can ship broken are invisible to every other test
and reach installed users directly:

* **A skill whose YAML frontmatter does not parse.** Claude Code reads ``name`` and
  ``description`` from that block to decide when a skill triggers. An unquoted plain scalar
  containing a second ``": "`` is read by YAML as a mapping separator and the whole block
  fails to parse, so the skill loads with empty metadata and never triggers — the body is
  still on disk, which is why the breakage is silent.
* **A ``version`` pinned in a manifest**, which strands installed users on that pin instead
  of the commit SHA (spec/AI_PLUGIN.md §Distribution, cited on ``_VERSION_RATIONALE``).
* **A ``bin/`` helper that is not executable**, which breaks the bare-command invocation
  every skill's ``allowed-tools`` grant assumes (§Packaging).
* **A skill set that has drifted from §Skills**, added or renamed without a spec row.

Both directions of the §Skills and §Packaging enumerations are asserted, following the shape
of this package's other members (``test_route_catalogue.py``): a skill or helper present on
disk but absent from the spec fails, and so does a spec row with nothing behind it.

**This file is a proxy, not a replacement.** It models the subset of ``claude plugin
validate`` that is checkable from file contents alone; the CLI stays authoritative, and
PyYAML here is a stand-in for Claude Code's own loader — a green run is not by itself a
passing ``claude plugin validate``.

Spec: spec/AI_PLUGIN.md §Packaging (the ``plugin/`` layout; "`bin/dataspoke-api` is the
single I/O primitive for the DataSpoke API … Every skill calls the API through this wrapper")
Spec: spec/AI_PLUGIN.md §Distribution ("Neither manifest declares a `version`, so the
update-cache key resolves to the commit SHA of `./plugin` and every merged commit reaches
installed users without a bump.")
Spec: spec/AI_PLUGIN.md §Skills (the six-row skill table) and §Scope → "Out of scope"
("Cluster operations (helm, `kubectl`), `src/`, the operational database … explicitly never
touched by any skill")
Spec: spec/TESTING.md §Assertion Discipline ("Author assertions so that a passing result is
only reachable when the spec'd behavior actually occurred.")

Unit-tier: pure file reads under the repo root. No dev environment, network, or database
(spec/TESTING.md §Unit Testing → Scope).
"""

import json
import re
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from ._api_md import parse_sections

# tests/unit/spec_conformance/test_plugin_manifests.py → parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugin"
SKILLS_ROOT = PLUGIN_ROOT / "skills"
BIN_ROOT = PLUGIN_ROOT / "bin"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = REPO_ROOT / ".claude-plugin" / "marketplace.json"
AI_PLUGIN_MD = REPO_ROOT / "spec" / "AI_PLUGIN.md"

#: Frontmatter keys Claude Code requires on every skill. ``name`` addresses the skill,
#: ``description`` is its entire trigger surface — an absent or empty one means the skill is
#: never selected. ``argument-hint`` is deliberately not required: it is optional metadata
#: with no spec'd contract behind it.
REQUIRED_SKILL_KEYS = ("name", "description")

#: Tools spec/AI_PLUGIN.md:47 places out of scope for every skill: "Cluster operations
#: (helm, `kubectl`), `src/`, the operational database." A skill that grants either has a
#: reach the spec says no skill has.
FORBIDDEN_TOOL_NAMES = ("kubectl", "helm")

#: Why a ``version`` key must not come back. Terse by design — the rule itself lives in the
#: spec, and this points at it rather than restating the mechanism a third time.
_VERSION_RATIONALE = (
    'spec/AI_PLUGIN.md §Distribution: "Neither manifest declares a `version`, so the '
    "update-cache key resolves to the commit SHA of `./plugin` and every merged commit "
    'reaches installed users without a bump." Delete the key — do not bump it.'
)

#: `| `skill-name` | …` — first column of a §Skills table row (spec/AI_PLUGIN.md:176-183).
_SKILL_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|")
#: `bin/<helper>`, as written in the §Packaging layout diagram and its prose
#: (spec/AI_PLUGIN.md:78-80, :83-92).
_BIN_HELPER_RE = re.compile(r"\bbin/([A-Za-z0-9][A-Za-z0-9._-]*)")


# ── Spec-side enumerations ───────────────────────────────────────────────────


def _section_lines(heading: str) -> tuple[str, ...]:
    """Body lines of the ``spec/AI_PLUGIN.md`` section with this heading.

    Fails loudly when the heading is absent — a renamed section must fail the conformance
    checks below rather than silently reduce their spec side to an empty set.
    """
    sections = [
        section
        for section in parse_sections(AI_PLUGIN_MD.read_text(encoding="utf-8"))
        if section.heading == heading
    ]
    if not sections:
        raise LookupError(
            f"spec/AI_PLUGIN.md has no section headed {heading!r} — it was renamed or "
            f"removed, and the conformance checks that read it cannot run."
        )
    return tuple(line for section in sections for line in section.lines)


def spec_skill_names() -> frozenset[str]:
    """Skill names enumerated by the ``spec/AI_PLUGIN.md`` §Skills table's first column."""
    return frozenset(
        match.group(1) for line in _section_lines("Skills") if (match := _SKILL_ROW_RE.match(line))
    )


def spec_bin_helpers() -> frozenset[str]:
    """Helper names ``spec/AI_PLUGIN.md`` §Packaging places under ``plugin/bin/``."""
    return frozenset(
        match.group(1)
        for line in _section_lines("Packaging")
        for match in _BIN_HELPER_RE.finditer(line)
    )


# ── Repo-side enumerations ───────────────────────────────────────────────────


def skill_dirs() -> list[Path]:
    """Every skill directory under ``plugin/skills/``, discovered by glob."""
    return sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))


def skill_ids() -> list[str]:
    return [path.name for path in skill_dirs()]


def bin_helper_ids() -> list[str]:
    """Helper names the spec declares, used to parametrize the per-helper checks.

    Driven from the spec rather than from ``ls plugin/bin`` so that a deleted helper fails
    its own case instead of dropping out of the parametrization.
    """
    return sorted(spec_bin_helpers())


# ── Parsing helpers ──────────────────────────────────────────────────────────


def extract_frontmatter(text: str) -> str:
    """Return the raw YAML frontmatter block of a Markdown document.

    Raises ``ValueError`` when the document does not open with a ``---`` fence or the fence
    is never closed, so a skill missing its frontmatter fails loudly instead of parsing to
    an empty mapping.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("document does not open with a `---` frontmatter fence")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    raise ValueError("frontmatter fence opened but never closed")


def load_frontmatter(path: Path) -> Any:
    return yaml.safe_load(extract_frontmatter(path.read_text(encoding="utf-8")))


def split_tool_grants(value: str) -> list[str]:
    """Split an ``allowed-tools`` value into grants, ignoring commas inside ``(…)``.

    ``Bash(dataspoke-api *), Read`` is two grants, not three — a naive ``split(",")`` would
    shred any future scope that contains a comma and hide what it was scoped to.
    """
    grants: list[str] = []
    depth = 0
    current: list[str] = []
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == "," and depth == 0:
            grants.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    grants.append("".join(current).strip())
    return [grant for grant in grants if grant]


def unscoped_bash_grants(grants: list[str]) -> list[str]:
    """Grants of the ``Bash`` tool that carry no ``(…)`` command scope.

    A bare ``Bash`` is unrestricted shell, which would let a skill reach the cluster and the
    repo that spec/AI_PLUGIN.md:45-49 puts out of scope regardless of what the body says.
    """
    return [
        grant
        for grant in grants
        if grant == "Bash" or (grant.startswith("Bash") and "(" not in grant)
    ]


def forbidden_tool_grants(grants: list[str]) -> list[str]:
    """Grants naming a tool spec/AI_PLUGIN.md:47 says no skill touches."""
    pattern = re.compile(rf"\b({'|'.join(FORBIDDEN_TOOL_NAMES)})\b")
    return [grant for grant in grants if pattern.search(grant)]


class TestFrontmatterExtraction:
    """Backstops proving the extractor + parser can actually fail.

    The per-skill assertions below run against real files and would pass identically if
    ``extract_frontmatter`` returned ``""`` for everything or if ``yaml.safe_load`` never
    raised. These feed synthetic documents through the same code path to prove otherwise —
    in particular the exact failure shape that shipped (an unquoted plain scalar carrying a
    second ``": "``), so this file cannot regress into certifying a broken skill.
    """

    def test_extractor_recovers_the_block(self) -> None:
        parsed = yaml.safe_load(
            extract_frontmatter("---\nname: demo\ndescription: A demo skill.\n---\n\n# Body\n")
        )
        assert parsed == {"name": "demo", "description": "A demo skill."}

    def test_extractor_rejects_a_document_without_frontmatter(self) -> None:
        with pytest.raises(ValueError, match="does not open"):
            extract_frontmatter("# Just a heading\n\nBody text.\n")

    def test_extractor_rejects_an_unclosed_fence(self) -> None:
        with pytest.raises(ValueError, match="never closed"):
            extract_frontmatter("---\nname: demo\n\n# Body\n")

    def test_parser_rejects_a_plain_scalar_with_an_internal_colon(self) -> None:
        """The shipped defect: ``description: … directly: register/edit …``.

        YAML reads the second ``": "`` as a mapping separator inside a plain scalar and the
        whole block fails to parse, taking ``name`` down with it — which is why the skill
        loaded with empty metadata and never triggered.
        """
        broken = (
            "---\n"
            "name: demo\n"
            "description: Manages slots directly: register/edit a conf, post results.\n"
            "---\n"
        )
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(extract_frontmatter(broken))


class TestToolGrantParsing:
    """Backstops proving the ``allowed-tools`` checks can fail.

    All six shipped skills already satisfy them, so applied to the real files these checks
    would look identical to functions that always return ``[]``. Synthetic grants prove each
    detector fires, and that it does not fire on the shapes the skills legitimately use.
    """

    def test_split_respects_parenthesised_scopes(self) -> None:
        assert split_tool_grants("Read, Bash(dataspoke-api *), WebFetch") == [
            "Read",
            "Bash(dataspoke-api *)",
            "WebFetch",
        ]

    def test_split_keeps_a_comma_inside_a_scope_together(self) -> None:
        assert split_tool_grants("Read, Bash(git add, git status)") == [
            "Read",
            "Bash(git add, git status)",
        ]

    def test_unscoped_bash_detector_fires_on_a_bare_grant(self) -> None:
        assert unscoped_bash_grants(split_tool_grants("Read, Bash, WebFetch")) == ["Bash"]

    def test_unscoped_bash_detector_ignores_a_scoped_grant(self) -> None:
        """Without this, the detector could pass above by flagging every Bash grant."""
        assert unscoped_bash_grants(split_tool_grants("Read, Bash(dataspoke-api *)")) == []

    def test_forbidden_tool_detector_fires(self) -> None:
        found = forbidden_tool_grants(split_tool_grants("Read, Bash(kubectl get pods)"))
        assert found == ["Bash(kubectl get pods)"]
        assert forbidden_tool_grants(split_tool_grants("Bash(helm upgrade *)")) == [
            "Bash(helm upgrade *)"
        ]

    def test_forbidden_tool_detector_ignores_unrelated_grants(self) -> None:
        assert forbidden_tool_grants(split_tool_grants("Read, Bash(dataspoke-api *)")) == []


class TestSpecEnumerationsParse:
    """Backstops proving the ``spec/AI_PLUGIN.md`` side of each set-equality is real.

    Both conformance checks below compare a spec-declared set against the repo. If the
    parser degraded to an empty set they would fail — unless the repo side degraded too, so
    each parsed set is separately asserted non-empty here.
    """

    def test_skills_table_is_parsed(self) -> None:
        parsed = spec_skill_names()
        assert parsed, (
            "No rows parsed from spec/AI_PLUGIN.md §Skills — the table format or the "
            "heading changed, so the skill-set conformance check has no spec side."
        )

    def test_bin_helpers_are_parsed(self) -> None:
        parsed = spec_bin_helpers()
        assert parsed, (
            "No `bin/<helper>` names parsed from spec/AI_PLUGIN.md §Packaging — the layout "
            "diagram changed, so the helper conformance checks have no spec side."
        )

    def test_missing_section_fails_loudly(self) -> None:
        """A renamed heading must raise, not yield an empty set that passes vacuously."""
        with pytest.raises(LookupError, match="no section headed"):
            _section_lines("A Heading That Does Not Exist")


class TestSkillSetMatchesSpec:
    """``plugin/skills/*/`` and the ``spec/AI_PLUGIN.md`` §Skills table agree, both ways.

    Spec: spec/AI_PLUGIN.md §Skills ("Six skills, each tracing to routes in `spec/API.md`")
    — the table at :176-183 is the enumeration. Asserting equality rather than a count floor
    catches a skill added without a spec row and a spec row whose directory was renamed,
    neither of which a floor sees.
    """

    def test_skill_directories_match_the_spec_table(self) -> None:
        on_disk = frozenset(skill_ids())
        declared = spec_skill_names()
        assert on_disk, f"No skill directories discovered under {SKILLS_ROOT}"
        assert declared, "No skills parsed from spec/AI_PLUGIN.md §Skills"
        assert on_disk == declared, (
            f"plugin/skills/ and spec/AI_PLUGIN.md §Skills disagree.\n"
            f"  on disk but not in the §Skills table: {sorted(on_disk - declared)}\n"
            f"  in the §Skills table but not on disk: {sorted(declared - on_disk)}\n"
            f"Every shipped skill needs a spec row, and every row needs a skill."
        )

    def test_every_skill_directory_holds_a_skill_md(self) -> None:
        """A directory under ``skills/`` without ``SKILL.md`` is invisible to the glob."""
        missing = sorted(
            path.name
            for path in SKILLS_ROOT.iterdir()
            if path.is_dir() and not (path / "SKILL.md").is_file()
        )
        assert not missing, (
            f"Directories under plugin/skills/ with no SKILL.md: {missing}. Claude Code will "
            f"not load them, and this file's glob-based checks do not see them either."
        )


class TestSkillFrontmatter:
    """Every ``plugin/skills/*/SKILL.md`` carries parseable, addressable metadata."""

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_frontmatter_parses_to_a_non_empty_mapping(self, skill_dir: Path) -> None:
        """Unparseable frontmatter loads the skill with empty metadata — it never triggers."""
        skill_md = skill_dir / "SKILL.md"
        try:
            parsed = load_frontmatter(skill_md)
        except (ValueError, yaml.YAMLError) as exc:
            pytest.fail(
                f"{skill_md.relative_to(REPO_ROOT)}: frontmatter does not parse as YAML "
                f"({exc}). Claude Code loads the skill with empty metadata, so it never "
                f"triggers. A common cause is an unquoted plain scalar containing a second "
                f"': ' — YAML reads it as a mapping separator."
            )
        assert isinstance(parsed, dict), (
            f"{skill_md.relative_to(REPO_ROOT)}: frontmatter parsed to "
            f"{type(parsed).__name__}, expected a mapping of skill metadata."
        )
        assert parsed, f"{skill_md.relative_to(REPO_ROOT)}: frontmatter is an empty mapping."

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_frontmatter_carries_required_keys(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        parsed = load_frontmatter(skill_md)
        assert isinstance(parsed, dict), (
            f"{skill_md.relative_to(REPO_ROOT)}: frontmatter is not a mapping."
        )
        for key in REQUIRED_SKILL_KEYS:
            value = parsed.get(key)
            assert isinstance(value, str) and value.strip(), (
                f"{skill_md.relative_to(REPO_ROOT)}: frontmatter key `{key}` is missing or "
                f"empty (got {value!r}). `name` addresses the skill and `description` is its "
                f"entire trigger surface; without both the skill is never selected."
            )

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_frontmatter_name_matches_directory(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        parsed = load_frontmatter(skill_md)
        assert isinstance(parsed, dict), (
            f"{skill_md.relative_to(REPO_ROOT)}: frontmatter is not a mapping."
        )
        assert parsed.get("name") == skill_dir.name, (
            f"{skill_md.relative_to(REPO_ROOT)}: frontmatter name "
            f"{parsed.get('name')!r} does not match its directory {skill_dir.name!r}."
        )


class TestSkillToolGrantsRespectScope:
    """``allowed-tools`` must not grant reach spec/AI_PLUGIN.md puts out of scope.

    Spec: spec/AI_PLUGIN.md §Scope → "Out of scope — explicitly never touched by any skill:
    Cluster operations (helm, `kubectl`), `src/`, the operational database." ``allowed-tools``
    is where that boundary is actually enforced; the SKILL.md body is only a suggestion.

    The exact per-skill tool list is deliberately not pinned — that would freeze an
    implementation detail and fail on every legitimate edit.
    """

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_allowed_tools_is_declared(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        parsed = load_frontmatter(skill_md)
        value = parsed.get("allowed-tools") if isinstance(parsed, dict) else None
        assert isinstance(value, str) and split_tool_grants(value), (
            f"{skill_md.relative_to(REPO_ROOT)}: `allowed-tools` is missing or empty "
            f"(got {value!r}). It is the enforcement surface for the out-of-scope boundary "
            f"in spec/AI_PLUGIN.md §Scope; an absent one grants the session default."
        )

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_every_bash_grant_is_scoped(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        grants = split_tool_grants(load_frontmatter(skill_md)["allowed-tools"])
        unscoped = unscoped_bash_grants(grants)
        assert not unscoped, (
            f"{skill_md.relative_to(REPO_ROOT)}: unrestricted Bash grant(s) {unscoped}. A "
            f"bare `Bash` reaches the cluster, `src/`, and the operational database, which "
            f"spec/AI_PLUGIN.md §Scope says no skill touches. Scope it, e.g. "
            f"`Bash(dataspoke-api *)`."
        )

    @pytest.mark.parametrize("skill_dir", skill_dirs(), ids=skill_ids())
    def test_no_cluster_tools_are_granted(self, skill_dir: Path) -> None:
        skill_md = skill_dir / "SKILL.md"
        grants = split_tool_grants(load_frontmatter(skill_md)["allowed-tools"])
        forbidden = forbidden_tool_grants(grants)
        assert not forbidden, (
            f"{skill_md.relative_to(REPO_ROOT)}: grants a cluster tool {forbidden}. "
            f'spec/AI_PLUGIN.md §Scope: "Out of scope — explicitly never touched by any '
            f'skill: Cluster operations (helm, `kubectl`)…"'
        )


class TestBinHelpers:
    """``plugin/bin/`` ships exactly the spec'd helpers, each bare-command invokable.

    Spec: spec/AI_PLUGIN.md §Packaging — the layout lists ``bin/dataspoke-api``,
    ``bin/dataspoke-schema`` and ``bin/datahub-graphql``, and "`bin/dataspoke-api` is the
    single I/O primitive for the DataSpoke API … Every skill calls the API through this
    wrapper rather than constructing auth inline."

    Claude Code puts a plugin's ``bin/`` on the Bash tool's PATH, so the helpers are invoked
    as bare commands — which is what every skill's ``Bash(dataspoke-api *)`` grant spells.
    That only works if the file is present, executable, and carries a shebang; the mode bit
    is tracked by git and is therefore losable in a commit, so it is asserted rather than
    assumed.
    """

    def test_bin_directory_matches_the_spec_layout(self) -> None:
        on_disk = frozenset(path.name for path in BIN_ROOT.iterdir() if path.is_file())
        declared = spec_bin_helpers()
        assert on_disk, f"No helpers found in {BIN_ROOT}"
        assert declared, "No helpers parsed from spec/AI_PLUGIN.md §Packaging"
        assert on_disk == declared, (
            f"plugin/bin/ and spec/AI_PLUGIN.md §Packaging disagree.\n"
            f"  on disk but not in the layout: {sorted(on_disk - declared)}\n"
            f"  in the layout but not on disk: {sorted(declared - on_disk)}"
        )

    @pytest.mark.parametrize("helper", bin_helper_ids())
    def test_helper_exists(self, helper: str) -> None:
        path = BIN_ROOT / helper
        assert path.is_file(), (
            f"plugin/bin/{helper} is missing. spec/AI_PLUGIN.md §Packaging lists it, and "
            f"the skills that grant `Bash({helper} *)` invoke it as a bare command."
        )

    @pytest.mark.parametrize("helper", bin_helper_ids())
    def test_helper_is_executable(self, helper: str) -> None:
        path = BIN_ROOT / helper
        assert path.is_file(), f"plugin/bin/{helper} is missing (see test_helper_exists)."
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, (
            f"plugin/bin/{helper} is not executable (mode {stat.filemode(mode)}, "
            f"{mode & 0o777:04o}). Claude Code adds plugin `bin/` to the Bash tool's PATH "
            f"and skills invoke it as a bare command; without the execute bit every such "
            f"call fails with 'permission denied'. git tracks this bit — restore it with "
            f"`chmod +x`."
        )

    @pytest.mark.parametrize("helper", bin_helper_ids())
    def test_helper_declares_a_shebang(self, helper: str) -> None:
        path = BIN_ROOT / helper
        assert path.is_file(), f"plugin/bin/{helper} is missing (see test_helper_exists)."
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!"), (
            f"plugin/bin/{helper} does not open with a `#!` shebang (first line: "
            f"{first_line!r}). Bare-command invocation relies on the kernel picking an "
            f"interpreter from it."
        )


class TestManifestsDeclareNoVersion:
    """Neither manifest pins a ``version``: the plugin is versioned by commit SHA.

    Spec: spec/AI_PLUGIN.md §Distribution ("Neither manifest declares a `version`, so the
    update-cache key resolves to the commit SHA of `./plugin`…").
    """

    def test_plugin_manifest_parses(self) -> None:
        """Backstop for the absence assertion below: the file is real JSON with content."""
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict) and manifest, (
            f"{PLUGIN_MANIFEST.relative_to(REPO_ROOT)} did not parse to a non-empty object."
        )
        # spec/AI_PLUGIN.md §Packaging: `plugin.json` is the "plugin manifest (name
        # "dataspoke", skills)"; §Distribution installs it as `dataspoke@dataspoke`.
        assert manifest.get("name") == "dataspoke", (
            f"{PLUGIN_MANIFEST.relative_to(REPO_ROOT)}: plugin name is "
            f"{manifest.get('name')!r}; spec/AI_PLUGIN.md §Distribution installs it with "
            f"`/plugin install dataspoke@dataspoke`."
        )

    def test_plugin_manifest_declares_no_version(self) -> None:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        assert "version" not in manifest, (
            f"{PLUGIN_MANIFEST.relative_to(REPO_ROOT)} declares "
            f"`version`: {manifest.get('version')!r}. {_VERSION_RATIONALE}"
        )

    def test_marketplace_manifest_parses(self) -> None:
        """Backstop: the marketplace lists the one plugin, sourced at ``./plugin``."""
        manifest = json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))
        assert isinstance(manifest, dict) and manifest, (
            f"{MARKETPLACE_MANIFEST.relative_to(REPO_ROOT)} did not parse to a non-empty object."
        )
        entries = manifest.get("plugins")
        assert isinstance(entries, list) and entries, (
            f"{MARKETPLACE_MANIFEST.relative_to(REPO_ROOT)}: `plugins` is "
            f"{entries!r}; the single-plugin marketplace must list its plugin."
        )
        sources = [entry.get("source") for entry in entries]
        # spec/AI_PLUGIN.md §Distribution names `./plugin` as the source the commit-SHA
        # cache key resolves against.
        assert "./plugin" in sources, (
            f"{MARKETPLACE_MANIFEST.relative_to(REPO_ROOT)}: no entry sourced at './plugin' "
            f"(got {sources}). {_VERSION_RATIONALE}"
        )

    def test_marketplace_manifest_declares_no_version(self) -> None:
        """Top level and every plugin entry — the entry is the cache key's fallback."""
        manifest = json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))
        offenders = []
        if "version" in manifest:
            offenders.append(f"top level → {manifest['version']!r}")
        offenders += [
            f"plugins[{index}] ({entry.get('name')!r}) → {entry['version']!r}"
            for index, entry in enumerate(manifest.get("plugins", []))
            if "version" in entry
        ]
        assert not offenders, (
            f"{MARKETPLACE_MANIFEST.relative_to(REPO_ROOT)} declares `version`: "
            f"{offenders}. {_VERSION_RATIONALE}"
        )
