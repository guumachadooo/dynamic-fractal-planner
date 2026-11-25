#!/usr/bin/env python3
"""Universal adaptive complexity & breadth triage analyzer with project-profile extensibility."""

from __future__ import annotations

import argparse
from functools import lru_cache
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_HOOKS_DIR = _SCRIPTS_DIR.parent / "hooks"
for _p in (str(_SCRIPTS_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema_migrations import migrate_profile, resolve_blueprint_config  # noqa: E402

try:
    from planner_state import detect_pace_mode  # noqa: E402
except Exception:
    def detect_pace_mode(text: str, profile: dict[str, Any] | None = None) -> str:  # type: ignore[misc]
        return "turbo"

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

_GREETING_LEAD_RE = re.compile(
    r"^(?:(?:oi+|ol[aá]+|e\s+a[ií]|fala|bom\s+dia|boa\s+tarde|boa\s+noite|"
    r"tudo\s+bem|td\s+bem|como\s+vai|hey|hi+|hello|good\s+(?:morning|afternoon|evening))"
    r"(?:[\s,!?.:-]+|$))+",
    re.IGNORECASE,
)

_FRAMEWORK_DOT_TOKENS = frozenset({
    "next.js", "vue.js", "node.js", "three.js", "react.js", "nuxt.js",
    "express.js", "solid.js", "alpine.js", "nest.js", "chart.js", "d3.js",
})

_FILE_EXT_PAT = (
    r"py|js|jsx|mjs|cjs|ts|tsx|cs|csproj|sln|vb|fs|java|kt|kts|swift|rs|go|"
    r"c|cpp|cc|h|hpp|rb|php|sql|sh|bash|yml|yaml|toml|html|css|scss|xml|json|"
    r"jsonl|axml|resx|md|txt|gradle|props|targets|lock|env|ini|cfg|conf"
)

_FILE_REF_RE = re.compile(
    rf"(?<![\w/.-])("
    rf"(?:[A-Za-z0-9_.~-]+/)*[A-Za-z0-9_.-]+\.(?:{_FILE_EXT_PAT})(?![\w.-])"
    rf"|(?:Makefile|Dockerfile|Jenkinsfile)(?![\w.-])"
    rf")",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://[^\s)\"'>]+", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```[\s\S]{120,}?```")
_DOUBLE_QUOTE_LONG_RE = re.compile(r'["“][^"”]{120,}["”]')
_BLOCKQUOTE_RUN_RE = re.compile(r"(?:^|\n)(?:>[^\n]*(?:\n|$))+")


@lru_cache(maxsize=512)
def compile_kw_regex(kw: str) -> re.Pattern[str]:
    k = kw.strip().lower()
    is_prefix = k.endswith("*")
    core = k[:-1] if is_prefix else k
    escaped = re.escape(core)
    start_bound = "" if core.startswith(".") else r"(?<![\w])"
    end_bound = r"[\w]*" if is_prefix else r"(?![\w])"
    return re.compile(f"{start_bound}{escaped}{end_bound}", re.IGNORECASE)


_BROAD_SCOPE_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "todos os", "todas as", "projeto inteiro", "repositório inteiro", "repositorio inteiro",
        "vários arquivos", "varios arquivos", "cada arquivo", "tudo isso",
        "all files", "all hooks", "all endpoints", "across the", "entire repo",
        "entire project", "whole codebase", "every file", "multiple files",
        "project-wide", "repo-wide", "everywhere",
    )
]

_READ_ONLY_GUARD_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "ainda não faz", "ainda nao faz", "não faz isso", "nao faz isso",
        "não mexe", "nao mexe", "sem alterar", "sem editar", "sem modificar",
        "somente leitura", "só leitura", "so leitura", "só me situa", "so me situa",
        "me situa", "só analisa", "so analisa", "don't edit", "do not edit",
        "read-only", "read only", "just explain", "without modifying", "without editing",
    )
]

_PLAN_PROPOSAL_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "plano", "plan", "auditoria", "audit", "audita", "propõe", "propoe",
        "propose", "architect*", "arquitetura", "roadmap",
    )
]

_GRATITUDE_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "obrigad*", "valeu", "brigad*", "agradeç*", "agradec*",
        "thanks", "thank you", "thx", "ty", "tmj",
    )
]

_DIRECT_APPROVAL_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "pode fazer", "pode seguir", "pode rodar", "pode aplicar", "pode ir",
        "manda ver", "bora", "fechou", "aprovado", "approved", "lgtm",
        "go ahead", "proceed", "do it", "faz isso", "faz tudo", "faça tudo",
        "faca tudo", "aplica", "executa", "vai fundo", "corrige aí", "corrige ai",
        "corrija aí", "corrija ai", "arruma aí", "arruma ai", "segue em frente",
    )
]

