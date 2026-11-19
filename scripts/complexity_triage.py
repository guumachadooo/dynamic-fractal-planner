#!/usr/bin/env python3
"""Universal adaptive complexity & breadth triage analyzer with project-profile extensibility."""

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

from schema_migrations import migrate_profile  # noqa: E402

ACTION_VERBS = (
    "adicion*", "acrescent*", "cria*", "implement*", "corrig*", "arrum*", "consert*",
    "melhor*", "otimiz*", "atualiz*", "configur*", "integr*", "remov*", "tir*",
    "coloc*", "mud*", "alter*", "troc*", "refator*", "reescrev*", "constru*",
    "unific*", "separ*", "mov*", "ajust*", "garant*", "verific*", "valid*",
    "proteg*", "bloque*", "automatiz*", "sincroniz*", "suport*", "transform*",
    "extra*", "empacot*",
    "add", "adds", "added", "adding", "creat*", "implement*", "fix*", "repair*",
    "improv*", "optimiz*", "updat*", "configur*", "integrat*", "remov*", "delet*",
    "chang*", "replac*", "refactor*", "rewrit*", "build*", "unify", "split",
    "mov*", "adjust*", "ensur*", "verif*", "validat*", "guard*", "automat*",
    "sync*", "support*", "design*", "redesign*", "port*", "extract*", "render*",
    "convert*", "packag*", "migrat*",
)

UNIVERSAL_DOMAINS: dict[str, dict[str, Any]] = {
    "ui-frontend": {
        "module_id": "MOD-UI-FRONTEND",
        "module_title": "UI Components, Layout, Motion & Localization",
        "keywords": (
            "ui", "ux", "frontend", "front-end", "layout", "css", "tailwind",
            "react", "vue", "svelte", "next.js", "nextjs", "compose", "swiftui",
            "html", "dom", "canvas", "shader*", "animat*", "animaç*", "animac*",
            "botão", "botoes", "botões", "button*", "tela", "telas", "screen*",
            "aba", "abas", "tab", "tabs", "modal", "dialog*", "seekbar", "slider*",
            "switch*", "ícone*", "icone*", "icon*", "fonte*", "font*", "tema", "temas",
            "theme*", "cor", "cores", "color*", "traduç*", "traduc*", "i18n", "l10n",
            "pt-br", "en-us", "responsive", "responsiv*", "clipping",
        ),
    },
    "api-backend": {
        "module_id": "MOD-BACKEND",
        "module_title": "API Endpoints, Services, Auth & Data Layer",
        "keywords": (
            "api", "rest", "graphql", "grpc", "endpoint*", "route*", "rota", "rotas",
            "backend", "back-end", "server", "servidor", "auth*", "oauth", "jwt",
            "database", "banco de dados", "sql", "postgres*", "sqlite", "mysql",
            "prisma", "orm", "schema*", "migrat*", "migraç*", "migrac*", "cache*",
            "webhook*", "scraper*", "http", "fetch", "middleware*",
        ),
    },
    "core-logic": {
        "module_id": "MOD-CORE-LOGIC",
        "module_title": "Core Algorithms, Parsers & State Machine",
        "keywords": (
            "algorithm*", "algoritmo*", "parser*", "lexer", "ast", "codec*",
            "decoder*", "encoder*", "demux*", "remux*", "state machine",
            "máquina de estados", "maquina de estados", "pipeline*", "engine",
            "motor", "dsp", "math", "physics", "física", "fisica", "interpolat*",
            "stream", "streams", "streaming", "buffer*", "queue", "fila",
            "convert*", "transform*", "extract*",
        ),
    },
    "runtime-infra": {
        "module_id": "MOD-RUNTIME",
        "module_title": "Concurrency, Process IPC, Filesystem & Runtime Safety",
        "keywords": (
            "thread*", "concorrên*", "concorren*", "concurrency", "async", "await",
            "deadlock*", "race condition*", "mutex", "lock*", "process*", "subprocess*",
            "pipe*", "ipc", "selinux", "permiss*", "filesystem", "docker", "container*",
            "memory", "memória", "memoria", "leak*", "dispose*", "crash*", "erro*",
            "bug*", "falha*",
        ),
    },
    "tests-qa": {
        "module_id": "MOD-TESTS",
        "module_title": "Automated Tests, Fixtures & Regression Verification",
        "keywords": (
            "test", "tests", "teste", "testes", "testar", "pytest", "jest", "vitest",
            "playwright", "cypress", "xunit", "nunit", "junit", "coverage",
            "cobertura", "benchmark*", "fixture*", "mock*", "e2e", "unit test*",
        ),
    },
    "build-ci": {
        "module_id": "MOD-BUILD-CI",
        "module_title": "Build, Packaging, Release & CI/CD",
        "is_ship": True,
        "keywords": (
            "build", "builds", "compil*", "ci", "cd", "github actions", "workflow*",
            "release*", "deploy*", "publish*", "publica*", "docker*", "apk", "npm",
            "cargo", "maven", "gradle", "dotnet", "commit", "push", "versão", "versao",
            "version*", "entreg*", "deliver*",
        ),
    },
    "agent-infra": {
        "module_id": "MOD-AGENT-INFRA",
        "module_title": "Agent Rules, Planner Engine & Lifecycle Hooks",
        "keywords": (
            "agents.md", "gemini.md", ".agents", "hook", "hooks", "skill", "skills",
            "plugin", "plugins", "planej*", "plan", "plano", "planos", "subagent*",
            "guardrail*", "anti-drift", "antigravity", "triage", "triagem",
        ),
    },
    "plugin-self": {
        "module_id": "MOD-PLUGIN-SELF",
        "module_title": "Plugin Backward Compatibility, Schema & Regression Gate",
        "keywords": (
            "dynamic-fractal-planner", "schema_migrations*", "planner_profile*",
            "planner_state*", "planner_guard*", "planner_post_tool*",
            "planner_stop*", "planner_session_start*", "planner_pre_invocation*",
            "complexity_triage*", "plan_tool*", "verify_universal*",
            "run_plugin_tests*", "extra_requests*",
        ),
    },
}

