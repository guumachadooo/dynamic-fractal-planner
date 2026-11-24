#!/usr/bin/env python3
"""Universal SOTA Blueprint Engine: classify, scaffold, Popperian lint/score & handoff.

Zero third-party dependencies (Python stdlib only). Works across any domain:
software, dsp-hw, ai-ml, security, ux-product, infra-ops, data-bi, biz-org.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from schema_migrations import resolve_blueprint_config  # noqa: E402

DOMAIN_PROFILES: dict[str, dict[str, Any]] = {
    "software": {
        "keywords": (
            "api", "backend", "frontend", "microservice", "monolith", "sdk", "cli", "plugin",
            "database", "schema", "cache", "queue", "event", "webhook", "graphql", "rest",
            "parser", "compiler", "state machine", "concurrency", "idempoten", "architecture",
        ),
        "lens": "Enforce explicit contracts, idempotency, bounded latency (p95/p99), backward-compatible schema migration & failure isolation.",
        "zwicky_axes": [
            ("Stateless/Edge vs Stateful/Centralized", "Stateless Edge", "Centralized State"),
            ("OSS/Commodity vs Custom Core", "Battle-Tested OSS", "Custom Differentiator"),
        ],
        "probe_hint": "pytest / unit contract tests + schema compat fixture",
    },
    "dsp-hw": {
        "keywords": (
            "audio", "dsp", "spatial", "atmos", "iamf", "ambisonic", "binaural", "hrtf", "brir",
            "vocal", "stft", "fft", "filter", "kalman", "wpe", "acoustic", "feedback", "scarlett",
            "usb", "uac2", "aaudio", "alsa", "rt ", "real-time", "tempo real", "latency", "wcet",
            "hardware", "fpga", "embedded", "neon", "simd", "npu", "gpu", "vulkan", "therm",
        ),
        "lens": "Split Offline / RT 0-alloc 0-lock hot-path (<1ms/block) / Async control; fuse owned signals/hw; prove math in offline golden bench first.",
        "zwicky_axes": [
            ("Physics/Analytical Math vs Learned Neural", "Analytical Physics/DSP", "Invariant Neural Model"),
            ("Offline Precompute vs Real-Time Control", "Offline Track/Prep Plane", "Hard-RT Zero-Alloc Plane"),
        ],
        "probe_hint": "Offline synthetic golden-bench SNR/ERLE/WCET + 0-alloc static audit",
    },
    "ai-ml": {
        "keywords": (
            "ai", "ia", "ml", "llm", "slm", "model", "modelo", "neural", "onnx", "int4", "fp16",
            "quantiz", "lora", "fine-tune", "rag", "embedding", "vector", "agent", "agente",
            "multi-agent", "onisciente", "omniscient", "classifier", "classificador", "semantic",
            "semântic", "training", "treino", "eval", "benchmark", "inference", "prompt",
        ),
        "lens": "Use AI ONLY where problem invariants generalize (0 per-song/user retraining); bound tokens/VRAM/latency; prove delta vs deterministic heuristic.",
        "zwicky_axes": [
            ("Deterministic Rules vs Compact LLM/ONNX", "Deterministic Rule/Classifier", "Compact Constrained AI Director"),
            ("On-Device Quantized vs Cloud Hybrid", "Local INT4/ONNX Offline", "Optional Cloud API Fallback"),
        ],
        "probe_hint": "Offline eval set vs deterministic baseline + JSON schema/latency gate",
    },
    "security": {
        "keywords": (
            "auth", "oauth", "oidc", "jwt", "hmac", "aes", "crypto", "encrypt", "secret", "key",
            "keystore", "tee", "strongbox", "biometric", "permission", "selinux", "w^x", "sandbox",
            "zero-trust", "stride", "threat", "abuse", "rate-limit", "isolation", "ipc", "token",
        ),
        "lens": "Apply STRIDE across trust boundaries, fail-closed auth/crypto, least privilege, secret redaction & attacker cost-asymmetry.",
        "zwicky_axes": [
            ("Capability/Zero-Trust vs Perimeter Trust", "Zero-Trust Capability Token", "Local IPC Signature Gate"),
            ("Fail-Closed Strict vs Graceful Degraded", "Strict Fail-Closed", "Bounded Safe Fallback"),
        ],
        "probe_hint": "Adversarial tamper/spoof/replay test + secret-scan static gate",
    },
    "ux-product": {
        "keywords": (
            "ui", "ux", "oneui", "screen", "tab", "button", "modal", "dialog", "slider", "canvas",
            "animation", "3d", "visual", "lyrics", "karaoke", "studio", "player", "workflow",
            "onboarding", "accessibility", "l10n", "i18n", "localization", "progressive disclosure",
        ),
        "lens": "Progressive disclosure (single primary CTA, zero jargon), offline/error clarity, <=3 taps to value, 60/120Hz zero-jank & 100% l10n parity.",
        "zwicky_axes": [
            ("Zero-Config Auto vs Pro Manual Control", "1-CTA Zero-Jargon Mode", "Collapsible Pro X-Ray"),
            ("Live Preview First vs Offline Wizard", "Immediate Live Feedback", "Guided Batch Builder"),
        ],
        "probe_hint": "L10n key-parity check + interaction step/jank budget test",
    },
    "infra-ops": {
        "keywords": (
            "ci", "cd", "github actions", "docker", "kubernetes", "cloudflare", "worker", "deploy",
            "release", "ota", "observability", "telemetry", "logging", "monitoring", "alert",
            "backup", "rollback", "daemon", "service", "foreground", "oom", "ram", "memory",
        ),
        "lens": "Design for unattended recovery, idempotent deploy/rollback, OOM/quota ceilings, zero-blind-retry backoff & structured observability.",
        "zwicky_axes": [
            ("Push/Webhook Event vs Pull/ETag Poll", "Event-Driven Wake", "Low-Cost ETag 304 Poll"),
            ("Ephemeral Stateless vs Durable Checkpoint", "Stateless Worker", "Atomic WAL/Checkpoint"),
        ],
        "probe_hint": "Fault-injection / OOM kill recovery + CI parity dry-run",
    },
    "data-bi": {
        "keywords": (
            "etl", "elt", "pipeline", "warehouse", "lake", "parquet", "sql", "sqlite", "postgres",
            "timeseries", "analytics", "lineage", "retention", "dedup", "index", "cache", "manifest",
            "bpm", "metadata", "lyrics", "corpus", "dataset", "ground truth", "forensic",
        ),
        "lens": "Guarantee schema lineage, deterministic replay, idempotent ingestion, bounded storage growth & anti-leakage ground-truth splits.",
        "zwicky_axes": [
            ("Batch Snapshot vs Streaming Incremental", "Immutable Batch Snapshot", "Incremental Delta Stream"),
            ("Raw Audio/Source Derived vs External Lookup", "100% Source-Derived Physics", "Verified External Prior Merge"),
        ],
        "probe_hint": "Idempotent replay + data-integrity checksum / golden corpus check",
    },
    "biz-org": {
        "keywords": (
            "roadmap", "strategy", "pricing", "unit econ", "quota", "budget", "cost", "roi",
            "wardley", "build vs buy", "team", "org", "conway", "onboarding", "governance",
            "compliance", "sla", "slo", "kpi", "okr", "gtm", "adoption", "friction",
        ),
        "lens": "Anchor on Wardley build-vs-commodity, unit economics, Conway slice boundaries, reversibility (1-way vs 2-way) & objective kill KPIs.",
        "zwicky_axes": [
            ("Build Differentiator vs Buy/Reuse Commodity", "Custom Core IP", "Standard Commodity/OSS"),
            ("Big-Bang Launch vs Rolling Thin Wedge", "Thin Wedge Wave-0", "Platform Generalization"),
        ],
        "probe_hint": "KPI / cost-per-unit threshold gate + Wave-0 VoI validation",
    },
}

_ONE_WAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:architect|schema|protocol|wire\s+format|file\s+format|crypto|migration|breaking|public\s+api|abi|selinux|hardware|firmware|kernel|persistent\s+storage|onisciente|omniscient)\b", re.I),
)

_HIGH_RISK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:real-?time|tempo\s+real|zero-?alloc|0-?alloc|feedback|howling|deadlock|race|oom|crash|security|encrypt|biometric|multi-?agent|sota|blueprint|end-?to-?end|onisciente|omniscient|neural|onnx)\b", re.I),
)

_WEASEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fast", re.compile(r"\b(?:fast|faster|ultra-?fast|r[áa]pido|r[áa]pida)\b", re.I)),
    ("robust", re.compile(r"\b(?:robust|robustness|robusto|robusta)\b", re.I)),
    ("seamless", re.compile(r"\b(?:seamless|seamlessly|transparente|transparentemente)\b", re.I)),
    ("graceful", re.compile(r"\b(?:graceful|gracefully)\b", re.I)),
    ("scalable", re.compile(r"\b(?:scalable|scalability|escal[áa]vel)\b", re.I)),
    ("user-friendly", re.compile(r"\b(?:user-?friendly|intuitive|intuitivo|intuitiva|amig[áa]vel)\b", re.I)),
    ("efficient", re.compile(r"\b(?:efficient|efficiently|eficiente|eficientemente)\b", re.I)),
    ("optimal", re.compile(r"\b(?:optimal|optimally|ideal|perfeito|perfeita)\b", re.I)),
    ("cleanly", re.compile(r"\b(?:cleanly|properly|appropriately|adequado|adequada|adequadamente)\b", re.I)),
    ("flexible", re.compile(r"\b(?:flexible|flexibility|flex[íi]vel)\b", re.I)),
    ("easy", re.compile(r"\b(?:easy|easily|f[áa]cil|facilmente)\b", re.I)),
    ("high-perf", re.compile(r"\b(?:high[\s-]+performance|alta\s+performance|alto\s+desempenho)\b", re.I)),
)

_NUMERIC_BOUND_RE = re.compile(
    r"(?:(?:<=|>=|<|>|==|!=|±|\+\/-|~|\bmax\b|\bmin\b|\bp9[059]\b)\s*)?"
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:%|ms|us|µs|ns|s|sec|min|h|hz|khz|mhz|ghz|db|dbtp|lufs|lkfs|lu|mb|gb|kb|tb|b\b|"
    r"fps|tok|tokens|x\b|req/s|ops/s|ppm|deg|°|ch|bit|bits|w\b|mw|usd|\$|items|lines|l\b|files|taps|frames|f\b)",
    re.IGNORECASE,
)

_PLACEHOLDER_RE = re.compile(r"<(?:FILL(?::[^>]*)?|TODO(?::[^>]*)?|TBD(?::[^>]*)?)>", re.IGNORECASE)
_CODE_FILE_IN_H2_RE = re.compile(
    r"(?<![\w/.-])[A-Za-z0-9_./-]+\.(?:cs|py|ts|tsx|js|jsx|kt|java|rs|go|cpp|c|h|swift)(?::\d+)?\b",
    re.IGNORECASE,
)
_CODE_SIG_IN_H2_RE = re.compile(
    r"\b(?:public|private|internal|sealed|class|struct|interface|enum|def|async\s+def|fn|func)\s+[A-Za-z_][A-Za-z0-9_]*",
)


def _slugify(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (raw or "").strip()).strip("_").upper()
    return cleaned[:48] or "UNTITLED"


def _kw_match(text: str, kw: str) -> bool:
    k = kw.strip().lower()
    if not k:
        return False
    if k.endswith(" "):
        return re.search(rf"(?<!\w){re.escape(k.strip())}(?!\w)", text, re.I) is not None
    return re.search(rf"(?<!\w){re.escape(k)}\w*", text, re.I) is not None


def classify_blueprint_request(prompt: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Infer sparse domain vector, active lenses, door type, risk tier, Zwicky axes, & Google-only subagent topology."""
    text = (prompt or "").strip()
    lower = text.lower()

    raw_scores: dict[str, float] = {}
    matched_kws: dict[str, list[str]] = {}
    for dom, spec in DOMAIN_PROFILES.items():
        hits = [kw for kw in spec["keywords"] if _kw_match(lower, kw)]
        if hits:
            raw_scores[dom] = float(len(hits))
            matched_kws[dom] = hits

    if not raw_scores:
        raw_scores = {"software": 1.0}
        matched_kws = {"software": []}

    max_score = max(raw_scores.values()) or 1.0
    domain_vector: dict[str, float] = {}
    for dom, sc in sorted(raw_scores.items(), key=lambda kv: (-kv[1], kv[0])):
        w = round(sc / max_score, 2)
        if w >= 0.25:
            domain_vector[dom] = w

    active_domains = [d for d, w in domain_vector.items() if w >= 0.3][:3]
    if not active_domains:
        active_domains = [next(iter(domain_vector))]

    custom_doms = (profile.get("domains") if isinstance(profile, dict) else None) or {}
    active_lenses: dict[str, str] = {}
    morph_axes: list[dict[str, str]] = []
    probe_hints: list[str] = []
    seen_axes: set[str] = set()

    for dom in active_domains:
        spec = DOMAIN_PROFILES[dom]
        custom_lens = ""
        if isinstance(custom_doms.get(dom), dict):
            custom_lens = str(custom_doms[dom].get("lens") or "").strip()
        lens = (custom_lens or spec["lens"])[:420]
        active_lenses[dom] = lens
        probe_hints.append(f"{dom}: {spec['probe_hint']}")
        for axis_name, pole_a, pole_b in spec["zwicky_axes"]:
            if axis_name not in seen_axes:
                seen_axes.add(axis_name)
                morph_axes.append({"axis": axis_name, "pole_a": pole_a, "pole_b": pole_b})

    is_one_way = any(p.search(text) for p in _ONE_WAY_PATTERNS) or len(active_domains) >= 3
    door_type = "1-way" if is_one_way else "2-way"

    high_risk_hits = sum(1 for p in _HIGH_RISK_PATTERNS if p.search(text))
    if len(active_domains) >= 3 or (is_one_way and high_risk_hits >= 1) or high_risk_hits >= 2:
        risk_tier = "T3"
    elif len(active_domains) >= 2 or is_one_way or high_risk_hits >= 1:
        risk_tier = "T2"
    else:
        risk_tier = "T1"

    solver_count = 3 if risk_tier == "T3" and len(active_domains) >= 3 else 2
    subagent_topology = {
        "policy": "google_only_thin_coordinator|fresh_1_shot|!cmd_echo|!fork|reply<=20L",
        "stage_1_scouts": {
            "count": min(3, max(1, len(active_domains))),
            "model": "flash",
            "leaf_model": "flash_lite",
            "role": "STORM live SOTA + OSS curation + codebase invariant discovery",
        },
        "stage_2_blind_solvers": {
            "count": solver_count,
            "model": "pro",
            "tag": "[L3]",
            "isolated": True,
            "axes": [a["axis"] for a in morph_axes[:solver_count]],
            "role": "Anti-anchoring blind Zwicky morphogenesis + TRIZ contradiction resolution (!see_peer)",
        },
        "stage_3_cold_redteam": {
            "count": 1,
            "model": "pro",
            "tag": "[L3]",
            "round_cap": 1,
            "cold_context": True,
            "role": "Masked-origin Pre-Mortem (T+6mo P0 trace) + Hostile STRIDE/Physics falsifier",
        },
    }

    return {
        "domain_vector": domain_vector,
        "active_domains": active_domains,
        "active_lenses": active_lenses,
        "matched_keywords": {d: matched_kws.get(d, [])[:6] for d in active_domains},
        "door_type": door_type,
        "risk_tier": risk_tier,
        "morph_axes": morph_axes[:4],
        "probe_hints": probe_hints,
        "subagent_topology": subagent_topology,
    }