_BARE_APPROVAL_WORDS = frozenset({"sim", "yes", "pode"})
_PRAISE_ACK_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "muito bom", "ótimo", "otimo", "perfeito", "excelente", "show",
        "massa", "top", "legal", "gostei", "looks good", "sounds good",
        "great", "awesome", "nice", "ok", "beleza", "certo", "combinado",
    )
]
_APPROVAL_VERB_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "pode", "faz", "fazer", "faça", "faca", "aplica", "aplicar",
        "executa", "executar", "implementa", "implementar", "manda",
        "segue", "seguir", "proceed", "go ahead", "do", "vai",
    )
]
_ANAPHORIC_REF_RE = re.compile(
    r"(?:tudo\s+isso|esse\s+plano|essa\s+proposta|essas\s+mudan[cç]as|"
    r"op[cç][aã]o\s+\d+|fase\s+\d+|all\s+of\s+(?:it|that)|that\s+plan|"
    r"those\s+changes|as\s+proposed|option\s+\d+|phase\s+\d+)",
    re.IGNORECASE,
)

_CONTINUATION_PATTERNS = [
    compile_kw_regex(p)
    for p in (
        "continua", "continue", "faz o resto", "faça o resto", "faca o resto",
        "próxima parte", "proxima parte", "próximo passo", "proximo passo",
        "next step", "next part", "retoma", "resume", "tenta de novo",
        "try again", "fix that error", "corrige esse erro", "arruma esse erro",
        "termina o resto", "finish the rest",
    )
]
_PART_STEP_RE = re.compile(r"\b(?:parte|part|etapa|step)\s+\d+\b", re.IGNORECASE)

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

_ACTION_PATTERNS = [compile_kw_regex(v) for v in ACTION_VERBS]
_DEPTH_PATTERNS = [(kw.rstrip("*"), compile_kw_regex(kw)) for kw in HIGH_DEPTH_SIGNALS]
_GDK_DOC_PATTERNS = [compile_kw_regex(k) for k in GDK_DOC_KEYWORDS]
_C7_DOC_PATTERNS = [compile_kw_regex(k) for k in CONTEXT7_DOC_KEYWORDS]
_DOC_INTENT_PATTERNS = [compile_kw_regex(k) for k in DOC_INTENT_KEYWORDS]

_BTW_QUEUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*/btw\b", re.IGNORECASE),
    re.compile(r"\b(?:manda|diz|diga|fala|avisa|pede|pe[cç]a)\s+(?:pra\s+|para\s+)?(?:ele|ela|o\s+agente(?:\s+principal)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:lembra|anota|registra)\s+(?:pra\s+|para\s+|de\s+)?(?:ele|ela|o\s+agente(?:\s+principal)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:quando|assim\s+que|depois\s+(?:que)?)\s+(?:ele\s+)?(?:acabar|terminar|finalizar|concluir)\b", re.IGNORECASE),
    re.compile(r"\b(?:adiciona|coloca|bota|p[oõ]e)\w*\s+nas?\s+(?:extra\s+)?requests?\b", re.IGNORECASE),
    re.compile(r"\bdepois\s+disso\b", re.IGNORECASE),
    re.compile(r"\b(?:coloca\w*\s+na\s+fila|enfileira\w*|para\s+o\s+agente\s+principal)\b", re.IGNORECASE),
    re.compile(r"\b(?:tell|ask|have)\s+(?:him|her|it|the\s+main\s+agent)\s+to\b", re.IGNORECASE),
    re.compile(r"\b(?:when|once|after)\s+(?:it|he|she|the\s+main\s+agent)\s+(?:finishes|is\s+done|completes)\b", re.IGNORECASE),
    re.compile(r"\b(?:queue\s+(?:this\s+)?for\s+(?:the\s+)?main|for\s+the\s+main\s+agent)\b", re.IGNORECASE),
)


def detect_btw_queue_intent(text: str) -> bool:
    """Detect `/btw` or main-agent queueing intent in short user prompts."""
    scoring = _strip_long_quotes(text or "").strip()
    if not scoring or len(scoring) > 500:
        return False
    return any(pat.search(scoring) for pat in _BTW_QUEUE_PATTERNS)