HIGH_DEPTH_SIGNALS = (
    "arquitetura", "architecture", "refator*", "refactor*", "reescrev*", "rewrit*",
    "pipeline*", "state machine", "máquina de estados", "maquina de estados",
    "sincroniz*", "sync*", "concorrên*", "concorren*", "concurrency", "thread*",
    "race condition*", "deadlock*", "streaming", "real-time", "tempo real",
    "engine", "motor", "renderer*", "decoder*", "demux*", "physics", "física",
    "fisica", "interpolat*", "shader*", "complex*", "muito difícil", "muito dificil",
    "do zero", "from scratch", "dinâmic*", "dinamic*", "dynamic", "módulo*",
    "modulo*", "module*", "fractal", "sub-plan*", "subplan*", "migraç*", "migrac*",
    "migration*", "end-to-end", "ponta a ponta", "multi-layer", "várias camadas",
    "varias camadas", "universal", "infraestrutura", "infrastructure",
)

QUESTION_ONLY_PREFIXES = (
    "o que é", "o que e", "como funciona", "qual é", "qual e", "me explica",
    "explique", "what is", "how does", "why does", "explain", "show me",
    "where is", "onde fica", "pode continuar", "continua", "status",
)

SMALL_CONVERSATIONAL = {
    "oi", "olá", "ola", "ok", "sim", "não", "nao", "beleza", "valeu",
    "obrigado", "obrigada", "thanks", "thank you", "continue", "continua",
    "pode seguir", "yes", "no", "go ahead", "proceed", "tudo bem",
}

GDK_DOC_KEYWORDS = (
    "android", "androidmanifest*", "targetsdk*", "mediastore", "mediasession",
    "audiotrack", "audiomanager", "foreground", "notification*", "saf", "ndk",
    "selinux", "jetpack", "compose", "kotlin", "gradle", "adb", "avd",
    "firebase", "firestore", "gcloud", "gcp", "google cloud", "cloud run",
    "bigquery", "iam", "vertex", "gemini", "adk", "tensorflow", "flutter",
    "dart", "chrome", "devtools", "webapi", "golang", "material design",
    "google-developer-knowledge", "developer knowledge",
)

CONTEXT7_DOC_KEYWORDS = (
    "react", "next.js", "nextjs", "vue", "nuxt", "svelte", "angular",
    "tailwind", "prisma", "drizzle", "supabase", "express", "fastify",
    "nestjs", "zod", "typescript", "vite", "webpack", "playwright",
    "vitest", "jest", "pytest", "fastapi", "django", "flask", "sqlalchemy",
    "pydantic", "three.js", "electron", "tauri", "docker", "kubernetes",
    "dotnet", ".net", "entity framework", "ef core", "maui", "skiasharp",
    "ffmpeg", "tokio", "serde", "axum", "langchain", "openai", "anthropic",
    "stripe", "auth0", "graphql", "trpc", "redux", "zustand", "shadcn",
    "radix", "axios", "tanstack", "context7",
)

