#!/usr/bin/env python3
"""Universal SOTA Blueprint Engine: classify, scaffold, Popperian lint/score & handoff.

Zero third-party dependencies (Python stdlib only). Works across any domain:
software, dsp-hw, ai-ml, security, ux-product, infra-ops, data-bi, biz-org.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
from datetime import datetime, timezone
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
            "refactor", "modular", "split", "partial", "linter", "rubric", "blueprint",
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
            "vocal", "stft", "fft", "filter", "kalman", "wpe", "acoustic", "feedback", "howling",
            "scarlett", "duplex", "usb", "uac2", "aaudio", "alsa", "rt ", "real-time", "tempo real",
            "latency", "wcet", "hardware", "fpga", "embedded", "neon", "simd", "npu", "gpu", "vulkan", "therm",
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
            "ui", "ux", "oneui", "screen", "tab", "button", "botão", "botao", "modal", "dialog",
            "slider", "toggle", "switch", "settings", "configura", "canvas", "animation", "3d",
            "visual", "blur", "lyrics", "karaoke", "studio", "player", "workflow", "label",
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
    re.compile(r"\b(?:architect|schema|protocol|wire\s+format|file\s+format|crypto|migration|breaking|public\s+api|abi|selinux|hardware|firmware|kernel|persistent\s+storage|onisciente|omniscient|duplex)\b", re.I),
)

_HIGH_RISK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:real-?time|tempo\s+real|zero-?alloc|0-?alloc|feedback|howling|deadlock|race|oom|crash|security|encrypt|biometric|multi-?agent|sota|blueprint|end-?to-?end|onisciente|omniscient|neural|onnx)\b", re.I),
)

_WEASEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fast", re.compile(r"(?<!-)\b(?:fast|faster|ultra-?fast|r[áa]pido|r[áa]pida)\b", re.I)),
    ("robust", re.compile(r"(?<!-)\b(?:robust|robustness|robusto|robusta)\b", re.I)),
    ("seamless", re.compile(r"(?<!-)\b(?:seamless|seamlessly|transparente|transparentemente)\b", re.I)),
    ("graceful", re.compile(r"(?<!-)\b(?:graceful|gracefully)\b", re.I)),
    ("scalable", re.compile(r"(?<!-)\b(?:scalable|scalability|escal[áa]vel)\b", re.I)),
    ("user-friendly", re.compile(r"(?<!-)\b(?:user-?friendly|intuitive|intuitivo|intuitiva|amig[áa]vel)\b", re.I)),
    ("efficient", re.compile(r"(?<!-)\b(?:efficient|efficiently|eficiente|eficientemente)\b", re.I)),
    ("optimal", re.compile(r"(?<!-)\b(?:optimal|optimally|ideal|perfeito|perfeita)\b", re.I)),
    ("cleanly", re.compile(r"(?<!-)\b(?:cleanly|properly|appropriately|adequado|adequada|adequadamente)\b", re.I)),
    ("flexible", re.compile(r"(?<!-)\b(?:flexible|flexibility|flex[íi]vel)\b", re.I)),
    ("easy", re.compile(r"(?<!-)\b(?:easy|easily|f[áa]cil|facilmente)\b", re.I)),
    ("high-perf", re.compile(r"(?<!-)\b(?:high[\s-]+performance|alta\s+performance|alto\s+desempenho)\b", re.I)),
)

_NUMERIC_BOUND_RE = re.compile(
    r"(?:"
    r"(?:(?:<=|>=|<|>|==|!=|±|\+\/-|~|\bmax\b|\bmin\b|\bp9[059]\b)\s*)?"
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:%|ms|us|µs|ns|s|sec|secs|min|h|hz|khz|mhz|ghz|db|dbtp|lufs|lkfs|lu|mb|gb|kb|tb|b\b|"
    r"fps|tok|tokens|x\b|req/s|ops/s|ppm|deg|°|ch|bit|bits|w\b|mw|usd|\$|items|lines|l\b|files|"
    r"taps|frames|f\b|pts|pt\b|members|subagents|calls|errors|warnings|blocks|samples|retries)"
    r"|(?:<=|>=|==|!=|<|>)\s*-?\d+(?:[.,]\d+)?"
    r")",
    re.IGNORECASE,
)

_COMPARATOR_OR_NUM_RE = re.compile(
    r"(?:<=|>=|==|!=|<|>)\s*\S+|\d+(?:[.,]\d+)?\s*(?:%|ms|us|s|sec|min|h|mb|gb|kb|db|hz|fps|tok|x\b|files|lines|items|calls|errors)",
    re.IGNORECASE,
)

_CODE_FILE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_./-]+\.(?:cs|py|ts|tsx|js|jsx|kt|java|rs|go|cpp|c|h|swift|md|json|yml|yaml)\b", re.I)
_CLI_FLAG_RE = re.compile(r"(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*")
_BACKTICK_SPAN_RE = re.compile(r"`[^`\n]+`")

_DUMMY_ORACLES = frozenset({
    "ok", "pass", "true", "none", "n/a", "na", "tbd", "todo", "echo ok", "echo pass", "<cmd>", "manual",
})
_DUMMY_ECHO_RE = re.compile(r"^echo\s+[A-Za-z0-9_.-]+$", re.I)

_PLACEHOLDER_RE = re.compile(r"<(?:FILL(?::[^>]*)?|TODO(?::[^>]*)?|TBD(?::[^>]*)?)>", re.IGNORECASE)
_CODE_FILE_IN_H2_RE = re.compile(
    r"(?<![\w/.-])[A-Za-z0-9_./-]+\.(?:cs|py|ts|tsx|js|jsx|kt|java|rs|go|cpp|c|h|swift)(?::\d+)?\b",
    re.IGNORECASE,
)
_CODE_SIG_IN_H2_RE = re.compile(
    r"\b(?:public|private|internal|sealed|class|struct|interface|enum|def|async\s+def|fn|func)\s+[A-Za-z_][A-Za-z0-9_]*",
)

VALID_FORMATS = ("mini", "compact", "full")


def _split_pipe_fields(line: str) -> list[str]:
    """Split a blueprint record line on single `|` delimiters while ignoring `||` and pipes inside backticks."""
    parts: list[str] = []
    buf: list[str] = []
    in_bt = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            in_bt = not in_bt
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and not in_bt:
            if i + 1 < n and line[i + 1] == "|":
                buf.append("||")
                i += 2
                continue
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return parts


def _is_substantive_reason(text: str) -> bool:
    s = (text or "").strip()
    if not _has_real_content(s) or len(s) < 12:
        return False
    words = re.findall(r"[A-Za-z0-9_]{2,}", s)
    return len(words) >= 3 or bool(_COMPARATOR_OR_NUM_RE.search(s) or _NUMERIC_BOUND_RE.search(s))


def _is_substantive_kill_expr(text: str) -> bool:
    s = (text or "").strip()
    if not _has_real_content(s):
        return False
    return bool(_COMPARATOR_OR_NUM_RE.search(s) or _NUMERIC_BOUND_RE.search(s) or re.search(r"\d", s))


def _is_substantive_oracle(text: str) -> bool:
    s = (text or "").strip().strip("`'\"")
    if not _has_real_content(s) or len(s) < 5:
        return False
    low = s.lower()
    if low in _DUMMY_ORACLES or _DUMMY_ECHO_RE.match(low):
        return False
    return True


def _weasel_hits_in_line(line: str, window: int = 90) -> list[str]:
    """Find unqualified weasel words in a normative line using a clause-local numeric window (ignoring backticks & `--flags`)."""
    # For structured KILL|id|risk|trigger|probe|mitigation rows, probe sits between trigger and mitigation;
    # evaluate with trigger adjacent to mitigation so a quantified trigger bounds immediate fallback phrasing.
    eval_line = line
    if line.lstrip("-* ").startswith("KILL|"):
        parts = _split_pipe_fields(line.lstrip("-* "))
        if len(parts) >= 6:
            eval_line = " | ".join([parts[2], parts[3], parts[5]])
    # Mask backticked code spans and CLI flags with same-length spaces so offsets stay aligned
    masked = _BACKTICK_SPAN_RE.sub(lambda m: " " * len(m.group(0)), eval_line)
    masked = _CLI_FLAG_RE.sub(lambda m: " " * len(m.group(0)), masked)
    hits: list[str] = []
    for label, pat in _WEASEL_PATTERNS:
        for m in pat.finditer(masked):
            s, e = m.start(), m.end()
            local_slice = eval_line[max(0, s - window) : min(len(eval_line), e + window)]
            if not _NUMERIC_BOUND_RE.search(local_slice):
                hits.append(f"{label} in `{line[:55]}`")
                break
        if hits:
            break
    return hits


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
    """Infer sparse domain vector, active lenses, door type, risk tier, format (`mini|compact|full`), Zwicky axes, & right-sized Google-only subagent topology."""
    text = (prompt or "").strip()
    # Strip code filenames (e.g., StudioBatchExporter.cs) before keyword matching so filename stems don't hijack domain classification
    cleaned_for_dom = _CODE_FILE_TOKEN_RE.sub(" ", text)
    lower = cleaned_for_dom.lower()

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

    cfg = resolve_blueprint_config(profile)
    use_tiered = bool(cfg.get("tiered_formats", True))
    if not use_tiered or risk_tier == "T3":
        rec_format = "full"
    elif risk_tier == "T2":
        rec_format = "compact"
    else:
        rec_format = "mini" if door_type == "2-way" else "compact"

    # Right-size subagent topology to (risk_tier, door_type) to avoid T3 ceremony on T1/T2 2-way tasks
    if rec_format == "mini":
        scout_count, solver_count, redteam_count = 0, 0, 0
        solver_model, redteam_model = "inline", "inline"
    elif rec_format == "compact" and door_type == "2-way":
        scout_count, solver_count, redteam_count = 1, 0, 1
        solver_model, redteam_model = "inline", "flash"
    else:
        scout_count = min(3, max(1, len(active_domains)))
        solver_count = 3 if risk_tier == "T3" and len(active_domains) >= 3 else 2
        redteam_count = 1
        solver_model, redteam_model = "pro", "pro"

    subagent_topology = {
        "policy": "google_only_thin_coordinator|fresh_1_shot|!cmd_echo|!fork|reply<=20L",
        "format": rec_format,
        "total_subagents": scout_count + solver_count + redteam_count,
        "stage_1_scouts": {
            "count": scout_count,
            "model": "flash" if scout_count else "inline",
            "leaf_model": "flash_lite",
            "role": "STORM live SOTA + OSS curation + codebase invariant discovery" if scout_count else "Inline scoped grep (0 subagents)",
        },
        "stage_2_blind_solvers": {
            "count": solver_count,
            "model": solver_model,
            "tag": "[L3]" if solver_model == "pro" else "",
            "isolated": solver_count > 0,
            "axes": [a["axis"] for a in morph_axes[: max(1, solver_count)]],
            "role": "Anti-anchoring blind Zwicky morphogenesis + TRIZ contradiction resolution (!see_peer)" if solver_count else "Inline alternative comparison (0 subagents)",
        },
        "stage_3_cold_redteam": {
            "count": redteam_count,
            "model": redteam_model,
            "tag": "[L3]" if redteam_model == "pro" else "",
            "round_cap": 1 if redteam_count else 0,
            "cold_context": redteam_count > 0,
            "role": "Masked-origin Pre-Mortem (T+6mo P0 trace) + Hostile STRIDE/Physics falsifier" if redteam_count else "Inline failure-guard check (0 subagents)",
        },
    }

    return {
        "domain_vector": domain_vector,
        "active_domains": active_domains,
        "active_lenses": active_lenses,
        "matched_keywords": {d: matched_kws.get(d, [])[:6] for d in active_domains},
        "door_type": door_type,
        "risk_tier": risk_tier,
        "recommended_format": rec_format,
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
    fmt: str = "",
    out_path: str = "",
    profile: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Generate a right-sized zero-boilerplate symbolic SOTA blueprint skeleton (`mini|compact|full`)."""
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

    resolved_fmt = (fmt or "").strip().lower()
    if resolved_fmt not in VALID_FORMATS:
        if tier_str == "T1" and cls["door_type"] == "2-way":
            resolved_fmt = "mini"
        elif tier_str == "T2":
            resolved_fmt = "compact"
        else:
            resolved_fmt = cls.get("recommended_format") or "full"
    cls["recommended_format"] = resolved_fmt

    dom_vec_str = ",".join(f"{k}:{v:.2f}" for k, v in cls["domain_vector"].items())
    axes = cls["morph_axes"] or [{"axis": "Simple vs General", "pole_a": "Thin Wedge", "pole_b": "Full Platform"}]
    ax1 = axes[0]
    ax2 = axes[1] if len(axes) > 1 else axes[0]

    target = Path(out_path).expanduser() if out_path else (repo / str(cfg["dir"]) / f"{bp_id}.md")
    if not target.is_absolute():
        target = (repo / target).resolve()
    if target.exists() and not force:
        raise FileExistsError(f"Blueprint already exists: {target} (pass --force to overwrite)")

    target.parent.mkdir(parents=True, exist_ok=True)
    heading = title.strip() or bp_id
    if resolved_fmt == "mini":
        content = f"""# {heading} (`{bp_id}`)

> STYLE|symbolic-EN

BP_META|id={bp_id}|format=mini|domain_vec={dom_vec_str}|door={cls['door_type']}|tier={tier_str}|status=SEED-v1|h1_wave=W1
VALUE|<FILL:1L measurable target delta>
NON_GOAL|<FILL:explicit out-of-scope boundary>
CANDIDATE|A|{ax1['pole_a']}|<FILL:rejected alternative>
REJECTED_BECAUSE|A|<FILL:quantified flaw or domain invariant failure>
WINNER|<FILL:chosen minimal approach + why it wins>
KILL|R1|<FILL:primary regression risk>|<FILL:numeric kill_expr>|<FILL:obs_window>|<FILL:rollback>
QAS|Q1|<FILL:stimulus>|<FILL:env>|<FILL:response>|<FILL:numeric_bound+unit>|<FILL:executable_oracle_cmd>
SLICE|W1|<FILL:owned scope>|<FILL:inputs->outputs>|<FILL:fitness_probe>
"""
    elif resolved_fmt == "compact":
        spike_line = (
            "SPIKE|S1|<FILL:VoI=P(fail)*Blast>|<FILL:1-way unknown>|<FILL:numeric pass_gate pre-H1>\n"
            if cls["door_type"] == "1-way"
            else ""
        )
        content = f"""# {heading} (`{bp_id}`)

> STYLE|symbolic-EN

## 0. Meta & Value
BP_META|id={bp_id}|format=compact|domain_vec={dom_vec_str}|door={cls['door_type']}|tier={tier_str}|status=SEED-v1|h1_wave=W1
VALUE|<FILL:1L target outcome + measurable delta>
WARDLEY|<FILL:build custom vs reuse OSS/commodity>
NON_GOAL|<FILL:out-of-scope #1>
NON_GOAL|<FILL:out-of-scope #2>

## 1. Morphogenesis & Risk Guards
CANDIDATE|A|{ax1['pole_a']}|<FILL:architecture & trade-off>
REJECTED_BECAUSE|A|<FILL:quantified flaw or invariant failure>
CANDIDATE|B|{ax2['pole_b']}|<FILL:architecture & trade-off>
REJECTED_BECAUSE|B|<FILL:quantified flaw or invariant failure>
WINNER|<FILL:chosen approach + why it dominates>
THREAT|<FILL:primary failure/abuse vector + defense>
KILL|R1|<FILL:risk>|<FILL:numeric kill_expr>|<FILL:obs_window>|<FILL:rollback>
{spike_line}
## 2. Budgets, QAS & Slices
BUDGET|latency=<FILL:val+unit>|mem=<FILL:val+unit>|degraded_floor=<FILL:safe floor>
QAS|Q1|<FILL:stimulus>|<FILL:env>|<FILL:response>|<FILL:numeric_bound+unit>|<FILL:executable_oracle_cmd>
QAS|Q2|<FILL:stimulus>|<FILL:env>|<FILL:response>|<FILL:numeric_bound+unit>|<FILL:executable_oracle_cmd>

### H1 Atomic Slices
SLICE|W1|<FILL:owned scope>|<FILL:inputs->outputs>|<FILL:fitness_probe>

### H2+ Outcome Stubs
STUB|W2|<FILL:1L outcome & gate ONLY (!deep files/signatures)>
"""
    else:
        content = f"""# {heading} (`{bp_id}`)

> STYLE|symbolic-EN

## 0. Meta & Lineage
BP_META|id={bp_id}|format=full|domain_vec={dom_vec_str}|door={cls['door_type']}|tier={tier_str}|status=SEED-v1|h1_wave=W0

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
"""
    target.write_text(content, encoding="utf-8")
    try:
        rel = target.resolve().relative_to(repo.resolve()).as_posix()
    except Exception:
        rel = str(target)
    return {
        "id": bp_id,
        "format": resolved_fmt,
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
    """Deterministically score & audit a SOTA blueprint (0-100 Anti-Gaming Popperian rubric, tiered for `mini|compact|full`)."""
    cfg = resolve_blueprint_config(profile)
    min_score = int(cfg.get("min_score") or 85)
    max_h2_ratio = float(cfg.get("max_h2_ratio") or 0.25)
    max_line_chars_warn = int(cfg.get("max_line_chars_warn") or 600)

    p = Path(path)
    if not p.is_file():
        return {
            "path": str(p),
            "format": "full",
            "score": 0,
            "passed": False,
            "threshold": min_score,
            "subscores": {},
            "issues": [f"CRITICAL|File not found: `{p}`"],
            "blockers": [f"File not found: `{p}`"],
            "warnings": [],
            "metrics": {},
        }

    text = p.read_text(encoding="utf-8", errors="ignore")
    size_kb = p.stat().st_size / 1024.0
    lines = text.splitlines()
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    max_line_len = max((len(ln) for ln in lines), default=0)
    est_tokens = round(len(text) / 3.5)
    issues: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def _add_issue(code: str, msg: str, blocker: bool = False) -> None:
        issues.append(f"{code}|{msg}")
        if blocker:
            blockers.append(f"{code}|{msg}")

    # 1. META (10): BP_META + VALUE + format-tiered WARDLEY/NON_GOAL
    bp_meta_line = next((ln for ln in nonempty if ln.startswith("BP_META|")), "")
    meta_fields: dict[str, str] = {}
    for tok in _split_pipe_fields(bp_meta_line)[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            meta_fields[k.strip()] = v.strip()

    bp_format = meta_fields.get("format", "").strip().lower()
    if bp_format not in VALID_FORMATS:
        bp_format = "full"

    if bp_format == "mini":
        max_lines = int(cfg.get("mini_max_lines") or 35)
        max_kb = float(cfg.get("mini_max_kb") or 4)
    elif bp_format == "compact":
        max_lines = int(cfg.get("compact_max_lines") or 65)
        max_kb = float(cfg.get("compact_max_kb") or 10)
    else:
        max_lines = int(cfg.get("max_lines") or 350)
        max_kb = float(cfg.get("max_kb") or 24)

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

    if bp_format == "mini":
        if value_lines and len(non_goals) >= 1:
            s_meta += 5
        else:
            _add_issue("META", "Mini blueprint needs `VALUE|` and >=1 `NON_GOAL|`.", blocker=True)
    else:
        if value_lines and wardley_lines and len(non_goals) >= 2:
            s_meta += 5
        elif value_lines and len(non_goals) >= 1:
            s_meta += 3
            _add_issue("META", "Need `VALUE|`, `WARDLEY|`, and >=2 `NON_GOAL|` lines.")
        else:
            _add_issue("META", "Missing `VALUE|`, `WARDLEY|`, or >=2 `NON_GOAL|` lines.", blocker=True)

    # 2. ORI (15): format-tiered candidates + substantive REJECTED_BECAUSE + WINNER
    candidates = [ln for ln in nonempty if ln.startswith(("CANDIDATE|", "ALT_REJECTED|")) and _has_real_content(ln)]
    raw_rejects = [ln for ln in nonempty if ln.startswith("REJECTED_BECAUSE|")]
    rejects: list[str] = []
    for rln in raw_rejects:
        rparts = _split_pipe_fields(rln)
        reason = rparts[-1] if len(rparts) >= 3 else (rln.split("|", 2)[-1] if "|" in rln else "")
        if _is_substantive_reason(reason):
            rejects.append(rln)
        elif _has_real_content(reason):
            _add_issue("ORI", f"Non-substantive `REJECTED_BECAUSE|` (<12c or vague): `{rln[:55]}`.", blocker=True)
    winners = [ln for ln in nonempty if ln.startswith("WINNER|") and _has_real_content(ln[7:])]

    min_cands = 1 if bp_format == "mini" else 2
    min_rejects = 1 if bp_format == "mini" else 2
    s_ori = 0
    if len(rejects) >= min_rejects and len(candidates) >= min_cands and winners:
        s_ori = 15
    elif len(rejects) >= 1 and (len(candidates) >= 1 or winners):
        s_ori = 7
        _add_issue("ORI", f"Need >={min_cands} `CANDIDATE|` + >={min_rejects} substantive `REJECTED_BECAUSE|` + `WINNER|` (found rejects={len(rejects)}).", blocker=True)
    else:
        _add_issue("ORI", f"Missing morphogenesis (>={min_cands} `CANDIDATE|` + >={min_rejects} substantive `REJECTED_BECAUSE|` + `WINNER|`).", blocker=True)

    # 3. IDS (20): QAS numeric bound+unit && substantive executable oracle + BUDGET (optional in mini)
    qas_lines = [ln for ln in nonempty if ln.startswith("QAS|")]
    budget_lines = [ln for ln in nonempty if ln.startswith("BUDGET|") and _has_real_content(ln) and _NUMERIC_BOUND_RE.search(ln)]
    valid_qas = 0
    for q in qas_lines:
        parts = _split_pipe_fields(q)
        if len(parts) < 7:
            _add_issue("IDS", f"Malformed QAS tuple (<7 fields): `{q[:60]}`.", blocker=True)
            continue
        bound_field, oracle_field = parts[5], parts[6]
        has_num = bool(_NUMERIC_BOUND_RE.search(bound_field)) and _has_real_content(bound_field)
        has_oracle = _is_substantive_oracle(oracle_field)
        if has_num and has_oracle:
            valid_qas += 1
        else:
            _add_issue("IDS", f"QAS `{parts[1]}` missing numeric bound+unit or real executable oracle cmd.", blocker=True)

    if not qas_lines:
        s_ids = 0
        _add_issue("IDS", "No `QAS|` falsifiable quality attribute scenarios found.", blocker=True)
    elif bp_format == "mini":
        s_ids = round(20.0 * (valid_qas / len(qas_lines)))
    else:
        qas_score = round(16.0 * (valid_qas / len(qas_lines)))
        budget_score = 4 if budget_lines else 0
        if not budget_lines:
            _add_issue("IDS", "Missing numeric `BUDGET|` envelope.", blocker=True)
        s_ids = min(20, qas_score + budget_score)

    # 4. WBS (10): Clause-local (+/-90c) weasel-word ban ignoring backticks & `--flags`
    normative_prefixes = (
        "VALUE|", "NON_GOAL|", "WINNER|", "QAS|", "BUDGET|",
        "SPIKE|", "SLICE|", "STUB|", "FITNESS|", "KILL|", "THREAT|",
    )
    weasel_hits: list[str] = []
    for ln in nonempty:
        if not ln.startswith(normative_prefixes) or _PLACEHOLDER_RE.search(ln):
            continue
        weasel_hits.extend(_weasel_hits_in_line(ln))
    if not weasel_hits:
        s_wbs = 10
    elif len(weasel_hits) == 1:
        s_wbs = 5
        _add_issue("WBS", f"Unqualified weasel word w/o local numeric bound: {weasel_hits[0]}", blocker=True)
    else:
        s_wbs = 0
        _add_issue("WBS", f"{len(weasel_hits)} unqualified weasel words w/o local numeric bounds ({weasel_hits[0]})", blocker=True)

    # 5. KCC (15): format-tiered PREMORTEM/THREAT + substantive numeric/comparator KILL|
    door_val = meta_fields.get("door", "1-way").lower()
    premortems = [ln for ln in nonempty if ln.startswith("PREMORTEM|") and _has_real_content(_split_pipe_fields(ln)[-1])]
    threats = [ln for ln in nonempty if ln.startswith("THREAT|") and _has_real_content(ln[7:])]
    kill_lines = [ln for ln in nonempty if ln.startswith("KILL|")]
    valid_kills = 0
    for kl in kill_lines:
        parts = _split_pipe_fields(kl)
        if len(parts) >= 6 and all(_has_real_content(x) for x in parts[1:6]) and _is_substantive_kill_expr(parts[3]):
            valid_kills += 1
        else:
            _add_issue("KCC", f"Malformed or non-numeric `KILL|` (need 6 fields + comparator/numeric kill_expr): `{kl[:55]}`.", blocker=True)

    if bp_format == "mini":
        if valid_kills >= 1:
            s_kcc = 15
        else:
            s_kcc = 0
            _add_issue("KCC", "Mini blueprint requires >=1 numeric/comparator `KILL|` abort circuit.", blocker=True)
    elif bp_format == "compact":
        req_kills = 2 if door_val == "1-way" else 1
        if (premortems or threats) and valid_kills >= req_kills:
            s_kcc = 15
        elif premortems or threats or valid_kills >= 1:
            s_kcc = 7
            _add_issue("KCC", f"Compact blueprint needs `THREAT|`/`PREMORTEM|` and >={req_kills} numeric `KILL|` trigger(s).", blocker=True)
        else:
            s_kcc = 0
            _add_issue("KCC", "Missing `THREAT|`/`PREMORTEM|` and `KILL|` abort circuits.", blocker=True)
    else:
        if premortems and valid_kills >= 2:
            s_kcc = 15
        elif premortems or valid_kills >= 1:
            s_kcc = 7
            _add_issue("KCC", f"Need `PREMORTEM|` and >=2 complete numeric `KILL|` triggers (found valid_kills={valid_kills}).", blocker=True)
        else:
            s_kcc = 0
            _add_issue("KCC", "Missing `PREMORTEM|` and `KILL|` abort circuits.", blocker=True)

    # 6. SPIKE (10): >=1 SPIKE| on full T2/T3/1-way, or compact 1-way (exempt on mini)
    tier_val = meta_fields.get("tier", "T2").upper()
    if bp_format == "mini":
        needs_spike = False
    elif bp_format == "compact":
        needs_spike = door_val == "1-way"
    else:
        needs_spike = (tier_val in ("T2", "T3")) or (door_val == "1-way")
    spikes = [ln for ln in nonempty if ln.startswith("SPIKE|")]
    valid_spikes = sum(
        1 for sp in spikes if len(_split_pipe_fields(sp)) >= 5 and all(_has_real_content(x) for x in _split_pipe_fields(sp)[1:5])
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

    # 8. CTB (10): 0 placeholders + format-calibrated line/KB ceilings + non-dummy FITNESS oracle
    placeholders = _PLACEHOLDER_RE.findall(text)
    fitness_lines = [ln.strip()[8:].strip() for ln in lines if ln.strip().startswith("FITNESS|")]
    dummy_fitness = [f for f in fitness_lines if not _is_substantive_oracle(f)]
    s_ctb = 10
    if placeholders:
        s_ctb = 0
        _add_issue("CTB", f"{len(placeholders)} unfilled placeholder(s) (e.g. `{placeholders[0]}`).", blocker=True)
    elif len(lines) > max_lines or size_kb > max_kb:
        s_ctb = 3
        _add_issue("CTB", f"Context bloat ({bp_format}): {len(lines)}L / {size_kb:.1f} KB exceeds limit ({max_lines}L / {max_kb:.0f} KB).", blocker=True)
    if dummy_fitness:
        s_ctb = max(0, s_ctb - 6)
        _add_issue("CTB", f"Dummy `FITNESS|` oracle (`{dummy_fitness[0][:40]}`): provide real executable verification cmd.", blocker=True)

    if max_line_len > max_line_chars_warn:
        warnings.append(f"DENSITY|max_line_len={max_line_len}c > {max_line_chars_warn}c (prefer concise <=400c lines).")

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
        "format": bp_format,
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
        "warnings": warnings,
        "metrics": {
            "format": bp_format,
            "lines": len(lines),
            "kb": round(size_kb, 2),
            "est_tokens": est_tokens,
            "max_line_len": max_line_len,
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


def list_blueprints(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    strict: bool | None = None,
    include_details: bool = False,
) -> list[dict[str, Any]]:
    """Discover and score all `BP_*.md` / `blueprints/*.md` docs in `repo` (single-pass optional detail attach)."""
    cfg = resolve_blueprint_config(profile)
    use_strict = bool(cfg.get("strict_verify", False)) if strict is None else bool(strict)
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
        res = lint_blueprint(f, strict=use_strict, profile=profile)
        try:
            rel = f.resolve().relative_to(repo.resolve()).as_posix()
        except Exception:
            rel = str(f)
        entry: dict[str, Any] = {
            "rel_path": rel,
            "id": res["id"],
            "format": res["format"],
            "status": res["status"],
            "door": res["door"],
            "tier": res["tier"],
            "score": res["score"],
            "passed": res["passed"],
            "issues_count": len(res["issues"]),
        }
        if include_details:
            entry["lint"] = res
            entry["gov"] = extract_blueprint_governance_meta(f)
        out.append(entry)
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


HUB_AUTO_START = "<!-- HUB:AUTO_START -->"
HUB_AUTO_END = "<!-- HUB:AUTO_END -->"

_HUB_SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", "obj", "bin", "archive",
    "__pycache__", ".venv", "venv", ".agents", ".aistudio", ".playwright-cli",
})


def _status_bucket(status: str) -> str:
    st = (status or "").strip().upper()
    if st.startswith("DONE") or st.startswith("COMPLETE"):
        return "done"
    if st.startswith("SUPERSEDED"):
        return "superseded"
    if st.startswith("DEFERRED"):
        return "deferred"
    if st.startswith("PARTIAL"):
        return "partial"
    return "active"


def extract_blueprint_governance_meta(path: Path) -> dict[str, Any]:
    """Extract governance directives (`SUPERSEDED_BY|`, `CONFLICTS_WITH|`, `CONFLICT_RESOLVED|`, `HYGIENE_NOTE|`, stubs) from a blueprint."""
    p = Path(path)
    if not p.is_file():
        return {"superseded_by": [], "open_conflicts": [], "resolved_conflicts": [], "hygiene_notes": [], "stubs": []}
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    superseded_by = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("SUPERSEDED_BY|") and "|" in ln]
    open_conflicts: list[str] = []
    resolved_conflicts: list[str] = []
    for ln in lines:
        if ln.startswith("CONFLICTS_WITH|"):
            body = ln.split("|", 1)[1].strip()
            if re.search(r"\bRESOLVED\b", body, re.IGNORECASE):
                resolved_conflicts.append(body)
            else:
                open_conflicts.append(body)
        elif ln.startswith("CONFLICT_RESOLVED|"):
            resolved_conflicts.append(ln.split("|", 1)[1].strip())
    hygiene_notes = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("HYGIENE_NOTE|") and "|" in ln]
    stubs = [ln.split("|", 1)[1].strip() for ln in lines if ln.startswith("STUB|") and "|" in ln]
    return {
        "superseded_by": superseded_by,
        "open_conflicts": open_conflicts,
        "resolved_conflicts": resolved_conflicts,
        "hygiene_notes": hygiene_notes,
        "stubs": stubs,
    }


