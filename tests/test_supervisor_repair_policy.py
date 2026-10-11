"""Guard the normative S0/S1 Supervisor repair boundary against doc drift.

The classification requires human judgment. These tests pin the binding Skill,
runtime protocol, and user-facing explanations, not a pretend automatic tier
classifier that could silently authorize a product write.
"""

from pathlib import Path

from conftest import SKILL_ROOT


def _read(relative_path: str) -> str:
    return (SKILL_ROOT / relative_path).read_text(encoding="utf-8")


def test_skill_grants_self_classification_but_only_s0_direct_repair():
    skill = _read("SKILL.md")
    section = skill.split(
        "## Supervisor repair triage: S0 versus S1", 1
    )[1].split("\n## ", 1)[0]
    assert "exactly one of two levels" in section
    assert "Classification\nis S's own judgment" in section
    assert "S **MAY and SHOULD fix S0 directly**" in section
    assert "even with `execution_owner=executor`" in section
    assert "S **MUST NOT implement S1 itself**" in section
    assert "normal native\n  `psc_invoke_executor` path" in section
    assert "For ambiguous or mixed findings, classify as S1" in section


def test_micro_product_edits_are_s1_even_if_trivial():
    skill = _read("SKILL.md")
    assert "**S1 — product micro-fix or normal development rework.**" in skill
    assert "however short or obvious" in skill
    assert "Never relabel\n  a semantic edit as S0" in skill
    assert "Never switch owners solely\nbecause an S1 fix looks easy." in skill


def test_s0_preserves_attempt_authority_and_cannot_race_executor():
    skill = _read("SKILL.md")
    protocol = _read("references/runtime-protocol.md")
    assert "S0 is forbidden while an Executor attempt is\nrunning" in skill
    assert "zero** E quality/abnormal retries" in skill
    assert "does not reset\nbudgets" in skill
    assert "S0 is **never** concurrent with running/possibly running E" in protocol
    assert "a cancelled or\ndisconnected MCP invocation is not proof" in protocol
    assert "There is no S2 classification." in protocol


def test_existing_explicit_owner_handoff_is_not_tier_escape():
    skill = _read("SKILL.md")
    protocol = _read("references/runtime-protocol.md")
    assert "existing **explicitly user-authorized**" in skill
    assert "not a route for S to\nunilaterally perform S1" in skill
    assert "no discretionary tier classification changes ownership" in protocol
    assert "explicit user decision" in protocol


def test_readmes_explain_same_two_levels():
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    assert "S can directly fix **S0 only**" in english
    assert "**S1 always goes to Executor" in english
    assert "Supervisor 自行裁量修复等级，只有两级" in chinese
    assert "**S0（整理性修复）**" in chinese
    assert "**S1（微型产品修复 + 正常开发返工）**" in chinese