DOC_INTENT_KEYWORDS = (
    "docs", "documentação", "documentacao", "api reference", "sdk",
    "biblioteca", "library", "framework", "sintaxe", "syntax",
    "deprecated", "depreciado", "migrar", "migration", "migração", "migracao",
)

GDK_DOMAINS = frozenset({"media-native"})


def compile_kw_regex(kw: str) -> re.Pattern[str]:
    k = kw.strip().lower()
    is_prefix = k.endswith("*")
    core = k[:-1] if is_prefix else k
    escaped = re.escape(core)
    start_bound = "" if core.startswith(".") else r"(?<![\w])"
    end_bound = r"[\w]*" if is_prefix else r"(?![\w])"
    return re.compile(f"{start_bound}{escaped}{end_bound}", re.IGNORECASE)


_ACTION_PATTERNS = [compile_kw_regex(v) for v in ACTION_VERBS]
_DEPTH_PATTERNS = [(kw.rstrip("*"), compile_kw_regex(kw)) for kw in HIGH_DEPTH_SIGNALS]
_GDK_DOC_PATTERNS = [compile_kw_regex(k) for k in GDK_DOC_KEYWORDS]
_C7_DOC_PATTERNS = [compile_kw_regex(k) for k in CONTEXT7_DOC_KEYWORDS]
_DOC_INTENT_PATTERNS = [compile_kw_regex(k) for k in DOC_INTENT_KEYWORDS]


def detect_doc_lookup_targets(prompt: str, domains: list[str] | None = None) -> list[str]:
    """Return ordered list of relevant doc MCP targets (`gdk`, `context7`) for a prompt."""
    clean = re.sub(r"\s+", " ", (prompt or "").strip().lower()).rstrip(".!?")
    if not clean or clean in SMALL_CONVERSATIONAL or len(clean) < 12:
        return []
    dom_set = set(domains or [])
    targets: list[str] = []
    if (dom_set & GDK_DOMAINS) or any(p.search(clean) for p in _GDK_DOC_PATTERNS):
        targets.append("gdk")
    if any(p.search(clean) for p in _C7_DOC_PATTERNS):
        targets.append("context7")
    elif not targets and any(p.search(clean) for p in _DOC_INTENT_PATTERNS):
        targets.append("context7")
    return targets


def load_project_profile(workspace_root: Path | None = None) -> dict[str, Any]:
    if not workspace_root:
        return {}
    profile_path = workspace_root / ".agents" / "planner_profile.json"
    if not profile_path.is_file():
        return {}
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(data, dict):
            return {}
        migrated, _, _ = migrate_profile(data)
        return migrated
    except Exception:
        return {}


def _merged_domain_specs(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if not profile.get("replace_default_domains"):
        for dom, spec in UNIVERSAL_DOMAINS.items():
            merged[dom] = dict(spec)
    custom = profile.get("domains") or {}
    if isinstance(custom, dict):
        for dom, spec in custom.items():
            if isinstance(spec, dict):
                merged[dom] = dict(spec)
    return merged


def _count_list_items(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if re.match(r"^\s*(?:\d{1,2}[\.\)\:]|[-*•])\s+\S{4,}", line):
            count += 1
    return count


def _count_action_clauses(text: str) -> int:
    raw_chunks = re.split(
        r"(?:[\n;!?]+|\.(?:\s+|$)|,\s+(?:e|and|mas|plus|também|tambem|also)\s+|\s+(?:além disso|alem disso|outra coisa|e também|e tambem|and also)\s+)",
        text.lower(),
    )
    active = 0
    for chunk in raw_chunks:
        c = chunk.strip()
        if len(c) < 8:
            continue
        if any(pat.search(c) for pat in _ACTION_PATTERNS):
            active += 1
    return active


def extract_auto_reqs(prompt: str, max_reqs: int = 8) -> list[str]:
    """Extract compact `REQ-01: ...` items from a user prompt in pure Python (0 LLM turns)."""
    text = (prompt or "").strip()
    if not text:
        return []

    candidates: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:\d{1,2}[\.\)\:]|[-*•])\s+(.+)$", line)
        if m:
            item = re.sub(r"\s+", " ", m.group(1).strip().strip("`"))
            if len(item) >= 6:
                candidates.append(item)

    if len(candidates) < 2:
        for line in text.splitlines():
            m = re.match(r"^\s*#{1,3}\s+(?:STEP|PART|[A-Z]\d+\.?|\d+\.)\s*[-—:]?\s*(.+)$", line, re.IGNORECASE)
            if m:
                item = re.sub(r"\s+", " ", m.group(1).strip())
                if len(item) >= 6 and item not in candidates:
                    candidates.append(item)

    if not candidates:
        raw_chunks = re.split(
            r"(?:[\n;!?]+|\.(?:\s+|$)|,\s+(?:e|and|mas|plus|também|tambem|also)\s+|\s+(?:além disso|alem disso|outra coisa|e também|e tambem|and also)\s+)",
            text,
            flags=re.IGNORECASE,
        )
        for chunk in raw_chunks:
            c = re.sub(r"\s+", " ", chunk.strip())
            if len(c) >= 10 and any(pat.search(c.lower()) for pat in _ACTION_PATTERNS):
                candidates.append(c)

    if not candidates:
        first_line = re.sub(r"\s+", " ", text.splitlines()[0].strip())
        if first_line:
            candidates.append(first_line)

    reqs: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        clean = item[:85].rstrip(" .,;:") + ("..." if len(item) > 85 else "")
        low = clean.lower()
        if low in seen:
            continue
        seen.add(low)
        reqs.append(f"REQ-{len(reqs) + 1:02d}: {clean}")
        if len(reqs) >= max_reqs:
            break
    return reqs