def _norm_rel(raw: str) -> str:
    s = str(raw or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _load_profile_if_none(repo: Path, profile: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(profile, dict):
        return profile
    p = repo / ".agents" / "planner_profile.json"
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return {}


def scan_oversized_code_files(repo: Path, profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return sorted list of workspace code files exceeding `code_weight_limits.warn_lines_all` (default 2000L)."""
    prof = _load_profile_if_none(repo, profile)
    cw = prof.get("code_weight_limits") if isinstance(prof.get("code_weight_limits"), dict) else {}
    limit = int(cw.get("warn_lines_all") or 2000)
    exts = {str(e).lower() for e in (prof.get("code_extensions") or [".cs", ".py", ".ts", ".tsx", ".js", ".cpp", ".c", ".h"])}
    prefixes = tuple(_norm_rel(p) for p in (prof.get("code_path_prefixes") or []) if _norm_rel(p))
    found: list[dict[str, Any]] = []
    root = repo.resolve()
    import os as _os

    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in _HUB_SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            ext = Path(fname).suffix.lower()
            if ext not in exts:
                continue
            full = Path(dirpath) / fname
            try:
                rel = full.relative_to(root).as_posix()
            except Exception:
                continue
            if prefixes and not rel.startswith(prefixes):
                continue
            try:
                with full.open("rb") as fh:
                    line_count = sum(1 for _ in fh)
            except Exception:
                continue
            if line_count > limit:
                found.append({"rel_path": rel, "lines": line_count, "limit": limit})
    found.sort(key=lambda item: (-item["lines"], item["rel_path"]))
    return found


def read_archived_ledger_entries(repo: Path, profile: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Parse `BP_ARCHIVED|...` index entries from `.agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md`."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
    ledger_path = repo / ledger_rel
    if not ledger_path.is_file():
        return []
    entries: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw in ledger_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = raw.strip()
        if not ln.startswith("BP_ARCHIVED|"):
            continue
        fields: dict[str, str] = {}
        for tok in _split_pipe_fields(ln)[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k.strip()] = v.strip()
        bp_id = fields.get("id", "").strip()
        if bp_id and bp_id not in seen_ids:
            seen_ids.add(bp_id)
            entries.append(fields)
    return entries


def render_hub_auto_block(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    precomputed_items: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Render deterministic auto-synced inventory block for `PLANS_HUB.md` (single-pass when `precomputed_items` supplied)."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    items = precomputed_items if precomputed_items is not None else list_blueprints(repo, profile=prof, include_details=True)
    archived = read_archived_ledger_entries(repo, profile=prof)
    buckets = {"done": 0, "partial": 0, "active": 0, "deferred": 0, "superseded": 0}
    open_conflicts_total = 0
    min_score = 100 if items else 0
    rows: list[str] = []

    for it in items:
        b = _status_bucket(it["status"])
        buckets[b] = buckets.get(b, 0) + 1
        min_score = min(min_score, int(it["score"]))
        gov = it.get("gov") if isinstance(it.get("gov"), dict) else extract_blueprint_governance_meta(repo / it["rel_path"])
        open_conflicts_total += len(gov["open_conflicts"])
        note_parts: list[str] = []
        if gov["open_conflicts"]:
            note_parts.append(f"OPEN_CONFLICT({len(gov['open_conflicts'])})")
        if gov["superseded_by"]:
            note_parts.append("superseded_by=" + "; ".join(gov["superseded_by"][:1])[:80])
        if gov["hygiene_notes"]:
            note_parts.append(gov["hygiene_notes"][0][:110])
        elif gov["resolved_conflicts"]:
            note_parts.append("resolved=" + gov["resolved_conflicts"][0][:100])
        note_str = (" ; ".join(note_parts) or "-").replace("|", "/")
        stub_count = len(gov["stubs"])
        rows.append(
            f"| `{it['id']}` | `{it['status']}` | `{it['score']}/100` | `{it['tier']}` | `{stub_count}` | {note_str} |"
        )

    monoliths = scan_oversized_code_files(repo, profile=prof)
    stats = {
        "blueprints_total": len(items),
        "archived_done": len(archived),
        "buckets": buckets,
        "min_score": min_score,
        "open_conflicts": open_conflicts_total,
        "monoliths_gt2000": len(monoliths),
    }

    arch_suffix = f"|archived_done={len(archived)}" if archived else ""
    lines = [
        HUB_AUTO_START,
        (
            f"HUB_STATS|blueprints={len(items)} "
            f"(done={buckets['done']}, partial={buckets['partial']}, active={buckets['active']}, "
            f"deferred={buckets['deferred']}, superseded={buckets['superseded']})"
            f"{arch_suffix}|"
            f"min_score={min_score}/100|open_conflicts={open_conflicts_total}|monoliths_gt2000={len(monoliths)}"
        ),
        "",
        "| Blueprint ID | Status | Score | Tier | H2+ Stubs | Hygiene / Lineage Note |",
        "| :--- | :---: | :---: | :---: | :---: | :--- |",
        *(rows or ["| *(none)* | `-` | `-` | `-` | `0` | - |"]),
    ]
    if archived:
        ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
        tar_rel = _norm_rel(cfg.get("archive_tarball") or ".agents/docs/archive/blueprints_done.tar.gz")
        arch_ids = ", ".join(f"`{a['id']}`" for a in archived)
        lines.extend([
            "",
            f"ARCHIVED_DONE|count={len(archived)}|ledger=`{ledger_rel}`|tarball=`{tar_rel}`|ids={arch_ids}",
        ])
    if monoliths:
        lines.extend([
            "",
            "### Oversized Code Files (`>2,000L` Live Scan)",
            "| File | Lines | Cap |",
            "| :--- | ---: | ---: |",
        ])
        for m in monoliths:
            lines.append(f"| `{m['rel_path']}` | `{m['lines']}` | `{m['limit']}` |")
    lines.append(HUB_AUTO_END)
    return "\n".join(lines), stats


def _default_hub_template(auto_block: str) -> str:
    return (
        "# Central Plans & Blueprints Hub (`PLANS_HUB.md`)\n\n"
        "> STYLE|lossless symbolic telegraphic EN ONLY (`!articles/copulas/filler`, ops `-> | && || ! w/ w/o`).\n"
        "> POLICY|BLUEPRINT_FIRST_DEFAULT (`require_by_default=true`; skip ONLY on explicit user opt-out to save tokens or `T0`/read-only) + MANDATORY PRE-RESUMPTION HYGIENE (`plan_tool.py blueprint hygiene --strict` BEFORE resuming any paused plan).\n\n"
        "## 1. Active & Pending Work Queue\n"
        "- *(Curated priority queue — never overwritten by `hub --sync`.)*\n\n"
        "## 2. Resolved Conflicts & Superseded Directives\n"
        "- *(Curated anti-regression registry — never overwritten by `hub --sync`.)*\n\n"
        "## 3. Auto-Synced Live Inventory\n"
        f"{auto_block}\n"
    )


def sync_plans_hub(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    apply: bool = True,
    precomputed_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check or atomically update the `<!-- HUB:AUTO_START -->..<!-- HUB:AUTO_END -->` block in `PLANS_HUB.md`."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    hub_path = repo / hub_rel
    auto_block, stats = render_hub_auto_block(repo, profile=prof, precomputed_items=precomputed_items)

    if not hub_path.is_file():
        if apply:
            hub_path.parent.mkdir(parents=True, exist_ok=True)
            hub_path.write_text(_default_hub_template(auto_block), encoding="utf-8")
            return {
                "path": str(hub_path),
                "rel_path": hub_rel,
                "exists": True,
                "in_sync": True,
                "updated": True,
                "changed": True,
                "created": True,
                "stats": stats,
            }
        return {
            "path": str(hub_path),
            "rel_path": hub_rel,
            "exists": False,
            "in_sync": False,
            "updated": False,
            "changed": False,
            "created": False,
            "stats": stats,
        }

    existing = hub_path.read_text(encoding="utf-8", errors="ignore")
    s_idx = existing.find(HUB_AUTO_START)
    e_idx = existing.find(HUB_AUTO_END)
    if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
        current_block = existing[s_idx : e_idx + len(HUB_AUTO_END)]
        if current_block == auto_block:
            return {
                "path": str(hub_path),
                "rel_path": hub_rel,
                "exists": True,
                "in_sync": True,
                "updated": False,
                "changed": False,
                "created": False,
                "stats": stats,
            }
        new_text = existing[:s_idx] + auto_block + existing[e_idx + len(HUB_AUTO_END) :]
    else:
        sep = "\n\n"
        new_text = existing.rstrip() + f"{sep}## Auto-Synced Live Inventory\n{auto_block}\n"

    if apply:
        hub_path.write_text(new_text, encoding="utf-8")
        return {
            "path": str(hub_path),
            "rel_path": hub_rel,
            "exists": True,
            "in_sync": True,
            "updated": True,
            "changed": True,
            "created": False,
            "stats": stats,
        }
    return {
        "path": str(hub_path),
        "rel_path": hub_rel,
        "exists": True,
        "in_sync": False,
        "updated": False,
        "changed": False,
        "created": False,
        "stats": stats,
    }


def hygiene_audit(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Run deterministic single-pass pre-resumption plan hygiene & conflict audit across blueprints, roadmaps, & `PLANS_HUB.md`."""
    import time as _time

    t0 = _time.monotonic()
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    explicit_hub_cfg = isinstance(prof.get("blueprint"), dict) and bool(prof["blueprint"].get("hub_file"))
    # Single pass: lint strictly and extract governance metadata ONCE per blueprint file
    items = list_blueprints(repo, profile=prof, strict=True, include_details=True)
    issues: list[str] = []
    warnings: list[str] = []

    for it in items:
        lint_res = it["lint"]
        if not lint_res["passed"]:
            issues.append(f"LINT|{it['id']}: score={lint_res['score']}/100 ({'; '.join(lint_res['issues'][:2])})")
        gov = it["gov"]
        for oc in gov["open_conflicts"]:
            issues.append(f"CONFLICT|{it['id']}: Unresolved CONFLICTS_WITH|{oc} — resolve before executing.")
        if gov["superseded_by"]:
            st_upper = str(it["status"]).upper()
            if st_upper not in ("SUPERSEDED", "DONE"):
                whole_sup = [s for s in gov["superseded_by"] if not re.match(r"^(?:section|part|slice|item)\s*:", s, re.I)]
                if whole_sup:
                    issues.append(
                        f"STALE_STATUS|{it['id']}: declares SUPERSEDED_BY|{whole_sup[0][:60]} but `BP_META` status=`{it['status']}` (mark `SUPERSEDED` or `DONE`)."
                    )

    hub_check = sync_plans_hub(repo, profile=prof, apply=False, precomputed_items=items)
    if not hub_check["exists"]:
        msg = f"HUB_MISSING|Central Plans Hub missing at `{hub_rel}` — run `plan_tool.py blueprint hub --sync`."
        (issues if (strict or explicit_hub_cfg) else warnings).append(msg)
    elif not hub_check["in_sync"]:
        msg = f"HUB_DRIFT|`{hub_rel}` auto-inventory is out of sync with workspace blueprints/monoliths — run `plan_tool.py blueprint hub --sync`."
        (issues if strict else warnings).append(msg)

    elapsed_ms = round((_time.monotonic() - t0) * 1000.0, 1)
    passed = (len(issues) == 0) and (not strict or len(warnings) == 0)
    st = hub_check["stats"]
    b = st["buckets"]
    counts = {
        "total": st["blueprints_total"],
        "archived_done": st.get("archived_done", 0),
        "done": b.get("done", 0),
        "partial": b.get("partial", 0),
        "active": b.get("active", 0),
        "deferred": b.get("deferred", 0),
        "superseded": b.get("superseded", 0),
    }
    return {
        "ok": passed,
        "passed": passed,
        "strict": strict,
        "elapsed_ms": elapsed_ms,
        "hub_rel": hub_rel,
        "hub_exists": hub_check["exists"],
        "hub_in_sync": hub_check["in_sync"],
        "counts": counts,
        "stats": st,
        "issues": issues,
        "warnings": warnings,
    }


def compact_done_blueprints(
    repo: Path,
    profile: dict[str, Any] | None = None,
    *,
    apply: bool = True,
    include_superseded: bool = False,
    keep_ids: list[str] | None = None,
    sync_hub: bool = True,
) -> dict[str, Any]:
    """Losslessly archive `DONE` (and optionally `SUPERSEDED`) blueprints into cold `.tar.gz` + searchable ledger (`O(1)` file count)."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    ledger_rel = _norm_rel(cfg.get("archive_ledger") or ".agents/docs/archive/BLUEPRINTS_DONE_LEDGER.md")
    tar_rel = _norm_rel(cfg.get("archive_tarball") or ".agents/docs/archive/blueprints_done.tar.gz")
    ledger_path = repo / ledger_rel
    tar_path = repo / tar_rel
    keep_set = {k.strip().upper() for k in (keep_ids or []) if k.strip()}

    items = list_blueprints(repo, profile=prof, strict=False, include_details=True)
    allowed_buckets = {"done", "superseded"} if include_superseded else {"done"}
    targets = [
        it for it in items
        if _status_bucket(it["status"]) in allowed_buckets
        and it["id"].strip().upper() not in keep_set
    ]

    if not targets:
        return {
            "applied": False,
            "compacted": [],
            "skipped": len(items),
            "ledger_rel": ledger_rel,
            "tarball_rel": tar_rel,
            "verified_sha256": True,
        }

    if not apply:
        return {
            "applied": False,
            "dry_run": True,
            "candidates": [t["id"] for t in targets],
            "count": len(targets),
            "ledger_rel": ledger_rel,
            "tarball_rel": tar_rel,
        }

    # 1. Read existing tar members (if any) + prepare new members with SHA-256 checksums
    existing_members: dict[str, bytes] = {}
    if tar_path.is_file():
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    fh = tf.extractfile(member)
                    if fh is not None:
                        existing_members[member.name] = fh.read()

    new_records: list[dict[str, str]] = []
    expected_hashes: dict[str, str] = {}
    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for it in targets:
        src = repo / it["rel_path"]
        raw_bytes = src.read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest()[:16]
        arc_name = src.name
        existing_members[arc_name] = raw_bytes
        expected_hashes[arc_name] = hashlib.sha256(raw_bytes).hexdigest()

        txt = raw_bytes.decode("utf-8", errors="ignore")
        nonempty = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        val = next((ln[6:].strip() for ln in nonempty if ln.startswith("VALUE|")), "")[:140].replace("|", "/")
        win = next((ln[7:].strip() for ln in nonempty if ln.startswith("WINNER|")), "")[:140].replace("|", "/")
        gov = it["gov"]
        note_bits: list[str] = []
        if gov["hygiene_notes"]:
            note_bits.append(gov["hygiene_notes"][0][:120])
        if gov["resolved_conflicts"]:
            note_bits.append("resolved=" + gov["resolved_conflicts"][0][:100])
        if gov["superseded_by"]:
            note_bits.append("superseded_by=" + gov["superseded_by"][0][:80])
        note = (" ; ".join(note_bits) or "-").replace("|", "/")
        new_records.append({
            "id": it["id"],
            "status": it["status"],
            "tier": it["tier"],
            "score": str(it["score"]),
            "archived_on": now_date,
            "sha256": sha,
            "file": arc_name,
            "value": val,
            "winner": win,
            "note": note,
            "src_path": str(src),
        })

    # 2. Write tar.gz atomically via temp file & verify 100% SHA-256 readback BEFORE unlinking any source
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = tar_path.with_suffix(".tmp.tar.gz")
    with tarfile.open(tmp_tar, "w:gz") as tf:
        for name in sorted(existing_members):
            data = existing_members[name]
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(data))

    with tarfile.open(tmp_tar, "r:gz") as tf:
        for arc_name, want_sha in expected_hashes.items():
            fh = tf.extractfile(arc_name)
            if fh is None or hashlib.sha256(fh.read()).hexdigest() != want_sha:
                tmp_tar.unlink(missing_ok=True)
                raise RuntimeError(f"Compaction abort: SHA-256 readback mismatch for {arc_name}")
    tmp_tar.replace(tar_path)

    # 3. Append deduplicated entries to BLUEPRINTS_DONE_LEDGER.md
    existing_ledger_entries = read_archived_ledger_entries(repo, profile=prof)
    by_id: dict[str, dict[str, str]] = {e["id"]: e for e in existing_ledger_entries if e.get("id")}
    for rec in new_records:
        by_id[rec["id"]] = rec

    ledger_lines = [
        "# Archived Completed Blueprints Ledger (`BLUEPRINTS_DONE_LEDGER.md`)",
        "",
        "> STYLE|symbolic-EN (`<=120L` bounded slice reads on demand; verbatim full `.md` preserved in `blueprints_done.tar.gz`).",
        "",
    ]
    for bp_id in sorted(by_id):
        e = by_id[bp_id]
        ledger_lines.append(
            f"BP_ARCHIVED|id={bp_id}|status={e.get('status', 'DONE')}|tier={e.get('tier', 'T3')}|"
            f"score={e.get('score', '100')}|archived_on={e.get('archived_on', now_date)}|sha256={e.get('sha256', '-')}|"
            f"file={e.get('file', bp_id + '.md')}|note={e.get('note', '-')}|value={e.get('value', '-')}"
        )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("\n".join(ledger_lines) + "\n", encoding="utf-8")

    # 4. Safe unlink ONLY after tarball verified & ledger persisted
    for rec in new_records:
        Path(rec["src_path"]).unlink(missing_ok=True)

    hub_synced = False
    if sync_hub and (repo / _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")).is_file():
        sync_plans_hub(repo, profile=prof, apply=True)
        hub_synced = True

    return {
        "applied": True,
        "compacted": [r["id"] for r in new_records],
        "count": len(new_records),
        "total_archived": len(by_id),
        "ledger_rel": ledger_rel,
        "tarball_rel": tar_rel,
        "verified_sha256": True,
        "hub_synced": hub_synced,
    }


def match_blueprints(
    prompt: str,
    repo: Path,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match a task prompt against active/partial/deferred blueprints to encourage reuse/amendment before creating duplicates."""
    prof = _load_profile_if_none(repo, profile)
    cls = classify_blueprint_request(prompt, profile=prof)
    prompt_tokens = {
        t for t in re.findall(r"[a-z0-9]{3,}", (prompt or "").lower())
        if t not in {"the", "and", "for", "with", "para", "com", "que", "uma", "dos", "das", "add", "fix", "update", "create", "blueprint"}
    }
    prompt_doms = set(cls.get("active_domains") or [])

    items = list_blueprints(repo, profile=prof, strict=False, include_details=False)
    matches: list[dict[str, Any]] = []
    for it in items:
        bucket = _status_bucket(it["status"])
        if bucket in ("done", "superseded"):
            continue
        p = repo / it["rel_path"]
        txt = p.read_text(encoding="utf-8", errors="ignore").lower()
        bp_tokens = set(re.findall(r"[a-z0-9]{3,}", it["id"].lower().replace("_", " ") + " " + txt[:1200]))
        overlap = prompt_tokens & bp_tokens
        id_overlap = prompt_tokens & set(re.findall(r"[a-z0-9]{3,}", it["id"].lower().replace("_", " ")))
        dom_overlap = [d for d in prompt_doms if d in txt[:500]]
        score = round(len(overlap) * 1.0 + len(id_overlap) * 2.5 + len(dom_overlap) * 1.5, 2)
        if score >= 2.0:
            matches.append({
                "id": it["id"],
                "rel_path": it["rel_path"],
                "status": it["status"],
                "format": it["format"],
                "score": score,
                "matched_tokens": sorted(overlap)[:8],
            })

    matches.sort(key=lambda m: (-m["score"], m["id"]))
    action = "AMEND" if matches and matches[0]["score"] >= 4.0 else "CREATE"
    return {
        "action": action,
        "recommended_format": cls["recommended_format"],
        "risk_tier": cls["risk_tier"],
        "door_type": cls["door_type"],
        "top_match": matches[0] if matches else None,
        "matches": matches[:5],
    }


def transition_blueprint(
    path: Path,
    *,
    status: str,
    note: str = "",
    superseded_by: str = "",
    sync_hub: bool = False,
    repo: Path | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically transition `BP_META` status + `HYGIENE_NOTE|`/`SUPERSEDED_BY|` and optionally sync `PLANS_HUB.md`."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Blueprint not found: {p}")
    new_status = status.strip().upper()
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    meta_idx = -1
    bp_id = p.stem
    for i, ln in enumerate(lines):
        if ln.strip().startswith("BP_META|"):
            meta_idx = i
            parts = _split_pipe_fields(ln.strip())
            updated_parts: list[str] = [parts[0]]
            found_st = False
            for tok in parts[1:]:
                if tok.startswith("id="):
                    bp_id = tok[3:].strip()
                    updated_parts.append(tok)
                elif tok.startswith("status="):
                    updated_parts.append(f"status={new_status}")
                    found_st = True
                else:
                    updated_parts.append(tok)
            if not found_st:
                updated_parts.append(f"status={new_status}")
            lines[i] = "|".join(updated_parts)
            break
    if meta_idx == -1:
        raise ValueError(f"Missing `BP_META|` line in {p}")

    insert_at = meta_idx + 1
    if superseded_by.strip():
        sup_line = f"SUPERSEDED_BY|{superseded_by.strip()}"
        if not any(ln.strip().startswith("SUPERSEDED_BY|") for ln in lines):
            lines.insert(insert_at, sup_line)
            insert_at += 1
        else:
            lines = [sup_line if ln.strip().startswith("SUPERSEDED_BY|") else ln for ln in lines]

    if note.strip():
        note_line = f"HYGIENE_NOTE|{note.strip()}"
        if not any(ln.strip().startswith("HYGIENE_NOTE|") for ln in lines):
            lines.insert(insert_at, note_line)
        else:
            lines = [note_line if ln.strip().startswith("HYGIENE_NOTE|") else ln for ln in lines]

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hub_synced = False
    if sync_hub and repo is not None:
        sync_plans_hub(repo, profile=profile, apply=True)
        hub_synced = True
    return {"id": bp_id, "path": str(p), "status": new_status, "hub_synced": hub_synced}


def check_plans_hub_hygiene(
    repo: Path,
    profile: dict[str, Any] | None,
    fails: list[str],
    warns: list[str],
) -> None:
    """Fast static verifier hook: if `PLANS_HUB.md` exists, flag drift or unresolved blueprint conflicts."""
    prof = _load_profile_if_none(repo, profile)
    cfg = resolve_blueprint_config(prof)
    hub_rel = _norm_rel(cfg.get("hub_file") or ".agents/docs/PLANS_HUB.md")
    if not (repo / hub_rel).is_file():
        return
    res = hygiene_audit(repo, profile=prof, strict=False)
    strict_verify = bool(cfg.get("strict_verify", False))
    for iss in res["issues"]:
        (fails if strict_verify or iss.startswith("CONFLICT|") else warns).append(f"plan-hygiene: {iss}")
    for w in res["warnings"]:
        warns.append(f"plan-hygiene: {w}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Universal SOTA Blueprint Engine (v2).")
    parser.add_argument("--workspace", default="", help="Workspace root override.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cls = sub.add_parser("classify", help="Classify prompt into domain vector, door type, format, Zwicky axes & right-sized subagent topology.")
    p_cls.add_argument("--prompt", required=True)
    p_cls.add_argument("--json", action="store_true")

    p_init = sub.add_parser("init", help="Scaffold a tiered symbolic SOTA blueprint skeleton (`BP_<SLUG>.md`).")
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--slug", default="")
    p_init.add_argument("--prompt", default="")
    p_init.add_argument("--domains", nargs="*", default=None)
    p_init.add_argument("--door", choices=["1-way", "2-way", ""], default="")
    p_init.add_argument("--tier", type=int, choices=[1, 2, 3], default=None)
    p_init.add_argument("--format", dest="fmt", choices=["mini", "compact", "full", ""], default="")
    p_init.add_argument("--out", default="")
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--json", action="store_true")

    for cname in ("lint", "score"):
        p_l = sub.add_parser(cname, help="Run deterministic 0-100 Anti-Gaming Popperian rubric audit on a blueprint.")
        p_l.add_argument("path")
        p_l.add_argument("--strict", action="store_true")
        p_l.add_argument("--json", action="store_true")

    for lname in ("status", "list"):
        p_ls = sub.add_parser(lname, help="List and score workspace SOTA blueprints.")
        p_ls.add_argument("--json", action="store_true")

    p_ho = sub.add_parser("handoff", help="Extract Wave-0/H1/QAS into a `plan_tool.py init` execution command.")
    p_ho.add_argument("path")
    p_ho.add_argument("--json", action="store_true")

    p_hy = sub.add_parser("hygiene", help="Audit all blueprints, roadmaps & PLANS_HUB.md for stale status, drift & conflicts.")
    p_hy.add_argument("--strict", action="store_true")
    p_hy.add_argument("--json", action="store_true")

    p_hub = sub.add_parser("hub", help="Sync or verify auto-managed inventory in `.agents/docs/PLANS_HUB.md`.")
    p_hub.add_argument("--sync", action="store_true", help="Update auto-inventory markers in PLANS_HUB.md.")
    p_hub.add_argument("--check", action="store_true", help="Check whether PLANS_HUB.md auto-inventory matches disk (exit 1 on drift).")
    p_hub.add_argument("--json", action="store_true")

    p_cmp = sub.add_parser("compact", help="Losslessly archive DONE blueprints into cold `.tar.gz` + searchable ledger.")
    p_cmp.add_argument("--apply", action="store_true", help="Execute archival compaction & remove archived DONE files.")
    p_cmp.add_argument("--dry-run", action="store_true", help="Preview compaction targets without modifying files.")
    p_cmp.add_argument("--include-superseded", action="store_true")
    p_cmp.add_argument("--keep", action="append", default=[], help="Blueprint ID to keep unarchived.")
    p_cmp.add_argument("--json", action="store_true")

    p_mt = sub.add_parser("match", help="Match a prompt against active/partial blueprints to prefer AMEND over duplicate CREATE.")
    p_mt.add_argument("--prompt", required=True)
    p_mt.add_argument("--json", action="store_true")

    p_tr = sub.add_parser("transition", help="Atomically update blueprint status, HYGIENE_NOTE, SUPERSEDED_BY & optional PLANS_HUB sync.")
    p_tr.add_argument("path")
    p_tr.add_argument("--status", required=True)
    p_tr.add_argument("--note", default="")
    p_tr.add_argument("--superseded-by", default="")
    p_tr.add_argument("--sync-hub", action="store_true")
    p_tr.add_argument("--json", action="store_true")

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
            topo = res["subagent_topology"]
            print(f"BLUEPRINT|domains={dv}|door={res['door_type']}|tier={res['risk_tier']}|format={res['recommended_format']}|subagents={topo['total_subagents']}")
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
                fmt=args.fmt,
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
            print(f"Created SOTA blueprint `{res['rel_path']}` (format={res['format']}, door={cls['door_type']}, tier={cls['risk_tier']})")
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
            print(f"BLUEPRINT {verdict} | {res['id']} ({res['format']}) | score={res['score']}/100 (min={res['threshold']}) | [{subs}]")
            for iss in res["issues"]:
                print(f"  - {iss}")
            for w in res.get("warnings") or []:
                print(f"  [WARN] {w}")
        return 0 if res["passed"] else 1

    if args.cmd in ("status", "list"):
        items = list_blueprints(ws, profile=profile)
        archived = read_archived_ledger_entries(ws, profile=profile)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        elif not items and not archived:
            print("No SOTA blueprints found in workspace.")
        else:
            for it in items:
                tag = "PASS" if it["passed"] else "FAIL"
                print(f"- [{tag} {it['score']:3d}/100] {it['id']} ({it['format']}, {it['door']}, {it['tier']}, {it['status']}) -> {it['rel_path']}")
            if archived:
                print(f"ARCHIVED_DONE|count={len(archived)} (verbatim in blueprints_done.tar.gz)")
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

    if args.cmd == "hygiene":
        res = hygiene_audit(ws, profile=profile, strict=bool(args.strict))
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            verdict = "PASS" if res["passed"] else "FAIL"
            st = res["stats"]
            b = st["buckets"]
            arch_part = f",arch={st.get('archived_done', 0)}" if st.get("archived_done") else ""
            print(
                f"HYGIENE {verdict} | bp={st['blueprints_total']} "
                f"(done={b['done']},partial={b['partial']},active={b['active']},def={b['deferred']},sup={b['superseded']}{arch_part}) | "
                f"hub={'OK' if res['hub_in_sync'] else ('DRIFT' if res['hub_exists'] else 'MISSING')} | "
                f"monoliths={st['monoliths_gt2000']} | {res['elapsed_ms']}ms"
            )
            for iss in res["issues"]:
                print(f"  [ISSUE] {iss}")
            for w in res["warnings"]:
                print(f"  [WARN]  {w}")
        return 0 if res["passed"] else 1

    if args.cmd == "hub":
        apply_sync = bool(args.sync) or not bool(args.check)
        res = sync_plans_hub(ws, profile=profile, apply=apply_sync)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            st = res["stats"]
            b = st["buckets"]
            state_lbl = "CREATED" if res["created"] else ("SYNCED" if res["updated"] else ("OK" if res["in_sync"] else "DRIFT"))
            arch_part = f",arch={st.get('archived_done', 0)}" if st.get("archived_done") else ""
            print(
                f"PLANS_HUB {state_lbl} | `{res['rel_path']}` | bp={st['blueprints_total']} "
                f"(done={b['done']},partial={b['partial']},active={b['active']},def={b['deferred']},sup={b['superseded']}{arch_part}) | "
                f"monoliths={st['monoliths_gt2000']}"
            )
        return 0 if res["in_sync"] else 1

    if args.cmd == "compact":
        do_apply = bool(args.apply) and not bool(args.dry_run)
        res = compact_done_blueprints(
            ws,
            profile=profile,
            apply=do_apply,
            include_superseded=bool(args.include_superseded),
            keep_ids=args.keep,
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res.get("dry_run"):
            print(f"COMPACT DRY-RUN | count={res['count']} | targets={','.join(res['candidates'])}")
        elif res.get("applied"):
            print(f"COMPACT APPLIED | archived={res['count']} (total={res['total_archived']}) | tar=`{res['tarball_rel']}` | ledger=`{res['ledger_rel']}`")
        else:
            print("COMPACT NO-OP | 0 eligible DONE blueprints to archive.")
        return 0

    if args.cmd == "match":
        res = match_blueprints(args.prompt, ws, profile=profile)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            top = res["top_match"]
            top_str = f"top={top['id']}({top['status']},score={top['score']})" if top else "top=none"
            print(f"MATCH|{res['action']}|fmt={res['recommended_format']}|tier={res['risk_tier']}|{top_str}")
        return 0

    if args.cmd == "transition":
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = (ws / target).resolve()
        try:
            res = transition_blueprint(
                target,
                status=args.status,
                note=args.note,
                superseded_by=args.superseded_by,
                sync_hub=bool(args.sync_hub),
                repo=ws,
                profile=profile,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"TRANSITION|{res['id']}|status={res['status']}|hub_synced={res['hub_synced']}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