_BLUEPRINT_AUTHORING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:creat\w*|writ\w*|draft\w*|design\w*|generat\w*|build\w*|author\w*|structur\w*|prepar\w*|propos\w*|"
        r"cri\w*|faz\w*|ger\w*|mont\w*|escrev\w*|estrutur\w*|elabor\w*|desenh\w*|planej\w*)\b"
        r"[^\n.;!?]{0,70}"
        r"\b(?:sota\s+blueprint|blueprint|design\s*doc|architecture\s+doc|technical\s+spec|spec\s+doc|rfc\b|"
        r"especifica[cç][aã]o\s+t[eé]cnica|documento\s+de\s+(?:arquitetura|design))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:sota\s+blueprint|blueprint\s+(?:for|of|de|para|do|da)|"
        r"plan_tool\.py\s+blueprint|sota-blueprint)\b",
        re.IGNORECASE,
    ),
)
_BLUEPRINT_META_EDIT_RE = re.compile(
    r"\b(?:blueprint_engine\.py|complexity_triage\.py|planner_pre_invocation\.py|run_plugin_tests\.py|detect_blueprint_intent)\b",
    re.IGNORECASE,
)
_BLUEPRINT_NEGATION_RE = re.compile(
    r"\b(?:n[ãa]o|sem|don't|do\s+not|never|skip|stop|par[ae]\s+de|deixa\s+(?:de|para))\b"
    r"[^\n.;!?]{0,30}\b(?:faz\w*|cri\w*|ger\w*|mont\w*|escrev\w*|creat\w*|writ\w*|draft\w*|generat\w*|build\w*)?"
    r"[^\n.;!?]{0,25}\b(?:sota\s+blueprint|blueprint|design\s*doc|rfc\b|technical\s+spec)\b",
    re.IGNORECASE,
)

_PLUGIN_SYNC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:atualiz\w*|sincroniz\w*|sync\w*|updat\w*|propaga\w*|sobe|subir|push\w*|alinh\w*)\b"
        r"[^\n.;!?]{0,45}"
        r"\b(?:plugin|dynamic-fractal-planner|fractal)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:plugin|dynamic-fractal-planner)\b"
        r"[^\n.;!?]{0,45}"
        r"\b(?:github|todos\s+os\s+(?:reposit|projet)|3\s+repo|frota|fleet|global|cavern|omniagent)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:sync-plugin|evolve\s+sync-all)\b", re.IGNORECASE),
)
_PLUGIN_SYNC_EXCLUDE_RE = re.compile(
    r"\b(?:cria\w*|creat\w*|implement\w*|adicion\w*|add\b|refator\w*|refactor\w*|architect\w*|arquitet\w*|"
    r"blueprint|corrig\w*|fix\b|debug\w*|bug\b|erro\w*|error\b|por\s+que|why\b|explic\w*|explain\w*|"
    r"como\s+funciona|how\s+does|novo\s+recurso|new\s+feature|edita\w*|edit\w*|escrev\w*|write\b|teste\w*|test\w*|revis\w*|review\b)\b",
    re.IGNORECASE,
)


def detect_plugin_sync_intent(text: str) -> bool:
    """Detect concise zero-thinking request to sync/push the plugin across Cavern/fleet."""
    scoring = _strip_long_quotes(text or "").strip()
    if not scoring or len(scoring) > 220:
        return False
    if _PLUGIN_SYNC_EXCLUDE_RE.search(scoring):
        return False
    return any(pat.search(scoring) for pat in _PLUGIN_SYNC_PATTERNS)


_BLUEPRINT_OPT_OUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    _BLUEPRINT_NEGATION_RE,
    re.compile(
        r"\b(?:sem\s+(?:fazer\s+)?(?:o\s+)?blueprint|pula\w*\s+(?:o\s+)?blueprint|"
        r"no\s+blueprint|skip\s+(?:the\s+)?blueprint|without\s+(?:a\s+)?blueprint|"
        r"economiz\w*\s+tokens?|poupar?\s+tokens?|save\s+tokens?|modo\s+eco|eco\s+mode)\b",
        re.IGNORECASE,
    ),
)

_PLAN_RESUME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:retom\w*|continu\w*|execut\w*|volt\w*\s+(?:pro|para\s+o|ao)|segu\w*\s+(?:com\s+)?o|resum\w*|pick\s+up)\b"
        r"[^\n.;!?]{0,45}"
        r"\b(?:plano|planos|roadmap|fase\s*\d+|phase\s*\d+|etapa\s*\d+|onda\s*\d+|wave\s*\d+|plan\b|blueprint)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:fase\s*3|phase\s*3|vocal\s+cortex|w3[1-9]\b|w4[0-4]\b|pr[oó]xima\s+(?:onda|etapa|fase)|next\s+(?:wave|phase|stage))\b",
        re.IGNORECASE,
    ),
)


def detect_blueprint_opt_out(text: str) -> bool:
    """Detect explicit user instruction to skip SOTA blueprint (e.g. to save tokens)."""
    scoring = _strip_long_quotes(text or "").strip()
    if not scoring:
        return False
    return any(pat.search(scoring) for pat in _BLUEPRINT_OPT_OUT_PATTERNS)