def _detect_domains(lower_text: str, domain_specs: dict[str, dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for dom, spec in domain_specs.items():
        kws = spec.get("keywords") or ()
        if any(compile_kw_regex(kw).search(lower_text) for kw in kws):
            found.append(dom)
    return found


def _is_pure_question_or_greeting(text: str, action_clauses: int, list_items: int) -> bool:
    clean = re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!?")
    if not clean:
        return True
    if clean in SMALL_CONVERSATIONAL:
        return True
    if len(clean) < 28 and action_clauses == 0 and list_items == 0:
        return True
    if list_items == 0 and action_clauses == 0 and clean.startswith(QUESTION_ONLY_PREFIXES):
        return True
    return False


def build_recommended_modules(
    tier: int,
    domains: list[str],
    depth_level: str,
    domain_specs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return an ordered list of composable planning module blueprints."""
    if tier == 0:
        return []

    specs = domain_specs or UNIVERSAL_DOMAINS
    modules: list[dict[str, Any]] = []
    if tier >= 2:
        modules.append({
            "id": "MOD-DISCOVERY",
            "title": "Scoped Reconnaissance & Invariant Map",
            "domain": "discovery",
            "purpose": "Locate exact files, line ranges, and constraints BEFORE deep code reads or edits.",
        })

    if tier == 3 or depth_level == "high":
        modules.append({
            "id": "MOD-CONTRACT",
            "title": "Architecture, State & Data Contract",
            "domain": "contract",
            "purpose": "Define types, thread/lifecycle boundaries, and error semantics BEFORE editing code.",
        })

    ship_modules: list[dict[str, Any]] = []
    impl_added = False
    for dom in domains:
        spec = specs.get(dom)
        if not spec:
            continue
        entry = {
            "id": spec.get("module_id") or f"MOD-{dom.upper()}",
            "title": spec.get("module_title") or dom,
            "domain": dom,
            "purpose": f"Execute scoped slices with `{dom}` guardrails attached.",
        }
        if spec.get("is_ship"):
            ship_modules.append(entry)
        else:
            modules.append(entry)
            impl_added = True

    if not impl_added:
        modules.append({
            "id": "MOD-IMPL",
            "title": "Core Implementation Slices",
            "domain": "general",
            "purpose": "Implement scoped changes in small verifiable batches (<= 3 files per step).",
        })

    if tier == 3 or depth_level == "high":
        modules.append({
            "id": "MOD-HARDEN",
            "title": "Edge-Case, Error Handling & Regression Hardening",
            "domain": "harden",
            "purpose": "Audit null/disposal paths, error handling, logging, and backward compatibility.",
        })

    modules.append({
        "id": "MOD-AUDIT",
        "title": "Verification, Build & REQ Sign-Off",
        "domain": "audit",
        "purpose": "Run verifier/tests and verify every REQ-XX against observed files/outputs.",
    })

    modules.extend(ship_modules)
    return modules


def analyze_prompt(prompt: str, workspace_root: Path | None = None) -> dict[str, Any]:
    """Classify prompt breadth, depth, tier (0..3), domains, and recommended modules."""
    text = (prompt or "").strip()
    lower = text.lower()

    profile = load_project_profile(workspace_root)
    domain_specs = _merged_domain_specs(profile)

    list_items = _count_list_items(text)
    action_clauses = _count_action_clauses(text)
    domains = _detect_domains(lower, domain_specs)
    depth_hits = [label for label, pat in _DEPTH_PATTERNS if pat.search(lower)]
    doc_targets = detect_doc_lookup_targets(text, domains)

    is_trivial_chat = _is_pure_question_or_greeting(text, action_clauses, list_items)
    if is_trivial_chat:
        return {
            "tier": 0,
            "tier_name": "DIRECT",
            "breadth_est": 0,
            "depth_level": "low",
            "depth_signals": [],
            "detected_domains": domains,
            "research_strategy": "none",
            "recommended_modules": [],
            "doc_lookup_targets": doc_targets,
        }

    breadth_est = max(list_items, action_clauses, 1 if (domains or len(text) > 25) else 0)

    tiny_fix_hints = ("typo", "ortografia", "1 linha", "uma linha", "rename", "renomea")
    is_tiny_fix = (
        breadth_est <= 1
        and len(text) < 120
        and len(domains) <= 1
        and not depth_hits
        and any(compile_kw_regex(h).search(lower) for h in tiny_fix_hints)
    )
    if is_tiny_fix:
        return {
            "tier": 0,
            "tier_name": "DIRECT",
            "breadth_est": 1,
            "depth_level": "low",
            "depth_signals": [],
            "detected_domains": domains,
            "research_strategy": "inline_grep",
            "recommended_modules": [],
            "doc_lookup_targets": doc_targets,
        }

    depth_score = len(depth_hits) * 2
    if len(domains) >= 3:
        depth_score += 3
    elif len(domains) == 2:
        depth_score += 2
    if len(text) >= 600:
        depth_score += 2
    elif len(text) >= 280:
        depth_score += 1

    if depth_score >= 4:
        depth_level = "high"
    elif depth_score >= 2 or len(domains) == 2:
        depth_level = "medium"
    else:
        depth_level = "low"

    if depth_level == "high" and (breadth_est >= 3 or len(domains) >= 2 or len(depth_hits) >= 2 or len(text) >= 400):
        tier = 3
        tier_name = "FRACTAL_DEEP"
    elif breadth_est >= 3 or (breadth_est >= 2 and len(domains) >= 2):
        tier = 2
        tier_name = "META_MODULES"
    elif breadth_est >= 1 or domains or depth_hits:
        tier = 1
        tier_name = "STANDARD"
    else:
        tier = 0
        tier_name = "DIRECT"

    if tier == 3 or (tier == 2 and len(domains) >= 3):
        research_strategy = "research_subagent_or_scoped_grep"
    elif tier >= 1:
        research_strategy = "scoped_grep_and_line_slices"
    else:
        research_strategy = "inline_grep"

    modules = build_recommended_modules(tier, domains, depth_level, domain_specs)
    return {
        "tier": tier,
        "tier_name": tier_name,
        "breadth_est": breadth_est,
        "depth_level": depth_level,
        "depth_signals": depth_hits[:8],
        "detected_domains": domains,
        "research_strategy": research_strategy,
        "recommended_modules": modules,
        "doc_lookup_targets": doc_targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze prompt complexity and recommend dynamic planning tier.")
    parser.add_argument("--prompt", default="", help="Prompt text to analyze.")
    parser.add_argument("--stdin", action="store_true", help="Read prompt text from stdin.")
    parser.add_argument("--workspace", default="", help="Workspace root for optional .agents/planner_profile.json.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    args = parser.parse_args()

    text = sys.stdin.read() if args.stdin else args.prompt
    ws = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    result = analyze_prompt(text, ws)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(
        f"Tier {result['tier']} ({result['tier_name']}) | "
        f"Breadth~{result['breadth_est']} | Depth={result['depth_level']} | "
        f"Domains={','.join(result['detected_domains']) or 'general'}"
    )
    print(f"Research Strategy: {result['research_strategy']}")
    for m in result["recommended_modules"]:
        print(f"  - [{m['id']}] ({m['domain']}) {m['title']}: {m['purpose']}")


if __name__ == "__main__":
    main()