def scaffold_blueprint(
    title: str,
    slug: str,
    repo: Path,
    *,
    prompt: str = "",
    domains: list[str] | None = None,
    door: str = "",
    tier: Any = 0,
    out_path: str = "",
    profile: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a compact symbolic SOTA blueprint skeleton (`BP_<SLUG>.md`)."""
    cfg = resolve_blueprint_config(profile)
    clean_slug = _slugify(slug or title)
    bp_id = clean_slug if clean_slug.startswith("BP_") else f"BP_{clean_slug}"

    cls = classify_blueprint_request(prompt or title, profile=profile)
    if domains:
        valid = [d.strip().lower() for d in domains if d.strip().lower() in DOMAIN_PROFILES]
        if valid:
            cls["active_domains"] = valid[:3]
            cls["domain_vector"] = {d: round(1.0 / max(1, i + 1), 2) for i, d in enumerate(valid[:3])}
            cls["active_lenses"] = {d: DOMAIN_PROFILES[d]["lens"][:420] for d in valid[:3]}
            cls["morph_axes"] = [
                {"axis": ax[0], "pole_a": ax[1], "pole_b": ax[2]}
                for d in valid[:3]
                for ax in DOMAIN_PROFILES[d]["zwicky_axes"]
            ][:4]

    if door in ("1-way", "2-way"):
        cls["door_type"] = door

    tier_str = cls["risk_tier"]
    if isinstance(tier, int) and tier in (1, 2, 3):
        tier_str = f"T{tier}"
    elif isinstance(tier, str) and tier.strip().upper() in ("T1", "T2", "T3", "1", "2", "3"):
        t = tier.strip().upper()
        tier_str = t if t.startswith("T") else f"T{t}"

    dom_vec_str = ",".join(f"{k}:{v:.2f}" for k, v in cls["domain_vector"].items())
    lens_lines = "\n".join(f"- `{d}`: {txt}" for d, txt in cls["active_lenses"].items())
    axes = cls["morph_axes"] or [{"axis": "Simple vs General", "pole_a": "Thin Wedge", "pole_b": "Full Platform"}]
    ax1 = axes[0]
    ax2 = axes[1] if len(axes) > 1 else axes[0]

    target = Path(out_path).expanduser() if out_path else (repo / str(cfg["dir"]) / f"{bp_id}.md")
    if not target.is_absolute():
        target = (repo / target).resolve()
    if target.exists() and not force:
        raise FileExistsError(f"Blueprint already exists: {target} (pass --force to overwrite)")

    target.parent.mkdir(parents=True, exist_ok=True)
    content = f"""# {title.strip() or bp_id} (`{bp_id}`)

> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`).

## 0. Meta & Lineage
BP_META|id={bp_id}|domain_vec={dom_vec_str}|door={cls['door_type']}|tier={tier_str}|status=SEED-v1|h1_wave=W0
PARENT_ADR|none
{lens_lines}

## 1. Value & Non-Goals
VALUE|<FILL:1L target outcome + measurable user/system delta>
WARDLEY|<FILL:genesis|custom|product|commodity split + build-vs-OSS choice>
NON_GOAL|<FILL:explicit out-of-scope boundary #1>
NON_GOAL|<FILL:explicit out-of-scope boundary #2>

## 2. Live SOTA & Blind Morphogenesis
SOTA_SOURCES|<FILL:papers/OSS/standards audited + why naive defaults fail>
TRIZ_CONTRADICTION|<FILL:improving param vs worsening param -> inventive principle>
CANDIDATE|A|{ax1['pole_a']}|<FILL:architecture, plane split & resource trade-off>
REJECTED_BECAUSE|A|<FILL:quantified flaw or domain invariant failure>
CANDIDATE|B|{ax2['pole_b']}|<FILL:architecture, plane split & resource trade-off>
REJECTED_BECAUSE|B|<FILL:quantified flaw or domain invariant failure>
WINNER|<FILL:chosen hybrid/candidate + why it dominates both poles>

## 3. Cold-Ctx Red-Team, Pre-Mortem & Kill Criteria
PREMORTEM|T+6mo|<FILL:P0 catastrophic failure causal trace>
THREAT|<FILL:STRIDE / physics / OOD / cost-asymmetry attack vector + defense>
KILL|R1|<FILL:risk>|<FILL:numeric kill_expr>|<FILL:obs_window>|<FILL:rollback_or_pivot>
KILL|R2|<FILL:risk>|<FILL:numeric kill_expr>|<FILL:obs_window>|<FILL:rollback_or_pivot>

## 4. Invariants, Budgets & QAS
BUDGET|latency=<FILL:val+unit>|mem=<FILL:val+unit>|cost=<FILL:val+unit>|degraded_floor=<FILL:safe floor>
PLANES|offline=<FILL:prep>|rt=<FILL:0-alloc hot-path>|async=<FILL:control loop>
QAS|Q1|<FILL:stimulus>|<FILL:env>|<FILL:response>|<FILL:numeric_bound+unit>|<FILL:executable_oracle_cmd>
QAS|Q2|<FILL:stimulus>|<FILL:env>|<FILL:response>|<FILL:numeric_bound+unit>|<FILL:executable_oracle_cmd>

## 5. Epistemic Wave-0 Spikes
SPIKE|S1|<FILL:VoI=P(fail)*Blast>|<FILL:1-way unknown hypothesis>|<FILL:numeric pass_gate pre-H1>

## 6. Rolling-Wave DSM Graph
### H1 Atomic Slices
SLICE|W1|<FILL:owned scope>|<FILL:inputs->outputs>|<FILL:fitness_probe>

### H2+ Outcome Stubs
STUB|W2|<FILL:1L target outcome & acceptance gate ONLY (!deep files/signatures)>

## 7. Fitness Functions & Evolution
FITNESS|<FILL:CI/runtime probe + drift verification command>
EVOLVE_RULE|on(kill_trigger || fitness_fail) -> mark SEED/ADR SUPERSEDED && delta-reenter Stage2-3 (!full rewrite)
"""
    target.write_text(content, encoding="utf-8")
    try:
        rel = target.resolve().relative_to(repo.resolve()).as_posix()
    except Exception:
        rel = str(target)
    return {
        "id": bp_id,
        "path": str(target),
        "rel_path": rel,
        "classification": cls,
    }


def _strip_h2_section(text: str) -> tuple[str, list[str]]:
    """Split markdown into full lines and H2+ outcome stub section lines."""
    lines = text.splitlines()
    h2_lines: list[str] = []
    in_h2 = False
    for ln in lines:
        s = ln.strip()
        if re.match(r"^#{2,4}\s+H2\+", s, re.I):
            in_h2 = True
            continue
        if in_h2 and re.match(r"^#{1,3}\s+(?!H2\+)", s):
            in_h2 = False
        if in_h2 and s:
            h2_lines.append(s)
    return text, h2_lines


def _has_real_content(val: str) -> bool:
    s = (val or "").strip()
    return bool(s) and not _PLACEHOLDER_RE.search(s)


def lint_blueprint(
    path: Path,
    *,
    strict: bool = False,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministically score & audit a SOTA blueprint (0-100 Popperian rubric)."""
    cfg = resolve_blueprint_config(profile)
    min_score = int(cfg.get("min_score") or 85)
    max_lines = int(cfg.get("max_lines") or 350)
    max_kb = float(cfg.get("max_kb") or 24)
    max_h2_ratio = float(cfg.get("max_h2_ratio") or 0.25)

    p = Path(path)
    if not p.is_file():
        return {
            "path": str(p),
            "score": 0,
            "passed": False,
            "threshold": min_score,
            "subscores": {},
            "issues": [f"CRITICAL|File not found: `{p}`"],
            "blockers": [f"File not found: `{p}`"],
            "metrics": {},
        }

    text = p.read_text(encoding="utf-8", errors="ignore")
    size_kb = p.stat().st_size / 1024.0
    lines = text.splitlines()
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    issues: list[str] = []
    blockers: list[str] = []

    def _add_issue(code: str, msg: str, blocker: bool = False) -> None:
        issues.append(f"{code}|{msg}")
        if blocker:
            blockers.append(f"{code}|{msg}")

    # 1. META (10): BP_META + VALUE + WARDLEY + >=2 NON_GOAL
    bp_meta_line = next((ln for ln in nonempty if ln.startswith("BP_META|")), "")
    meta_fields: dict[str, str] = {}
    for tok in bp_meta_line.split("|")[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            meta_fields[k.strip()] = v.strip()

    req_meta = ("id", "domain_vec", "door", "tier", "status")
    has_all_meta = all(_has_real_content(meta_fields.get(k, "")) for k in req_meta)
    value_lines = [ln for ln in nonempty if ln.startswith("VALUE|") and _has_real_content(ln[6:])]
    wardley_lines = [ln for ln in nonempty if ln.startswith("WARDLEY|") and _has_real_content(ln[8:])]
    non_goals = [ln for ln in nonempty if ln.startswith("NON_GOAL|") and _has_real_content(ln[9:])]

    s_meta = 0
    if has_all_meta:
        s_meta += 5
    else:
        _add_issue("META", "Missing/incomplete `BP_META|id|domain_vec|door|tier|status`.", blocker=True)
    if value_lines and wardley_lines and len(non_goals) >= 2:
        s_meta += 5
    elif value_lines and len(non_goals) >= 1:
        s_meta += 3
        _add_issue("META", "Need `VALUE|`, `WARDLEY|`, and >=2 `NON_GOAL|` lines.")
    else:
        _add_issue("META", "Missing `VALUE|`, `WARDLEY|`, or >=2 `NON_GOAL|` lines.", blocker=True)

    # 2. ORI (15): >=2 orthogonal REJECTED_BECAUSE| + >=2 CANDIDATE| + WINNER|
    candidates = [ln for ln in nonempty if ln.startswith("CANDIDATE|") and _has_real_content(ln)]
    rejects = [ln for ln in nonempty if ln.startswith("REJECTED_BECAUSE|") and _has_real_content(ln.split("|", 2)[-1])]
    winners = [ln for ln in nonempty if ln.startswith("WINNER|") and _has_real_content(ln[7:])]
    s_ori = 0
    if len(rejects) >= 2 and len(candidates) >= 2 and winners:
        s_ori = 15
    elif len(rejects) >= 1 and (len(candidates) >= 1 or winners):
        s_ori = 7
        _add_issue("ORI", f"Need >=2 `CANDIDATE|` + >=2 `REJECTED_BECAUSE|` + `WINNER|` (found rejects={len(rejects)}).", blocker=True)
    else:
        _add_issue("ORI", "Missing blind morphogenesis (`>=2 CANDIDATE|` + `>=2 REJECTED_BECAUSE|` + `WINNER|`).", blocker=True)

    # 3. IDS (20): QAS numeric bound+unit && executable oracle + BUDGET
    qas_lines = [ln for ln in nonempty if ln.startswith("QAS|")]
    budget_lines = [ln for ln in nonempty if ln.startswith("BUDGET|") and _has_real_content(ln) and _NUMERIC_BOUND_RE.search(ln)]
    valid_qas = 0
    for q in qas_lines:
        parts = [pt.strip() for pt in q.split("|")]
        if len(parts) < 7:
            _add_issue("IDS", f"Malformed QAS tuple (<7 fields): `{q[:60]}`.", blocker=True)
            continue
        bound_field, oracle_field = parts[5], parts[6]
        has_num = bool(_NUMERIC_BOUND_RE.search(bound_field)) and _has_real_content(bound_field)
        has_oracle = _has_real_content(oracle_field) and len(oracle_field) >= 3
        if has_num and has_oracle:
            valid_qas += 1
        else:
            _add_issue("IDS", f"QAS `{parts[1]}` missing numeric bound+unit or oracle cmd.", blocker=True)

    if not qas_lines:
        s_ids = 0
        _add_issue("IDS", "No `QAS|` falsifiable quality attribute scenarios found.", blocker=True)
    else:
        qas_score = round(16.0 * (valid_qas / len(qas_lines)))
        budget_score = 4 if budget_lines else 0
        if not budget_lines:
            _add_issue("IDS", "Missing numeric `BUDGET|` envelope.", blocker=True)
        s_ids = min(20, qas_score + budget_score)

    # 4. WBS (10): Weasel-word ban on normative lines unless paired w/ numeric bound
    normative_prefixes = (
        "VALUE|", "NON_GOAL|", "WINNER|", "QAS|", "BUDGET|",
        "SPIKE|", "SLICE|", "STUB|", "FITNESS|", "KILL|", "THREAT|",
    )
    weasel_hits: list[str] = []
    for ln in nonempty:
        if not ln.startswith(normative_prefixes) or _PLACEHOLDER_RE.search(ln):
            continue
        if _NUMERIC_BOUND_RE.search(ln):
            continue
        for label, pat in _WEASEL_PATTERNS:
            if pat.search(ln):
                weasel_hits.append(f"{label} in `{ln[:55]}`")
                break
    if not weasel_hits:
        s_wbs = 10
    elif len(weasel_hits) == 1:
        s_wbs = 5
        _add_issue("WBS", f"Unqualified weasel word w/o numeric bound: {weasel_hits[0]}", blocker=True)
    else:
        s_wbs = 0
        _add_issue("WBS", f"{len(weasel_hits)} unqualified weasel words w/o numeric bounds ({weasel_hits[0]})", blocker=True)

    # 5. KCC (15): PREMORTEM| + >=2 KILL| w/ trigger+action
    premortems = [ln for ln in nonempty if ln.startswith("PREMORTEM|") and _has_real_content(ln.split("|", 2)[-1])]
    kill_lines = [ln for ln in nonempty if ln.startswith("KILL|")]
    valid_kills = 0
    for kl in kill_lines:
        parts = [pt.strip() for pt in kl.split("|")]
        if len(parts) >= 6 and all(_has_real_content(x) for x in parts[1:6]):
            valid_kills += 1
        else:
            _add_issue("KCC", f"Malformed `KILL|` (need 6 fields): `{kl[:55]}`.", blocker=True)
    if premortems and valid_kills >= 2:
        s_kcc = 15
    elif premortems or valid_kills >= 1:
        s_kcc = 7
        _add_issue("KCC", f"Need `PREMORTEM|` and >=2 complete `KILL|` triggers (found valid_kills={valid_kills}).", blocker=True)
    else:
        s_kcc = 0
        _add_issue("KCC", "Missing `PREMORTEM|` and `KILL|` abort circuits.", blocker=True)

    # 6. SPIKE (10): >=1 SPIKE| on T2/T3 or 1-way
    tier_val = meta_fields.get("tier", "T2").upper()
    door_val = meta_fields.get("door", "1-way").lower()
    needs_spike = (tier_val in ("T2", "T3")) or (door_val == "1-way")
    spikes = [ln for ln in nonempty if ln.startswith("SPIKE|")]
    valid_spikes = sum(
        1 for sp in spikes if len(sp.split("|")) >= 5 and all(_has_real_content(x) for x in sp.split("|")[1:5])
    )
    if valid_spikes >= 1 or not needs_spike:
        s_spike = 10
    else:
        s_spike = 0
        _add_issue("SPIKE", f"T{tier_val.lstrip('T')}/{door_val} blueprint requires >=1 `SPIKE|` gate pre-H1.", blocker=True)

    # 7. AWF (10): H2+ ratio <= max_h2_ratio & 0 deep code files/signatures in H2+
    _, h2_lines = _strip_h2_section(text)
    h1_slices = [ln for ln in nonempty if ln.startswith("SLICE|") and _has_real_content(ln)]
    h2_ratio = round(len(h2_lines) / max(1, len(nonempty)), 3)
    h2_code_leaks = [
        ln for ln in h2_lines
        if _CODE_FILE_IN_H2_RE.search(ln) or _CODE_SIG_IN_H2_RE.search(ln)
    ]
    s_awf = 10
    if not h1_slices:
        s_awf -= 4
        _add_issue("AWF", "Missing `SLICE|` atomic execution steps in `### H1 Atomic Slices`.", blocker=True)
    if h2_ratio > max_h2_ratio:
        s_awf -= 6
        _add_issue("AWF", f"Premature Waterfall: `H2+` ratio {h2_ratio:.2f} > {max_h2_ratio:.2f} (keep H2+ as 1L outcome stubs).", blocker=True)
    if h2_code_leaks:
        s_awf = max(0, s_awf - 6)
        _add_issue("AWF", f"Premature Waterfall: deep code/signature inside `H2+` (`{h2_code_leaks[0][:55]}`).", blocker=True)

    # 8. CTB (10): 0 placeholders + <= max_lines + <= max_kb
    placeholders = _PLACEHOLDER_RE.findall(text)
    s_ctb = 10
    if placeholders:
        s_ctb = 0
        _add_issue("CTB", f"{len(placeholders)} unfilled placeholder(s) (e.g. `{placeholders[0]}`).", blocker=True)
    elif len(lines) > max_lines or size_kb > max_kb:
        s_ctb = 3
        _add_issue("CTB", f"Context bloat: {len(lines)}L / {size_kb:.1f} KB exceeds limit ({max_lines}L / {max_kb:.0f} KB).", blocker=True)

    subscores = {
        "META": s_meta,
        "ORI": s_ori,
        "IDS": s_ids,
        "WBS": s_wbs,
        "KCC": s_kcc,
        "SPIKE": s_spike,
        "AWF": s_awf,
        "CTB": s_ctb,
    }
    total_score = sum(subscores.values())
    score_passed = (total_score >= min_score) and (len(placeholders) == 0)
    passed = (score_passed and len(blockers) == 0) if strict else score_passed

    return {
        "path": str(p),
        "id": meta_fields.get("id") or p.stem,
        "status": meta_fields.get("status") or "UNKNOWN",
        "door": door_val,
        "tier": tier_val,
        "score": total_score,
        "passed": passed,
        "strict": strict,
        "threshold": min_score,
        "subscores": subscores,
        "issues": issues,
        "blockers": blockers,
        "metrics": {
            "lines": len(lines),
            "kb": round(size_kb, 2),
            "h2_ratio": h2_ratio,
            "qas_total": len(qas_lines),
            "qas_valid": valid_qas,
            "rejects": len(rejects),
            "kills_valid": valid_kills,
            "spikes_valid": valid_spikes,
            "placeholders": len(placeholders),
            "weasel_hits": len(weasel_hits),
        },
    }


def list_blueprints(repo: Path, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Discover and score all `BP_*.md` / `blueprints/*.md` docs in `repo`."""
    cfg = resolve_blueprint_config(profile)
    bp_dir = repo / str(cfg.get("dir") or ".agents/docs/blueprints")
    seen: set[Path] = set()
    candidates: list[Path] = []
    for base, pat in ((bp_dir, "*.md"), (repo / ".agents" / "docs", "BP_*.md")):
        if base.is_dir():
            for f in sorted(base.glob(pat)):
                if f.name.lower() == "readme.md":
                    continue
                rf = f.resolve()
                if rf not in seen:
                    seen.add(rf)
                    candidates.append(f)

    out: list[dict[str, Any]] = []
    for f in candidates:
        res = lint_blueprint(f, strict=bool(cfg.get("strict_verify", False)), profile=profile)
        try:
            rel = f.resolve().relative_to(repo.resolve()).as_posix()
        except Exception:
            rel = str(f)
        out.append({
            "rel_path": rel,
            "id": res["id"],
            "status": res["status"],
            "door": res["door"],
            "tier": res["tier"],
            "score": res["score"],
            "passed": res["passed"],
            "issues_count": len(res["issues"]),
        })
    return out


def build_handoff(path: Path) -> dict[str, Any]:
    """Extract Wave-0 spikes, H1 slices & QAS into a ready `plan_tool.py init` command."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Blueprint not found: {p}")
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    bp_meta = next((ln for ln in lines if ln.startswith("BP_META|")), "")
    bp_id = p.stem
    tier_num = 3
    for tok in bp_meta.split("|")[1:]:
        if tok.startswith("id="):
            bp_id = tok[3:].strip()
        elif tok.startswith("tier="):
            m = re.search(r"([123])", tok[5:])
            if m:
                tier_num = int(m.group(1))

    val = next((ln[6:].strip() for ln in lines if ln.startswith("VALUE|") and _has_real_content(ln[6:])), "")
    goal = f"{bp_id}: {val}" if val else f"Execute Wave-0/H1 of {bp_id}"
    spikes = [ln for ln in lines if ln.startswith("SPIKE|") and _has_real_content(ln)]
    slices = [ln for ln in lines if ln.startswith("SLICE|") and _has_real_content(ln)]
    qas = [ln for ln in lines if ln.startswith("QAS|") and _has_real_content(ln)]

    reqs: list[str] = []
    for sp in spikes[:2]:
        parts = sp.split("|")
        reqs.append(f"{parts[1]}: {parts[3]} -> {parts[4]}" if len(parts) >= 5 else sp)
    for q in qas[:4]:
        parts = q.split("|")
        reqs.append(f"{parts[1]}: {parts[4]} ({parts[5]})" if len(parts) >= 6 else q)
    for sl in slices[:3]:
        parts = sl.split("|")
        reqs.append(f"{parts[1]}: {parts[2]} -> {parts[4]}" if len(parts) >= 5 else sl)

    q_goal = goal.replace('"', "'")[:140]
    req_flags = " ".join(f'--req "{r.replace(chr(34), chr(39))[:110]}"' for r in (reqs[:6] or [f"Execute {bp_id} H1"]))
    modules = "MOD-SOTA-PREFLIGHT,MOD-CONTRACT,MOD-IMPL,MOD-AUDIT" if tier_num == 3 else "MOD-CONTRACT,MOD-IMPL,MOD-AUDIT"
    init_cmd = f'python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py init --goal "{q_goal}" --tier {tier_num} --modules {modules} {req_flags}'

    return {
        "id": bp_id,
        "goal": goal,
        "tier": tier_num,
        "spikes": spikes,
        "h1_slices": slices,
        "qas": qas,
        "reqs": reqs,
        "plan_init_cmd": init_cmd,
    }


def check_blueprint_files(
    repo: Path,
    files: list[str],
    profile: dict[str, Any] | None,
    fails: list[str],
    warns: list[str],
) -> None:
    """Static verifier hook: lint changed SOTA blueprint files (`BP_*.md` / `blueprints/*.md`)."""
    cfg = resolve_blueprint_config(profile)
    strict_verify = bool(cfg.get("strict_verify", False))
    bp_pat = re.compile(r"(?:^|/)(?:blueprints/[^/]+\.md|BP_[^/]+\.md)$", re.IGNORECASE)
    for rel in files:
        norm = rel.replace("\\", "/").lstrip("/")
        while norm.startswith("./"):
            norm = norm[2:]
        if "/archive/" in norm or not bp_pat.search(norm) or norm.lower().endswith("/readme.md"):
            continue
        full = repo / norm
        if not full.is_file():
            continue
        res = lint_blueprint(full, strict=True, profile=profile)
        if not res["passed"]:
            top_issues = "; ".join(res["issues"][:3])
            msg = f"{norm}: SOTA blueprint score={res['score']}/100 (<{res['threshold']} or blockers) -> {top_issues}"
            status = str(res.get("status") or "").upper()
            if strict_verify or status in ("LOCKED", "ACTIVE", "APPROVED"):
                fails.append(msg)
            else:
                warns.append(msg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Universal SOTA Blueprint Engine.")
    parser.add_argument("--workspace", default="", help="Workspace root override.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cls = sub.add_parser("classify", help="Classify prompt into domain vector, door type, Zwicky axes & subagent topology.")
    p_cls.add_argument("--prompt", required=True)
    p_cls.add_argument("--json", action="store_true")

    p_init = sub.add_parser("init", help="Scaffold a symbolic SOTA blueprint skeleton (`BP_<SLUG>.md`).")
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--slug", default="")
    p_init.add_argument("--prompt", default="")
    p_init.add_argument("--domains", nargs="*", default=None)
    p_init.add_argument("--door", choices=["1-way", "2-way", ""], default="")
    p_init.add_argument("--tier", type=int, choices=[1, 2, 3], default=None)
    p_init.add_argument("--out", default="")
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--json", action="store_true")

    for cname in ("lint", "score"):
        p_l = sub.add_parser(cname, help="Run deterministic 0-100 Popperian rubric audit on a blueprint.")
        p_l.add_argument("path")
        p_l.add_argument("--strict", action="store_true")
        p_l.add_argument("--json", action="store_true")

    for lname in ("status", "list"):
        p_ls = sub.add_parser(lname, help="List and score workspace SOTA blueprints.")
        p_ls.add_argument("--json", action="store_true")

    p_ho = sub.add_parser("handoff", help="Extract Wave-0/H1/QAS into a `plan_tool.py init` execution command.")
    p_ho.add_argument("path")
    p_ho.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    profile_path = ws / ".agents" / "planner_profile.json"
    profile: dict[str, Any] = {}
    if profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            profile = {}

    if args.cmd == "classify":
        res = classify_blueprint_request(args.prompt, profile=profile)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            dv = ",".join(f"{k}:{v:.2f}" for k, v in res["domain_vector"].items())
            print(f"BLUEPRINT|domains={dv}|door={res['door_type']}|tier={res['risk_tier']}")
            for d, l in res["active_lenses"].items():
                print(f"  LENS|{d}: {l}")
            print("  MORPH_AXES|" + " ; ".join(a["axis"] for a in res["morph_axes"]))
        return 0

    if args.cmd == "init":
        try:
            res = scaffold_blueprint(
                args.title,
                args.slug,
                ws,
                prompt=args.prompt,
                domains=args.domains,
                door=args.door,
                tier=args.tier or 0,
                out_path=args.out,
                profile=profile,
                force=bool(args.force),
            )
        except FileExistsError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            cls = res["classification"]
            print(f"Created SOTA blueprint `{res['rel_path']}` (door={cls['door_type']}, tier={cls['risk_tier']})")
        return 0

    if args.cmd in ("lint", "score"):
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = (ws / target).resolve()
        res = lint_blueprint(target, strict=bool(args.strict), profile=profile)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            verdict = "PASS" if res["passed"] else "FAIL"
            subs = " ".join(f"{k}:{v}" for k, v in res["subscores"].items())
            print(f"BLUEPRINT {verdict} | {res['id']} | score={res['score']}/100 (min={res['threshold']}) | [{subs}]")
            for iss in res["issues"]:
                print(f"  - {iss}")
        return 0 if res["passed"] else 1

    if args.cmd in ("status", "list"):
        items = list_blueprints(ws, profile=profile)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        elif not items:
            print("No SOTA blueprints found in workspace.")
        else:
            for it in items:
                tag = "PASS" if it["passed"] else "FAIL"
                print(f"- [{tag} {it['score']:3d}/100] {it['id']} ({it['door']}, {it['tier']}, {it['status']}) -> {it['rel_path']}")
        return 0

    if args.cmd == "handoff":
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = (ws / target).resolve()
        try:
            res = build_handoff(target)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"HANDOFF|{res['id']}|tier=T{res['tier']}|spikes={len(res['spikes'])}|h1={len(res['h1_slices'])}|qas={len(res['qas'])}")
            print(res["plan_init_cmd"])
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