def detect_plan_resume_intent(text: str) -> bool:
    """Detect intent to resume or execute an existing plan, roadmap, phase, or wave."""
    scoring = _strip_long_quotes(text or "").strip()
    if not scoring or len(scoring) < 6:
        return False
    return any(pat.search(scoring) for pat in _PLAN_RESUME_PATTERNS)


def detect_blueprint_intent(text: str) -> bool:
    """Detect explicit request to author/scaffold a SOTA blueprint, RFC, or technical design spec."""
    scoring = _strip_long_quotes(text or "").strip()
    if not scoring or len(scoring) < 12:
        return False
    if _BLUEPRINT_META_EDIT_RE.search(scoring) or detect_blueprint_opt_out(scoring):
        return False
    lower = scoring.lower()
    if lower.startswith(QUESTION_ONLY_PREFIXES) and not any(
        compile_kw_regex(v).search(lower) for v in ("cria*", "faz*", "gera*", "monta*", "creat*", "writ*", "draft*", "generat*", "build*")
    ):
        return False
    return any(pat.search(scoring) for pat in _BLUEPRINT_AUTHORING_PATTERNS)


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
                base = dict(merged.get(dom) or {})
                base.update(spec)
                merged[dom] = base
    return merged


def _ordered_union(*groups: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for grp in groups:
        for item in grp or ():
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _strip_long_quotes(text: str) -> str:
    """Strip >=120-char quoted/fenced/blockquote blocks for imperative scoring when >=30 chars remain."""
    raw = text or ""
    if len(raw) < 150:
        return raw
    stripped = _FENCED_BLOCK_RE.sub(" ", raw)
    stripped = _DOUBLE_QUOTE_LONG_RE.sub(" ", stripped)

    def _drop_big_bq(m: re.Match[str]) -> str:
        blk = m.group(0)
        return "\n" if len(blk.strip()) >= 120 else blk

    stripped = _BLOCKQUOTE_RUN_RE.sub(_drop_big_bq, stripped)
    stripped = re.sub(r"[ \t]+", " ", stripped).strip()
    return stripped if len(stripped) >= 30 else raw


def _extract_file_refs(text: str) -> list[str]:
    cleaned = _URL_RE.sub(" ", text or "")
    refs: list[str] = []
    seen: set[str] = set()
    for m in _FILE_REF_RE.finditer(cleaned):
        tok = m.group(1).strip("`'\",.;:()[]{}")
        low = tok.lower()
        if not tok or low in _FRAMEWORK_DOT_TOKENS:
            continue
        if low not in seen:
            seen.add(low)
            refs.append(tok)
    return refs


def _has_broad_scope(lower_text: str) -> bool:
    return any(p.search(lower_text) for p in _BROAD_SCOPE_PATTERNS)


def _strip_leading_greetings(text: str) -> str:
    cur = (text or "").strip()
    while cur:
        nxt = _GREETING_LEAD_RE.sub("", cur, count=1).strip()
        if nxt == cur:
            break
        cur = nxt
    return cur


def _split_action_clauses(text: str) -> list[str]:
    primary = re.split(
        r"(?:[\n;!?]+|\.(?:\s+|$)|,\s+(?:e|and|mas|plus|também|tambem|also)\s+|\s+(?:além disso|alem disso|outra coisa|e também|e tambem|and also)\s+)",
        text,
        flags=re.IGNORECASE,
    )
    out: list[str] = []
    conj_re = re.compile(r"\s+(?:e|and|then|depois|tamb[eé]m)\s+", re.IGNORECASE)
    for chunk in primary:
        c = chunk.strip()
        if not c:
            continue
        parts = conj_re.split(c)
        if len(parts) == 1:
            out.append(c)
            continue
        buf = parts[0]
        for nxt in parts[1:]:
            head = " ".join(nxt.strip().split()[:3]).lower()
            if any(pat.search(head) for pat in _ACTION_PATTERNS):
                if buf.strip():
                    out.append(buf.strip())
                buf = nxt
            else:
                buf = f"{buf} e {nxt}"
        if buf.strip():
            out.append(buf.strip())
    return out


def _count_list_items(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if re.match(r"^\s*(?:\d{1,2}[\.\)\:]|[-*•])\s+\S{4,}", line):
            count += 1
    return count


def _count_action_clauses(text: str) -> int:
    active = 0
    for chunk in _split_action_clauses(text.lower()):
        c = chunk.strip()
        if len(c) < 8:
            continue
        if any(pat.search(c) for pat in _ACTION_PATTERNS):
            active += 1
    return active


def extract_auto_reqs(prompt: str, max_reqs: int = 6, structured_only: bool = False) -> list[str]:
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

    is_structured = bool(candidates)
    if structured_only and not is_structured:
        return []

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
        clean = item[:65].rstrip(" .,;:") + ("..." if len(item) > 65 else "")
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
    stripped = _strip_leading_greetings(text)
    if not stripped:
        return True
    clean = re.sub(r"\s+", " ", stripped.lower()).rstrip(".!?")
    if not clean or clean in SMALL_CONVERSATIONAL:
        return True
    if len(clean) < 28 and action_clauses == 0 and list_items == 0:
        return True
    if list_items == 0 and action_clauses == 0:
        if clean.startswith(QUESTION_ONLY_PREFIXES) or stripped.rstrip().endswith("?"):
            return True
    return False


def classify_followup_intent(text: str) -> str:
    """Classify turn follow-up intent: gratitude_close | approval_execute | task_continuation | pure_question | none."""
    raw = (text or "").strip()
    if not raw:
        return "none"
    lower = re.sub(r"\s+", " ", raw.lower())
    stripped = _strip_leading_greetings(raw)
    stripped_clean = re.sub(r"\s+", " ", stripped.lower()).strip(" .!?")

    has_gratitude = any(p.search(lower) for p in _GRATITUDE_PATTERNS)
    has_action = any(p.search(lower) for p in _ACTION_PATTERNS)
    has_direct_approval = any(p.search(lower) for p in _DIRECT_APPROVAL_PATTERNS) or (
        stripped_clean in _BARE_APPROVAL_WORDS
    )
    has_praise_ack = any(p.search(lower) for p in _PRAISE_ACK_PATTERNS)
    has_approval_verb = any(p.search(lower) for p in _APPROVAL_VERB_PATTERNS)
    has_anaphoric = bool(_ANAPHORIC_REF_RE.search(lower))
    has_comp_approval = has_praise_ack and (has_approval_verb or has_anaphoric)
    has_continuation = any(p.search(lower) for p in _CONTINUATION_PATTERNS) or bool(_PART_STEP_RE.search(lower))

    clauses = _count_action_clauses(raw)
    items = _count_list_items(raw)
    is_q = _is_pure_question_or_greeting(raw, clauses, items)

    if has_gratitude and not has_action and not has_direct_approval and not has_comp_approval and not has_continuation and not (is_q and "?" in raw):
        return "gratitude_close"
    if has_direct_approval or has_comp_approval:
        return "approval_execute"
    if has_continuation:
        return "task_continuation"

    if is_q:
        return "pure_question"
    return "none"


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

    if tier == 3:
        modules.append({
            "id": "MOD-SOTA-PREFLIGHT",
            "title": "First-Principles SOTA, Omniscient Fusion, Plane Split & Golden Bench",
            "domain": "sota-preflight",
            "purpose": "Verify live SOTA (web/MCP), fuse owned upstream signals/hw (joint solver > blind serial chain), split offline/RT/control planes, justify AI vs deterministic math, and define fast offline golden tests BEFORE architecture lock.",
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


_TIER_NAMES = {0: "DIRECT", 1: "STANDARD", 2: "META_MODULES", 3: "FRACTAL_DEEP"}
_DEPTH_ORDER = {"low": 0, "medium": 1, "high": 2}


def _research_strategy_for(tier: int, domains: list[str], tiny_fix: bool = False) -> str:
    if tier == 3 or (tier == 2 and len(domains) >= 3):
        return "research_subagent_or_scoped_grep"
    if tier >= 1:
        return "scoped_grep_and_line_slices"
    return "inline_grep" if tiny_fix else "none"


def _compute_depth_level(depth_hits: list[str], domains: list[str], text_len: int, n_files: int, broad_scope: bool) -> str:
    depth_score = len(depth_hits) * 2
    if len(domains) >= 3:
        depth_score += 3
    elif len(domains) == 2:
        depth_score += 2
    if text_len >= 600:
        depth_score += 2
    elif text_len >= 280:
        depth_score += 1
    if n_files >= 6:
        depth_score += 2
    elif n_files >= 4:
        depth_score += 1
    if broad_scope:
        depth_score += 1

    if depth_score >= 4:
        return "high"
    if depth_score >= 2 or len(domains) == 2:
        return "medium"
    return "low"


def _triage_confidence(tier: int, scoring_len: int, file_refs: list[str], domains: list[str], depth_hits: list[str], list_items: int, source: str, intent: str) -> str:
    if source in ("context_approval", "context_continuation", "exit_plan_mode"):
        return "high"
    if intent in ("gratitude_close", "tiny_fix", "pure_question"):
        return "high"
    if tier >= 2 and (len(file_refs) >= 3 or list_items >= 2 or len(domains) >= 2 or len(depth_hits) >= 2):
        return "high"
    if tier <= 1 and scoring_len < 40 and not file_refs and not domains:
        return "low"
    if tier == 1 and not file_refs and not domains:
        return "low"
    return "medium"


def analyze_prompt(
    prompt: str,
    workspace_root: Path | None = None,
    context: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Classify prompt breadth, depth, tier (0..3), domains, and recommended modules (w/ multi-turn continuity)."""
    text = (prompt or "").strip()
    lower = text.lower()
    scoring_text = _strip_long_quotes(text)
    scoring_lower = scoring_text.lower()

    profile = load_project_profile(workspace_root)
    pace_mode = detect_pace_mode(text, profile)
    domain_specs = _merged_domain_specs(profile)

    file_refs = _extract_file_refs(text)
    domains = _detect_domains(lower, domain_specs)
    depth_hits = [label for label, pat in _DEPTH_PATTERNS if pat.search(lower)]
    doc_targets = detect_doc_lookup_targets(text, domains)

    list_items = _count_list_items(scoring_text)
    action_clauses = _count_action_clauses(scoring_text)
    broad_scope = _has_broad_scope(scoring_lower)
    followup = classify_followup_intent(scoring_text)
    plugin_sync_intent = detect_plugin_sync_intent(text)
    bp_cfg = resolve_blueprint_config(profile)
    bp_opt_out = detect_blueprint_opt_out(text)
    plan_resume = detect_plan_resume_intent(text)
    if plugin_sync_intent:
        return {
            "tier": 0,
            "tier_name": "DIRECT",
            "breadth_est": 0,
            "depth_level": "low",
            "depth_signals": [],
            "detected_domains": domains,
            "research_strategy": "none",
            "recommended_modules": [],
            "doc_lookup_targets": [],
            "source": "prompt",
            "intent": "plugin_sync_fastpath",
            "confidence": "high",
            "file_refs": file_refs[:12],
            "btw_queue_intent": False,
            "blueprint_intent": False,
            "blueprint_opt_out": bp_opt_out,
            "blueprint_required_by_policy": False,
            "plan_resume_intent": False,
            "plugin_sync_intent": True,
            "pace_mode": pace_mode,
        }
    btw_intent = detect_btw_queue_intent(text)
    bp_intent = detect_blueprint_intent(text)
    has_read_only = any(p.search(scoring_lower) for p in _READ_ONLY_GUARD_PATTERNS)
    has_plan_prop = any(p.search(scoring_lower) for p in _PLAN_PROPOSAL_PATTERNS)

    tiny_fix_hints = ("typo", "ortografia", "1 linha", "uma linha", "rename", "renomea")
    tiny_copy_re = re.compile(
        r"\b(?:mud\w*|alter\w*|troc\w*|corrig\w*|chang\w*|updat\w*|fix\w*|renam\w*)\s+"
        r"(?:o\s+|a\s+|the\s+)?(?:texto|label|r[oó]tulo|cor|color|t[ií]tulo|title|placeholder|copy)\b"
        r"|\b(?:texto|label|cor)\s+do\s+bot[aã]o\b"
        r"|\bbutton\s+(?:text|label|color)\b",
        re.IGNORECASE,
    )
    raw_breadth = max(list_items, action_clauses, len(file_refs), 1 if (domains or len(scoring_text) > 25) else 0)
    if broad_scope:
        raw_breadth = max(raw_breadth, 3)

    is_explicit_copy_fix = (
        raw_breadth <= 1
        and len(file_refs) < 2
        and not broad_scope
        and len(scoring_text) < 120
        and not depth_hits
        and not bp_intent
        and bool(tiny_copy_re.search(scoring_lower))
    )
    is_tiny_fix = is_explicit_copy_fix or (
        raw_breadth <= 1
        and len(file_refs) < 2
        and not broad_scope
        and len(scoring_text) < 120
        and len(domains) <= 1
        and not depth_hits
        and any(compile_kw_regex(h).search(scoring_lower) for h in tiny_fix_hints)
    )

    is_pure_q = _is_pure_question_or_greeting(scoring_text, action_clauses, list_items)

    # Check multi-turn context continuity eligibility before early exits
    cur_ts = float(now if now is not None else time.time())
    ctx = context if isinstance(context, dict) else {}
    ctx_tier = int(ctx.get("tier") or 0)
    ctx_ts = float(ctx.get("ts") or 0.0)
    ctx_ttl = float(ctx.get("ttl_secs") or 1800.0)
    ctx_valid = bool(ctx and ctx_tier >= 2 and ctx_ts > 0.0 and (cur_ts - ctx_ts) <= ctx_ttl)
    ctx_status = str(ctx.get("status") or "")
    read_only_block = has_read_only and not has_plan_prop

    can_inherit = (
        ctx_valid
        and not is_tiny_fix
        and not read_only_block
        and followup not in ("gratitude_close", "pure_question")
        and (
            (ctx_status == "proposed" and followup in ("approval_execute", "task_continuation"))
            or (
                ctx_status == "in_progress"
                and (followup == "task_continuation" or (bool(ctx.get("build_failed")) and followup == "approval_execute"))
            )
        )
    )

    if not can_inherit and followup == "gratitude_close":
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
            "source": "prompt",
            "intent": "gratitude_close",
            "confidence": "high",
            "file_refs": file_refs[:12],
            "btw_queue_intent": btw_intent,
            "blueprint_intent": False,
            "blueprint_opt_out": bp_opt_out,
            "blueprint_required_by_policy": False,
            "plan_resume_intent": False,
            "plugin_sync_intent": False,
            "pace_mode": pace_mode,
        }

    if not can_inherit and is_pure_q:
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
            "source": "prompt",
            "intent": "pure_question",
            "confidence": "high",
            "file_refs": file_refs[:12],
            "btw_queue_intent": btw_intent,
            "blueprint_intent": bp_intent,
            "blueprint_opt_out": bp_opt_out,
            "blueprint_required_by_policy": False,
            "plan_resume_intent": plan_resume,
            "plugin_sync_intent": False,
            "pace_mode": pace_mode,
        }

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
            "source": "prompt",
            "intent": "tiny_fix",
            "confidence": "high",
            "file_refs": file_refs[:12],
            "btw_queue_intent": btw_intent,
            "blueprint_intent": False,
            "blueprint_opt_out": bp_opt_out,
            "blueprint_required_by_policy": False,
            "plan_resume_intent": False,
            "plugin_sync_intent": False,
            "pace_mode": pace_mode,
        }

    breadth_est = raw_breadth
    depth_level = _compute_depth_level(depth_hits, domains, len(scoring_text), len(file_refs), broad_scope)

    if depth_level == "high" and (breadth_est >= 3 or len(domains) >= 2 or len(depth_hits) >= 2 or len(scoring_text) >= 400):
        tier = 3
    elif breadth_est >= 3 or len(file_refs) >= 4 or (breadth_est >= 2 and len(domains) >= 2):
        tier = 2
    elif breadth_est >= 1 or domains or depth_hits:
        tier = 1
    else:
        tier = 0

    source = "prompt"
    intent = "plan_proposal" if has_plan_prop else "task"
    inherited_turn: int | None = None

    if read_only_block:
        intent = "read_only_analysis"
        tier = 0 if (action_clauses == 0 and list_items == 0 and not depth_hits) else min(tier, 1)
        if tier <= 1:
            breadth_est = min(breadth_est, 1)

    if can_inherit:
        tier = max(tier, ctx_tier)
        domains = _ordered_union(domains, ctx.get("detected_domains"))
        doc_targets = _ordered_union(doc_targets, ctx.get("doc_lookup_targets"))
        depth_hits = _ordered_union(depth_hits, ctx.get("depth_signals"))
        ctx_depth = str(ctx.get("depth_level") or "low")
        if _DEPTH_ORDER.get(ctx_depth, 0) > _DEPTH_ORDER.get(depth_level, 0):
            depth_level = ctx_depth
        file_refs = _ordered_union(file_refs, ctx.get("file_refs"))
        breadth_est = max(breadth_est, 3 if tier >= 2 else 1)
        source = "context_approval" if followup == "approval_execute" else "context_continuation"
        intent = followup
        inherited_turn = int(ctx.get("turn_id") or 0)

    tier_name = _TIER_NAMES.get(tier, "DIRECT")
    research_strategy = _research_strategy_for(tier, domains)
    modules = build_recommended_modules(tier, domains, depth_level, domain_specs)
    confidence = _triage_confidence(tier, len(scoring_text), file_refs, domains, depth_hits, list_items, source, intent)

    bp_min_tier = int(bp_cfg.get("default_min_tier") or 1)
    bp_required = (
        bool(bp_cfg.get("require_by_default", False))
        and not bp_opt_out
        and not btw_intent
        and intent not in ("read_only_analysis", "gratitude_close", "pure_question", "tiny_fix")
        and tier >= bp_min_tier
    )

    bp_rec_fmt = "none" if tier == 0 else ("mini" if tier == 1 else ("compact" if tier == 2 else "full"))
    result: dict[str, Any] = {
        "tier": tier,
        "tier_name": tier_name,
        "breadth_est": breadth_est,
        "depth_level": depth_level,
        "depth_signals": depth_hits[:8],
        "detected_domains": domains,
        "research_strategy": research_strategy,
        "recommended_modules": modules,
        "doc_lookup_targets": doc_targets,
        "source": source,
        "intent": intent,
        "confidence": confidence,
        "file_refs": file_refs[:12],
        "btw_queue_intent": btw_intent,
        "blueprint_intent": bp_intent,
        "blueprint_opt_out": bp_opt_out,
        "blueprint_required_by_policy": bp_required,
        "recommended_blueprint_format": bp_rec_fmt,
        "plan_resume_intent": plan_resume,
        "plugin_sync_intent": False,
        "pace_mode": pace_mode,
    }
    if inherited_turn is not None:
        result["inherited_from_turn"] = inherited_turn
    return result


def analyze_plan_text(plan_text: str, workspace_root: Path | None = None) -> dict[str, Any]:
    """Extract real scope/tier from native `exit_plan_mode(plan=...)` text."""
    text = (plan_text or "").strip()
    lower = text.lower()
    profile = load_project_profile(workspace_root)
    domain_specs = _merged_domain_specs(profile)

    file_refs = _extract_file_refs(text)
    domains = _detect_domains(lower, domain_specs)
    depth_hits = [label for label, pat in _DEPTH_PATTERNS if pat.search(lower)]
    doc_targets = detect_doc_lookup_targets(text, domains)

    steps = _count_list_items(text)
    heading_steps = sum(
        1 for ln in text.splitlines() if re.match(r"^\s*(?:#{1,4}\s+|STEP\s+\d+|MOD-|PHASE\s+\d+)", ln, re.IGNORECASE)
    )
    n_files = len(file_refs)
    n_steps = max(steps, heading_steps, _count_action_clauses(text))
    depth_level = _compute_depth_level(depth_hits, domains, len(text), n_files, _has_broad_scope(lower))

    if n_files >= 5 or (n_files >= 3 and (len(domains) >= 2 or len(depth_hits) >= 1)):
        tier = 3
    elif n_files >= 3 or (n_files >= 2 and (n_steps >= 3 or len(domains) >= 2)) or n_steps >= 5:
        tier = 2
    elif n_files >= 1 or n_steps >= 1:
        tier = 1
    else:
        tier = 0

    return {
        "tier": tier,
        "tier_name": _TIER_NAMES.get(tier, "DIRECT"),
        "breadth_est": max(n_files, n_steps),
        "depth_level": depth_level,
        "depth_signals": depth_hits[:8],
        "detected_domains": domains,
        "research_strategy": _research_strategy_for(tier, domains),
        "recommended_modules": build_recommended_modules(tier, domains, depth_level, domain_specs),
        "doc_lookup_targets": doc_targets,
        "file_refs": file_refs[:16],
        "planned_files_est": n_files,
        "steps_est": n_steps,
        "source": "exit_plan_mode",
        "confidence": "high",
    }


def merge_plan_triage(
    existing_triage: dict[str, Any] | None,
    plan_triage: dict[str, Any] | None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Merge prompt triage with live `exit_plan_mode` plan triage (monotonic upgrade)."""
    base = dict(existing_triage or {})
    plan = dict(plan_triage or {})
    old_tier = int(base.get("tier") or 0)
    new_tier = int(plan.get("tier") or 0)
    tier = max(old_tier, new_tier)

    domains = _ordered_union(base.get("detected_domains"), plan.get("detected_domains"))
    doc_targets = _ordered_union(base.get("doc_lookup_targets"), plan.get("doc_lookup_targets"))
    depth_signals = _ordered_union(base.get("depth_signals"), plan.get("depth_signals"))[:8]
    file_refs = _ordered_union(base.get("file_refs"), plan.get("file_refs"))[:16]

    d1 = str(base.get("depth_level") or "low")
    d2 = str(plan.get("depth_level") or "low")
    depth_level = d2 if _DEPTH_ORDER.get(d2, 0) >= _DEPTH_ORDER.get(d1, 0) else d1

    profile = load_project_profile(workspace_root)
    domain_specs = _merged_domain_specs(profile)

    out = dict(base)
    out.update({
        "tier": tier,
        "tier_name": _TIER_NAMES.get(tier, "DIRECT"),
        "breadth_est": max(int(base.get("breadth_est") or 0), int(plan.get("breadth_est") or 0)),
        "depth_level": depth_level,
        "depth_signals": depth_signals,
        "detected_domains": domains,
        "research_strategy": _research_strategy_for(tier, domains),
        "recommended_modules": build_recommended_modules(tier, domains, depth_level, domain_specs),
        "doc_lookup_targets": doc_targets,
        "file_refs": file_refs,
        "planned_files_est": int(plan.get("planned_files_est") or len(plan.get("file_refs") or [])),
        "plan_backed": True,
    })
    if new_tier > old_tier or not existing_triage:
        out["source"] = "exit_plan_mode"
        out["confidence"] = "high"
    else:
        out.setdefault("source", "prompt")
        out.setdefault("confidence", "high")
    return out


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
