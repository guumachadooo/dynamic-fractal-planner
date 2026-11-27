#!/usr/bin/env python3
"""Comprehensive regression suite for Dynamic Fractal Planner v3.

Supports:
  --fast : Single-process in-memory execution (<5s on Termux ARM64 PRoot)
  --full : Full subprocess integration execution (default)
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
HOOKS_DIR = PLUGIN_ROOT / "hooks"
FIXTURES_DIR = TESTS_DIR / "fixtures"

for p in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import blueprint_engine  # noqa: E402
import complexity_triage  # noqa: E402
import log_digest  # noqa: E402
import model_mode_cache  # noqa: E402
import planner_state  # noqa: E402
import plugin_diagnostics  # noqa: E402
import repo_map  # noqa: E402
import schema_migrations  # noqa: E402
import verify_universal  # noqa: E402

FAST_MODE = False
_MOD_CACHE: dict[str, Any] = {}


def _load_mod(path: Path) -> Any:
    key = str(path)
    if key in _MOD_CACHE:
        return _MOD_CACHE[key]
    spec = importlib.util.spec_from_file_location(f"_dyn_{path.stem}", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _MOD_CACHE[key] = mod
    return mod


@contextlib.contextmanager
def _patched_env(env_extra: dict[str, str] | None, cwd: Path | None = None, stdin_txt: str | None = None):
    old_env = os.environ.copy()
    old_cwd = Path.cwd()
    old_stdin = sys.stdin
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        if env_extra:
            os.environ.update(env_extra)
        if cwd is not None:
            os.chdir(str(cwd))
        sys.stdin = io.StringIO(stdin_txt or "")
        sys.stdout = out_buf
        sys.stderr = err_buf
        yield out_buf, err_buf
    finally:
        sys.stdin = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        if cwd is not None:
            try:
                os.chdir(str(old_cwd))
            except Exception:
                pass
        os.environ.clear()
        os.environ.update(old_env)


def run_hook(
    script_name: str,
    payload: dict[str, Any],
    env_extra: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, str, dict[str, Any]]:
    merged_env = {"PLANNER_DISABLE_TELEMETRY": "1"}
    if env_extra:
        merged_env.update(env_extra)
    cli_args = list(extra_args or [])
    if FAST_MODE:
        mod = _load_mod(HOOKS_DIR / script_name)
        rc = 0
        old_argv = sys.argv
        try:
            sys.argv = [str(HOOKS_DIR / script_name), *cli_args]
            with _patched_env(merged_env, stdin_txt=json.dumps(payload)) as (out_buf, err_buf):
                try:
                    mod.main()
                except SystemExit as exc:
                    rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                out_txt = out_buf.getvalue().strip()
                err_txt = err_buf.getvalue().strip()
        finally:
            sys.argv = old_argv
        parsed: dict[str, Any] = {}
        if out_txt:
            try:
                parsed = json.loads(out_txt)
            except Exception:
                parsed = {"_raw": out_txt}
        return rc, err_txt, parsed

    env = os.environ.copy()
    env.update(merged_env)
    res = subprocess.run(
        [sys.executable, str(HOOKS_DIR / script_name), *cli_args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    out_txt = res.stdout.strip()
    parsed = {}
    if out_txt:
        try:
            parsed = json.loads(out_txt)
        except Exception:
            parsed = {"_raw": out_txt}
    return res.returncode, res.stderr.strip(), parsed


def run_script(
    script_name: str,
    args: list[str],
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = {"PLANNER_DISABLE_TELEMETRY": "1"}
    if env_extra:
        merged_env.update(env_extra)
    if FAST_MODE:
        mod = _load_mod(SCRIPTS_DIR / script_name)
        rc = 0
        old_argv = sys.argv
        try:
            sys.argv = [str(SCRIPTS_DIR / script_name), *args]
            with _patched_env(merged_env, cwd=cwd or PLUGIN_ROOT, stdin_txt=input_text) as (out_buf, err_buf):
                try:
                    mod.main()
                except SystemExit as exc:
                    if isinstance(exc.code, int):
                        rc = exc.code
                    elif exc.code:
                        print(str(exc.code), file=sys.stderr)
                        rc = 1
                return subprocess.CompletedProcess(
                    args=[script_name, *args],
                    returncode=rc,
                    stdout=out_buf.getvalue(),
                    stderr=err_buf.getvalue(),
                )
        finally:
            sys.argv = old_argv

    env = os.environ.copy()
    env.update(merged_env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        cwd=str(cwd) if cwd else str(PLUGIN_ROOT),
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )


_GIT_TEMPLATE_DIR: Path | None = None


def _get_git_template() -> Path:
    global _GIT_TEMPLATE_DIR
    if _GIT_TEMPLATE_DIR is None or not (_GIT_TEMPLATE_DIR / ".git").is_dir():
        tpl = Path(tempfile.mkdtemp(prefix="dfp_git_tpl_"))
        subprocess.run(["git", "init", "-q"], cwd=str(tpl), check=True)
        with (tpl / ".git" / "config").open("a", encoding="utf-8") as fh:
            fh.write("[user]\n\temail = test@example.com\n\tname = Test User\n")
        _GIT_TEMPLATE_DIR = tpl
        atexit.register(shutil.rmtree, str(tpl), True)
    return _GIT_TEMPLATE_DIR


def init_git_repo(repo: Path) -> None:
    shutil.copytree(_get_git_template() / ".git", repo / ".git")


class TestSchemaMigrations(unittest.TestCase):
    def test_v1_generic_fixture_migrates_losslessly_to_v3(self) -> None:
        raw = json.loads((FIXTURES_DIR / "profile_v1_generic.json").read_text(encoding="utf-8"))
        snapshot = copy.deepcopy(raw)
        migrated, ver, warnings = schema_migrations.migrate_profile(raw)

        self.assertEqual(raw, snapshot, "migrate_profile must never mutate input dict")
        self.assertEqual(ver, 5)
        self.assertEqual(migrated["schema_version"], 5)
        self.assertEqual(warnings, [])
        self.assertEqual(migrated["custom_owner_note"], "preserve-me-losslessly")
        self.assertEqual(migrated["planner_mode"], "lean")
        self.assertEqual(migrated["read_budget"], {"max_full_read_lines": 300, "max_slice_lines": 250})
        self.assertEqual(migrated["secret_guard"], {"enabled": True})
        self.assertEqual(migrated["blast_radius"], {"simple_tier_max_files": 3})
        self.assertEqual(migrated["code_weight_limits"], {"enabled": True, "warn_lines": 1500, "warn_lines_all": 2000})
        self.assertTrue(migrated["delegation"]["enabled"])
        self.assertTrue(migrated["delegation"]["enforce_google_only"])
        self.assertEqual(migrated["delegation"]["model_tiers"], {"trivial": "flash_lite", "routine": "flash", "complex": "pro"})

        cmds = {r.get("label"): r for r in (migrated.get("build_rules") or [])}
        self.assertIn("verify_command", cmds)
        self.assertEqual(cmds["verify_command"]["command"], "npm test")
        self.assertIn("verify_non_code_command", cmds)
        self.assertTrue(cmds["verify_non_code_command"].get("run_without_build"))
        self.assertTrue(migrated["memory_feedback"]["enabled"])
        self.assertIn("payments", migrated["domains"])

    def test_v1_and_v2_cavern_fixtures_migrate_losslessly(self) -> None:
        v1 = json.loads((FIXTURES_DIR / "profile_v1_cavern.json").read_text(encoding="utf-8"))
        self.assertNotIn("schema_version", v1)
        m1, ver1, w1 = schema_migrations.migrate_profile(v1)
        self.assertEqual(ver1, 5)
        self.assertEqual(w1, [])
        self.assertEqual(m1["build_rules"], v1["build_rules"])
        self.assertEqual(m1["file_guards"], v1["file_guards"])
        self.assertEqual(m1["diff_rules"], v1["diff_rules"])
        self.assertEqual(m1["planner_mode"], "lean")

        v2 = json.loads((FIXTURES_DIR / "profile_v2_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v2.get("schema_version"), 2)
        m2, ver2, w2 = schema_migrations.migrate_profile(v2)
        self.assertEqual(ver2, 5)
        self.assertEqual(w2, [])
        self.assertEqual(m2["schema_version"], 5)
        self.assertEqual(m2["planner_mode"], "lean")
        self.assertEqual(m2["build_rules"], v2["build_rules"])
        self.assertEqual(m2["command_guards"], v2["command_guards"])
        self.assertEqual(m2["key_parity_rules"], v2["key_parity_rules"])

        v3 = json.loads((FIXTURES_DIR / "profile_v3_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v3.get("schema_version"), 3)
        m3, ver3, w3 = schema_migrations.migrate_profile(v3)
        self.assertEqual(ver3, 5)
        self.assertEqual(w3, [])
        self.assertEqual(m3["schema_version"], 5)
        self.assertEqual(m3["planner_mode"], "auto")
        self.assertEqual(m3["diff_rules"], v3["diff_rules"])
        self.assertTrue(m3["code_weight_limits"]["enabled"])

        v4 = json.loads((FIXTURES_DIR / "profile_v4_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v4.get("schema_version"), 4)
        m4, ver4, w4 = schema_migrations.migrate_profile(v4)
        self.assertEqual(ver4, 5)
        self.assertEqual(w4, [])
        self.assertEqual(m4["schema_version"], 5)
        self.assertEqual(m4["code_weight_limits"], v4["code_weight_limits"])
        self.assertEqual(m4["file_budget_limits"], v4["file_budget_limits"])
        self.assertTrue(m4["delegation"]["enabled"])
        self.assertTrue(m4["delegation"]["enforce_google_only"])

    def test_synthetic_chain_v1_to_v10(self) -> None:
        def make_hop(src_v: int):
            def _hop(d: dict[str, Any]) -> dict[str, Any]:
                out = copy.deepcopy(d)
                hops = list(out.get("hops") or [])
                hops.append(f"{src_v}->{src_v + 1}")
                out["hops"] = hops
                out[f"field_v{src_v + 1}"] = True
                return out
            return _hop

        chain = {v: make_hop(v) for v in range(1, 10)}
        v1 = {"project_name": "ChainProj", "keep": "yes"}
        out, ver, warnings = schema_migrations.migrate_profile(v1, migrators=chain, target_version=10)
        self.assertEqual(ver, 10)
        self.assertEqual(out["schema_version"], 10)
        self.assertEqual(warnings, [])
        self.assertEqual(out["hops"], [f"{i}->{i + 1}" for i in range(1, 10)])

    def test_newer_schema_warns_without_crashing(self) -> None:
        future = {"schema_version": 99, "project_name": "FutureRepo", "future_field": [1, 2, 3]}
        out, ver, warnings = schema_migrations.migrate_profile(future)
        self.assertEqual(ver, 99)
        self.assertTrue(any("99" in w for w in warnings))
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".agents").mkdir()
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(future), encoding="utf-8")
            warns: list[str] = []
            loaded = planner_state.load_project_profile(repo, warnings_out=warns)
            self.assertEqual(loaded["schema_version"], 99)
            self.assertTrue(any("99" in w for w in warns))

    def test_in_memory_load_and_cli_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            agents_dir = repo / ".agents"
            agents_dir.mkdir()
            prof_file = agents_dir / "planner_profile.json"
            v1_text = (FIXTURES_DIR / "profile_v1_generic.json").read_text(encoding="utf-8")
            prof_file.write_text(v1_text, encoding="utf-8")

            loaded = planner_state.load_project_profile(repo)
            self.assertEqual(loaded["schema_version"], 5)
            self.assertEqual(prof_file.read_text(encoding="utf-8"), v1_text)

            res = run_script("plan_tool.py", ["--workspace", str(repo), "migrate"])
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue((agents_dir / "planner_profile.json.bak").is_file())
            disk_after = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_after["schema_version"], 5)
            self.assertEqual(disk_after["planner_mode"], "lean")

            res2 = run_script("plan_tool.py", ["--workspace", str(repo), "migrate"])
            self.assertEqual(res2.returncode, 0)
            self.assertIn("already up to date", res2.stdout.lower())


class TestAutoBootstrapAndHeadings(unittest.TestCase):
    def test_is_real_project_root_guards(self) -> None:
        self.assertFalse(planner_state.is_real_project_root(Path("/")))
        self.assertFalse(planner_state.is_real_project_root(Path("/tmp")))
        self.assertFalse(planner_state.is_real_project_root(Path.home()))
        self.assertFalse(planner_state.is_real_project_root(Path.home() / ".gemini"))
        self.assertFalse(planner_state.is_real_project_root(PLUGIN_ROOT))

        with tempfile.TemporaryDirectory() as empty_td:
            empty_dir = Path(empty_td)
            (empty_dir / "notes.txt").write_text("just a note", encoding="utf-8")
            self.assertFalse(planner_state.is_real_project_root(empty_dir))

        with tempfile.TemporaryDirectory() as dart_td:
            d = Path(dart_td)
            (d / "pubspec.yaml").write_text("name: flutter_app\n", encoding="utf-8")
            self.assertTrue(planner_state.is_real_project_root(d))

    def test_bootstrap_v3_stacks(self) -> None:
        # TS / Next / Vite
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "build": "next build"}}), encoding="utf-8")
            (r / "tsconfig.json").write_text("{}", encoding="utf-8")
            planner_state.bootstrap_workspace(r)
            prof = json.loads((r / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertEqual(prof["schema_version"], 5)
            self.assertEqual(prof["planner_mode"], "lean")
            self.assertTrue(prof["delegation"]["enabled"])
            cmds = [x["command"] for x in prof["build_rules"]]
            self.assertIn("npx tsc --noEmit", cmds)
            self.assertIn("npm run lint", cmds)

        # Generic .NET / C#
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"></Project>", encoding="utf-8")
            planner_state.bootstrap_workspace(r)
            prof = json.loads((r / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertTrue(any("dotnet build" in x["command"] for x in prof["build_rules"]))

        # Android / Gradle / Kotlin
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "build.gradle.kts").write_text("plugins {}", encoding="utf-8")
            (r / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
            planner_state.bootstrap_workspace(r)
            prof = json.loads((r / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertTrue(any("./gradlew test" in x["command"] for x in prof["build_rules"]))

        # Flutter / Dart
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)
            (r / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
            planner_state.bootstrap_workspace(r)
            prof = json.loads((r / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertTrue(any("flutter test" in x["command"] for x in prof["build_rules"]))

    def test_session_start_auto_bootstrap_and_non_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / "package.json").write_text(json.dumps({"name": "demo", "scripts": {"test": "jest"}}), encoding="utf-8")
            rc, err, parsed = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            msg = parsed["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("Auto-initialized project docs/profile", msg)
            prof = json.loads((repo / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertEqual(prof["schema_version"], 5)

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            empty_dir = Path(td)
            rc, err, _ = run_hook("planner_session_start.py", {
                "workspacePaths": [str(empty_dir)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            self.assertFalse((empty_dir / ".agents").exists())

    def test_tolerant_extra_requests_headings(self) -> None:
        variants = [
            ("## Pending Extra Requests\n- Task A\n", ["Task A"]),
            ("## Pedidos Extras Pendentes\n- Tarefa B\n", ["Tarefa B"]),
            ("### Queue:\n- Item D\n## Completed\n- Done E\n", ["Item D"]),
            ("# Pending\n- Item G\n", ["Item G"]),
            ("# Extra Request Queue\n\n## Pending Extra Requests\n(No pending requests.)\n", []),
        ]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            docs = repo / ".agents" / "docs"
            docs.mkdir(parents=True)
            extra = docs / "EXTRA_REQUESTS.md"
            for content, expected_sub in variants:
                extra.write_text(content, encoding="utf-8")
                _, items = planner_state.get_pending_extra_requests(repo, {})
                self.assertEqual(len(items), len(expected_sub))


class TestSelfProtectionGate(unittest.TestCase):
    def test_triage_detects_plugin_self(self) -> None:
        res = complexity_triage.analyze_prompt(
            "Update ~/.gemini/config/plugins/dynamic-fractal-planner/hooks/planner_stop.py and schema_migrations.py",
            PLUGIN_ROOT,
        )
        self.assertIn("plugin-self", res["detected_domains"])
        self.assertGreaterEqual(res["tier"], 2)

    def test_plugin_edit_gate_strict_finish_and_stop(self) -> None:
        strict_env = {"PLANNER_MODE": "strict"}
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            ws = Path(td)
            init_git_repo(ws)
            target_plugin_file = str(PLUGIN_ROOT / "hooks" / "planner_stop.py")

            # Strict mode denies outside-repo plugin edit without plan
            rc, _, resp = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Edit planner_stop.py in dynamic-fractal-planner plugin",
                "toolCall": {"name": "edit_file", "args": {"Path": target_plugin_file, "Target": "a", "Replacement": "b"}},
            }, env_extra=strict_env)
            self.assertEqual(rc, 0)
            self.assertEqual(resp.get("decision"), "deny")
            self.assertIn("PLAN GATE", resp.get("reason", ""))

            # Frozen v1 & v2 fixtures protected with force_ask
            for fx in ("profile_v1_generic.json", "profile_v2_cavern.json"):
                _, _, resp_fix = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(ws)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(FIXTURES_DIR / fx), "Target": "a", "Replacement": "b"}},
                }, env_extra=strict_env)
                self.assertEqual(resp_fix.get("decision"), "force_ask", fx)

            # Initialize plan -> allow edit
            init_res = run_script("plan_tool.py", [
                "--dir", art, "--workspace", str(ws), "init",
                "--tier", "2", "--goal", "Update plugin", "--req", "REQ-01: Update stop hook",
            ])
            self.assertEqual(init_res.returncode, 0, init_res.stderr)

            _, _, resp_ok = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Update plugin",
                "toolCall": {"name": "edit_file", "args": {"Path": target_plugin_file, "Target": "a", "Replacement": "b"}},
            }, env_extra=strict_env)
            self.assertEqual(resp_ok.get("decision"), "allow")

            # PostToolUse marks needs_plugin_tests=True
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": target_plugin_file}},
            })
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            self.assertTrue(json.loads(hook_file.read_text(encoding="utf-8")).get("needs_plugin_tests"))

            # Stop blocked while needs_plugin_tests & open plan
            _, _, stop_blocked = run_hook("planner_stop.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(stop_blocked.get("decision"), "continue")
            self.assertIn("run_plugin_tests.py", stop_blocked.get("reason", ""))

            # plan_tool.py finish closes all modules & REQs in 1 turn + compacts done modules
            fin = run_script("plan_tool.py", ["--dir", art, "--workspace", str(ws), "finish", "--evidence", "tested"])
            self.assertEqual(fin.returncode, 0, fin.stdout + fin.stderr)
            self.assertIn("PLAN AUDIT PASS", fin.stdout)
            task_md_txt = (Path(art) / "TASK_PLAN.md").read_text(encoding="utf-8")
            self.assertIn("- [x] `MOD-AUDIT` |", task_md_txt)
            self.assertNotIn("### [x] `MOD-AUDIT`", task_md_txt)

            # run_plugin_tests clears needs_plugin_tests -> Stop allows
            time.sleep(0.01)
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"python3 {TESTS_DIR / 'run_plugin_tests.py'}"}},
            })
            _, _, stop_ok = run_hook("planner_stop.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(stop_ok, {})

    def test_verify_universal_plugin_tests_flag(self) -> None:
        if FAST_MODE or os.environ.get("PLANNER_PLUGIN_TEST_DEPTH") == "1":
            return
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            hook_dir = Path(art) / "scratch"
            hook_dir.mkdir(parents=True)
            hook_file = hook_dir / ".hook_state.json"
            hook_file.write_text(json.dumps({
                "needs_plugin_tests": True,
                "last_plugin_edit_ts": time.time() - 5,
            }), encoding="utf-8")
            res = run_script(
                "verify_universal.py",
                ["--workspace", str(repo), "--files", "--plugin-tests"],
                cwd=repo,
                env_extra={"PLANNER_ARTIFACT_DIR": art, "PLANNER_PLUGIN_TEST_DEPTH": "1"},
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("plugin tests passed", res.stdout)
            self.assertFalse(json.loads(hook_file.read_text(encoding="utf-8")).get("needs_plugin_tests"))


class TestV3Features(unittest.TestCase):
    def test_bypass_switch(self) -> None:
        bypass_file = PLUGIN_ROOT / ".bypass_gates"
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            try:
                bypass_file.write_text("1", encoding="utf-8")
                _, _, r_read = run_hook("planner_guard_read.py", {
                    "workspacePaths": [str(repo)],
                    "toolCall": {"name": "read_file", "args": {"Path": "/root/.gemini/antigravity/brain/abc/transcript.jsonl"}},
                })
                self.assertEqual(r_read.get("decision"), "allow")
                _, _, r_cmd = run_hook("planner_guard_command.py", {
                    "workspacePaths": [str(repo)],
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "dotnet build CavernizeAndroid.csproj"}},
                })
                self.assertEqual(r_cmd.get("decision"), "allow")
                _, _, r_pre = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": "1. A\n2. B\n3. C",
                    "invocationNum": 1,
                })
                self.assertEqual(r_pre, {})
            finally:
                if bypass_file.exists():
                    bypass_file.unlink()

    def test_neon_handoff_preserves_turn_state(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            prompt = "1. Implement parser\n2. Add tests\n3. Update docs"
            _, _, first = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prompt,
                "invocationNum": 1,
            })
            self.assertIn("injectSteps", first)

            # Simulate edits during planning/execution turn
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            hs["code_files_edited"] = ["src/a.py"]
            hs["needs_verify"] = True
            hs["last_invocation_num"] = 4
            hook_file.write_text(json.dumps(hs), encoding="utf-8")

            # Neon handoff resets invocationNum back to 1 with identical prompt within window
            _, _, handoff = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prompt,
                "invocationNum": 1,
            })
            self.assertEqual(handoff, {}, "Must NOT treat Neon handoff as new turn or re-inject triage")
            hs_after = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertEqual(hs_after["code_files_edited"], ["src/a.py"])
            self.assertTrue(hs_after["needs_verify"])
            self.assertTrue(hs_after["triage_injected"])

    def test_lean_auto_reqs_jit_and_blast_radius(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "src").mkdir()
            # Tier >= 2 in lean mode -> auto REQs + JIT checklists, no PLAN GATE
            _, _, pre = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "1. Fix CSS layout clipping\n2. Add pytest unit tests\n3. Update API endpoint",
                "invocationNum": 1,
            })
            msg = pre["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("mode=lean", msg)
            self.assertIn("REQS|REQ-01:", msg)
            self.assertIn("JIT|", msg)

            # Simple Tier 1 turn -> Blast Radius Guard triggers on 4th distinct code file
            _, _, _ = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix typo in helper.py",
                "invocationNum": 1,
            })
            for idx in range(3):
                fp = repo / "src" / f"f{idx}.py"
                fp.write_text("x = 1\n", encoding="utf-8")
                _, _, dec = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": "Fix typo in helper.py",
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp), "Target": "x = 1", "Replacement": "x = 2"}},
                })
                self.assertEqual(dec.get("decision"), "allow", f"File {idx} should allow")
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp)}},
                })
            f4 = repo / "src" / "f3.py"
            f4.write_text("x = 1\n", encoding="utf-8")
            _, _, dec4 = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix typo in helper.py",
                "toolCall": {"name": "edit_file", "args": {"Path": str(f4), "Target": "x = 1", "Replacement": "x = 2"}},
            })
            # Default action is non-blocking "warn" (no force_ask popup spam)
            self.assertEqual(dec4.get("decision"), "allow")
            _, _, pre_br = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix typo in helper.py",
                "invocationNum": 2,
            })
            self.assertIn("BLAST RADIUS", pre_br["injectSteps"][0]["systemMessage"]["systemMessage"])

            # Opt-in action="force_ask" asks ONCE per turn, then allows 5th file once approved
            (repo / ".agents").mkdir(exist_ok=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 3, "blast_radius": {"simple_tier_max_files": 3, "action": "force_ask"}}),
                encoding="utf-8",
            )
            _, _, dec4_ask = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix typo in helper.py",
                "toolCall": {"name": "edit_file", "args": {"Path": str(f4), "Target": "x = 1", "Replacement": "x = 2"}},
            })
            self.assertEqual(dec4_ask.get("decision"), "force_ask")
            self.assertIn("BLAST RADIUS", dec4_ask.get("reason", ""))
            # Approve 4th file edit -> 5th file must NOT re-ask
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(f4)}},
            })
            f5 = repo / "src" / "f4.py"
            f5.write_text("x = 1\n", encoding="utf-8")
            _, _, dec5 = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix typo in helper.py",
                "toolCall": {"name": "edit_file", "args": {"Path": str(f5), "Target": "x = 1", "Replacement": "x = 2"}},
            })
            self.assertEqual(dec5.get("decision"), "allow")

    def test_eco_mode_memory_anchor_14_and_long_chat_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("# History\n", encoding="utf-8")
            os.utime(repo / ".agents" / "docs" / "PROJECT_HISTORY.md", (time.time() - 7200, time.time() - 7200))
            for i in range(2):
                p = repo / f"m{i}.py"
                p.write_text("a=1\n", encoding="utf-8")

            # ECO_MODE prompt injects directive & suppresses memory_feedback on commit
            _, _, pre_eco = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Economiza cota, corrige m0.py rápido",
                "invocationNum": 1,
            })
            self.assertIn("ECO_MODE|ON", pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"])
            _, _, cg_eco = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'eco'"}},
            })
            self.assertEqual(cg_eco.get("decision"), "allow")

        # Memory anchor every 14 invocations ONLY when open_reqs >= 3
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            prompt = "Anchor test"
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": prompt, "invocationNum": 1,
            })
            run_script("plan_tool.py", [
                "--dir", art, "--workspace", str(repo), "init", "--tier", "2", "--goal", "Anchor test",
                "--req", "REQ-01: A", "--req", "REQ-02: B", "--req", "REQ-03: C",
            ])
            # At inv 8 -> must NOT trigger yet (interval is 14)
            saw_anchor_early = False
            for inv in range(2, 13):
                _, _, out = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": prompt, "invocationNum": inv,
                })
                if "Memory Anchor" in json.dumps(out):
                    saw_anchor_early = True
            self.assertFalse(saw_anchor_early)
            # By inv 15 -> MUST trigger
            saw_anchor_14 = False
            for inv in range(13, 17):
                _, _, out = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": prompt, "invocationNum": inv,
                })
                if "Memory Anchor" in json.dumps(out):
                    saw_anchor_14 = True
            self.assertTrue(saw_anchor_14)

            # With open_reqs < 3, Memory Anchor must NOT trigger even after 14 invocations
            run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "req-verify", "REQ-01", "--evidence", "done"])
            hs_reset = json.loads((Path(art) / "scratch" / ".hook_state.json").read_text(encoding="utf-8"))
            hs_reset["invocations_since_anchor"] = 14
            (Path(art) / "scratch" / ".hook_state.json").write_text(json.dumps(hs_reset), encoding="utf-8")
            _, _, out_lt3 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": prompt, "invocationNum": 18,
            })
            self.assertNotIn("Memory Anchor", json.dumps(out_lt3))

            # Long-chat nudge at total_turns >= 25
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            hs["total_turns"] = 25
            hs["handoff_nudged"] = False
            hook_file.write_text(json.dumps(hs), encoding="utf-8")
            _, _, nudge_out = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Turn 26", "invocationNum": 1,
            })
            self.assertIn("QUOTA TIP", json.dumps(nudge_out))

    def test_read_guard_log_digest_and_repo_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)

            # 1. transcript*.jsonl blocked
            _, _, r_tr = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": "/root/.gemini/antigravity/brain/xyz/transcript_full.jsonl"}},
            })
            self.assertEqual(r_tr.get("decision"), "deny")

            # 2. PROJECT_HISTORY.md > 150L without EndLine blocked; with EndLine=60 allowed
            hist = repo / ".agents" / "docs" / "PROJECT_HISTORY.md"
            hist.write_text("\n".join(f"- Line {i}" for i in range(180)), encoding="utf-8")
            _, _, r_h1 = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": str(hist)}},
            })
            self.assertEqual(r_h1.get("decision"), "deny")
            _, _, r_h2 = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": str(hist), "StartLine": 1, "EndLine": 60}},
            })
            self.assertEqual(r_h2.get("decision"), "allow")

            # 3. Large code file > 300L -> deny + free symbol map; slice <= 250L -> allow
            big_cs = repo / "PlayerEngine.cs"
            cs_lines = ["namespace Cavernize {", "  public class PlayerEngine {"]
            for i in range(360):
                if i == 40:
                    cs_lines.append("    public void CreateEnglish() {}")
                elif i == 200:
                    cs_lines.append("    public int ComputeLatency(int x) { return x; }")
                else:
                    cs_lines.append(f"    int _f{i} = {i};")
            cs_lines.extend(["  }", "}"])
            big_cs.write_text("\n".join(cs_lines), encoding="utf-8")

            _, _, r_cs_full = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": str(big_cs)}},
            })
            self.assertEqual(r_cs_full.get("decision"), "deny")
            self.assertIn("CreateEnglish()", r_cs_full.get("reason", ""))
            self.assertIn("L43|", r_cs_full.get("reason", ""))

            _, _, r_cs_slice = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": str(big_cs), "StartLine": 35, "EndLine": 120}},
            })
            self.assertEqual(r_cs_slice.get("decision"), "allow")

            # 4. Large log > 300L -> deny + log_digest.py test
            big_log = repo / "app.log"
            log_lines = ["=== Session 1 ===", "Error: old error that should not appear"] + ["info ok" for _ in range(320)]
            log_lines += [
                "=== Session 2 ===",
                "2026-11-19 00:00:01 info boot",
                "2026-11-19 00:00:02 info loading",
                "2026-11-19 00:00:03 warn retrying",
                "2026-11-19 00:00:04 warn retrying",
                "2026-11-19 00:00:05 warn retrying",
                "2026-11-19 00:00:06 FATAL crash in decoder",
                "2026-11-19 00:00:07 stack frame 1",
            ]
            big_log.write_text("\n".join(log_lines), encoding="utf-8")
            _, _, r_log = run_hook("planner_guard_read.py", {
                "workspacePaths": [str(repo)],
                "toolCall": {"name": "read_file", "args": {"Path": str(big_log)}},
            })
            self.assertEqual(r_log.get("decision"), "deny")
            self.assertIn("log_digest.py", r_log.get("reason", ""))

            dig = log_digest.digest_log_text(big_log.read_text(encoding="utf-8"), name="app.log")
            self.assertNotIn("old error", dig)
            self.assertIn("FATAL crash in decoder", dig)
            self.assertIn("[repeated 3x]", dig)

            # 5. repo_map.py cached in /tmp, zero repo pollution, invalidates on git change
            subprocess.run(["git", "add", "PlayerEngine.cs"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add cs"], cwd=str(repo), check=True)
            rm1 = repo_map.build_repo_map(repo, use_cache=True)
            self.assertIn("PlayerEngine.cs|", rm1)
            self.assertIn("CreateEnglish()", rm1)
            st_clean = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True)
            self.assertNotIn("repomap", st_clean.stdout)
            (repo / "ExtraModule.py").write_text("def BrandNewFunc():\n    pass\n", encoding="utf-8")
            subprocess.run(["git", "add", "ExtraModule.py"], cwd=str(repo), check=True)
            rm2 = repo_map.build_repo_map(repo, use_cache=True)
            self.assertIn("BrandNewFunc()", rm2)

    def test_shift_left_destructive_write_secrets_and_undo_turn(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "Cavern.Format").mkdir()
            (repo / "CavernSamples" / "CavernizeAndroid").mkdir(parents=True)
            good = repo / "Cavern.Format" / "Codec.cs"
            good.write_text("\n".join(f"class C{i} {{ }}" for i in range(180)) + "\n", encoding="utf-8")
            pre_dirty = repo / "PreDirty.txt"
            pre_dirty.write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
            pre_dirty.write_text("preexisting_uncommitted_change\n", encoding="utf-8")

            prof = json.loads((FIXTURES_DIR / "profile_v2_cavern.json").read_text(encoding="utf-8"))
            (repo / ".agents").mkdir(exist_ok=True)
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(prof), encoding="utf-8")

            # Shift-left fail diff_rule blocks BEFORE disk write
            _, _, sl_bad = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(good), "Target": "class C0 { }", "Replacement": "class C0 { void F(object x) { if (x is not null) {} } }"},
                },
            })
            self.assertEqual(sl_bad.get("decision"), "deny")
            self.assertIn("SHIFT-LEFT", sl_bad.get("reason", ""))

            # Warn diff_rule does NOT block
            ui = repo / "CavernSamples" / "CavernizeAndroid" / "Ui.cs"
            ui.write_text("class Ui {}\n", encoding="utf-8")
            _, _, sl_warn = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {
                    "name": "edit_file",
                    "args": {"Path": str(ui), "Target": "class Ui {}", "Replacement": "class Ui { void F() { Orientation.Horizontal; new Button(); } }"},
                },
            })
            self.assertEqual(sl_warn.get("decision"), "allow")

            # Destructive write_file on >150L file: <70% -> force_ask; >=70% -> deny
            _, _, dw_ask = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "write_file", "args": {"Path": str(good), "Content": "class Only { }\n"}},
            })
            self.assertEqual(dw_ask.get("decision"), "force_ask")

            _, _, dw_deny = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "write_file", "args": {"Path": str(good), "Content": "\n".join(f"class D{i} {{ }}" for i in range(160))}},
            })
            self.assertEqual(dw_deny.get("decision"), "deny")

            # Secret & merge conflict shields (no false positives on regex definitions)
            fake_ghp = "ghp_" + "A1b2C3d4E5" * 3
            _, _, sec_deny = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(ui), "Target": "class Ui {}", "Replacement": f'var tok = "{fake_ghp}";'}},
            })
            self.assertEqual(sec_deny.get("decision"), "deny")

            _, _, conf_deny = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(ui), "Target": "class Ui {}", "Replacement": "<<<<<<< HEAD\nint a = 1;\n=======\nint a = 2;\n>>>>>>> main"}},
            })
            self.assertEqual(conf_deny.get("decision"), "deny")

            _, _, regex_ok = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(ui), "Target": "class Ui {}", "Replacement": 'var rx = @"ghp_[A-Za-z0-9]{20,}";'}},
            })
            self.assertEqual(regex_ok.get("decision"), "allow")

            # Invisible Tier>=2 git snapshot + undo-turn
            _, _, _ = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "1. Refactor Codec\n2. Fix Ui\n3. Update logic",
                "invocationNum": 1,
            })
            _, _, _ = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(good), "Target": "class C0 { }", "Replacement": "class C0 { int x; }"}},
            })
            good.write_text("MODIFIED_IN_TURN\n", encoding="utf-8")
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(good)}},
            })
            dry = run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "undo-turn", "--dry-run"])
            self.assertEqual(dry.returncode, 0)
            self.assertIn("RESTORE|Cavern.Format/Codec.cs|", dry.stdout)
            applied = run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "undo-turn"])
            self.assertEqual(applied.returncode, 0)
            self.assertIn("class C0 { }", good.read_text(encoding="utf-8"))
            self.assertEqual(pre_dirty.read_text(encoding="utf-8"), "preexisting_uncommitted_change\n")

    def test_circuit_breaker_cmd_diet_push_flight_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
            (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
            (repo / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # Circuit Breaker on 2 failed same commands
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Fix test", "invocationNum": 1,
            })
            for _ in range(2):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "exitCode": 1,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
                })
            _, _, cb = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Fix test", "invocationNum": 2,
            })
            self.assertIn("CIRCUIT BREAKER", cb["injectSteps"][0]["systemMessage"]["systemMessage"])

            # Stop blocked while last_build_failed == True; passing pytest clears it
            _, _, st_fail = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "fullyIdle": True, "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(st_fail.get("decision"), "continue")
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "exitCode": 0,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            })
            _, _, st_ok = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "fullyIdle": True, "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(st_ok, {})

            # Batched 5 edits in the SAME invocation must NOT trip Circuit Breaker
            for _ in range(5):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "app.py")}},
                })
            _, _, cb_batch = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Fix test", "invocationNum": 3,
            })
            self.assertNotIn("CIRCUIT BREAKER", json.dumps(cb_batch))

            # Circuit Breaker DOES trigger across 4 distinct edit invocations without passing build/test
            for inv_num in (4, 5, 6):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "app.py")}},
                })
                _, _, cb_edit = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Fix test", "invocationNum": inv_num,
                })
            cb_edit_msg = cb_edit["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("CIRCUIT BREAKER", cb_edit_msg)
            self.assertIn("edited 4x", cb_edit_msg)
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "exitCode": 0,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            })

            # Silent static auto-verify on Stop (<0.5s): valid edit -> Stop clears & allows!
            (repo / "app.py").write_text("def ok():\n    return 2\n", encoding="utf-8")
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "app.py")}},
            })
            _, _, st_auto = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "fullyIdle": True, "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(st_auto, {})

            # Syntax error in .py -> Stop blocks with file:line error
            (repo / "app.py").write_text("def broken(:\n", encoding="utf-8")
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "app.py")}},
            })
            _, _, st_err = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "fullyIdle": True, "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(st_err.get("decision"), "continue")
            self.assertIn("app.py", st_err.get("reason", ""))
            (repo / "app.py").write_text("def ok():\n    return 2\n", encoding="utf-8")

            # Command diet: dotnet build -> --nologo -v:minimal; existing -v:detailed untouched; raw cat on >300L log -> deny
            _, _, cg_dn = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "dotnet build Foo.csproj"}},
            })
            self.assertIn("--nologo -v:minimal", cg_dn.get("overwrite", {}).get("CommandLine", ""))
            _, _, cg_dn_keep = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "dotnet build Foo.csproj -v:detailed"}},
            })
            self.assertNotIn("overwrite", cg_dn_keep)
            _, _, cg_py = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            })
            self.assertEqual(cg_py.get("overwrite", {}).get("CommandLine"), "pytest tests/ -q --tb=short")
            _, _, cg_py_v = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -v tests/"}},
            })
            self.assertNotIn("overwrite", cg_py_v)
            _, _, cg_grep = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "grep -n foo scripts/plan_tool.py"}},
            })
            self.assertNotIn("overwrite", cg_grep)
            _, _, cg_pt = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "python3 scripts/plan_tool.py status"}},
            })
            self.assertIn("PLANNER_ARTIFACT_DIR=", cg_pt.get("overwrite", {}).get("CommandLine", ""))

            # Pre-push blocked when code edited with needs_build=True
            hs_b = json.loads((Path(art) / "scratch" / ".hook_state.json").read_text(encoding="utf-8"))
            hs_b["code_files_edited"] = ["app.py"]
            hs_b["needs_build"] = True
            (Path(art) / "scratch" / ".hook_state.json").write_text(json.dumps(hs_b), encoding="utf-8")
            _, _, cg_unbuilt = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
            }, env_extra={"PLANNER_SKIP_DNS_CHECK": "1"})
            self.assertEqual(cg_unbuilt.get("decision"), "deny")
            self.assertIn("needs_build", cg_unbuilt.get("reason", ""))
            hs_b["code_files_edited"] = []
            hs_b["needs_build"] = False
            (Path(art) / "scratch" / ".hook_state.json").write_text(json.dumps(hs_b), encoding="utf-8")

            big_log = repo / "run.log"
            big_log.write_text("\n".join("ok" for _ in range(320)), encoding="utf-8")
            _, _, cg_cat = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "cat run.log"}},
            })
            self.assertEqual(cg_cat.get("decision"), "deny")
            big_log.unlink()

            # Pre-push flight checks: untracked code file blocks push; offline DNS blocks push; docs-only triggers [skip ci] tip
            (repo / "orphan.py").write_text("x = 1\n", encoding="utf-8")
            _, _, cg_untr = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
            }, env_extra={"PLANNER_SKIP_DNS_CHECK": "1"})
            self.assertEqual(cg_untr.get("decision"), "deny")
            self.assertIn("Untracked code", cg_untr.get("reason", ""))
            (repo / "orphan.py").unlink()

            _, _, cg_off = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
            }, env_extra={"PLANNER_FORCE_OFFLINE": "1"})
            self.assertEqual(cg_off.get("decision"), "deny")
            self.assertIn("Offline/DNS", cg_off.get("reason", ""))

            (repo / "README.md").write_text("# Updated\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "docs update"], cwd=str(repo), check=True)
            _, _, cg_skip = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
            }, env_extra={"PLANNER_SKIP_DNS_CHECK": "1"})
            self.assertEqual(cg_skip.get("decision"), "force_ask")
            self.assertIn("[skip ci]", cg_skip.get("reason", ""))

    def test_verify_brackets_orphan_weight_handoff_stats_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("\n".join(f"L{i}" for i in range(160)), encoding="utf-8")
            (repo / "Program.cs").write_text(
                'class Program {\n  static void LegacyHelperMethod() {}\n  static void Main() { LegacyHelperMethod(); var s = "{"; /* { */ }\n}\n',
                encoding="utf-8",
            )
            (repo / "Ui.tsx").write_text("export const x = `val ${fn(\"(\")}`;\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # Remove LegacyHelperMethod declaration while call in Main remains -> orphan warning
            (repo / "Program.cs").write_text(
                'class Program {\n  static void Main() { LegacyHelperMethod(); var s = "{"; /* { */ }\n}\n',
                encoding="utf-8",
            )
            res_ok = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertEqual(res_ok.returncode, 0, res_ok.stdout)
            self.assertIn("LegacyHelperMethod", res_ok.stdout)
            self.assertIn("PROJECT_HISTORY.md", res_ok.stdout)

            # Unbalanced bracket in .cs fails
            (repo / "Program.cs").write_text('class Program { void F() { }\n', encoding="utf-8")
            res_bad = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertNotEqual(res_bad.returncode, 0)
            self.assertIn("Unclosed `{`", res_bad.stdout)

            # should_run_plugin_tests must NOT trigger on unrelated repo even if plugin repo is dirty
            with _patched_env({"PLANNER_ARTIFACT_DIR": str(art), "CAVERN_ARTIFACT_DIR": str(art)}):
                self.assertFalse(verify_universal.should_run_plugin_tests(repo, PLUGIN_ROOT, ["Program.cs"], False))

            # HANDOFF.md write, SessionStart injection, clear
            ho = run_script("plan_tool.py", ["--workspace", str(repo), "handoff", "--summary", "Refactored X", "--next", "Run build"])
            self.assertEqual(ho.returncode, 0)
            _, _, ss = run_hook("planner_session_start.py", {"workspacePaths": [str(repo)], "artifactDirectoryPath": art})
            self.assertIn("HANDOFF|", ss["injectSteps"][0]["systemMessage"]["systemMessage"])
            ho_clr = run_script("plan_tool.py", ["--workspace", str(repo), "handoff", "--clear"])
            self.assertEqual(ho_clr.returncode, 0)
            self.assertFalse((repo / ".agents" / "docs" / "HANDOFF.md").exists())

            # Telemetry stats
            metrics_p = Path(art) / "metrics.jsonl"
            planner_state.record_telemetry("turn", {"tier": 2})
            with _patched_env({"PLANNER_METRICS_FILE": str(metrics_p), "PLANNER_DISABLE_TELEMETRY": "0"}):
                planner_state.record_telemetry("read_deflected_symbol_map", {"file": "A.cs"})
            st_out = run_script("plan_tool.py", ["stats"], env_extra={"PLANNER_METRICS_FILE": str(metrics_p)})
            self.assertIn("read_deflected_symbol_map|1", st_out.stdout)

            # install.sh --rollback tested in isolated temp copy
            if not FAST_MODE:
                plug_copy = Path(td) / "plug_copy"
                shutil.copytree(
                    PLUGIN_ROOT,
                    plug_copy,
                    ignore=shutil.ignore_patterns(".git", ".last_good_backup", "__pycache__", "*.pyc", ".metrics.jsonl"),
                )
                ins = subprocess.run([str(plug_copy / "install.sh"), "--target", str(plug_copy), "--skip-tests"], capture_output=True, text=True, check=False)
                self.assertEqual(ins.returncode, 0, ins.stderr + ins.stdout)
                orig_ver = (plug_copy / "plugin.json").read_text(encoding="utf-8")
                (plug_copy / "plugin.json").write_text('{"broken": true}', encoding="utf-8")
                rb = subprocess.run([str(plug_copy / "install.sh"), "--target", str(plug_copy), "--rollback"], capture_output=True, text=True, check=False)
                self.assertEqual(rb.returncode, 0, rb.stderr + rb.stdout)
                self.assertEqual((plug_copy / "plugin.json").read_text(encoding="utf-8"), orig_ver)


class TestCavernV1V2V3Parity(unittest.TestCase):
    LIVE_CAVERN_PROFILE = Path("/root/.gemini/antigravity/scratch/Cavern/.agents/planner_profile.json")

    def _make_cavern_repo(self, td: str, profile_data: dict[str, Any]) -> Path:
        repo = Path(td)
        init_git_repo(repo)
        (repo / ".agents" / "docs").mkdir(parents=True)
        (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("# History\n", encoding="utf-8")
        (repo / ".agents" / "docs" / "SAVED_IDEAS.md").write_text("# Ideas\n", encoding="utf-8")
        (repo / ".agents" / "docs" / "EXTRA_REQUESTS.md").write_text("## Pending Extra Requests\n(No pending requests.)\n", encoding="utf-8")
        (repo / "CavernSamples" / "CavernizeAndroid" / "Properties").mkdir(parents=True)
        (repo / "CavernSamples" / "CavernizeAndroid" / "Language").mkdir(parents=True)
        (repo / "Cavern.Format").mkdir(parents=True)
        (repo / ".github" / "workflows").mkdir(parents=True)

        (repo / "CavernSamples" / "CavernizeAndroid" / "CavernizeAndroid.csproj").write_text(
            "<Project><PropertyGroup><ApplicationDisplayVersion>2.8.28</ApplicationDisplayVersion></PropertyGroup></Project>\n",
            encoding="utf-8",
        )
        (repo / ".github" / "workflows" / "BuildAndroid.yml").write_text(
            "run: dotnet publish -p:ApplicationDisplayVersion=2.8.${{ github.run_number }}\n",
            encoding="utf-8",
        )
        expected_sdk = "35" if any("35" in (g.get("must_contain_regex") or "") for g in (profile_data.get("file_guards") or [])) else "28"
        (repo / "CavernSamples" / "CavernizeAndroid" / "Properties" / "AndroidManifest.xml").write_text(
            f'<manifest><uses-sdk android:targetSdkVersion="{expected_sdk}" /></manifest>\n',
            encoding="utf-8",
        )
        (repo / "CavernSamples" / "CavernizeAndroid" / "Language" / "AndroidStrings.cs").write_text(
            'class AndroidStrings {\n  static Dictionary<string, string> CreateEnglish() => new() {\n    ["Hello"] = "Hello",\n  };\n  static void ApplyPortuguese(Dictionary<string, string> d) {\n    d["Hello"] = "Olá";\n  }\n}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

        prof = copy.deepcopy(profile_data)
        dotnet_marker = repo / ".fake_dotnet"
        dotnet_marker.write_text("#!/bin/sh\n", encoding="utf-8")
        for g in prof.get("command_guards") or []:
            cond = g.get("conditions") or {}
            if "exists_path" in cond:
                cond["exists_path"] = str(dotnet_marker)
            if "not_exists_glob" in cond:
                cond["not_exists_glob"] = str(repo / ".nonexistent_android_pack*")
        (repo / ".agents" / "planner_profile.json").write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
        return repo

    def _assert_parity_for_profile(self, label: str, profile_data: dict[str, Any]) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = self._make_cavern_repo(td, profile_data)

            # 1. Triage
            tri = complexity_triage.analyze_prompt(
                "Corrigir layout One UI na tela Play, ajustar decoder TrueHD em Cavern.Format, resolver AudioTrack e gerar APK release",
                repo,
            )
            self.assertEqual(tri["tier"], 3, label)
            for dom in ("ui-oneui9", "dsp-format", "media-native", "build-deliver"):
                self.assertIn(dom, tri["detected_domains"], f"{label} missing {dom}")

            # 2. SessionStart
            rc, err, ss = run_hook("planner_session_start.py", {"workspacePaths": [str(repo)], "artifactDirectoryPath": art})
            self.assertEqual(rc, 0, err)
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("csproj=2.8.28 | CI=2.8.${{ github.run_number }}", ss_msg)
            self.assertNotIn("Schema warning", ss_msg)

            # 3. Lean PreInvocation (auto_reqs + JIT checklists)
            multi_prompt = "1. Ajustar decoder em Cavern.Format\n2. Corrigir layout One UI na tela Play\n3. Gerar APK release"
            _, _, pre_lean = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": multi_prompt, "invocationNum": 1,
            }, env_extra={"PLANNER_MODE": "lean"})
            lean_msg = pre_lean["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("mode=lean", lean_msg)
            self.assertIn("REQS|REQ-01:", lean_msg)

            # 4. Strict PreInvocation + PLAN GATE
            with tempfile.TemporaryDirectory() as art_strict:
                _, _, pre_strict = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)], "artifactDirectoryPath": art_strict, "lastUserInput": multi_prompt, "invocationNum": 1,
                }, env_extra={"PLANNER_MODE": "strict"})
                self.assertIn("mode=strict", pre_strict["injectSteps"][0]["systemMessage"]["systemMessage"])
                _, _, fg_gate = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)], "artifactDirectoryPath": art_strict, "lastUserInput": multi_prompt,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "Cavern.Format" / "Sample.cs"), "Target": "a", "Replacement": "b"}},
                }, env_extra={"PLANNER_MODE": "strict"})
                self.assertEqual(fg_gate.get("decision"), "deny", label)
                self.assertIn("PLAN GATE", fg_gate.get("reason", ""))

            # 5. File guard: targetSdkVersion mismatch
            expected_sdk = "35" if any("35" in (g.get("must_contain_regex") or "") for g in (profile_data.get("file_guards") or [])) else "28"
            manifest = str(repo / "CavernSamples" / "CavernizeAndroid" / "Properties" / "AndroidManifest.xml")
            _, _, fg_bad = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": manifest, "Target": f'android:targetSdkVersion="{expected_sdk}"', "Replacement": 'android:targetSdkVersion="34"'}},
            })
            self.assertEqual(fg_bad.get("decision"), "force_ask", label)

            # 6. Command guards & rewrite
            _, _, cg_apk = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "DOTNET_GCHeapHardLimit=20000000 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 dotnet build CavernSamples/CavernizeAndroid/CavernizeAndroid.csproj"}},
            })
            self.assertEqual(cg_apk.get("decision"), "deny", label)

            _, _, cg_ok = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "DOTNET_GCHeapHardLimit=20000000 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 /root/.dotnet/dotnet build CavernSamples/Reusable/Cavernize.Logic/Cavernize.Logic.csproj"}},
            })
            self.assertEqual(cg_ok.get("decision"), "allow", label)
            self.assertIn("--nologo -v:minimal", cg_ok.get("overwrite", {}).get("CommandLine", ""))

            # 7. verify_universal diff_rules & key_parity_rules
            (repo / "Cavern.Format" / "Bad.cs").write_text("namespace Foo;\nclass Bad { void F(object x) { if (x is not null) {} } }\n", encoding="utf-8")
            (repo / "CavernSamples" / "CavernizeAndroid" / "BadUi.cs").write_text("class BadUi { void F() { var d = new AlertDialog(); var s = new Spinner(); } }\n", encoding="utf-8")
            (repo / "CavernSamples" / "CavernizeAndroid" / "Language" / "AndroidStrings.cs").write_text(
                'class AndroidStrings {\n  static Dictionary<string, string> CreateEnglish() => new() {\n    ["Hello"] = "Hello",\n    ["MissingPtKey"] = "Missing",\n  };\n  static void ApplyPortuguese(Dictionary<string, string> d) {\n    d["Hello"] = "Olá";\n  }\n}\n',
                encoding="utf-8",
            )
            v_fail = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertNotEqual(v_fail.returncode, 0, label)
            self.assertIn("is not", v_fail.stdout)
            self.assertIn("AlertDialog", v_fail.stdout)
            self.assertIn("MissingPtKey", v_fail.stdout)

            (repo / "Cavern.Format" / "Bad.cs").unlink()
            (repo / "CavernSamples" / "CavernizeAndroid" / "BadUi.cs").unlink()
            (repo / "CavernSamples" / "CavernizeAndroid" / "Language" / "AndroidStrings.cs").write_text(
                'class AndroidStrings {\n  static Dictionary<string, string> CreateEnglish() => new() {\n    ["Hello"] = "Hello",\n    ["MissingPtKey"] = "Missing",\n  };\n  static void ApplyPortuguese(Dictionary<string, string> d) {\n    d["Hello"] = "Olá";\n    d["MissingPtKey"] = "Faltando";\n  }\n}\n',
                encoding="utf-8",
            )
            v_pass = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertEqual(v_pass.returncode, 0, v_pass.stdout + v_pass.stderr)
            self.assertIn("UNIVERSAL VERIFY PASS", v_pass.stdout)

    def test_cavern_v1_v2_and_live_parity(self) -> None:
        if not FAST_MODE:  # v1->v5 migration parity already covered by TestSchemaMigrations fixtures
            v1_cavern = json.loads((FIXTURES_DIR / "profile_v1_cavern.json").read_text(encoding="utf-8"))
            self._assert_parity_for_profile("v1_cavern_fixture", v1_cavern)
            v4_cavern = json.loads((FIXTURES_DIR / "profile_v4_cavern.json").read_text(encoding="utf-8"))
            self._assert_parity_for_profile("v4_cavern_fixture", v4_cavern)

        v2_cavern = json.loads((FIXTURES_DIR / "profile_v2_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v2_cavern.get("schema_version"), 2)
        self._assert_parity_for_profile("v2_cavern_fixture", v2_cavern)

        if self.LIVE_CAVERN_PROFILE.is_file():
            live_cavern = json.loads(self.LIVE_CAVERN_PROFILE.read_text(encoding="utf-8"))
            self.assertIn(live_cavern.get("schema_version"), (2, 3, 4, 5))
            self._assert_parity_for_profile(f"live_v{live_cavern.get('schema_version')}_cavern", live_cavern)


class TestDocMcpIntegration(unittest.TestCase):
    def test_detect_doc_mcps_and_profile_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td)
            self.assertEqual(planner_state.detect_doc_mcps({}, plugins_dir=pdir), {"gdk": False, "context7": False})

            (pdir / "google-developer-knowledge").mkdir()
            (pdir / "google-developer-knowledge" / "mcp_config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(planner_state.detect_doc_mcps({}, plugins_dir=pdir), {"gdk": True, "context7": False})

            (pdir / "context7").mkdir()
            (pdir / "context7" / "mcp_config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(planner_state.detect_doc_mcps({}, plugins_dir=pdir), {"gdk": True, "context7": True})

            # Profile disable & per-provider override
            self.assertEqual(
                planner_state.detect_doc_mcps({"doc_lookup": {"enabled": False}}, plugins_dir=pdir),
                {"gdk": False, "context7": False},
            )
            self.assertEqual(
                planner_state.detect_doc_mcps({"doc_lookup": {"gdk": False, "context7": True}}, plugins_dir=pdir),
                {"gdk": False, "context7": True},
            )

    def test_triage_and_hooks_doc_mcp_routing(self) -> None:
        self.assertIn("gdk", complexity_triage.detect_doc_lookup_targets("Como configurar MediaSession e targetSdkVersion no Android?"))
        self.assertIn("context7", complexity_triage.detect_doc_lookup_targets("Write a Prisma query for Next.js 15"))
        hybrid = complexity_triage.detect_doc_lookup_targets("Integrate Firebase Auth with React and Next.js")
        self.assertEqual(hybrid, ["gdk", "context7"])
        self.assertEqual(complexity_triage.detect_doc_lookup_targets("oi"), [])

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as pdir, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            plugins = Path(pdir)
            for slug in ("google-developer-knowledge", "context7"):
                (plugins / slug).mkdir()
                (plugins / slug / "mcp_config.json").write_text("{}", encoding="utf-8")

            env_mcps = {"PLANNER_PLUGINS_DIR": str(plugins)}

            # SessionStart emits DOC_MCP
            rc, err, sess = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
            }, env_extra=env_mcps)
            self.assertEqual(rc, 0, err)
            sess_msg = sess["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("DOC_MCP|google-developer-knowledge(search_documents/answer_query) + context7(resolve-library-id->query-docs)", sess_msg)

            # Tier 0 technical question routes to GDK
            _, _, pre_q = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "lastUserInput": "Como funciona MediaStore no Android?",
                "invocationNum": 1,
            }, env_extra=env_mcps)
            q_msg = pre_q["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("DOC_LOOKUP|GDK(", q_msg)
            self.assertNotIn("Context7(", q_msg)

            # Same turn inv 2 does NOT repeat DOC_LOOKUP
            _, _, pre_q2 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "lastUserInput": "Como funciona MediaStore no Android?",
                "invocationNum": 2,
            }, env_extra=env_mcps)
            self.assertNotIn("DOC_LOOKUP|", json.dumps(pre_q2))

            # ECO_MODE suppresses Tier 0 DOC_LOOKUP
            _, _, pre_eco = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "lastUserInput": "Economiza cota, explica MediaStore no Android",
                "invocationNum": 1,
            }, env_extra=env_mcps)
            eco_msg = pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("ECO_MODE|ON", eco_msg)
            self.assertNotIn("DOC_LOOKUP|", eco_msg)

            # Circuit breaker includes GDK/Context7 hint
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            hs["same_cmd_fails"] = 2
            hs["last_failed_cmd_sig"] = "dotnet build Foo.csproj"
            hook_file.write_text(json.dumps(hs), encoding="utf-8")
            _, _, pre_cb = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "lastUserInput": "Economiza cota, explica MediaStore no Android",
                "invocationNum": 2,
            }, env_extra=env_mcps)
            cb_msg = pre_cb["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("CIRCUIT BREAKER", cb_msg)
            self.assertIn("GDK/Context7", cb_msg)


class TestAutoModelMode(unittest.TestCase):
    @staticmethod
    def _run_hook_args(
        script_name: str,
        payload: dict[str, Any],
        extra_args: list[str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[int, str, dict[str, Any]]:
        merged_env = {"PLANNER_DISABLE_TELEMETRY": "1"}
        if env_extra:
            merged_env.update(env_extra)
        cli_args = list(extra_args or [])
        if FAST_MODE:
            mod = _load_mod(HOOKS_DIR / script_name)
            rc = 0
            old_argv = sys.argv
            try:
                sys.argv = [str(HOOKS_DIR / script_name), *cli_args]
                with _patched_env(merged_env, stdin_txt=json.dumps(payload)) as (out_buf, err_buf):
                    try:
                        mod.main()
                    except SystemExit as exc:
                        rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                    out_txt = out_buf.getvalue().strip()
                    err_txt = err_buf.getvalue().strip()
            finally:
                sys.argv = old_argv
            parsed = json.loads(out_txt) if out_txt.startswith("{") else ({"_raw": out_txt} if out_txt else {})
            return rc, err_txt, parsed

        env = os.environ.copy()
        env.update(merged_env)
        res = subprocess.run(
            [sys.executable, str(HOOKS_DIR / script_name), *cli_args],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
            check=False,
        )
        out_txt = res.stdout.strip()
        parsed = json.loads(out_txt) if out_txt.startswith("{") else ({"_raw": out_txt} if out_txt else {})
        return res.returncode, res.stderr.strip(), parsed

    def test_seed_cache_hit_fast_path_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            cache_file = repo / ".cache_models.json"
            prof_auto = {"schema_version": 3, "planner_mode": "auto"}
            base_env = {
                "PLANNER_MODEL_CACHE_FILE": str(cache_file),
                "PLANNER_MODEL_OFFLINE": "1",
                "PLANNER_MODEL_DISABLE_AGY_CLI": "1",
                "PLANNER_MODE": "",
            }

            with _patched_env(base_env):
                # Seeded frontier/orchestrator models -> lean (zero network)
                for m in (
                    "gemini-4-neon",
                    "Gemini 4 Neon (Medium)",
                    "gemini-4-neon-xhigh",
                    "gemini-pro-orchestrator-neon",
                    "Orchestrator (Carbon)",
                    "claude-opus-5-5",
                ):
                    mode, src, _ = planner_state.resolve_planner_mode_info(prof_auto, {"modelName": m})
                    self.assertEqual(mode, "lean", m)
                    self.assertEqual(src, "auto:seed", m)

                # Seeded lightweight/executor models -> strict
                for m in ("gemini-4-hydrogen", "gemini-3.6-flash-medium", "claude-sonnet-5-5", "gpt-oss-120b"):
                    mode, src, _ = planner_state.resolve_planner_mode_info(prof_auto, {"modelName": m})
                    self.assertEqual(mode, "strict", m)
                    self.assertEqual(src, "auto:seed", m)

                # Missing or literal "auto" -> safe lean
                self.assertEqual(planner_state.resolve_planner_mode_info(prof_auto, {})[:2], ("lean", "auto:no_model"))
                self.assertEqual(
                    planner_state.resolve_planner_mode_info(prof_auto, {"modelName": "auto"})[:2],
                    ("lean", "auto:no_model"),
                )

                # Zero regex/keyword guessing: unknown models stay safe lean ("auto:unseen")
                for unseen_m in ("future-flash-9000", "future-sonnet-9", "future-orchestrator-z"):
                    mode, src, _ = planner_state.resolve_planner_mode_info(prof_auto, {"modelName": unseen_m})
                    self.assertEqual(mode, "lean", unseen_m)
                    self.assertEqual(src, "auto:unseen", unseen_m)

                # Precedence: PLANNER_MODE env > profile.planner_mode > auto_mode.overrides > cache > seed
                override_prof = {
                    "schema_version": 3,
                    "planner_mode": "auto",
                    "auto_mode": {"overrides": {"gemini-4-neon": "strict", "my-custom-v9": "strict"}},
                }
                self.assertEqual(
                    planner_state.resolve_planner_mode_info(override_prof, {"modelName": "gemini-4-neon"})[:2],
                    ("strict", "auto:profile_override"),
                )
                self.assertEqual(
                    planner_state.resolve_planner_mode_info(override_prof, {"modelName": "my-custom-v9"})[:2],
                    ("strict", "auto:profile_override"),
                )
                strict_prof = {"schema_version": 3, "planner_mode": "strict"}
                self.assertEqual(
                    planner_state.resolve_planner_mode_info(strict_prof, {"modelName": "gemini-4-neon"})[:2],
                    ("strict", "profile"),
                )
                with _patched_env({"PLANNER_MODE": "lean"}):
                    self.assertEqual(
                        planner_state.resolve_planner_mode_info(strict_prof, {"modelName": "gemini-3.6-flash-medium"})[:2],
                        ("lean", "env"),
                    )

                # Cache overrides seed
                model_mode_cache.set_model_mode("gemini-4-hydrogen", "lean", why="user override", cache_path=cache_file)
                self.assertEqual(
                    planner_state.resolve_planner_mode_info(prof_auto, {"modelName": "gemini-4-hydrogen"})[:2],
                    ("lean", "auto:cache:ai"),
                )

                # Fast-path timing: 50 lookups < 50ms
                t0 = time.monotonic()
                for _ in range(50):
                    planner_state.resolve_planner_mode_info(prof_auto, {"modelName": "Gemini 4 Neon (Medium)"})
                self.assertLess(time.monotonic() - t0, 0.05)

            # Known model in PreInvocation -> zero MODEL_MODE_CLASSIFY & live mid-conversation switch
            (repo / ".agents").mkdir()
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(prof_auto), encoding="utf-8")
            src_f = repo / "app.py"
            src_f.write_text("x = 1\n", encoding="utf-8")
            complex_prompt = "1. Refactor auth\n2. Add pytest tests\n3. Update API routes"

            _, _, pre_known = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-neon",
                "lastUserInput": complex_prompt,
                "invocationNum": 1,
            }, env_extra=base_env)
            self.assertNotIn("MODEL_MODE_CLASSIFY", json.dumps(pre_known))

            _, _, dec_neon = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-neon",
                "lastUserInput": complex_prompt,
                "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
            }, env_extra=base_env)
            self.assertEqual(dec_neon.get("decision"), "allow")

            _, _, dec_flash = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-3.6-flash-medium",
                "lastUserInput": complex_prompt,
                "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
            }, env_extra=base_env)
            self.assertEqual(dec_flash.get("decision"), "deny")
            self.assertIn("PLAN GATE", dec_flash.get("reason", ""))

    def test_unseen_model_ai_classification_and_persistence(self) -> None:
        hits = {"count": 0}
        mock_md = (
            "# Choose a model\n\n"
            "## Reasoning model\n"
            "| Model | Free | Pro | Ultra | Enterprise |\n"
            "| Gemini 5 Krypton | ✅ | ✅ | ✅ | ✅ |\n\n"
            "### Gemini 5 Krypton\n"
            "Fast, lightweight everyday coding and execution model for routine edits.\n"
        ).encode("utf-8")

        class MockDocsHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits["count"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.end_headers()
                self.wfile.write(mock_md)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), MockDocsHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
                repo = Path(td)
                init_git_repo(repo)
                (repo / ".agents").mkdir()
                (repo / ".agents" / "planner_profile.json").write_text(
                    json.dumps({"schema_version": 3, "planner_mode": "auto"}),
                    encoding="utf-8",
                )
                src_f = repo / "app.py"
                src_f.write_text("x = 1\n", encoding="utf-8")
                cache_file = repo / ".cache_models.json"
                docs_url = f"http://127.0.0.1:{port}/docs/models.md"
                env_live = {
                    "PLANNER_MODEL_CACHE_FILE": str(cache_file),
                    "PLANNER_MODEL_DOCS_URL": docs_url,
                    "PLANNER_MODEL_DISABLE_AGY_CLI": "1",
                    "PLANNER_MODEL_COOLDOWN_SECS": "900",
                    "PLANNER_MODE": "",
                }
                complex_prompt = "1. Refactor parser\n2. Add unit tests\n3. Update docs"

                # First unseen invocation fetches docs once & injects compact prompt while temporarily lean
                _, _, pre1 = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-5-krypton-high",
                    "lastUserInput": complex_prompt,
                    "invocationNum": 1,
                }, env_extra=env_live)
                self.assertEqual(hits["count"], 1)
                msg1 = pre1["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[MODEL_MODE_CLASSIFY|model=gemini-5-krypton-high|key=gemini-5-krypton|tmp=lean]", msg1)
                self.assertIn("CRITERIA|", msg1)
                self.assertIn("CTX|", msg1)
                self.assertIn("Krypton", msg1)
                self.assertIn('model-mode set "gemini-5-krypton"', msg1)

                # While unclassified -> safe lean (no PLAN GATE block)
                _, _, dec_tmp = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-5-krypton-high",
                    "lastUserInput": complex_prompt,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
                }, env_extra=env_live)
                self.assertEqual(dec_tmp.get("decision"), "allow")

                # Second invocation in same turn -> NO duplicate classify prompt & NO extra HTTP fetch
                _, _, pre2 = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-5-krypton-high",
                    "lastUserInput": complex_prompt,
                    "invocationNum": 2,
                }, env_extra=env_live)
                self.assertEqual(hits["count"], 1)
                self.assertNotIn("MODEL_MODE_CLASSIFY", json.dumps(pre2))

                # Persist AI verdict via CLI -> strict
                res_set = run_script("plan_tool.py", [
                    "--workspace", str(repo), "model-mode", "set", "gemini-5-krypton", "strict",
                    "--why", "Fast lightweight everyday executor",
                ], env_extra=env_live)
                self.assertEqual(res_set.returncode, 0, res_set.stderr)
                self.assertIn("MODEL_MODE_SET|key=gemini-5-krypton|mode=strict|src=ai|", res_set.stdout)

                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                self.assertEqual(cached["models"]["gemini-5-krypton"]["mode"], "strict")
                self.assertNotIn("gemini-5-krypton", cached["pending"])

                # New turn with different effort suffix -> 0 network, cached strict enforces PLAN GATE
                new_prompt = "1. Refactor cache\n2. Add pytest tests\n3. Update API routes"
                _, _, pre3 = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-5-krypton-low",
                    "lastUserInput": new_prompt,
                    "invocationNum": 1,
                }, env_extra=env_live)
                self.assertEqual(hits["count"], 1)
                self.assertNotIn("MODEL_MODE_CLASSIFY", json.dumps(pre3))

                _, _, dec_strict = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-5-krypton-low",
                    "lastUserInput": new_prompt,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
                }, env_extra=env_live)
                self.assertEqual(dec_strict.get("decision"), "deny")
                self.assertIn("PLAN GATE", dec_strict.get("reason", ""))

                # Verify CLI get / list / clear subcommands
                res_get = run_script("plan_tool.py", [
                    "--workspace", str(repo), "model-mode", "get", "gemini-5-krypton-low",
                ], env_extra=env_live)
                self.assertEqual(res_get.returncode, 0)
                self.assertIn("MODEL_MODE|key=gemini-5-krypton|mode=strict|src=auto:cache:ai", res_get.stdout)

                res_list = run_script("plan_tool.py", ["--workspace", str(repo), "model-mode", "list"], env_extra=env_live)
                self.assertEqual(res_list.returncode, 0)
                self.assertIn("gemini-5-krypton|strict|ai|", res_list.stdout)

                res_clr = run_script("plan_tool.py", [
                    "--workspace", str(repo), "model-mode", "clear", "gemini-5-krypton",
                ], env_extra=env_live)
                self.assertEqual(res_clr.returncode, 0)
                self.assertIn("MODEL_MODE_CLEARED|removed=1", res_clr.stdout)
        finally:
            server.shutdown()
            server.server_close()

    def test_unseen_model_offline_fallback_and_cooldown(self) -> None:
        hits = {"count": 0}

        class MockDocsHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits["count"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"# Models\n| Gemini 9 Offline | New |\n")

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), MockDocsHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
                repo = Path(td)
                init_git_repo(repo)
                (repo / ".agents").mkdir()
                (repo / ".agents" / "planner_profile.json").write_text(
                    json.dumps({"schema_version": 3, "planner_mode": "auto"}),
                    encoding="utf-8",
                )
                src_f = repo / "app.py"
                src_f.write_text("x = 1\n", encoding="utf-8")
                cache_file = repo / ".cache_models.json"
                docs_url = f"http://127.0.0.1:{port}/docs/models.md"

                env_off = {
                    "PLANNER_MODEL_CACHE_FILE": str(cache_file),
                    "PLANNER_MODEL_OFFLINE": "1",
                    "PLANNER_MODEL_DISABLE_AGY_CLI": "1",
                    "PLANNER_MODEL_COOLDOWN_SECS": "3600",
                    "PLANNER_MODE": "",
                }
                prompt1 = "1. Refactor auth\n2. Add tests\n3. Update docs"
                _, _, pre_off = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-9-offline",
                    "lastUserInput": prompt1,
                    "invocationNum": 1,
                }, env_extra=env_off)
                self.assertNotIn("MODEL_MODE_CLASSIFY", json.dumps(pre_off))

                # Safe lean when offline (no PLAN GATE)
                _, _, dec_off = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-9-offline",
                    "lastUserInput": prompt1,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
                }, env_extra=env_off)
                self.assertEqual(dec_off.get("decision"), "allow")

                cache = json.loads(cache_file.read_text(encoding="utf-8"))
                pend = cache["pending"]["gemini-9-offline"]
                self.assertEqual(pend["status"], "offline")
                self.assertGreater(pend["next_retry_ts"], pend["last_attempt_ts"])

                # Next turn inside cooldown -> 0 HTTP requests
                env_on = {
                    "PLANNER_MODEL_CACHE_FILE": str(cache_file),
                    "PLANNER_MODEL_DOCS_URL": docs_url,
                    "PLANNER_MODEL_DISABLE_AGY_CLI": "1",
                    "PLANNER_MODEL_COOLDOWN_SECS": "3600",
                    "PLANNER_MODE": "",
                }
                _, _, pre_cd = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-9-offline",
                    "lastUserInput": "Retry check",
                    "invocationNum": 1,
                }, env_extra=env_on)
                self.assertEqual(hits["count"], 0)
                self.assertNotIn("MODEL_MODE_CLASSIFY", json.dumps(pre_cd))

                # Expire cooldown -> triggers fetch & classification prompt once
                cache["pending"]["gemini-9-offline"]["next_retry_ts"] = 0
                cache_file.write_text(json.dumps(cache), encoding="utf-8")
                _, _, pre_retry = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-9-offline",
                    "lastUserInput": "Retry after cooldown",
                    "invocationNum": 1,
                }, env_extra=env_on)
                self.assertEqual(hits["count"], 1)
                msg_retry = pre_retry["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[MODEL_MODE_CLASSIFY|model=gemini-9-offline|key=gemini-9-offline|tmp=lean]", msg_retry)
        finally:
            server.shutdown()
            server.server_close()

    def test_native_plan_mode_auto_pin_and_handoff_no_collision(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir()
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 3, "planner_mode": "auto"}),
                encoding="utf-8",
            )
            src_f = repo / "app.py"
            src_f.write_text("x = 1\n", encoding="utf-8")
            cache_file = repo / ".cache_models.json"
            env_pin = {
                "PLANNER_MODEL_CACHE_FILE": str(cache_file),
                "PLANNER_MODEL_OFFLINE": "1",
                "PLANNER_MODEL_DISABLE_AGY_CLI": "1",
                "PLANNER_MODE": "",
            }

            # 1. PreToolUse --pre on exit_plan_mode auto-pins unseen model to lean
            _, _, pre_pin = self._run_hook_args(
                "planner_post_tool.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "future-planner-x",
                    "toolCall": {"name": "exit_plan_mode", "args": {}},
                },
                extra_args=["--pre"],
                env_extra=env_pin,
            )
            self.assertEqual(pre_pin.get("decision"), "allow")
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(cache["models"]["future-planner-x"]["mode"], "lean")
            self.assertEqual(cache["models"]["future-planner-x"]["source"], "native_plan_tool")

            # 2. PostToolUse on enter_plan_mode upgrades previously strict model to lean
            with _patched_env(env_pin):
                model_mode_cache.set_model_mode("hybrid-exec-y", "strict", why="initial", cache_path=cache_file)
            _, _, post_pin = self._run_hook_args(
                "planner_post_tool.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "hybrid-exec-y",
                    "toolCall": {"name": "enter_plan_mode", "args": {}},
                },
                env_extra=env_pin,
            )
            self.assertEqual(post_pin, {})
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(cache["models"]["hybrid-exec-y"]["mode"], "lean")
            self.assertEqual(cache["models"]["hybrid-exec-y"]["source"], "native_plan_tool")

            # 3. Mid-turn Orchestrator handoff to seeded strict executor (gemini-4-hydrogen) keeps lean -> NO collision!
            turn1_prompt = "1. Refactor auth\n2. Add pytest tests\n3. Update API routes"
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-pro-orchestrator-neon",
                "lastUserInput": turn1_prompt,
                "invocationNum": 1,
            }, env_extra=env_pin)
            self._run_hook_args(
                "planner_post_tool.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "modelName": "gemini-pro-orchestrator-neon",
                    "lastUserInput": turn1_prompt,
                    "toolCall": {"name": "exit_plan_mode", "args": {}},
                },
                extra_args=["--pre"],
                env_extra=env_pin,
            )
            _, _, dec_handoff = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-hydrogen",
                "lastUserInput": turn1_prompt,
                "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
            }, env_extra=env_pin)
            self.assertEqual(dec_handoff.get("decision"), "allow")

            # 4. New standalone turn directly on gemini-4-hydrogen -> strict enforces PLAN GATE
            turn2_prompt = "1. Refactor cache\n2. Add pytest tests\n3. Update API routes"
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-hydrogen",
                "lastUserInput": turn2_prompt,
                "invocationNum": 1,
            }, env_extra=env_pin)
            _, _, dec_new = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-hydrogen",
                "lastUserInput": turn2_prompt,
                "toolCall": {"name": "edit_file", "args": {"Path": str(src_f), "Target": "x = 1", "Replacement": "x = 2"}},
            }, env_extra=env_pin)
            self.assertEqual(dec_new.get("decision"), "deny")
            self.assertIn("PLAN GATE", dec_new.get("reason", ""))


class TestSymbolicTelegraphicStyle(unittest.TestCase):
    def test_symbolic_telegraphic_en_rules_templates_and_nudges(self) -> None:
        agents_txt = (PLUGIN_ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
        mem_skill = (PLUGIN_ROOT / "skills" / "project-memory" / "SKILL.md").read_text(encoding="utf-8")
        plan_skill = (PLUGIN_ROOT / "skills" / "dynamic-planner" / "SKILL.md").read_text(encoding="utf-8")

        for txt in (agents_txt, mem_skill, plan_skill):
            self.assertIn("symbolic telegraphic en", txt.lower())
        self.assertIn("user's language", agents_txt)
        self.assertIn("regardless of chat language", agents_txt)

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            planner_state.bootstrap_workspace(repo)

            docs_dir = repo / ".agents" / "docs"
            for name in ("PROJECT_HISTORY.md", "SAVED_IDEAS.md", "EXTRA_REQUESTS.md"):
                content = (docs_dir / name).read_text(encoding="utf-8")
                self.assertIn("STYLE|lossless symbolic telegraphic EN", content)
                self.assertIn("->", content)

            _, pending = planner_state.get_pending_extra_requests(repo, {})
            self.assertEqual(pending, [])

            # Stale memory docs + 2 dirty code files trigger MEMORY_FEEDBACK with symbolic telegraphic EN hint
            old_ts = time.time() - 7200
            for doc in docs_dir.glob("*.md"):
                os.utime(doc, (old_ts, old_ts))
            for idx in range(2):
                (repo / f"mod_{idx}.py").write_text(f"val = {idx}\n", encoding="utf-8")

            conv_id = f"sym_style_test_{os.getpid()}"
            marker = Path(f"/tmp/.agy_mem_reminded_{conv_id}")
            if marker.exists():
                marker.unlink()
            try:
                _, _, dec_mem = run_hook("planner_guard_command.py", {
                    "conversationId": conv_id,
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'test'"}},
                })
                self.assertEqual(dec_mem.get("decision"), "deny")
                self.assertIn("symbolic telegraphic EN", dec_mem.get("reason", ""))
            finally:
                if marker.exists():
                    marker.unlink()

            # Long-chat QUOTA TIP nudge includes symbolic telegraphic EN guidance
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Check status",
                "invocationNum": 1,
            })
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            hs["total_turns"] = 25
            hs["handoff_nudged"] = False
            hook_file.write_text(json.dumps(hs), encoding="utf-8")
            _, _, nudge_out = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Turn 26",
                "invocationNum": 1,
            })
            nudge_str = json.dumps(nudge_out)
            self.assertIn("QUOTA TIP", nudge_str)
            self.assertIn("symbolic telegraphic EN", nudge_str)

    def test_auto_upgrade_legacy_profile_and_empty_boilerplate_docs(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            agents_dir = repo / ".agents"
            docs_dir = agents_dir / "docs"
            docs_dir.mkdir(parents=True)

            v1_text = (FIXTURES_DIR / "profile_v1_generic.json").read_text(encoding="utf-8")
            prof_file = agents_dir / "planner_profile.json"
            prof_file.write_text(v1_text, encoding="utf-8")

            (docs_dir / "PROJECT_HISTORY.md").write_text(
                "# Project History & Engineering Pitfalls (`PROJECT_HISTORY.md`)\n\n"
                "> **Chronological Archive Only:** Never write `Current State`.\n\n"
                "## 1. Environment & Stack Overview\n"
                "- (Document runtime/build/test commands and key environments here.)\n\n"
                "## 2. Technical Lessons & Known Pitfalls\n"
                "- (Record non-obvious bugs, edge cases, and invariants so future sessions never repeat them.)\n\n"
                "## 3. Version History (Newest First)\n"
                "<!-- MAINTENANCE RULE: Always PREPEND -->\n",
                encoding="utf-8",
            )
            (docs_dir / "SAVED_IDEAS.md").write_text(
                "# Saved Ideas & Archived Prototypes (`SAVED_IDEAS.md`)\n\n"
                "> Stores promising ideas.\n\n---\n",
                encoding="utf-8",
            )
            (docs_dir / "EXTRA_REQUESTS.md").write_text(
                "# Extra Request Queue (Parallel `/btw` Threads)\n\n"
                "> **Instructions for the Primary Agent:**\n"
                "> Before finishing any task, read this file.\n\n"
                "## Pending Extra Requests\n(No pending requests.)\n\n"
                "## Completed Requests\n(No completed requests recorded yet.)\n",
                encoding="utf-8",
            )

            rc, err, ss = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("AUTO_UPGRADED|", ss_msg)
            self.assertNotIn("DOCS_AUTO_MIGRATE", ss_msg)

            upgraded_prof = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(upgraded_prof["schema_version"], 5)
            self.assertEqual(upgraded_prof["docs_format_version"], 1)
            self.assertEqual(upgraded_prof["custom_owner_note"], "preserve-me-losslessly")

            for name in ("PROJECT_HISTORY.md", "SAVED_IDEAS.md", "EXTRA_REQUESTS.md"):
                self.assertIn("STYLE|lossless symbolic telegraphic EN", (docs_dir / name).read_text(encoding="utf-8"))
            self.assertEqual(planner_state.get_pending_extra_requests(repo, {})[1], [])

    def test_auto_migrate_populated_legacy_docs_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            agents_dir = repo / ".agents"
            docs_dir = agents_dir / "docs"
            docs_dir.mkdir(parents=True)

            v2_prof = {"schema_version": 2, "project_name": "PopulatedLegacy", "build_rules": []}
            prof_file = agents_dir / "planner_profile.json"
            prof_file.write_text(json.dumps(v2_prof), encoding="utf-8")

            pop_hist = "# Project History\n\n## 3. Version History\n- 2026-01-10: Corrigido bug no AudioTrack\n"
            pop_ideas = "# Saved Ideas\n\n- Ideia shader bloom com render target separado\n"
            empty_extra = (
                "# Extra Request Queue\n\n"
                "## Pending Extra Requests\n(No pending requests.)\n\n"
                "## Completed Requests\n(No completed requests recorded yet.)\n"
            )
            (docs_dir / "PROJECT_HISTORY.md").write_text(pop_hist, encoding="utf-8")
            (docs_dir / "SAVED_IDEAS.md").write_text(pop_ideas, encoding="utf-8")
            (docs_dir / "EXTRA_REQUESTS.md").write_text(empty_extra, encoding="utf-8")

            # 1. SessionStart upgrades profile v2->v4 & empty EXTRA_REQUESTS.md, preserves populated docs byte-for-byte
            rc, err, ss = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("DOCS_AUTO_MIGRATE|v0->v1", ss_msg)
            self.assertIn("PROJECT_HISTORY.md", ss_msg)
            self.assertIn("SAVED_IDEAS.md", ss_msg)
            self.assertNotIn("EXTRA_REQUESTS.md", ss_msg.split("DOCS_AUTO_MIGRATE", 1)[1].split("ACT:", 1)[0])

            prof_after_ss = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(prof_after_ss["schema_version"], 5)
            self.assertEqual(prof_after_ss.get("docs_format_version", 0), 0)
            self.assertEqual((docs_dir / "PROJECT_HISTORY.md").read_text(encoding="utf-8"), pop_hist)
            self.assertEqual((docs_dir / "SAVED_IDEAS.md").read_text(encoding="utf-8"), pop_ideas)
            self.assertIn("STYLE|lossless symbolic telegraphic EN", (docs_dir / "EXTRA_REQUESTS.md").read_text(encoding="utf-8"))

            # 2. PreInvocation injects [DOCS_AUTO_MIGRATE once per turn & Neon handoff does not re-inject
            prompt = "Fix bug in AudioTrack"
            _, _, pre1 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prompt,
                "invocationNum": 1,
            })
            pre1_msg = pre1["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[DOCS_AUTO_MIGRATE|v0->v1|", pre1_msg)

            _, _, pre_handoff = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prompt,
                "invocationNum": 1,
            })
            self.assertEqual(pre_handoff, {})

            # 3. docs-migrated fails before populated docs are converted
            dm_fail = run_script("plan_tool.py", ["--workspace", str(repo), "docs-migrated"])
            self.assertNotEqual(dm_fail.returncode, 0)
            self.assertIn("DOCS_MIGRATION_INCOMPLETE", dm_fail.stdout)

            # 4. Convert populated docs and stamp docs_format_version=1
            (docs_dir / "PROJECT_HISTORY.md").write_text(
                "# Project History\n> STYLE|lossless symbolic telegraphic EN ONLY\n- 2026-01-10|fix|AudioTrack glitch -> sync buffer\n",
                encoding="utf-8",
            )
            (docs_dir / "SAVED_IDEAS.md").write_text(
                "# Saved Ideas\n> STYLE|lossless symbolic telegraphic EN ONLY\n- IDEA|bloom shader -> deferred | offscreen RT\n",
                encoding="utf-8",
            )
            dm_ok = run_script("plan_tool.py", ["--workspace", str(repo), "docs-migrated"])
            self.assertEqual(dm_ok.returncode, 0, dm_ok.stdout + dm_ok.stderr)
            self.assertIn("DOCS_MIGRATED|docs_format_version=1", dm_ok.stdout)
            prof_done = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(prof_done["docs_format_version"], 1)

            # 5. Fast path on subsequent SessionStart & new-turn PreInvocation
            _, _, ss2 = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertNotIn("DOCS_AUTO_MIGRATE", ss2["injectSteps"][0]["systemMessage"]["systemMessage"])
            _, _, pre2 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "New task turn 2",
                "invocationNum": 1,
            })
            self.assertNotIn("DOCS_AUTO_MIGRATE", json.dumps(pre2))

    def test_handoff_v1_marker_and_future_schema_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            planner_state.bootstrap_workspace(repo)

            ho = run_script("plan_tool.py", ["--workspace", str(repo), "handoff", "--summary", "fix|a.py", "--next", "test"])
            self.assertEqual(ho.returncode, 0, ho.stdout + ho.stderr)
            ho_txt = (repo / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
            self.assertIn("fmt=1", ho_txt)
            self.assertTrue(planner_state.doc_has_current_format(".agents/docs/HANDOFF.md", ho_txt))

            future_prof = {"schema_version": 99, "docs_format_version": 99, "project_name": "FutureApp"}
            future_raw = json.dumps(future_prof)
            prof_path = repo / ".agents" / "planner_profile.json"
            prof_path.write_text(future_raw, encoding="utf-8")
            up = planner_state.auto_upgrade_workspace(repo)
            self.assertIsNone(up["profile_migrated"])
            self.assertEqual(prof_path.read_text(encoding="utf-8"), future_raw)

    def test_verify_universal_dedup_plugin_tests_and_git_stat(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # 1. should_run_plugin_tests skips redundant re-run when tests already passed since last plugin edit
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            hook_file.write_text(json.dumps({
                "needs_plugin_tests": False,
                "last_plugin_edit_ts": now - 10,
                "last_plugin_test_ts": now - 1,
                "needs_build": True,
                "needs_verify": True,
                "file_edit_counts": {"app.py": 3},
                "file_edit_last_inv": {"app.py": "1:2"},
            }), encoding="utf-8")
            with _patched_env({"PLANNER_ARTIFACT_DIR": art, "CAVERN_ARTIFACT_DIR": art}):
                self.assertFalse(verify_universal.should_run_plugin_tests(PLUGIN_ROOT, PLUGIN_ROOT, ["README.md"], False))
                self.assertTrue(verify_universal.should_run_plugin_tests(PLUGIN_ROOT, PLUGIN_ROOT, ["README.md"], True))

            hook_file.write_text(json.dumps({
                "needs_plugin_tests": True,
                "last_plugin_edit_ts": now,
                "last_plugin_test_ts": now - 10,
            }), encoding="utf-8")
            with _patched_env({"PLANNER_ARTIFACT_DIR": art, "CAVERN_ARTIFACT_DIR": art}):
                self.assertTrue(verify_universal.should_run_plugin_tests(repo, PLUGIN_ROOT, [], False))

            # 2. Chained `pytest && python3 verify_universal.py` clears BOTH needs_verify AND needs_build
            hook_file.write_text(json.dumps({
                "needs_build": True,
                "needs_verify": True,
                "file_edit_counts": {"app.py": 3},
                "file_edit_last_inv": {"app.py": "1:2"},
            }), encoding="utf-8")
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "exitCode": 0,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest -q && python3 scripts/verify_universal.py"}},
            })
            hs_after = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertFalse(hs_after.get("needs_verify"))
            self.assertFalse(hs_after.get("needs_build"))
            self.assertEqual(hs_after.get("file_edit_counts"), {})

            # 3. verify_universal.py --git-stat appends git status -s and git diff --stat
            (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            res_gs = run_script(
                "verify_universal.py",
                ["--workspace", str(repo), "--git-stat"],
                cwd=repo,
                env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1", "PLANNER_ARTIFACT_DIR": art, "CAVERN_ARTIFACT_DIR": art},
            )
            self.assertEqual(res_gs.returncode, 0, res_gs.stdout + res_gs.stderr)
            self.assertIn("UNIVERSAL VERIFY PASS", res_gs.stdout)
            self.assertIn("M app.py", res_gs.stdout)
            self.assertIn("1 file changed", res_gs.stdout)

    def test_plugin_edits_not_double_counted_in_blast_radius(self) -> None:
        with tempfile.TemporaryDirectory() as art:
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(PLUGIN_ROOT)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Fix hook bug",
                "invocationNum": 1,
            })
            for rel in ("hooks/planner_stop.py", "hooks/planner_state.py", "scripts/plan_tool.py"):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(PLUGIN_ROOT)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(PLUGIN_ROOT / rel)}},
                })
            hs = json.loads((Path(art) / "scratch" / ".hook_state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(hs.get("plugin_files_edited") or []), 3)
            self.assertEqual(hs.get("code_files_edited") or [], [])

    def test_root_agents_md_auto_migration_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            planner_state.bootstrap_workspace(repo)
            # Simulate an outdated project that also has verbose root AGENTS.md
            prof_file = repo / ".agents" / "planner_profile.json"
            prof = json.loads(prof_file.read_text(encoding="utf-8"))
            prof["docs_format_version"] = 0
            prof_file.write_text(json.dumps(prof), encoding="utf-8")

            root_agents = repo / "AGENTS.md"
            verbose_agents = "# Project Rules\n\nPlease make sure to always run npm test before pushing.\n"
            root_agents.write_text(verbose_agents, encoding="utf-8")

            managed = planner_state.get_managed_project_docs(repo, prof)
            self.assertIn("AGENTS.md", managed)

            rc, err, ss = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("DOCS_AUTO_MIGRATE|v0->v1", ss_msg)
            self.assertIn("AGENTS.md", ss_msg)
            # Never overwritten by Python
            self.assertEqual(root_agents.read_text(encoding="utf-8"), verbose_agents)

            # Opt-out via docs_migration.include_root_agent_docs: false excludes root AGENTS.md
            prof["docs_migration"] = {"include_root_agent_docs": False}
            prof_file.write_text(json.dumps(prof), encoding="utf-8")
            self.assertNotIn("AGENTS.md", planner_state.get_managed_project_docs(repo, prof))

            # Re-enable, convert AGENTS.md, and stamp docs-migrated
            del prof["docs_migration"]
            prof_file.write_text(json.dumps(prof), encoding="utf-8")
            root_agents.write_text(
                "# Rules\n> STYLE|lossless symbolic telegraphic EN ONLY\n- Pre-push -> `npm test`.\n",
                encoding="utf-8",
            )
            dm = run_script("plan_tool.py", ["--workspace", str(repo), "docs-migrated"])
            self.assertEqual(dm.returncode, 0, dm.stdout + dm.stderr)
            self.assertEqual(json.loads(prof_file.read_text(encoding="utf-8"))["docs_format_version"], 1)

    def test_unstructured_prose_skips_auto_reqs_echo(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            prose = (
                "Eu percebi que tem um problema no sistema. "
                "Precisa melhorar o cache da api no servidor e também corrigir o fluxo de build e testes antes de publicar a nova versão."
            )
            _, _, pre = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prose,
                "invocationNum": 1,
            })
            msg = pre["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[Triage|", msg)
            self.assertIn("JIT|", msg)
            self.assertNotIn("REQS|", msg)


class TestSelfDiagnostics(unittest.TestCase):
    def test_diagnostics_isolation_redaction_and_ring_buffer(self) -> None:
        # 1. Isolated during tests without PLANNER_DIAGNOSTICS_DIR
        with _patched_env({"PLANNER_DISABLE_TELEMETRY": "1", "PLANNER_DIAGNOSTICS_DIR": ""}):
            self.assertIsNone(plugin_diagnostics.resolve_diagnostics_file())
            self.assertFalse(plugin_diagnostics.record_anomaly("hook_slow", "test.py", "sig", "detail"))

        # 2. Secret redaction + ring-buffer trimming
        with tempfile.TemporaryDirectory() as diag_dir:
            fake_ghp = "ghp_" + "A1b2C3d4E5" * 3
            fake_sk = "sk-" + "proj-" + "AbCdEf123456789012"
            fake_bearer = "Bearer " + "tokSecretVal123456"
            with _patched_env({"PLANNER_DIAGNOSTICS_DIR": diag_dir}):
                ok = plugin_diagnostics.record_anomaly(
                    "hook_exception",
                    "test_hook.py",
                    "ValueError:test",
                    f"bad {fake_ghp} and {fake_sk} auth={fake_bearer}",
                )
                self.assertTrue(ok)
                diag_file = Path(diag_dir) / "anomalies.jsonl"
                txt = diag_file.read_text(encoding="utf-8")
                self.assertNotIn(fake_ghp, txt)
                self.assertNotIn(fake_sk, txt)
                self.assertNotIn("tokSecretVal123456", txt)
                self.assertIn("<REDACTED>", txt)

                # Write >240 bulky entries to exceed TRIM_THRESHOLD_BYTES and cap at <= 200 lines
                pad = "X" * 190
                for idx in range(250):
                    plugin_diagnostics.record_anomaly("hook_slow", "test.py", f"s:{idx}", f"{idx}:{pad}")
                lines = [ln for ln in diag_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
                self.assertLessEqual(len(lines), plugin_diagnostics.MAX_ANOMALY_LINES)

    def test_diagnostics_detectors_and_cli_report(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art, tempfile.TemporaryDirectory() as diag_dir:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            planner_state.bootstrap_workspace(repo)
            env_diag = {"PLANNER_DIAGNOSTICS_DIR": diag_dir}

            # 1. record_hook_exception captures exception type + frame
            with _patched_env(env_diag):
                try:
                    raise RuntimeError("boom_diag_test")
                except RuntimeError as exc:
                    self.assertTrue(plugin_diagnostics.record_hook_exception("fake_hook.py", exc, repo=repo))

            # 2. stop_gate_loop: 2 consecutive identical Stop blocks record loop; passing Stop resets streak
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hook_file.parent.mkdir(parents=True, exist_ok=True)
            hook_file.write_text(json.dumps({"needs_build": True, "code_files_edited": ["app.py"]}), encoding="utf-8")
            for _ in range(2):
                _, _, st = run_hook("planner_stop.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "fullyIdle": True,
                    "terminationReason": "NO_TOOL_CALL",
                }, env_extra=env_diag)
                self.assertEqual(st.get("decision"), "continue")

            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertEqual(hs.get("stop_block_streak"), 2)
            hs["needs_build"] = False
            hook_file.write_text(json.dumps(hs), encoding="utf-8")
            _, _, st_ok = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            }, env_extra=env_diag)
            self.assertEqual(st_ok, {})
            self.assertEqual(json.loads(hook_file.read_text(encoding="utf-8")).get("stop_block_streak"), 0)

            # 3. guard_deny_burst: 2 consecutive read-budget denies in same turn
            big_py = repo / "big.py"
            big_py.write_text("\n".join(f"x_{i} = {i}" for i in range(340)), encoding="utf-8")
            for _ in range(2):
                _, _, rd = run_hook("planner_guard_read.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "read_file", "args": {"Path": str(big_py)}},
                }, env_extra=env_diag)
                self.assertEqual(rd.get("decision"), "deny")

            # 4. circuit_breaker_suspect & injection_bloat in PreInvocation
            prompt = "1. Fix auth\n2. Add pytest tests\n3. Update API"
            hook_file.write_text(json.dumps({
                "turn_id": 1,
                "total_turns": 1,
                "turn_sig": f"1:{planner_state.prompt_hash(prompt)}",
                "prompt_hash": planner_state.prompt_hash(prompt),
                "turn_started_ts": time.time(),
                "last_activity_ts": time.time(),
                "last_invocation_num": 1,
                "triage": {"tier": 2, "tier_name": "META_MODULES", "detected_domains": ["api-backend", "tests-qa"]},
                "file_edit_counts": {"app.py": 4},
                "same_cmd_fails": 0,
                "last_build_failed": False,
            }), encoding="utf-8")
            env_bloat = {"PLANNER_DIAGNOSTICS_DIR": diag_dir, "PLANNER_INJECTION_BLOAT_CHARS": "50"}
            _, _, pre = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": prompt,
                "invocationNum": 2,
            }, env_extra=env_bloat)
            self.assertIn("CIRCUIT BREAKER", json.dumps(pre))

            # 5. CLI `plan_tool.py diag`, filtering, SessionStart scoping & `--clear`
            diag_out = run_script("plan_tool.py", ["diag"], env_extra=env_diag)
            self.assertEqual(diag_out.returncode, 0, diag_out.stdout + diag_out.stderr)
            for expected in ("hook_exception", "stop_gate_loop", "guard_deny_burst", "circuit_breaker_suspect", "injection_bloat"):
                self.assertIn(expected, diag_out.stdout)

            filt_out = run_script("plan_tool.py", ["diag", "--kind", "stop_gate_loop"], env_extra=env_diag)
            self.assertEqual(filt_out.returncode, 0)
            self.assertIn("stop_gate_loop", filt_out.stdout)
            self.assertNotIn("guard_deny_burst", filt_out.stdout)

            # User repo SessionStart MUST NOT inject DIAGNOSTICS|, PLUGIN_ROOT SessionStart MUST
            _, _, ss_user = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            }, env_extra=env_diag)
            self.assertNotIn("DIAGNOSTICS|", json.dumps(ss_user))

            _, _, ss_plugin = run_hook("planner_session_start.py", {
                "workspacePaths": [str(PLUGIN_ROOT)],
                "artifactDirectoryPath": art,
            }, env_extra=env_diag)
            self.assertIn("DIAGNOSTICS|", ss_plugin["injectSteps"][0]["systemMessage"]["systemMessage"])

            clr = run_script("plan_tool.py", ["diag", "--clear"], env_extra=env_diag)
            self.assertEqual(clr.returncode, 0)
            self.assertIn("DIAGNOSTICS_CLEARED|", clr.stdout)
            empty_out = run_script("plan_tool.py", ["diag"], env_extra=env_diag)
            self.assertIn("DIAGNOSTICS|total=0|clean", empty_out.stdout)


class TestAdaptiveTriageAndContinuity(unittest.TestCase):
    def test_single_prompt_adaptive_triage(self) -> None:
        # 1. Explicit file refs >=4 -> T>=2; URLs + framework tokens (Next.js/Vue.js) ignored
        r_files = complexity_triage.analyze_prompt(
            "Adjust hooks/a.py, hooks/b.py, scripts/c.py and tests/d.py after checking https://example.com/foo.js and Next.js / Vue.js"
        )
        self.assertGreaterEqual(r_files["tier"], 2)
        self.assertEqual(len(r_files["file_refs"]), 4)

        # 2. Broad cross-cutting scope disqualifies tiny_fix -> T>=2
        r_broad = complexity_triage.analyze_prompt("Fix typo across all files")
        self.assertNotEqual(r_broad["intent"], "tiny_fix")
        self.assertGreaterEqual(r_broad["tier"], 2)

        # 3. Verb-chained "e/and" splits into >=2 clauses vs noun pair stays 1 clause
        r_verb = complexity_triage.analyze_prompt("corrige o auth e adiciona testes")
        r_noun = complexity_triage.analyze_prompt("atualiza hooks e scripts")
        self.assertGreaterEqual(r_verb["breadth_est"], 2)
        self.assertEqual(r_noun["breadth_est"], 1)

        # 4. Greeting + question -> Tier 0 pure_question
        r_greet_q = complexity_triage.analyze_prompt("Oi, tudo bem? Como funciona o plugin?")
        self.assertEqual(r_greet_q["tier"], 0)
        self.assertEqual(r_greet_q["intent"], "pure_question")

        # 5. Read-only catch-up capped vs explicit audit/plan proposal preserved
        r_ro = complexity_triage.analyze_prompt(
            "Só me situa como está o sistema de auth e hooks, ainda não faz nada nem altera arquivo"
        )
        self.assertEqual(r_ro["intent"], "read_only_analysis")
        self.assertLessEqual(r_ro["tier"], 1)

        r_plan = complexity_triage.analyze_prompt(
            "Não faça merge ainda, mas faça uma auditoria e um plano de arquitetura para migrar o backend e testes"
        )
        self.assertEqual(r_plan["intent"], "plan_proposal")
        self.assertGreaterEqual(r_plan["tier"], 2)

        # 6. Long pasted quote (>120 chars) stripped from imperative scoring
        big_quote = '"' + ("Tier 3 refactor architecture pipeline migration across 5 files and rewrite everything. " * 2) + '"'
        r_quoted_q = complexity_triage.analyze_prompt(
            f"Valeu! Só me explica o que significa esse trecho {big_quote}?"
        )
        self.assertEqual(r_quoted_q["tier"], 0)
        self.assertEqual(r_quoted_q["intent"], "pure_question")

        # 7. unique_edited_files deduplicates plugin files listed in both code & plugin sets
        deduped = planner_state.unique_edited_files({
            "code_files_edited": ["hooks/a.py", "src/app.py"],
            "plugin_files_edited": ["hooks/a.py"],
        })
        self.assertEqual(deduped, ["src/app.py", "plugin:hooks/a.py"])

    def test_multiturn_task_context_continuity(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art, tempfile.TemporaryDirectory() as diag_dir:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "src").mkdir()
            env_diag = {"PLANNER_DIAGNOSTICS_DIR": diag_dir, "PLANNER_SLOW_HOOK_MS": "10000"}
            hook_file = Path(art) / "scratch" / ".hook_state.json"

            # Turn 1: Large Tier 3 proposal (0 edits)
            t1_prompt = "Audita e refatora toda a arquitetura de pipeline e hooks do backend e testes antes do deploy"
            _, _, pre1 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": t1_prompt,
                "invocationNum": 1,
            }, env_extra=env_diag)
            self.assertIn("[Triage|T3", pre1["injectSteps"][0]["systemMessage"]["systemMessage"])

            # Turn 2: Approval follow-up inherits T3 + domains + avoids Blast Radius / triage_mismatch on 6 edits
            t2_prompt = "Muito bom, pode fazer tudo isso que você propôs!"
            _, _, pre2 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": t2_prompt,
                "invocationNum": 1,
            }, env_extra=env_diag)
            msg2 = pre2["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[Triage|T3(ctx)|", msg2)
            hs2 = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertEqual(hs2["triage"]["source"], "context_approval")

            for idx in range(6):
                fp = repo / "src" / f"m{idx}.py"
                fp.write_text("x = 1\n", encoding="utf-8")
                _, _, dec = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": t2_prompt,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp), "Target": "x = 1", "Replacement": "x = 2"}},
                }, env_extra=env_diag)
                self.assertEqual(dec.get("decision"), "allow")
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp)}},
                }, env_extra=env_diag)

            _, _, pre2_next = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": t2_prompt,
                "invocationNum": 2,
            }, env_extra=env_diag)
            self.assertNotIn("BLAST RADIUS", json.dumps(pre2_next))
            _, _, stop2 = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            }, env_extra=env_diag)
            self.assertEqual(stop2, {})
            diag_file = Path(diag_dir) / "anomalies.jsonl"
            self.assertFalse(diag_file.is_file() and diag_file.read_text(encoding="utf-8").strip())

            # Intervening Q&A turns preserve proposed T3 context across 3 turns
            with tempfile.TemporaryDirectory() as art_qa:
                for p in (
                    t1_prompt,
                    "O que é a opção 1?",
                    "Qual arquivo muda?",
                ):
                    run_hook("planner_pre_invocation.py", {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_qa,
                        "lastUserInput": p,
                        "invocationNum": 1,
                    }, env_extra=env_diag)
                _, _, pre_t4 = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art_qa,
                    "lastUserInput": "Perfeito, pode seguir com tudo isso!",
                    "invocationNum": 1,
                }, env_extra=env_diag)
                self.assertIn("[Triage|T3(ctx)|", pre_t4["injectSteps"][0]["systemMessage"]["systemMessage"])

            # Exclusions & in_progress continuation rules (pure analyze_prompt + context state)
            now = 1_700_000_000.0
            ctx_proposed = {
                "turn_id": 1,
                "tier": 3,
                "depth_level": "high",
                "depth_signals": ["arquitetura"],
                "detected_domains": ["api-backend"],
                "doc_lookup_targets": ["context7"],
                "file_refs": [],
                "status": "proposed",
                "build_failed": False,
                "ts": now - 60.0,
                "ttl_secs": 1800.0,
            }
            self.assertEqual(complexity_triage.analyze_prompt("Valeu, obrigado!", repo, context=ctx_proposed, now=now)["tier"], 0)
            self.assertEqual(complexity_triage.analyze_prompt("Fix typo in helper.py", repo, context=ctx_proposed, now=now)["tier"], 0)
            expired = dict(ctx_proposed, ts=now - 2000.0)
            self.assertEqual(complexity_triage.analyze_prompt("Sim, pode fazer tudo isso", repo, context=expired, now=now)["tier"], 0)

            ctx_prog = dict(ctx_proposed, status="in_progress")
            self.assertEqual(complexity_triage.analyze_prompt("ok", repo, context=ctx_prog, now=now)["tier"], 0)
            r_cont = complexity_triage.analyze_prompt("continua a parte 2", repo, context=ctx_prog, now=now)
            self.assertEqual(r_cont["tier"], 3)
            self.assertEqual(r_cont["source"], "context_continuation")

            # Post-commit clears carried task_context
            cleared = planner_state.build_carried_task_context(
                {"turn_id": 2, "triage": {"tier": 3}, "code_files_edited": ["a.py"], "post_commit_nudge": True},
                {},
                now,
            )
            self.assertEqual(cleared, {})

    def test_exit_plan_mode_live_plan_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art, tempfile.TemporaryDirectory() as diag_dir:
            repo = Path(td)
            init_git_repo(repo)
            (repo / "src").mkdir()
            env_diag = {"PLANNER_DIAGNOSTICS_DIR": diag_dir}
            vague = "arruma aí"

            # 1. Vague Turn 1 + 5-file exit_plan_mode(plan=...) upgrades to T3(plan) on handoff
            _, _, pre1 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-pro-orchestrator-neon",
                "lastUserInput": vague,
                "invocationNum": 1,
            }, env_extra=env_diag)
            self.assertEqual(pre1, {})

            plan_5 = (
                "1. Fix auth in src/a.py\n"
                "2. Update API in src/b.py\n"
                "3. Refactor core in src/c.py\n"
                "4. Add pytest tests in tests/d.py\n"
                "5. Update CI in scripts/e.py"
            )
            _, _, pre_exit = run_hook("planner_post_tool.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-pro-orchestrator-neon",
                "lastUserInput": vague,
                "toolCall": {"name": "exit_plan_mode", "args": {"plan": plan_5}},
            }, env_extra=env_diag, extra_args=["--pre"])
            self.assertEqual(pre_exit.get("decision"), "allow")

            # Neon handoff re-invokes with invocationNum=1 and same prompt -> injects T3(plan) + JIT
            _, _, pre_exec = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "modelName": "gemini-4-hydrogen",
                "lastUserInput": vague,
                "invocationNum": 1,
            }, env_extra=env_diag)
            exec_msg = pre_exec["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[Triage|T3(plan)|", exec_msg)
            self.assertIn("JIT|", exec_msg)

            for idx in range(5):
                fp = repo / "src" / f"p{idx}.py"
                fp.write_text("x = 1\n", encoding="utf-8")
                run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": vague,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp), "Target": "x = 1", "Replacement": "x = 2"}},
                }, env_extra=env_diag)
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(fp)}},
                }, env_extra=env_diag)

            _, _, pre_after = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": vague,
                "invocationNum": 2,
            }, env_extra=env_diag)
            self.assertNotIn("BLAST RADIUS", json.dumps(pre_after))
            _, _, stop_ok = run_hook("planner_stop.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            }, env_extra=env_diag)
            self.assertEqual(stop_ok, {})
            diag_file = Path(diag_dir) / "anomalies.jsonl"
            self.assertFalse(diag_file.is_file() and diag_file.read_text(encoding="utf-8").strip())

            # 2. Vague Turn 1 + 1-file plan stays T1 and STILL warns Blast Radius on 4th edit
            with tempfile.TemporaryDirectory() as art_small:
                run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art_small,
                    "modelName": "gemini-pro-orchestrator-neon",
                    "lastUserInput": vague,
                    "invocationNum": 1,
                }, env_extra=env_diag)
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art_small,
                    "modelName": "gemini-pro-orchestrator-neon",
                    "lastUserInput": vague,
                    "toolCall": {"name": "exit_plan_mode", "args": {"plan": "1. Fix typo in src/only.py"}},
                }, env_extra=env_diag, extra_args=["--pre"])
                for idx in range(4):
                    fp = repo / "src" / f"s{idx}.py"
                    fp.write_text("x = 1\n", encoding="utf-8")
                    run_hook("planner_guard_files.py", {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_small,
                        "lastUserInput": vague,
                        "toolCall": {"name": "edit_file", "args": {"Path": str(fp), "Target": "x = 1", "Replacement": "x = 2"}},
                    }, env_extra=env_diag)
                    run_hook("planner_post_tool.py", {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_small,
                        "toolCall": {"name": "edit_file", "args": {"Path": str(fp)}},
                    }, env_extra=env_diag)
                _, _, pre_warn = run_hook("planner_pre_invocation.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art_small,
                    "lastUserInput": vague,
                    "invocationNum": 2,
                }, env_extra=env_diag)
                self.assertIn("BLAST RADIUS", json.dumps(pre_warn))


class TestV25EvolutionMechanics(unittest.TestCase):
    def test_m1_wiring_gate_orphan_apis_branches_settings_and_refs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir()
            (repo / "Lib").mkdir()
            (repo / "Ui").mkdir()
            (repo / "Lang").mkdir()
            (repo / "Docs").mkdir()
            (repo / "Lang" / "Strings.cs").write_text(
                'class Strings {\n  static Dictionary<string, string> En = new() {\n    ["Hello"] = "Hello",\n  };\n}\n',
                encoding="utf-8",
            )
            (repo / "Lib" / "Settings.cs").write_text(
                "public class Settings {\n  public bool OldFlag { get; set; }\n}\n",
                encoding="utf-8",
            )
            (repo / "Lib" / "Core.cs").write_text("public class Core { }\n", encoding="utf-8")
            (repo / "Ui" / "Screen.cs").write_text("class Screen { }\n", encoding="utf-8")
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "wiring_gate": {
                            "enabled": True,
                            "severity": "fail",
                            "library_prefixes": ["Lib/"],
                            "settings_files": ["Lib/Settings.cs"],
                        },
                        "key_parity_rules": [
                            {
                                "path": "Lang/Strings.cs",
                                "added_key_regex": '^\\s*\\["([^"]+)"\\]\\s*=',
                                "source_key_regex": '\\["([^"]+)"\\]\\s*=',
                                "target_section_regex": "En = new\\(\\) \\{(.*?)\\};",
                                "ref_regex": 'S\\["([^"]+)"\\]',
                                "ref_path_prefixes": ["Ui/"],
                                "ref_severity": "fail",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # Orphan APIs (C#/TS/Py), .md-only mention, identical if/else (brace + Py), unwired setting, missing ref key
            (repo / "Lib" / "Core.cs").write_text(
                "public class Core {\n"
                "  public override string ToString() => \"Core\";\n"
                "  public void OrphanMethod(int x) {\n"
                "    if (x > 0) { DoOne(x); } else { DoOne(x); }\n"
                "  }\n"
                "  public string BranchOk(bool f) {\n"
                '    if (f) { return "A"; } else { return "B"; }\n'
                "  }\n"
                "  private void DoOne(int x) { }\n"
                "}\n",
                encoding="utf-8",
            )
            (repo / "Lib" / "mod.ts").write_text("export function orphanExportFn(v: number) { return v; }\n", encoding="utf-8")
            (repo / "Lib" / "algo.py").write_text(
                "def orphan_py_api(x):\n"
                "    if x:\n"
                "        return x + 1\n"
                "    else:\n"
                "        return x + 1\n",
                encoding="utf-8",
            )
            (repo / "Lib" / "Settings.cs").write_text(
                "public class Settings {\n  public bool OldFlag { get; set; }\n  public bool UnwiredToggle { get; set; }\n}\n",
                encoding="utf-8",
            )
            (repo / "Docs" / "notes.md").write_text(
                "Mentions OrphanMethod and orphanExportFn and orphan_py_api and UnwiredToggle.\n",
                encoding="utf-8",
            )
            (repo / "Ui" / "Screen.cs").write_text(
                'class Screen {\n  void Render(dynamic S, Core c, string dyn) {\n    var ok = c.BranchOk(true);\n    var a = S["Hello"];\n    var b = S["MissingUiKey"];\n    var c2 = S[$"Key_{dyn}"];\n    var commentOnly = "if (a) { b(); } else { b(); }";\n  }\n}\n',
                encoding="utf-8",
            )

            res_fail = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertNotEqual(res_fail.returncode, 0, res_fail.stdout)
            self.assertIn("new public symbol `OrphanMethod`", res_fail.stdout)
            self.assertIn("new public symbol `orphanExportFn`", res_fail.stdout)
            self.assertIn("new public symbol `orphan_py_api`", res_fail.stdout)
            self.assertNotIn("BranchOk", res_fail.stdout)
            self.assertNotIn("ToString", res_fail.stdout)
            self.assertIn("Lib/Core.cs:4: identical if/else branches", res_fail.stdout)
            self.assertIn("Lib/algo.py:2: identical if/else branches", res_fail.stdout)
            self.assertNotIn("Ui/Screen.cs", [ln for ln in res_fail.stdout.splitlines() if "identical if/else" in ln])
            self.assertIn("setting/config key `UnwiredToggle`", res_fail.stdout)
            self.assertIn("MissingUiKey", res_fail.stdout)
            self.assertNotIn("Key_{dyn}", res_fail.stdout)

            # Test warn severity does not fail exit code
            prof = json.loads((repo / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            prof["wiring_gate"]["severity"] = "warn"
            prof["key_parity_rules"][0]["ref_severity"] = "warn"
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(prof), encoding="utf-8")
            res_warn = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertEqual(res_warn.returncode, 0, res_warn.stdout)
            self.assertIn("WARNINGS:", res_warn.stdout)
            self.assertIn("OrphanMethod", res_warn.stdout)

            # Wire callers, fix identical branches, fix UI key -> clean pass
            prof["wiring_gate"]["severity"] = "fail"
            prof["key_parity_rules"][0]["ref_severity"] = "fail"
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(prof), encoding="utf-8")
            (repo / "Lib" / "Core.cs").write_text(
                "public class Core {\n"
                "  public void OrphanMethod(int x) {\n"
                "    if (x > 0) { DoOne(x); } else { DoTwo(x); }\n"
                "  }\n"
                "  public string BranchOk(bool f) {\n"
                '    if (f) { return "A"; } else { return "B"; }\n'
                "  }\n"
                "  private void DoOne(int x) { }\n"
                "  private void DoTwo(int x) { }\n"
                "}\n",
                encoding="utf-8",
            )
            (repo / "Lib" / "algo.py").write_text(
                "def orphan_py_api(x):\n    if x:\n        return x + 1\n    else:\n        return x - 1\n",
                encoding="utf-8",
            )
            (repo / "Ui" / "Screen.cs").write_text(
                'class Screen {\n  void Render(dynamic S, Core c, Settings st) {\n    var ok = c.BranchOk(true);\n    var a = S["Hello"];\n    if (st.UnwiredToggle) { c.OrphanMethod(1); }\n  }\n}\n',
                encoding="utf-8",
            )
            (repo / "Ui" / "consumer.ts").write_text("import { orphanExportFn } from '../Lib/mod';\norphanExportFn(1);\n", encoding="utf-8")
            (repo / "Ui" / "caller.py").write_text("from Lib.algo import orphan_py_api\norphan_py_api(2)\n", encoding="utf-8")
            res_pass = run_script("verify_universal.py", ["--workspace", str(repo)], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertEqual(res_pass.returncode, 0, res_pass.stdout + res_pass.stderr)
            self.assertNotIn("Wiring-gate", res_pass.stdout)

    def test_m2_recon_sweep_cli_guard_and_turn_continuity(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / "src").mkdir()
            (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
            (repo / ".agents" / "docs" / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 3, "code_path_prefixes": ["src/"], "code_extensions": [".py"]}),
                encoding="utf-8",
            )

            r_start = run_script(
                "plan_tool.py",
                ["--dir", art, "--workspace", str(repo), "recon", "start", "--scope", "Slot 3+5"],
                cwd=repo,
            )
            self.assertEqual(r_start.returncode, 0, r_start.stderr)
            self.assertIn("RECON_START|active=true|scope=Slot 3+5", r_start.stdout)
            self.assertIn("[BUG] | [UNWIRED] | [STRUCTURAL] | [UPGRADE]", r_start.stdout)

            # Code edit denied while recon active
            _, _, fg_code = run_hook(
                "planner_guard_files.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "app.py"), "Target": "x = 1", "Replacement": "x = 2"}},
                },
            )
            self.assertEqual(fg_code.get("decision"), "deny")
            self.assertIn("RECON MODE", fg_code.get("reason", ""))
            self.assertIn("[BUG] | [UNWIRED]", fg_code.get("reason", ""))

            # Docs, roadmaps, and TASK_PLAN.md allowed while recon active
            _, _, fg_doc = run_hook(
                "planner_guard_files.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / ".agents" / "docs" / "ROADMAP.md"), "Target": "# Roadmap", "Replacement": "# Roadmap\n- [BUG] src/app.py:1"}},
                },
            )
            self.assertEqual(fg_doc.get("decision"), "allow")
            _, _, fg_plan = run_hook(
                "planner_guard_files.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "write_file", "args": {"Path": str(Path(art) / "TASK_PLAN.md"), "Content": "# Plan\n"}},
                },
            )
            self.assertEqual(fg_plan.get("decision"), "allow")

            # SessionStart & new-turn PreInvocation carry recon state
            _, _, ss = run_hook("planner_session_start.py", {"workspacePaths": [str(repo)], "artifactDirectoryPath": art})
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("RECON|ACTIVE|scope=Slot 3+5", ss_msg)

            _, _, pre = run_hook(
                "planner_pre_invocation.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": "New turn while recon active",
                    "invocationNum": 1,
                },
            )
            pre_msg = pre["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[RECON|READ-ONLY|scope=Slot 3+5]", pre_msg)

            r_stat = run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "recon", "status"], cwd=repo)
            self.assertIn("RECON_STATUS|active=true|scope=Slot 3+5", r_stat.stdout)

            r_stop = run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "recon", "stop"], cwd=repo)
            self.assertIn("RECON_STOP|active=false", r_stop.stdout)

            _, _, fg_after = run_hook(
                "planner_guard_files.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "src" / "app.py"), "Target": "x = 1", "Replacement": "x = 2"}},
                },
            )
            self.assertEqual(fg_after.get("decision"), "allow")

    def test_m3_wave_handoff_and_archive_read_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs" / "archive").mkdir(parents=True)
            big_lines = ["# Archive", "## Sec 1"] + [f"def old_fn_{i}(): pass" for i in range(180)]
            arch_1 = repo / ".agents" / "docs" / "archive" / "OLD.md"
            arch_2 = repo / ".agents" / "docs" / "PHASE1.md"
            arch_3 = repo / ".agents" / "docs" / "LEGACY_PLAN.md"
            for ap in (arch_1, arch_2, arch_3):
                ap.write_text("\n".join(big_lines) + "\n", encoding="utf-8")

            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 3, "archive_docs": [".agents/docs/LEGACY_PLAN.md"]}),
                encoding="utf-8",
            )

            res = run_script(
                "plan_tool.py",
                [
                    "--workspace", str(repo),
                    "handoff",
                    "--roadmap", ".agents/docs/PHASE2_ROADMAP.md",
                    "--wave", "W21",
                    "--archive", ".agents/docs/PHASE1.md",
                    "--summary", "W20 complete",
                    "--next", "Audit W21",
                ],
                cwd=repo,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            ho_txt = (repo / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
            self.assertLessEqual(len(ho_txt.splitlines()), 20)
            self.assertIn("ROADMAP|.agents/docs/PHASE2_ROADMAP.md", ho_txt)
            self.assertIn("WAVE|W21", ho_txt)
            self.assertIn("ARCHIVE|.agents/docs/PHASE1.md", ho_txt)

            _, _, ss = run_hook("planner_session_start.py", {"workspacePaths": [str(repo)], "artifactDirectoryPath": art})
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("WAVE_BRIDGE|roadmap=.agents/docs/PHASE2_ROADMAP.md|wave=W21|archive=.agents/docs/PHASE1.md", ss_msg)

            # Archive read guard across all 3 mechanisms: archive/ dir, HANDOFF ARCHIVE|, profile.archive_docs
            for ap in (arch_1, arch_2, arch_3):
                _, _, deny_full = run_hook("planner_guard_read.py", {"workspacePaths": [str(repo)], "toolCall": {"name": "read_file", "args": {"Path": str(ap)}}})
                self.assertEqual(deny_full.get("decision"), "deny", ap.name)
                self.assertIn("ARCHIVE DOC", deny_full.get("reason", ""))
                self.assertIn("[SYMBOL_MAP]", deny_full.get("reason", ""))

                _, _, deny_open_end = run_hook("planner_guard_read.py", {"workspacePaths": [str(repo)], "toolCall": {"name": "read_file", "args": {"Path": str(ap), "StartLine": 1}}})
                self.assertEqual(deny_open_end.get("decision"), "deny", ap.name)

                _, _, deny_big_slice = run_hook("planner_guard_read.py", {"workspacePaths": [str(repo)], "toolCall": {"name": "read_file", "args": {"Path": str(ap), "StartLine": 1, "EndLine": 160}}})
                self.assertEqual(deny_big_slice.get("decision"), "deny", ap.name)

                _, _, allow_slice = run_hook("planner_guard_read.py", {"workspacePaths": [str(repo)], "toolCall": {"name": "read_file", "args": {"Path": str(ap), "StartLine": 1, "EndLine": 80}}})
                self.assertEqual(allow_slice.get("decision"), "allow", ap.name)

    def test_m4_build_cascade_shadow_dedup_and_diff_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir()
            (repo / "Layer1").mkdir()
            (repo / "Layer2").mkdir()
            (repo / "App").mkdir()
            (repo / "Layer1" / "A.cs").write_text("class A { }\n", encoding="utf-8")
            (repo / "Layer2" / "B.cs").write_text("class B { }\n", encoding="utf-8")
            (repo / "App" / "C.cs").write_text("class C { }\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # Edit only Layer1; verify continue_on_error=false skips Layer2 and triggers App shadow fallback w/ dedup
            (repo / "Layer1" / "A.cs").write_text("class A { int x = 1; }\n", encoding="utf-8")
            (repo / "Layer2" / "B.cs").write_text("class B { void F(object x) { if (x is not null) {} } }\n", encoding="utf-8")

            profile_data = {
                "schema_version": 3,
                "diff_rules": [
                    {
                        "path_prefixes": ["Layer1/", "Layer2/"],
                        "path_regex": "\\.cs$",
                        "forbid_regex": "\\bis\\s+not\\b",
                        "severity": "fail",
                        "message": "C# 9 is not forbidden across Layer1 and Layer2.",
                    }
                ],
                "build_rules": [
                    {
                        "label": "01_Layer1",
                        "trigger_path_prefixes": ["Layer1/"],
                        "trigger_extensions": [".cs"],
                        "continue_on_error": False,
                        "command": "python3 -c \"import sys; print('Layer1/A.cs(1,1): error CS0103: MissingX [/repo/L1.csproj]'); sys.exit(1)\"",
                    },
                    {
                        "label": "02_Layer2",
                        "trigger_path_prefixes": ["Layer1/", "Layer2/"],
                        "trigger_extensions": [".cs"],
                        "continue_on_error": False,
                        "command": "python3 -c \"print('SHOULD_NOT_RUN')\"",
                    },
                    {
                        "label": "03_AppShadow",
                        "trigger_path_prefixes": ["App/"],
                        "trigger_extensions": [".cs"],
                        "command": "python3 -c \"print('NORMAL_OK')\"",
                        "on_upstream_fail_command": "python3 -c \"import sys; print('Layer1/A.cs(1,1): error CS0103: MissingX [/repo/Shadow.csproj]'); print('App/C.cs(5,2): error CS0246: MissingY [/repo/Shadow.csproj]'); sys.exit(1)\"",
                    },
                ],
            }
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps(profile_data), encoding="utf-8")

            # Shift-left guard & verify_universal both enforce diff_rules.path_prefixes
            _, _, fg_shift = run_hook(
                "planner_guard_files.py",
                {
                    "workspacePaths": [str(repo)],
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "Layer1" / "A.cs"), "Target": "int x = 1;", "Replacement": "if (x is not null) {}"}},
                },
            )
            self.assertEqual(fg_shift.get("decision"), "deny")
            self.assertIn("SHIFT-LEFT BLOCK", fg_shift.get("reason", ""))

            # Clear Layer2 diff_rule violation so --build proceeds to run_build_rules
            (repo / "Layer2" / "B.cs").write_text("class B { }\n", encoding="utf-8")
            res_bld = run_script("verify_universal.py", ["--workspace", str(repo), "--build"], cwd=repo, env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"})
            self.assertNotEqual(res_bld.returncode, 0)
            self.assertNotIn("SHOULD_NOT_RUN", res_bld.stdout)
            self.assertIn("BUILD_SUMMARY|layers=01_Layer1:FAIL(1),02_Layer2:SKIPPED_UPSTREAM,03_AppShadow:SHADOW_FAIL(1)|unique_errors=2|deduped=1", res_bld.stdout)

            # log_digest --errors-only dedup test
            log_p = repo / "build.log"
            log_p.write_text(
                "A.cs(1,1): error CS0103: Foo [/a/L1.csproj]\n"
                "A.cs(1,1): error CS0103: Foo [/a/Shadow.csproj]\n"
                "src/x.ts(2,1): error TS2304: Bar\n",
                encoding="utf-8",
            )
            ld_res = run_script("log_digest.py", [str(log_p), "--errors-only"], cwd=repo)
            self.assertEqual(ld_res.returncode, 0)
            self.assertIn("[LOG_DIGEST_ERRORS|build.log|unique=2|deduped=1]", ld_res.stdout)

    def test_m5_jit_discovery_and_domain_lenses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir()
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "planner_mode": "lean",
                        "domains": {
                            "ui-frontend": {
                                "lens": "Principal UX/UI: Nielsen heuristics, single primary CTA, zero jargon.",
                                "checklist": ["Guard responsive layout."],
                            },
                            "payments": {
                                "module_id": "MOD-PAY",
                                "module_title": "Payments",
                                "keywords": ["stripe", "billing"],
                                "lens": "Audit idempotency && webhook replay safety.",
                                "checklist": ["Verify webhook signature."],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            # T2 -> AUDIT_EXISTING only (no CURATE_OSS), active domain lens only
            t2_prompt = "1. Update React CSS button spacing\n2. Fix frontend layout clipping\n3. Style responsive modal dialog\n4. Adjust sidebar theme colors"
            with tempfile.TemporaryDirectory() as art2:
                _, _, pre2 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art2,
                        "lastUserInput": t2_prompt,
                        "invocationNum": 1,
                    },
                )
                msg2 = pre2["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[Triage|T2", msg2)
                self.assertIn("DISC|AUDIT_EXISTING:", msg2)
                self.assertNotIn("CURATE_OSS", msg2)
                self.assertIn("LENS|ui-frontend:Principal UX/UI", msg2)
                self.assertNotIn("payments:", msg2)

            # T3 -> AUDIT_EXISTING + CURATE_OSS
            with tempfile.TemporaryDirectory() as art3:
                _, _, pre3 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art3,
                        "lastUserInput": "Architect and refactor authentication, API endpoints, database schema, billing stripe webhooks, UI layout and test suite across 6 modules",
                        "invocationNum": 1,
                    },
                )
                msg3 = pre3["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DISC|AUDIT_EXISTING:", msg3)
                self.assertIn("CURATE_OSS:", msg3)
                self.assertIn("LENS|", msg3)

            # ECO_MODE suppresses DISC and LENS
            with tempfile.TemporaryDirectory() as art_eco:
                _, _, pre_eco = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_eco,
                        "lastUserInput": "low quota: Architect and refactor UI layout, API endpoints, database schema and stripe billing",
                        "invocationNum": 1,
                    },
                )
                msg_eco = pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertNotIn("DISC|", msg_eco)
                self.assertNotIn("LENS|", msg_eco)

            # Tight bloat budget suppresses secondary DISC/LENS without bloat anomaly
            with tempfile.TemporaryDirectory() as art_tight:
                _, _, pre_tight = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_tight,
                        "lastUserInput": t2_prompt,
                        "invocationNum": 1,
                    },
                    env_extra={"PLANNER_INJECTION_BLOAT_CHARS": "260"},
                )
                msg_tight = pre_tight["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[Triage|", msg_tight)
                self.assertNotIn("DISC|", msg_tight)
                self.assertNotIn("LENS|", msg_tight)

    def test_m6_btw_queue_intent_and_ultra_terse_hint(self) -> None:
        for p in (
            "manda ele fazer push quando acabar",
            "lembra ele de fazer push quando acabar",
            "quando terminar, atualiza o README",
            "coloca nas requests para testar",
            "tell the main agent to run install.sh when it finishes",
            "/btw queue release",
        ):
            res = complexity_triage.analyze_prompt(p)
            self.assertTrue(res.get("btw_queue_intent"), p)

        self.assertFalse(complexity_triage.detect_btw_queue_intent("Fix layout clipping in Button.tsx"))
        self.assertFalse(planner_state.is_plugin_path(str(PLUGIN_ROOT / ".agents" / "docs" / "EXTRA_REQUESTS.md"))[1])

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir()
            (repo / ".agents" / "planner_profile.json").write_text(json.dumps({"schema_version": 3}), encoding="utf-8")

            # Editing plugin's .agents/docs/EXTRA_REQUESTS.md must NOT flip needs_plugin_tests
            _, _, post_extra = run_hook(
                "planner_post_tool.py",
                {
                    "workspacePaths": [str(PLUGIN_ROOT)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(PLUGIN_ROOT / ".agents" / "docs" / "EXTRA_REQUESTS.md")}},
                },
            )
            self.assertEqual(post_extra, {})
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            if hook_file.is_file():
                self.assertFalse(json.loads(hook_file.read_text(encoding="utf-8")).get("needs_plugin_tests"))

            # Short /btw prompt injects ONLY [BTW_QUEUE] without [Triage|, JIT|, or DOC_LOOKUP|
            _, _, pre_btw = run_hook(
                "planner_pre_invocation.py",
                {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "lastUserInput": "lembra ele de fazer push no React quando acabar",
                    "invocationNum": 1,
                },
            )
            msg_btw = pre_btw["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("[BTW_QUEUE]", msg_btw)
            self.assertIn(".agents/docs/EXTRA_REQUESTS.md", msg_btw)
            self.assertIn("<=2L", msg_btw)
            self.assertNotIn("[Triage|", msg_btw)
            self.assertNotIn("JIT|", msg_btw)
            self.assertNotIn("DOC_LOOKUP|", msg_btw)

            # Stop-gate circuit breaker: blocks streak 1 & 2, fails open at streak 3
            hs_blk = json.loads(hook_file.read_text(encoding="utf-8"))
            hs_blk["needs_build"] = True
            hook_file.write_text(json.dumps(hs_blk), encoding="utf-8")
            stop_payload = {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            }
            _, _, st1 = run_hook("planner_stop.py", stop_payload)
            _, _, st2 = run_hook("planner_stop.py", stop_payload)
            _, _, st3 = run_hook("planner_stop.py", stop_payload)
            self.assertEqual(st1.get("decision"), "continue")
            self.assertEqual(st2.get("decision"), "continue")
            self.assertEqual(st3, {})
            self.assertEqual(json.loads(hook_file.read_text(encoding="utf-8")).get("stop_block_streak"), 0)

            with tempfile.TemporaryDirectory() as art2:
                _, _, pre_norm = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art2,
                        "lastUserInput": "Fix layout clipping in Button.tsx",
                        "invocationNum": 1,
                    },
                )
                msg_norm = json.dumps(pre_norm)
                self.assertNotIn("[BTW_QUEUE]", msg_norm)


class TestV260UniversalEfficiencyAndSelfEvolution(unittest.TestCase):
    def test_universal_error_learner_timeless_and_multistack(self) -> None:
        import error_learner as el

        self.assertEqual(el.extract_code_from_line("src/app.ts(10,3): error TS2322: Type 'number' is not assignable"), "TS2322")
        self.assertEqual(el.extract_code_from_line("error[E0382]: borrow of moved value"), "E0382")
        self.assertTrue(el.lint_timelessness("Always target Android 16 and actions/checkout@v4"))
        self.assertEqual(el.lint_timelessness("Guard API-gated calls with runtime OS version checks"), [])

        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "BUILD_LESSONS.md"
            lp.write_text(
                "# Lessons\n\n### `BL-001` | Starter\n"
                "- `Category`: `build-process`\n- `Codes`: `CS0104`\n- `Keywords`: `ambiguous`\n"
                "- `RootCause`: Ambiguous type reference.\n- `TimelessRule`: Qualify ambiguous types explicitly.\n"
                "- `DynamicLookup`: Inspect symbol declarations in repo.\n- `AutoGuard`: `verify_universal.py`\n\n"
                f"{el.AUTO_START_MARKER}\n{el.EMPTY_AUTO_PLACEHOLDER}\n{el.AUTO_END_MARKER}\n",
                encoding="utf-8",
            )
            ok, errs = el.audit_lessons_file(lp)
            self.assertTrue(ok, errs)
            items = el.load_diagnostics_from_log_text("src/x.ts(1,1): error TS2322: Type 'string' is not assignable to 'number'")
            res = el.analyze_diagnostics(items, el.parse_lessons(lp.read_text(encoding="utf-8")))
            self.assertEqual(len(res.new_candidates), 1)
            changed, _ = el.apply_new_lessons(lp, res.new_candidates)
            self.assertTrue(changed)
            ok2, errs2 = el.audit_lessons_file(lp)
            self.assertTrue(ok2, errs2)

    def test_live_bridge_client_hmac_and_encrypt_then_mac(self) -> None:
        import live_bridge_client as lbc

        raw_secret = "CAVPAIR-" + "a1b2c3d4" * 8
        norm = lbc.normalize_pair_secret_hex(raw_secret)
        self.assertEqual(len(norm), 64)
        h1 = lbc.compute_wake_channel_hash(norm)
        self.assertEqual(len(h1), 64)

        sig = lbc.sign_wake_request(norm, "wake123", 1700000000, 2, 1800, "Inspect DSP")
        self.assertEqual(len(sig), 64)

        payload = {"sessionCode": "CAV-ABCD-EFGH-IJKL-MNOP-QRST-UVWX", "tier": 2, "ttlSeconds": 1800}
        env = lbc.encrypt_approval_payload(norm, "wake123", payload)
        self.assertNotIn("CAV-ABCD", json.dumps(env))
        dec = lbc.decrypt_approval_payload(norm, "wake123", env)
        self.assertEqual(dec, payload)

        tampered = dict(env)
        tampered["ct"] = "AA" + env["ct"][2:]
        with self.assertRaises(ValueError):
            lbc.decrypt_approval_payload(norm, "wake123", tampered)

    def test_self_evolve_classifier_and_dual_sync_ledger(self) -> None:
        import self_evolve as se

        r_uni = se.classify_improvement("Universalize timeless error_learner and live diagnostics fetcher in planner hooks")
        self.assertEqual(r_uni["scope"], "plugin-universal")

        r_proj = se.classify_improvement("Tune HRIR room reflections and OneUI player volume slider in MainActivity.cs")
        self.assertEqual(r_proj["scope"], "project-only")

        r_dual = se.classify_improvement("Upgrade live bridge protocol client in plugin and add Android BiometricPrompt wake service in MainActivity.cs")
        self.assertEqual(r_dual["scope"], "dual-project+plugin")

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            touched = se.record_sync_entry(
                repo,
                scope="dual",
                title="Test Dual Sync",
                summary="Added universal client + host integration",
                files=["scripts/live_bridge_client.py", "Services/LiveBridge.cs"],
            )
            self.assertEqual(len(touched), 2)
            host_txt = (repo / ".agents/docs/EXTERNAL_AI_SYNC.md").read_text(encoding="utf-8")
            plug_txt = (repo / ".agents/plugins/dynamic-fractal-planner/.agents/docs/EXTERNAL_AI_SYNC.md").read_text(encoding="utf-8")
            self.assertIn("Test Dual Sync", host_txt)
            self.assertIn("## Plugin (`dynamic-fractal-planner`) Sync Entries", host_txt)
            self.assertIn("Test Dual Sync", plug_txt)

    def test_verify_universal_audits_build_lessons_timelessness(self) -> None:
        import error_learner as el

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            bad = repo / ".agents" / "docs" / "BUILD_LESSONS.md"
            bad.write_text(
                "# Lessons\n\n### `BL-001` | Bad\n"
                "- `Category`: `build`\n- `Codes`: `CS0001`\n- `Keywords`: `bad`\n"
                "- `RootCause`: Fails on Android 16.\n- `TimelessRule`: Use actions/checkout@v4.\n"
                "- `DynamicLookup`: Check docs.\n- `AutoGuard`: `manual`\n\n"
                f"{el.AUTO_START_MARKER}\n{el.EMPTY_AUTO_PLACEHOLDER}\n{el.AUTO_END_MARKER}\n",
                encoding="utf-8",
            )
            _, fails, _ = verify_universal.run_static_checks(repo, {}, changed=[".agents/docs/BUILD_LESSONS.md"])
            self.assertTrue(any("timelessness audit failed" in f for f in fails), fails)

    def test_verify_universal_csharp_syntax_deep_and_gitless_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)  # intentionally gitless container
            (repo / "src").mkdir()
            cs = repo / "src" / "Strings.cs"
            cs.write_text(
                'class Strings {\n'
                '    static Dictionary<string, string> Create() => new() {\n'
                '        ["ValidInit"] = "ok",\n'
                '    };\n'
                '    static void Apply(Dictionary<string, string> d) {\n'
                '        d["OkStmt"] = "ok";\n'
                '        d["BrokenStmt"] = "oops",\n'
                '    }\n'
                '}\n',
                encoding="utf-8",
            )
            discovered, fails, _ = verify_universal.run_static_checks(repo, {})
            self.assertIn("src/Strings.cs", discovered)
            self.assertTrue(any("src/Strings.cs:7:" in f and "ends with `,`" in f for f in fails), fails)
            self.assertFalse(any("src/Strings.cs:3:" in f for f in fails), fails)

    def test_v280_sota_preflight_lens_budget_and_memory_byte_guard(self) -> None:
        # 1) Tier 3 recommended_modules includes MOD-SOTA-PREFLIGHT right after MOD-DISCOVERY
        mods_t3 = complexity_triage.build_recommended_modules(3, ["backend"], "deep")
        mod_ids_t3 = [m["id"] for m in mods_t3]
        self.assertIn("MOD-SOTA-PREFLIGHT", mod_ids_t3)
        self.assertEqual(mod_ids_t3.index("MOD-SOTA-PREFLIGHT"), mod_ids_t3.index("MOD-DISCOVERY") + 1)
        mods_t2 = complexity_triage.build_recommended_modules(2, ["backend"], "medium")
        self.assertNotIn("MOD-SOTA-PREFLIGHT", [m["id"] for m in mods_t2])

        # 2) PreInvocation injects SOTA_PREFLIGHT| on T3, suppresses on ECO/tight bloat, and preserves >220-char lens
        long_lens = (
            "Principal DSP Architect: challenge prior seed via Tabu recon, run Physics-vs-AI tournament, "
            "split RT CPU vs NPU planes, validate offline golden bench before production code, enforce zero-GC audio thread."
        )
        self.assertGreater(len(long_lens), 200)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "planner_mode": "lean",
                        "domains": {
                            "audio-dsp": {
                                "module_id": "MOD-DSP",
                                "module_title": "Audio DSP",
                                "keywords": ["dsp", "kalman", "audio"],
                                "lens": long_lens,
                                "checklist": ["Enforce zero-GC RT path."],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            t3_prompt = (
                "Architect and refactor audio dsp kalman filter, api endpoints, database schema, "
                "authentication pipeline, ui controls and benchmark suite across 6 modules"
            )
            with tempfile.TemporaryDirectory() as art3:
                _, _, pre3 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art3,
                        "lastUserInput": t3_prompt,
                        "invocationNum": 1,
                    },
                )
                msg3 = pre3["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("SOTA_PREFLIGHT|", msg3)
                self.assertIn("zero-GC audio thread.", msg3)

            with tempfile.TemporaryDirectory() as art_eco:
                _, _, pre_eco = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art_eco,
                        "lastUserInput": "low quota: " + t3_prompt,
                        "invocationNum": 1,
                    },
                )
                msg_eco = pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertNotIn("SOTA_PREFLIGHT|", msg_eco)

            # 3) check_memory_weight warns on byte bloat even when line count <= 150
            (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text(
                "# History\n" + ("- " + ("x" * 1000) + "\n") * 32,
                encoding="utf-8",
            )
            (repo / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md").write_text(
                "# Sync\n" + ("- " + ("y" * 2000) + "\n") * 26,
                encoding="utf-8",
            )
            warns: list[str] = []
            verify_universal.check_memory_weight(repo, {}, warns)
            self.assertTrue(any("PROJECT_HISTORY.md" in w and "KB" in w for w in warns), warns)
            self.assertTrue(any("EXTERNAL_AI_SYNC.md" in w and "KB" in w for w in warns), warns)

    def test_v290_retroactive_upgrade_higher_version_sync_and_code_weight_guard(self) -> None:
        import self_evolve

        # 1) Retroactive auto_upgrade_workspace stamps v4 + plugin_version and blocks downgrades
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "package.json").write_text('{"name":"retro"}\n', encoding="utf-8")
            (repo / ".agents" / "docs").mkdir(parents=True)
            prof_p = repo / ".agents" / "planner_profile.json"
            prof_p.write_text(
                json.dumps({"schema_version": 3, "docs_format_version": 1, "plugin_version": "2.5.1"}),
                encoding="utf-8",
            )
            up = planner_state.auto_upgrade_workspace(repo)
            self.assertEqual(up["profile_migrated"], (3, 5))
            self.assertEqual(up["plugin_upgraded"][0], "2.5.1")
            self.assertEqual(up["plugin_upgraded"][1], planner_state.read_plugin_version())
            disk = json.loads(prof_p.read_text(encoding="utf-8"))
            self.assertEqual(disk["schema_version"], 5)
            self.assertEqual(disk["plugin_version"], planner_state.read_plugin_version())

            # Higher workspace plugin_version must NEVER downgrade
            disk["plugin_version"] = "9.9.9"
            prof_p.write_text(json.dumps(disk), encoding="utf-8")
            up2 = planner_state.auto_upgrade_workspace(repo)
            self.assertIsNone(up2["plugin_upgraded"])
            self.assertEqual(up2["plugin_downgrade_blocked"], ("9.9.9", planner_state.read_plugin_version()))

        # 2) Bidirectional higher-version plugin sync (embedded > global -> backup + runtime preserved; global > embedded -> upgrades repo)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            emb = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
            glob = root / "global_plugin"
            emb.mkdir(parents=True)
            glob.mkdir(parents=True)

            (emb / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "2.9.0"}), encoding="utf-8")
            (emb / "marker.txt").write_text("from-2.9.0", encoding="utf-8")
            (glob / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "2.5.1"}), encoding="utf-8")
            (glob / ".metrics.jsonl").write_text('{"ev":"keep"}\n', encoding="utf-8")

            chk = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=False)
            self.assertEqual(chk["direction"], "embedded->global")
            self.assertFalse(chk["applied"])

            applied = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertTrue(applied["applied"])
            self.assertEqual(json.loads((glob / "plugin.json").read_text(encoding="utf-8"))["version"], "2.9.0")
            self.assertEqual((glob / "marker.txt").read_text(encoding="utf-8"), "from-2.9.0")
            self.assertEqual((glob / ".metrics.jsonl").read_text(encoding="utf-8"), '{"ev":"keep"}\n')
            self.assertTrue((glob / ".last_good_backup" / "plugin.json").is_file())

            # Equal versions -> in-sync no-op
            eq = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertEqual(eq["direction"], "in-sync")
            self.assertFalse(eq["applied"])

            # Global newer than embedded -> propagates global->embedded
            (glob / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "3.0.0"}), encoding="utf-8")
            (glob / "marker.txt").write_text("from-3.0.0", encoding="utf-8")
            rev = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertEqual(rev["direction"], "global->embedded")
            self.assertTrue(rev["applied"])
            self.assertEqual(json.loads((emb / "plugin.json").read_text(encoding="utf-8"))["version"], "3.0.0")
            self.assertEqual((emb / "marker.txt").read_text(encoding="utf-8"), "from-3.0.0")

        # 3) code_weight_limits monolith guard + cross-file key_parity_rules.target_path + gitless --all plugin-sync parity
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir()
            (repo / "src" / "Huge.cs").write_text("// line\n" * 60, encoding="utf-8")
            cw_warns: list[str] = []
            verify_universal.check_code_weight(
                repo,
                ["src/Huge.cs"],
                {"code_weight_limits": {"enabled": True, "warn_lines": 50, "warn_lines_all": 100}},
                cw_warns,
                scan_all=False,
            )
            self.assertTrue(any("src/Huge.cs: 60L (>50L)" in w for w in cw_warns), cw_warns)

            cw_all_warns: list[str] = []
            verify_universal.check_code_weight(
                repo,
                ["src/Huge.cs"],
                {"code_weight_limits": {"enabled": True, "warn_lines": 50, "warn_lines_all": 100}},
                cw_all_warns,
                scan_all=True,
            )
            self.assertEqual(cw_all_warns, [])

            # Cross-file key_parity_rules.target_path
            (repo / "src" / "Strings.cs").write_text(
                'static Dictionary<string, string> CreateEnglish() => new() {\n'
                '    ["Hello"] = "Hello",\n'
                '    ["MissingKey"] = "Missing",\n'
                '};\n',
                encoding="utf-8",
            )
            (repo / "src" / "Strings.PtBr.cs").write_text(
                'static void ApplyPortuguese(Dictionary<string, string> d) {\n'
                '    d["Hello"] = "Olá";\n'
                '}\n',
                encoding="utf-8",
            )
            kp_fails: list[str] = []
            kp_warns: list[str] = []
            verify_universal.check_key_parity_rules(
                repo,
                ["src/Strings.cs"],
                {
                    "key_parity_rules": [
                        {
                            "path": "src/Strings.cs",
                            "target_path": "src/Strings.PtBr.cs",
                            "added_key_regex": r'^\s*\["([^"]+)"\]\s*=',
                            "target_section_regex": r"static\s+void\s+ApplyPortuguese\b(.*)",
                            "target_key_template": 'd["{key}"]',
                            "severity": "fail",
                        }
                    ]
                },
                kp_fails,
                kp_warns,
            )
            self.assertTrue(any("MissingKey" in f for f in kp_fails), kp_fails)
            self.assertFalse(any("Hello" in f for f in kp_fails), kp_fails)

            # Gitless --all plugin-sync parity emits 0 false warnings when EXTERNAL_AI_SYNC.md mentions plugin.json version
            plug = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
            (plug / ".agents" / "docs").mkdir(parents=True)
            (plug / "plugin.json").write_text(json.dumps({"version": "2.9.0"}), encoding="utf-8")
            (plug / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md").write_text("### 2026-11-23 | Plugin v2.9.0\n", encoding="utf-8")
            ps_warns: list[str] = []
            verify_universal.check_plugin_sync_parity(
                repo,
                [".agents/plugins/dynamic-fractal-planner/plugin.json"],
                ps_warns,
                scan_all=True,
            )
            self.assertEqual(ps_warns, [])

    def test_v210_file_budget_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src" / "sub").mkdir(parents=True)
            for i in range(6):
                (repo / "src" / "sub" / f"f{i}.cs").write_text("// ok\n", encoding="utf-8")

            # Below warn -> 0 warnings, 0 failures
            f1: list[str] = []
            w1: list[str] = []
            verify_universal.check_file_budget(
                repo,
                {"file_budget_limits": {"enabled": True, "warn_files": 8, "fail_files": 10, "hard_platform_cap": 999}},
                f1,
                w1,
            )
            self.assertEqual((f1, w1), ([], []))

            # Above warn, below fail -> 1 warning with heaviest dir diagnostic
            f2: list[str] = []
            w2: list[str] = []
            verify_universal.check_file_budget(
                repo,
                {"file_budget_limits": {"enabled": True, "warn_files": 4, "fail_files": 10, "hard_platform_cap": 999}},
                f2,
                w2,
            )
            self.assertEqual(f2, [])
            self.assertTrue(any("6/999" in w and "src/sub=6" in w for w in w2), w2)

            # Above fail -> 1 failure
            f3: list[str] = []
            w3: list[str] = []
            verify_universal.check_file_budget(
                repo,
                {"file_budget_limits": {"enabled": True, "warn_files": 3, "fail_files": 5, "hard_platform_cap": 999}},
                f3,
                w3,
            )
            self.assertTrue(any("6/999" in f for f in f3), f3)
            self.assertEqual(w3, [])

    def test_v211_host_context_matrix_baton_and_auto_classifier(self) -> None:
        import host_context  # noqa: E402

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / ".agents" / "scripts").mkdir(parents=True)
            (repo / ".agents" / "preview").mkdir(parents=True)
            (repo / ".agents" / "docs").mkdir(parents=True)

            # Isolated empty probe paths so host OS (/sdcard, /root/.dotnet) never bleeds into unit checks
            empty_probes = {
                "termux_dir": repo / ".no_termux",
                "sdcard_dir": repo / ".no_sdcard",
                "local_dotnet_bin": repo / ".no_dotnet",
                "gh_hosts_file": repo / ".no_gh",
                "omni_local_config": repo / ".no_omni",
                "runtime_env_bridge": repo / ".no_rt_env",
            }

            # 1. Host detection across all 5 host profiles
            (repo / ".agents" / "preview" / "server.mjs").write_text("// preview\n", encoding="utf-8")
            h_cloud = host_context.detect_host(repo, env={}, probe_paths=empty_probes)
            self.assertEqual(h_cloud["host_id"], "ai-studio-cloud")
            self.assertTrue(h_cloud["is_cloud"])

            h_ci = host_context.detect_host(repo, env={"GITHUB_ACTIONS": "true"}, probe_paths=empty_probes)
            self.assertEqual(h_ci["host_id"], "ci-runner")

            h_ag_local = host_context.detect_host(repo, env={"ANTIGRAVITY_AGENT": "1"}, probe_paths=empty_probes)
            self.assertEqual(h_ag_local["host_id"], "antigravity-local")
            self.assertTrue(h_ag_local["is_antigravity"])

            termux_probes = dict(empty_probes)
            termux_probes["termux_dir"] = repo / "fake_termux"
            termux_probes["termux_dir"].mkdir()
            h_ag_termux = host_context.detect_host(repo, env={"ANTIGRAVITY_AGENT": "1"}, probe_paths=termux_probes)
            self.assertEqual(h_ag_termux["host_id"], "antigravity-termux")

            (repo / ".git").mkdir()
            h_ide = host_context.detect_host(repo, env={}, probe_paths=empty_probes)
            self.assertEqual(h_ide["host_id"], "local-ide")

            # 2. Matrix audit catches unclassified new script & stale exact path
            (repo / ".agents" / "scripts" / "check_ok.py").write_text("# universal\n", encoding="utf-8")
            (repo / ".agents" / "scripts" / "cloud_watcher.py").write_text(
                "# /tmp/.cavern_runtime_env.json watcher\n", encoding="utf-8"
            )
            (repo / ".agents" / "host_matrix.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "strict_coverage": False,
                        "hosts": {
                            "antigravity-termux": {"ignore_cloud_env_alerts": True, "boot_steps": ["host-boot"]},
                            "ai-studio-cloud": {"ignore_cloud_env_alerts": False, "boot_steps": ["recover"]},
                        },
                        "scan_globs": [".agents/scripts/*.py"],
                        "ignore_globs": ["**/test_*.py"],
                        "entries": [
                            {
                                "id": "check-ok",
                                "category": "universal",
                                "paths": [".agents/scripts/check_ok.py", ".agents/scripts/missing_gone.py"],
                                "purpose": "Universal check",
                                "per_host": {"antigravity-termux": {"mode": "active"}},
                            }
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            aud = host_context.audit_host_matrix(repo)
            self.assertFalse(aud["ok"])
            self.assertEqual(aud["stale"], [".agents/scripts/missing_gone.py"])
            self.assertEqual(len(aud["unclassified"]), 1)
            self.assertEqual(aud["unclassified"][0]["path"], ".agents/scripts/cloud_watcher.py")
            self.assertEqual(aud["unclassified"][0]["suggested_category"], "ai-studio-only")

            # verify_universal surfaces unclassified + stale warnings
            vf_fails: list[str] = []
            vf_warns: list[str] = []
            verify_universal.check_host_matrix_coverage(repo, vf_fails, vf_warns)
            self.assertEqual(vf_fails, [])
            self.assertTrue(any("missing_gone.py" in w for w in vf_warns), vf_warns)
            self.assertTrue(any("cloud_watcher.py" in w and "host-classify" in w for w in vf_warns), vf_warns)

            # 3. host-classify --apply registers tool & clears unclassified
            cls_res = run_script(
                "plan_tool.py",
                ["--workspace", str(repo), "host-classify", ".agents/scripts/cloud_watcher.py", "--apply", "--json"],
                cwd=repo,
            )
            self.assertEqual(cls_res.returncode, 0, cls_res.stderr)
            cls_json = json.loads(cls_res.stdout)
            self.assertTrue(cls_json["applied"])
            self.assertEqual(cls_json["category"], "ai-studio-only")
            aud2 = host_context.audit_host_matrix(repo)
            self.assertEqual(aud2["unclassified"], [])

            # 4. handoff auto-stamps host= & preserves ROADMAP| / WAVE| / ARCHIVE|
            handoff_p = repo / ".agents" / "docs" / "HANDOFF.md"
            handoff_p.write_text(
                "# Session Handoff\n"
                "ts=1700000000|branch=master|head=abc1234|dirty=0|host=ai-studio-cloud|fmt=1\n"
                "ROADMAP|.agents/docs/PLAN.md\n"
                "WAVE|W48 [x]\n"
                "ARCHIVE|.agents/docs/archive/{A,B}.md (!read_full)\n",
                encoding="utf-8",
            )
            ho_res = run_script(
                "plan_tool.py",
                ["--workspace", str(repo), "handoff", "--summary", "Shipped host matrix", "--next", "Start W49"],
                cwd=repo,
                env_extra={"PLANNER_HOST_OVERRIDE": "antigravity-termux"},
            )
            self.assertEqual(ho_res.returncode, 0, ho_res.stderr)
            ho_txt = handoff_p.read_text(encoding="utf-8")
            self.assertIn("host=antigravity-termux", ho_txt)
            self.assertIn("ROADMAP|.agents/docs/PLAN.md", ho_txt)
            self.assertIn("WAVE|W48 [x]", ho_txt)
            self.assertIn("ARCHIVE|.agents/docs/archive/{A,B}.md", ho_txt)

            # 5. BATON_SWITCH & BRIDGE_AHEAD detection
            bridge_ho = repo / "LATEST_HANDOFF.md"
            bridge_ho.write_text(
                "# AI Studio — live\n- **UTC**: `2099-01-01T00:00:00Z`\nLive AI Studio edit\n",
                encoding="utf-8",
            )
            now_t = time.time()
            os.utime(handoff_p, (now_t - 120, now_t - 120))
            os.utime(bridge_ho, (now_t, now_t))
            baton = host_context.detect_baton_state(repo, "ai-studio-cloud", bridge_handoff_path=bridge_ho)
            self.assertTrue(baton["switched"])
            self.assertEqual(baton["from_host"], "antigravity-termux")
            self.assertEqual(baton["to_host"], "ai-studio-cloud")
            self.assertTrue(baton["bridge_ahead"])

            ss_lines = host_context.format_session_start_lines(
                repo, env={"PLANNER_HOST_OVERRIDE": "ai-studio-cloud"}, bridge_handoff_path=bridge_ho, probe_paths=empty_probes
            )
            self.assertTrue(any(ln.startswith("HOST|") for ln in ss_lines), ss_lines)
            self.assertTrue(any("BATON_SWITCH|" in ln and "BRIDGE_AHEAD|" in ln for ln in ss_lines), ss_lines)

            # 6. Real Cavern SessionStart char budget (<1800) when host_matrix.json present
            live_cavern = Path("/root/.gemini/antigravity/scratch/Cavern")
            if live_cavern.is_dir() and (live_cavern / ".agents" / "host_matrix.json").is_file():
                rc_ss, err_ss, ss_live = run_hook(
                    "planner_session_start.py",
                    {"workspacePaths": [str(live_cavern)], "artifactDirectoryPath": art},
                )
                self.assertEqual(rc_ss, 0, err_ss)
                msg_live = ss_live["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("HOST|", msg_live)
                self.assertLess(len(msg_live), 1800, f"SessionStart bloat ({len(msg_live)} chars):\n{msg_live}")



class TestDelegationGuardAndPreInvocation(unittest.TestCase):
    def _guard(self, repo: Path, art: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        rc, err, parsed = run_hook(
            "planner_guard_delegation.py",
            {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": tool_name, "args": args},
            },
        )
        self.assertEqual(rc, 0, err)
        return parsed

    def test_v212_delegation_guard_google_only_and_complexity_routing(self) -> None:
        hooks_cfg = json.loads((PLUGIN_ROOT / "hooks.json").read_text(encoding="utf-8"))
        pre_hooks = hooks_cfg["dynamic-fractal-planner"]["PreToolUse"]
        self.assertTrue(
            any(
                h.get("matcher") == "invoke_subagent|swarm|run_workflow"
                and any("planner_guard_delegation.py" in sub.get("command", "") for sub in h.get("hooks", []))
                for h in pre_hooks
            )
        )

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / ".agents").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )

            # 1) Non-Google/OpenAI/Claude models are denied across invoke_subagent, swarm, run_workflow
            for bad in ("claude-3-7-sonnet", "claude-opus-4", "gpt-4o", "openai/o3-mini", "o3", "chatgpt-4o-latest", "anthropic/haiku", "codex-mini"):
                res = self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "Finder", "Prompt": "find files", "Model": bad})
                self.assertEqual(res["decision"], "deny", f"{bad}: {res}")
                self.assertIn("google_only", res["reason"])

                sw = self._guard(repo, art, "swarm", {"action": "add", "name": "w1", "model": bad, "brief": "do work"})
                self.assertEqual(sw["decision"], "deny", bad)
                sw_m = self._guard(repo, art, "swarm", {"action": "model", "name": "w1", "model": bad})
                self.assertEqual(sw_m["decision"], "deny", bad)

            wf_deny = self._guard(
                repo,
                art,
                "run_workflow",
                {"Description": "fanout", "Script": 'agent("audit", model="claude-3-5-sonnet")'},
            )
            self.assertEqual(wf_deny["decision"], "deny")
            self.assertIn("google_only", wf_deny["reason"])
            wf_ok = self._guard(
                repo,
                art,
                "run_workflow",
                {"Description": "fanout", "Script": 'agent("audit", model="gemini-2.5-flash")'},
            )
            self.assertEqual(wf_ok["decision"], "allow")

            # 2) Omitted Model & unannotated inherit are denied; exempt image-generator passes
            omit = self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "Scout", "Prompt": "check files"})
            self.assertEqual(omit["decision"], "deny")
            self.assertIn("explicit_model", omit["reason"])

            inh_bad = self._guard(repo, art, "invoke_subagent", {"TypeName": "self", "Role": "Worker", "Prompt": "edit file", "Model": "inherit"})
            self.assertEqual(inh_bad["decision"], "deny")
            inh_ok = self._guard(repo, art, "invoke_subagent", {"TypeName": "self", "Role": "Worker [L3]", "Prompt": "complex refactor", "Model": "inherit"})
            self.assertEqual(inh_ok["decision"], "allow")

            img_ok = self._guard(repo, art, "invoke_subagent", {"TypeName": "image-generator", "Role": "Icon", "Prompt": "make icon"})
            self.assertEqual(img_ok["decision"], "allow")

            # 3) Trivial research+pro is denied vs [L3]/dsp allow; flash_lite/flash pass
            triv_pro = self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "File Finder", "Prompt": "list files in src", "Model": "pro"})
            self.assertEqual(triv_pro["decision"], "deny")
            dsp_pro = self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "DSP Auditor", "Prompt": "audit kalman dsp math", "Model": "pro"})
            self.assertEqual(dsp_pro["decision"], "allow")
            self.assertEqual(
                self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "Finder", "Prompt": "grep foo", "Model": "flash_lite"})["decision"],
                "allow",
            )
            self.assertEqual(
                self._guard(repo, art, "invoke_subagent", {"TypeName": "self", "Role": "Impl", "Prompt": "implement helper", "Model": "flash"})["decision"],
                "allow",
            )

            # 4) Swarm add without model denies; explicit gemini-2.5-flash & status allow
            self.assertEqual(self._guard(repo, art, "swarm", {"action": "add", "name": "w1", "brief": "work"})["decision"], "deny")
            self.assertEqual(self._guard(repo, art, "swarm", {"action": "add", "name": "w1", "model": "gemini-2.5-flash", "brief": "work"})["decision"], "allow")
            self.assertEqual(self._guard(repo, art, "swarm", {"action": "status"})["decision"], "allow")

            # 5) delegation.enabled: false bypasses all delegation checks
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "delegation": {"enabled": False}}),
                encoding="utf-8",
            )
            self.assertEqual(self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "R", "Prompt": "p"})["decision"], "allow")
            self.assertEqual(
                self._guard(repo, art, "invoke_subagent", {"TypeName": "research", "Role": "R", "Prompt": "p", "Model": "claude-3-5-sonnet"})["decision"],
                "allow",
            )

    def test_v212_pre_invocation_delegate_directive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )
            t2_prompt = "1. Refactor playback state machine\n2. Add batch queue unit tests\n3. Update docs"

            # T2 normal & eco_mode both emit DELEGATE|
            with tempfile.TemporaryDirectory() as art2:
                _, _, pre2 = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art2, "lastUserInput": t2_prompt, "invocationNum": 1},
                )
                msg2 = pre2["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DELEGATE|", msg2)
                self.assertIn("!cmd-echo", msg2)
                self.assertIn("Google-only(L0:flash_lite|L1-2:flash|L3:pro)", msg2)
                self.assertIn("reply<=20L", msg2)

            with tempfile.TemporaryDirectory() as art3:
                _, _, pre3 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art3,
                        "lastUserInput": "Architect audio dsp filter, api schema, auth pipeline, ui tabs and tests across 5 modules",
                        "invocationNum": 1,
                    },
                )
                msg3 = pre3["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DELEGATE|", msg3)
                self.assertIn("SOTA_PREFLIGHT|", msg3)
                self.assertLessEqual(len(msg3), 1500)

            with tempfile.TemporaryDirectory() as art_eco:
                _, _, pre_eco = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art_eco, "lastUserInput": "low quota: " + t2_prompt, "invocationNum": 1},
                )
                msg_eco = pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DELEGATE|", msg_eco)

            # T0/T1 trivial prompt omits DELEGATE|
            with tempfile.TemporaryDirectory() as art0:
                _, _, pre0 = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art0, "lastUserInput": "fix typo in README.md", "invocationNum": 1},
                )
                self.assertEqual(pre0, {})

            # delegation.enabled: false suppresses DELEGATE|
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "delegation": {"enabled": False}}),
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as art_off:
                _, _, pre_off = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art_off, "lastUserInput": t2_prompt, "invocationNum": 1},
                )
                msg_off = pre_off["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertNotIn("DELEGATE|", msg_off)


class TestSotaBlueprintEngine(unittest.TestCase):
    """Tests for v2.13.0 Universal 6-Stage SOTA Blueprint Engine & Popperian Linter."""

    @staticmethod
    def _valid_blueprint_text(status: str = "SEED-v1", h2_override: str | None = None, body_override: dict[str, str] | None = None) -> str:
        sections = {
            "meta": f"BP_META|id=BP_OMNISCIENT_SPATIAL|domain_vec=dsp-hw:1.00,ai-ml:0.85,ux-product:0.45|door=1-way|tier=T3|status={status}|h1_wave=W0",
            "value": "VALUE|Convert stereo songs into beat-locked 3D Atmos/IAMF trajectories achieving >=4.2/5 MOS listener preference.",
            "wardley": "WARDLEY|Custom Trajectory Governor + Lyric Director (Genesis) over existing ONNX stems & ADM/IAMF writers (Product/OSS).",
            "ng1": "NON_GOAL|Zero per-song neural retraining on user phone.",
            "ng2": "NON_GOAL|Never pan Lead Vocals >15 deg or rotate LFE sub-bass.",
            "sota": "SOTA_SOURCES|Audio-driven beat/onset trackers + BS.2076 audioBlockFormat + constrained LLM JSON choreography.",
            "triz": "TRIZ_CONTRADICTION|Rich 3D motion vs vocal clarity/motion-sickness -> Dynamics + Cushion-in-advance (Governor clamps).",
            "c1": "CANDIDATE|A|Pure DSP LFO Orbit|Zero AI, deterministic beat-synchronous panning; misses lyric semantics.",
            "r1": "REJECTED_BECAUSE|A|Fails lyric cues ('left/right') and repeats generic 8D motion yielding <2.8 MOS.",
            "c2": "CANDIDATE|B|Unconstrained Video/LLM Frame Predictor|Predicts raw XYZ per 20ms frame directly from LLM.",
            "r2": "REJECTED_BECAUSE|B|Exceeds 50 ms frame jitter budget and rotates Lead Vocal/Kick causing >25 deg phase smear.",
            "win": "WINNER|3-Plane Hybrid: Offline Beat/Stem Forensics + Compact INT4 JSON Cue Director + Deterministic Spline Governor (<0.3 ms/block).",
            "pm": "PREMORTEM|T+6mo|Bad external BPM tag or unguarded LLM cue rotates Lead Vocal mid-chorus causing listener fatigue.",
            "th": "THREAT|Malformed LLM JSON / OOD tempo sweep -> strict schema validator + angular velocity ceiling <=45 deg/s.",
            "k1": "KILL|R1|Vocal phase smearing|vocal_anchor_err_deg > 15 deg|10 songs eval|Lock Lead Vocal azimuth <= 10 deg.",
            "k2": "KILL|R2|Mobile OOM/thermal stall|peak_rss_mb > 900 MB|10 min batch|Switch SLM to rule-only semantic cue fallback.",
            "budget": "BUDGET|latency=<0.5 ms/block|mem=<450 MB|cost=0 USD offline|degraded_floor=DSP-only stem orbits",
            "planes": "PLANES|offline=stems+beat+SLM JSON cues|rt=0-alloc Catmull-Rom trajectory interpolator|async=section telemetry bus",
            "q1": "QAS|Q1|Tempo transition 90->140 BPM|On-device offline|Spline locks beat grid|<=15 ms drift|pytest tests/test_traj.py",
            "q2": "QAS|Q2|Lyric cue 'left, right'|Sync LRC+ADM|Voice/ad-lib sweeps L->R|<=0.1 deg zipper|dotnet test --filter AdmTraj",
            "spike": "SPIKE|S1|VoI=0.8|Test BS.2076 audioBlockFormat + IAMF decoder parity|ffmpeg + IamfReader roundtrip == 0 errors",
            "h1": "SLICE|W1|TrajectoryGovernor+BeatMap|stems+LRC->spline keyframes|pytest tests/test_traj.py",
            "h2": h2_override if h2_override is not None else "STUB|W2|Wire ADM/IAMF block writers & 3D Room X-Ray playback <=1% CPU overhead.",
            "fit": "FITNESS|CI runs python3 -B .agents/plugins/dynamic-fractal-planner/scripts/plan_tool.py blueprint lint BP_OMNISCIENT_SPATIAL.md --strict",
            "evo": "EVOLVE_RULE|on(kill_trigger || fitness_fail) -> mark SEED-v1 SUPERSEDED && delta-reenter Stage2-3",
        }
        if body_override:
            sections.update(body_override)
        return (
            "# Omniscient Spatial Cortex (`BP_OMNISCIENT_SPATIAL`)\n\n"
            "> STYLE|lossless symbolic telegraphic EN ONLY.\n\n"
            f"## 0. Meta & Lineage\n{sections['meta']}\nPARENT_ADR|none\n\n"
            f"## 1. Value & Non-Goals\n{sections['value']}\n{sections['wardley']}\n{sections['ng1']}\n{sections['ng2']}\n\n"
            f"## 2. Live SOTA & Blind Morphogenesis\n{sections['sota']}\n{sections['triz']}\n"
            f"{sections['c1']}\n{sections['r1']}\n{sections['c2']}\n{sections['r2']}\n{sections['win']}\n\n"
            f"## 3. Cold-Ctx Red-Team, Pre-Mortem & Kill Criteria\n{sections['pm']}\n{sections['th']}\n{sections['k1']}\n{sections['k2']}\n\n"
            f"## 4. Invariants, Budgets & QAS\n{sections['budget']}\n{sections['planes']}\n{sections['q1']}\n{sections['q2']}\n\n"
            f"## 5. Epistemic Wave-0 Spikes\n{sections['spike']}\n\n"
            f"## 6. Rolling-Wave DSM Graph\n### H1 Atomic Slices\n{sections['h1']}\n\n### H2+ Outcome Stubs\n{sections['h2']}\n\n"
            f"## 7. Fitness Functions & Evolution\n{sections['fit']}\n{sections['evo']}\n"
        )

    def test_classify_multi_domain_and_topology(self) -> None:
        c1 = blueprint_engine.classify_blueprint_request(
            "Build real-time spatial audio DSP + ONNX neural classifier + OneUI 3D screen"
        )
        self.assertIn("dsp-hw", c1["active_domains"])
        self.assertIn("ai-ml", c1["active_domains"])
        self.assertEqual(c1["risk_tier"], "T3")
        for lens in c1["active_lenses"].values():
            self.assertLessEqual(len(lens), 420)
        self.assertGreaterEqual(len(c1["morph_axes"]), 2)
        topo = c1["subagent_topology"]
        self.assertEqual(topo["stage_1_scouts"]["model"], "flash")
        self.assertEqual(topo["stage_2_blind_solvers"]["model"], "pro")
        self.assertEqual(topo["stage_3_cold_redteam"]["round_cap"], 1)

        c2 = blueprint_engine.classify_blueprint_request("OAuth JWT API microservice with SELinux key isolation and UI login modal")
        self.assertIn("security", c2["active_domains"])
        self.assertIn("software", c2["active_domains"])

        c3 = blueprint_engine.classify_blueprint_request("Wardley strategy pricing unit econ SLA roadmap for Q3")
        self.assertIn("biz-org", c3["active_domains"])

    def test_detect_blueprint_intent_positive_and_negative(self) -> None:
        pos = (
            "Cria um blueprint SOTA completo para o córtex espacial",
            "Write a SOTA blueprint and RFC for our distributed audio sync protocol",
            "Elabora uma especificação técnica / design doc para autenticação zero-trust",
        )
        for p in pos:
            self.assertTrue(complexity_triage.detect_blueprint_intent(p), f"Expected True for: {p}")
            res = complexity_triage.analyze_prompt(p)
            self.assertIn("blueprint_intent", res)
            self.assertTrue(res["blueprint_intent"], f"analyze_prompt missed blueprint_intent for: {p}")

        neg = (
            "Architect audio dsp filter, api schema, auth pipeline, ui tabs and tests across 5 modules",
            "Fix null reference crash in MainActivity.cs when exporting IAMF",
            "O que é um blueprint?",
            "Ô, não faz o blueprint não, só salva as ideias",
            "Don't create a blueprint yet, just explain",
            "Update blueprint_engine.py to fix typo",
            "obrigado",
        )
        for n in neg:
            self.assertFalse(complexity_triage.detect_blueprint_intent(n), f"Expected False for: {n}")
            res = complexity_triage.analyze_prompt(n)
            self.assertIn("blueprint_intent", res)
            self.assertFalse(res["blueprint_intent"], f"analyze_prompt false positive for: {n}")

    def test_scaffold_and_popperian_lint_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            sc = blueprint_engine.scaffold_blueprint("Spatial Cortex", "spatial_cortex", repo, prompt="DSP audio AI neural system")
            bp_path = Path(sc["path"])
            self.assertTrue(bp_path.is_file())
            r_sc = blueprint_engine.lint_blueprint(bp_path, strict=True)
            self.assertFalse(r_sc["passed"])
            self.assertGreater(r_sc["metrics"]["placeholders"], 0)

            good_path = repo / ".agents" / "docs" / "blueprints" / "BP_GOOD.md"
            good_path.write_text(self._valid_blueprint_text(), encoding="utf-8")
            r_good = blueprint_engine.lint_blueprint(good_path, strict=True)
            self.assertTrue(r_good["passed"], f"Expected PASS, got issues={r_good['issues']}")
            self.assertGreaterEqual(r_good["score"], 90)

            # Unqualified weasel words fail WBS
            weasel_p = repo / "BP_WEASEL.md"
            weasel_p.write_text(
                self._valid_blueprint_text(body_override={"value": "VALUE|Deliver fast, robust and seamless spatial audio.", "q1": "QAS|Q1|Beat|Live|Seamless fast output|fast|<cmd>"}),
                encoding="utf-8",
            )
            r_w = blueprint_engine.lint_blueprint(weasel_p, strict=True)
            self.assertFalse(r_w["passed"])
            self.assertLess(r_w["subscores"]["WBS"], 10)

            # Missing REJECTED_BECAUSE fails ORI
            noori_p = repo / "BP_NOORI.md"
            noori_p.write_text(self._valid_blueprint_text(body_override={"r1": "", "r2": ""}), encoding="utf-8")
            r_o = blueprint_engine.lint_blueprint(noori_p, strict=True)
            self.assertFalse(r_o["passed"])
            self.assertEqual(r_o["subscores"]["ORI"], 0)

            # Missing KILL fails KCC (drops below 15 when PREMORTEM exists, 0 when both absent)
            nokill_p = repo / "BP_NOKILL.md"
            nokill_p.write_text(self._valid_blueprint_text(body_override={"k1": "", "k2": ""}), encoding="utf-8")
            r_k = blueprint_engine.lint_blueprint(nokill_p, strict=True)
            self.assertFalse(r_k["passed"])
            self.assertLess(r_k["subscores"]["KCC"], 15)

            # Bloated H2+ with code leak fails AWF
            bloat_h2 = "\n".join(
                ["STUB|W2|Implement Cavern/Spatial/TrajectoryEngine.cs `public class TrajectoryEngine { }`"]
                + [f"STUB|W{i}|Extra verbose step {i} detailing internal structs" for i in range(3, 18)]
            )
            awf_p = repo / "BP_AWF.md"
            awf_p.write_text(self._valid_blueprint_text(h2_override=bloat_h2), encoding="utf-8")
            r_a = blueprint_engine.lint_blueprint(awf_p, strict=True)
            self.assertFalse(r_a["passed"])
            self.assertLess(r_a["subscores"]["AWF"], 10)

    def test_cli_pre_invocation_and_verify_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".agents").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )
            p_cls = run_script("plan_tool.py", ["--workspace", str(repo), "blueprint", "classify", "--prompt", "Create DSP AI blueprint", "--json"], cwd=repo)
            self.assertEqual(p_cls.returncode, 0, p_cls.stderr)
            self.assertIn("dsp-hw", json.loads(p_cls.stdout)["active_domains"])

            p_init = run_script("plan_tool.py", ["--workspace", str(repo), "blueprint", "init", "--title", "Demo BP", "--slug", "DEMO_BP", "--prompt", "DSP AI system"], cwd=repo)
            self.assertEqual(p_init.returncode, 0, p_init.stderr)
            bp_file = repo / ".agents" / "docs" / "blueprints" / "BP_DEMO_BP.md"
            self.assertTrue(bp_file.is_file())

            # Replace with valid complete blueprint and test lint + status + handoff
            bp_file.write_text(self._valid_blueprint_text(status="LOCKED"), encoding="utf-8")
            p_lint = run_script("plan_tool.py", ["--workspace", str(repo), "blueprint", "lint", str(bp_file), "--strict"], cwd=repo)
            self.assertEqual(p_lint.returncode, 0, p_lint.stdout + p_lint.stderr)
            p_st = run_script("plan_tool.py", ["--workspace", str(repo), "blueprint", "status"], cwd=repo)
            self.assertEqual(p_st.returncode, 0)
            self.assertIn("PASS", p_st.stdout)
            p_ho = run_script("plan_tool.py", ["--workspace", str(repo), "blueprint", "handoff", str(bp_file), "--json"], cwd=repo)
            self.assertEqual(p_ho.returncode, 0)
            self.assertIn("plan_tool.py init", json.loads(p_ho.stdout)["plan_init_cmd"])

            # PreInvocation hook injects [BLUEPRINT_MODE|SOTA] within 1500c cap, silent on normal prompts
            with tempfile.TemporaryDirectory() as art1:
                _, _, pre_bp = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art1,
                        "lastUserInput": "Crie um blueprint SOTA completo para audio espacial, neural e interface 3D",
                        "invocationNum": 1,
                    },
                )
                msg_bp = pre_bp["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[BLUEPRINT_MODE|SOTA]", msg_bp)
                self.assertLessEqual(len(msg_bp), 1500)

            with tempfile.TemporaryDirectory() as art2:
                _, _, pre_norm = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art2,
                        "lastUserInput": "Refactor audio decoder buffer, cache, and unit tests across 3 files",
                        "invocationNum": 1,
                    },
                )
                msg_norm = pre_norm["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertNotIn("[BLUEPRINT_MODE|SOTA]", msg_norm)

            # verify_universal check_blueprint_quality warn vs strict fail & pass
            fails: list[str] = []
            warns: list[str] = []
            verify_universal.check_blueprint_quality(repo, [".agents/docs/blueprints/BP_DEMO_BP.md"], {}, fails, warns)
            self.assertEqual(fails, [])
            self.assertEqual(warns, [])

            bad_rel = ".agents/docs/blueprints/BP_BAD.md"
            (repo / bad_rel).write_text("# Bad (`BP_BAD`)\nBP_META|id=BP_BAD|status=SEED-v1\n<FILL:x>\n", encoding="utf-8")
            warns.clear()
            verify_universal.check_blueprint_quality(repo, [bad_rel], {}, fails, warns)
            self.assertEqual(fails, [])
            self.assertEqual(len(warns), 1)
            verify_universal.check_blueprint_quality(repo, [bad_rel], {"blueprint": {"strict_verify": True}}, fails, warns)
            self.assertEqual(len(fails), 1)


class TestFleetSyncAndFastPath(unittest.TestCase):
    def test_equal_semver_content_drift_mtime_and_in_sync(self) -> None:
        import self_evolve

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "repo"
            emb = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
            glob = root / "global_plugin"
            emb.mkdir(parents=True)
            glob.mkdir(parents=True)

            manifest = json.dumps({"name": "dynamic-fractal-planner", "version": "2.14.0"})
            (emb / "plugin.json").write_text(manifest, encoding="utf-8")
            (glob / "plugin.json").write_text(manifest, encoding="utf-8")
            (emb / "marker.txt").write_text("same", encoding="utf-8")
            (glob / "marker.txt").write_text("same", encoding="utf-8")

            eq = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertEqual(eq["direction"], "in-sync")
            self.assertFalse(eq["applied"])

            t_old = 1_700_000_000.0
            t_new = 1_700_000_100.0
            (emb / "marker.txt").write_text("emb-newer", encoding="utf-8")
            (glob / "marker.txt").write_text("glob-older", encoding="utf-8")
            for p in (emb / "plugin.json", emb / "marker.txt"):
                os.utime(p, (t_new, t_new))
            for p in (glob / "plugin.json", glob / "marker.txt"):
                os.utime(p, (t_old, t_old))

            fwd = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertEqual(fwd["direction"], "embedded->global")
            self.assertTrue(fwd["applied"])
            self.assertEqual((glob / "marker.txt").read_text(encoding="utf-8"), "emb-newer")

            (glob / "marker.txt").write_text("glob-newer", encoding="utf-8")
            (emb / "marker.txt").write_text("emb-older", encoding="utf-8")
            for p in (glob / "plugin.json", glob / "marker.txt"):
                os.utime(p, (t_new + 50, t_new + 50))
            for p in (emb / "plugin.json", emb / "marker.txt"):
                os.utime(p, (t_old, t_old))

            rev = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
            self.assertEqual(rev["direction"], "global->embedded")
            self.assertTrue(rev["applied"])
            self.assertEqual((emb / "marker.txt").read_text(encoding="utf-8"), "glob-newer")

    def test_standalone_host_excludes_and_preservation(self) -> None:
        import self_evolve

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws_file = root / ".workspaces.json"
            glob = root / "global_plugin"
            standalone = root / "standalone_plugin"
            host = root / "host_repo"
            emb = host / ".agents" / "plugins" / "dynamic-fractal-planner"
            glob.mkdir(parents=True)
            standalone.mkdir(parents=True)
            emb.mkdir(parents=True)

            host_sentinels = {
                "package.json": '{"name":"standalone-shim"}\n',
                "AGENTS.md": "# Standalone AGENTS\n",
                ".agents/preview/server.mjs": "// keep preview server\n",
                ".agents/federation.json": '{"standalone":true}\n',
                ".agents/bridge_profile.json": '{"bridge":"keep"}\n',
                ".agents/env_manifest.json": '{"env":"keep"}\n',
                ".agents/host_matrix.json": '{"host":"keep"}\n',
                ".agents/planner_profile.json": '{"planner_mode":"lean"}\n',
            }
            for rel, body in host_sentinels.items():
                p = standalone / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body, encoding="utf-8")

            (standalone / "plugin.json").write_text(
                json.dumps({"name": "dynamic-fractal-planner", "version": "2.0.0"}),
                encoding="utf-8",
            )
            (standalone / "scripts").mkdir(parents=True)
            (standalone / "scripts" / "core.py").write_text("VER = '2.0.0'\n", encoding="utf-8")

            (emb / "plugin.json").write_text(
                json.dumps({"name": "dynamic-fractal-planner", "version": "2.1.0"}),
                encoding="utf-8",
            )
            (emb / "scripts").mkdir(parents=True)
            (emb / "scripts" / "core.py").write_text("VER = '2.1.0'\n", encoding="utf-8")

            with _patched_env({"PLANNER_WORKSPACES_FILE": str(ws_file)}):
                res1 = self_evolve.sync_plugin_fleet(
                    host,
                    apply=True,
                    push=False,
                    run_tests=False,
                    global_dir=glob,
                    extra_repos=[str(standalone)],
                )
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["authoritative"]["kind"], "embedded")
            self.assertEqual(json.loads((standalone / "plugin.json").read_text(encoding="utf-8"))["version"], "2.1.0")
            self.assertEqual((standalone / "scripts" / "core.py").read_text(encoding="utf-8"), "VER = '2.1.0'\n")
            for rel, body in host_sentinels.items():
                self.assertEqual((standalone / rel).read_text(encoding="utf-8"), body, rel)

            (standalone / "plugin.json").write_text(
                json.dumps({"name": "dynamic-fractal-planner", "version": "2.2.0"}),
                encoding="utf-8",
            )
            (standalone / "scripts" / "core.py").write_text("VER = '2.2.0'\n", encoding="utf-8")
            with _patched_env({"PLANNER_WORKSPACES_FILE": str(ws_file)}):
                res2 = self_evolve.sync_plugin_fleet(
                    standalone,
                    apply=True,
                    push=False,
                    run_tests=False,
                    global_dir=glob,
                    extra_repos=[str(host)],
                )
            self.assertTrue(res2["ok"])
            self.assertEqual(res2["authoritative"]["kind"], "standalone")
            self.assertEqual(json.loads((emb / "plugin.json").read_text(encoding="utf-8"))["version"], "2.2.0")
            self.assertEqual((emb / "scripts" / "core.py").read_text(encoding="utf-8"), "VER = '2.2.0'\n")
            for excl in self_evolve.STANDALONE_HOST_EXCLUDES:
                self.assertFalse((emb / excl).exists(), f"excluded path leaked into embedded: {excl}")

    def test_fleet_multi_repo_sync_and_metadata_bump(self) -> None:
        import self_evolve

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws_file = root / ".workspaces.json"
            glob = root / "global_plugin"
            standalone = root / "standalone_plugin"
            host1 = root / "host_one"
            host2 = root / "host_two"
            for d in (glob, standalone, host1, host2):
                d.mkdir(parents=True)

            (standalone / "package.json").write_text('{"name":"dfp"}\n', encoding="utf-8")
            (standalone / "plugin.json").write_text(
                json.dumps({"name": "dynamic-fractal-planner", "version": "2.14.0"}),
                encoding="utf-8",
            )
            (standalone / ".agents" / "docs").mkdir(parents=True)
            (standalone / ".agents" / "docs" / "HANDOFF.md").write_text(
                "# Handoff\n- `Release`: `v2.12.0`\n",
                encoding="utf-8",
            )

            (glob / "plugin.json").write_text(
                json.dumps({"name": "dynamic-fractal-planner", "version": "2.10.0"}),
                encoding="utf-8",
            )

            for hdir, ver in ((host1, "2.10.0"), (host2, "2.11.0")):
                emb = hdir / ".agents" / "plugins" / "dynamic-fractal-planner"
                (emb / ".agents" / "docs").mkdir(parents=True)
                (emb / "plugin.json").write_text(
                    json.dumps({"name": "dynamic-fractal-planner", "version": ver}),
                    encoding="utf-8",
                )
                (emb / ".agents" / "docs" / "HANDOFF.md").write_text(
                    f"# Handoff\n- `Release`: `v{ver}`\n",
                    encoding="utf-8",
                )
                (hdir / ".agents" / "planner_profile.json").write_text(
                    json.dumps({"schema_version": 5, "plugin_version": ver}),
                    encoding="utf-8",
                )
                (hdir / "AGENTS.md").write_text(
                    f"# Repo\nPlanner (`dynamic-fractal-planner v{ver}`)\n",
                    encoding="utf-8",
                )

            with _patched_env({"PLANNER_WORKSPACES_FILE": str(ws_file)}):
                self.assertTrue(self_evolve.register_workspace(standalone, global_dir=glob))
                self.assertTrue(self_evolve.register_workspace(host1, global_dir=glob))
                res = self_evolve.sync_plugin_fleet(
                    host1,
                    apply=True,
                    push=False,
                    run_tests=False,
                    global_dir=glob,
                    extra_repos=[str(host2)],
                )

            self.assertTrue(res["ok"])
            self.assertEqual(res["authoritative"]["kind"], "standalone")
            self.assertEqual(res["authoritative"]["version"], "2.14.0")
            kinds = {t["kind"] for t in res["targets"]}
            self.assertEqual(kinds, {"standalone", "global", "embedded"})
            self.assertEqual(len(res["targets"]), 4)
            for t in res["targets"]:
                self.assertEqual(t["version_after"], "2.14.0")

            self.assertIn("`v2.14.0`", (standalone / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8"))
            self.assertIn("`v2.14.0`", (glob / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8"))
            for hdir in (host1, host2):
                prof = json.loads((hdir / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
                self.assertEqual(prof["plugin_version"], "2.14.0")
                self.assertIn("dynamic-fractal-planner v2.14.0", (hdir / "AGENTS.md").read_text(encoding="utf-8"))
                emb_handoff = (hdir / ".agents" / "plugins" / "dynamic-fractal-planner" / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
                self.assertIn("`v2.14.0`", emb_handoff)

            # Monotonic: lower target_ver never downgrades metadata or Release
            self.assertEqual(self_evolve._update_host_metadata(host1, "2.10.0", apply=True), [])
            self.assertFalse(self_evolve._refresh_plugin_handoff_release(standalone / ".agents" / "docs" / "HANDOFF.md", "2.10.0"))
            self.assertEqual(json.loads((host1 / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))["plugin_version"], "2.14.0")
            self.assertIn("`v2.14.0`", (standalone / ".agents" / "docs" / "HANDOFF.md").read_text(encoding="utf-8"))

    def test_detect_plugin_sync_intent_positive_and_negative(self) -> None:
        positives = (
            "Atualiza o plugin no cavern. Vai rápido",
            "sincroniza o plugin em todos os repos",
            "sync plugin",
            "update plugin to github",
        )
        for p in positives:
            self.assertTrue(complexity_triage.detect_plugin_sync_intent(p), f"expected True for: {p!r}")

        negatives = (
            "Implementa nova feature no plugin",
            "Corrige bug no complexity_triage.py",
            "Crie um blueprint do plugin",
            "Como funciona o plugin?",
            "sync plugin " + ("x" * 215),
        )
        for n in negatives:
            self.assertFalse(complexity_triage.detect_plugin_sync_intent(n), f"expected False for: {n!r}")

    def test_analyze_prompt_plugin_sync_fastpath(self) -> None:
        res = complexity_triage.analyze_prompt("Atualiza o plugin no cavern. Vai rápido")
        self.assertEqual(res["tier"], 0)
        self.assertEqual(res["intent"], "plugin_sync_fastpath")
        self.assertTrue(res["plugin_sync_intent"])

    def test_pre_invocation_plugin_sync_fastpath_normal_and_eco(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )

            with tempfile.TemporaryDirectory() as art1:
                _, _, pre_norm = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art1,
                        "lastUserInput": "Atualiza o plugin no cavern. Vai rápido",
                        "invocationNum": 1,
                    },
                )
                msg_norm = pre_norm["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[PLUGIN_SYNC_FASTPATH]", msg_norm)
                for forbidden in ("Triage|", "DELEGATE|", "SOTA_PREFLIGHT|"):
                    self.assertNotIn(forbidden, msg_norm)

            with tempfile.TemporaryDirectory() as art2:
                _, _, pre_eco = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art2,
                        "lastUserInput": "low quota: sync plugin",
                        "invocationNum": 1,
                    },
                )
                msg_eco = pre_eco["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[ECO_MODE|ON]", msg_eco)
                self.assertIn("[PLUGIN_SYNC_FASTPATH]", msg_eco)
                for forbidden in ("Triage|", "DELEGATE|", "SOTA_PREFLIGHT|"):
                    self.assertNotIn(forbidden, msg_eco)

    def test_bootstrap_workspace_embed_plugin_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "new_project"
            repo.mkdir()
            ws_file = root / ".workspaces.json"
            gemini_home = root / ".gemini"

            with _patched_env({"PLANNER_WORKSPACES_FILE": str(ws_file), "GEMINI_HOME": str(gemini_home)}):
                created, _ = planner_state.bootstrap_workspace(repo, embed_plugin=True)

            self.assertIn(".agents/planner_profile.json", created)
            self.assertIn(".agents/plugins/dynamic-fractal-planner", created)
            self.assertIn(".agents/host_matrix.json", created)

            prof = json.loads((repo / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertEqual(prof["planner_mode"], "auto")
            self.assertIn("file_budget_limits", prof)
            self.assertIn("plugin_evolution", prof)
            self.assertTrue(prof["plugin_evolution"]["auto_sync_higher_version"])
            self.assertTrue((repo / ".agents" / "plugins" / "dynamic-fractal-planner" / "plugin.json").is_file())
            self.assertTrue((repo / ".agents" / "host_matrix.json").is_file())

            reg = json.loads(ws_file.read_text(encoding="utf-8"))
            self.assertIn(str(repo.resolve()), reg["workspaces"])


class TestV215PaceAndZeroQuotaSync(unittest.TestCase):
    """Tests for v2.15.0 turbo/eco/normal parallel cubicle pace, zero-quota global->embedded SessionStart sync & PROJECT_HISTORY parity."""

    def test_v215_pace_modes_zero_quota_global_to_embedded_and_history_sync(self) -> None:
        import self_evolve  # noqa: E402

        # 1) Pace detection: turbo default, eco on quota-low cues, normal on explicit normal/step-by-step
        self.assertEqual(planner_state.detect_pace_mode("Implement feature across 4 modules"), "turbo")
        self.assertEqual(planner_state.detect_pace_mode("vai rápido e implementa isso"), "turbo")
        self.assertEqual(planner_state.detect_pace_mode("cota tá acabando, economiza cota"), "eco")
        self.assertEqual(planner_state.detect_pace_mode("low quota: fix auth"), "eco")
        self.assertEqual(planner_state.detect_pace_mode("modo normal, passo a passo sem pressa"), "normal")
        self.assertEqual(
            planner_state.detect_pace_mode("do this task", {"delegation": {"default_pace_mode": "normal"}}),
            "normal",
        )

        # 2) PreInvocation: DELEGATE|pace=... & [ECO_MODE|ON] parallel cubicle permission
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )
            t2_prompt = "Implement audio spatial filter, API contract, auth pipeline, and tests across 4 files"
            with tempfile.TemporaryDirectory() as art1:
                _, _, pre_t = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art1, "lastUserInput": t2_prompt, "invocationNum": 1},
                )
                msg_t = pre_t["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DELEGATE|pace=turbo|", msg_t)
                self.assertIn("parallel 1-shot cubicles", msg_t)
                self.assertLessEqual(len(msg_t), 1500)

            with tempfile.TemporaryDirectory() as art2:
                _, _, pre_e = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art2, "lastUserInput": "cota tá acabando: " + t2_prompt, "invocationNum": 1},
                )
                msg_e = pre_e["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[ECO_MODE|ON]", msg_e)
                self.assertIn("parallel flash_lite/flash subagent cubicles", msg_e)
                self.assertIn("DELEGATE|pace=eco|", msg_e)

            with tempfile.TemporaryDirectory() as art3:
                _, _, pre_n = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art3, "lastUserInput": "modo normal, passo a passo: " + t2_prompt, "invocationNum": 1},
                )
                msg_n = pre_n["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("DELEGATE|pace=normal|", msg_n)

        # 3) SessionStart zero-quota global->embedded auto-sync + automatic host PROJECT_HISTORY.md entry
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "host_app"
            emb = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
            glob = root / ".gemini" / "config" / "plugins" / "dynamic-fractal-planner"
            (repo / ".agents" / "docs").mkdir(parents=True)
            (emb / ".agents" / "docs").mkdir(parents=True)
            glob.mkdir(parents=True)

            (repo / "package.json").write_text('{"name":"host-app"}\n', encoding="utf-8")
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "auto", "plugin_version": "2.12.0", "plugin_evolution": {"enabled": True, "auto_sync_higher_version": True}}),
                encoding="utf-8",
            )
            (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text(
                "# Project History (`PROJECT_HISTORY.md`)\n\n## 3. Version History (Newest First)\n- `v1.0` initial\n",
                encoding="utf-8",
            )
            (emb / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "2.12.0"}), encoding="utf-8")
            (glob / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "2.15.0"}), encoding="utf-8")
            (glob / "CHANGELOG.md").write_text(
                "# Plugin History\n\n- 2026-11-24|v2.15.0|turbo/eco/normal parallel cubicles & zero-quota sync\n",
                encoding="utf-8",
            )
            (glob / "marker.txt").write_text("from-2.15.0\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as art:
                # Test direct sync_plugin_versions global->embedded + host PROJECT_HISTORY auto-entry
                res = self_evolve.sync_plugin_versions(repo, global_dir=glob, apply=True)
                self.assertEqual(res["direction"], "global->embedded")
                self.assertTrue(res["applied"])
                self.assertEqual(json.loads((emb / "plugin.json").read_text(encoding="utf-8"))["version"], "2.15.0")
                self.assertEqual((emb / "marker.txt").read_text(encoding="utf-8"), "from-2.15.0\n")
                hist_txt = (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").read_text(encoding="utf-8")
                self.assertIn("Plugin v2.15.0 [x]", hist_txt)
                self.assertIn("turbo/eco/normal", hist_txt)

                # Reset embedded to 2.12.0 and test SessionStart zero-quota auto-sync via GEMINI_HOME
                (emb / "plugin.json").write_text(json.dumps({"name": "dynamic-fractal-planner", "version": "2.12.0"}), encoding="utf-8")
                (emb / "marker.txt").unlink()
                _, _, ss = run_hook(
                    "planner_session_start.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art},
                    env_extra={"GEMINI_HOME": str(root / ".gemini")},
                )
                ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("PLUGIN_SYNCED|global->embedded|v2.12.0->v2.15.0", ss_msg)
                self.assertEqual(json.loads((emb / "plugin.json").read_text(encoding="utf-8"))["version"], "2.15.0")

        # 4) check_plugin_sync_parity flags missing plugin PROJECT_HISTORY.md entry independently of EXTERNAL_AI_SYNC.md
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            plug = repo / ".agents" / "plugins" / "dynamic-fractal-planner"
            (plug / ".agents" / "docs").mkdir(parents=True)
            (plug / "plugin.json").write_text(json.dumps({"version": "2.15.0"}), encoding="utf-8")
            (plug / ".agents" / "docs" / "EXTERNAL_AI_SYNC.md").write_text("### 2026-11-24 | Plugin v2.15.0\n", encoding="utf-8")
            (plug / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("## 3. Version History\n- 2026-11-23|v2.11.0|old\n", encoding="utf-8")
            w_miss: list[str] = []
            verify_universal.check_plugin_sync_parity(
                repo,
                [".agents/plugins/dynamic-fractal-planner/plugin.json", ".agents/plugins/dynamic-fractal-planner/.agents/docs/EXTERNAL_AI_SYNC.md"],
                w_miss,
                scan_all=False,
            )
            self.assertTrue(any("PROJECT_HISTORY.md" in w for w in w_miss), w_miss)
            (plug / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("## 3. Version History\n- 2026-11-24|v2.15.0|new\n", encoding="utf-8")
            w_ok: list[str] = []
            verify_universal.check_plugin_sync_parity(
                repo,
                [".agents/plugins/dynamic-fractal-planner/plugin.json", ".agents/plugins/dynamic-fractal-planner/.agents/docs/EXTERNAL_AI_SYNC.md", ".agents/plugins/dynamic-fractal-planner/.agents/docs/PROJECT_HISTORY.md"],
                w_ok,
                scan_all=False,
            )
            self.assertEqual(w_ok, [])


class TestV216SubSubagentTiering(unittest.TestCase):
    """Tests for v2.16.0 configurable delegation.sub_subagent & Google medium/low tiering."""

    def test_v216_schema_sub_subagent_compat_and_normalization(self) -> None:
        v4 = json.loads((FIXTURES_DIR / "profile_v4_cavern.json").read_text(encoding="utf-8"))
        m4, ver4, w4 = schema_migrations.migrate_profile(v4)
        self.assertEqual(ver4, 5)
        self.assertEqual(w4, [])
        expected_sub = {
            "allow_nesting": True,
            "default_model": "flash_lite",
            "swarm_medium_model": "gemini-4-hydrogen-medium",
            "allow_medium_tier": True,
        }
        self.assertEqual(m4["delegation"]["sub_subagent"], expected_sub)

        v5_partial = {
            "schema_version": 5,
            "planner_mode": "lean",
            "delegation": {"sub_subagent": {"allow_medium_tier": False}},
        }
        resolved = schema_migrations.resolve_delegation_config(v5_partial)
        self.assertTrue(resolved["sub_subagent"]["allow_nesting"])
        self.assertEqual(resolved["sub_subagent"]["default_model"], "flash_lite")
        self.assertEqual(resolved["sub_subagent"]["swarm_medium_model"], "gemini-4-hydrogen-medium")
        self.assertFalse(resolved["sub_subagent"]["allow_medium_tier"])

    def test_v216_guard_nested_sub_subagent_and_swarm_medium_low(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / ".agents").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )

            def guard(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
                rc, err, parsed = run_hook(
                    "planner_guard_delegation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art,
                        "toolCall": {"name": tool_name, "args": args},
                    },
                )
                self.assertEqual(rc, 0, err)
                return parsed

            # Nested sub-subagent flash_lite & flash allowed
            for model in ("flash_lite", "flash"):
                res = guard(
                    "invoke_subagent",
                    {"TypeName": "self", "Role": "Nested Sub-Subagent Worker", "Prompt": "spawn nested sub-subagent slice", "Model": model},
                )
                self.assertEqual(res["decision"], "allow", f"{model}: {res}")

            # Swarm Google-only medium/low tier allowed for non-complex slices when allow_medium_tier=true
            for sw_m in ("gemini-4-hydrogen-medium", "gemini-2.5-flash-medium", "gemini-4-hydrogen-low"):
                res_add = guard("swarm", {"action": "add", "name": "leaf1", "model": sw_m, "brief": "non-complex bulk grep slice"})
                self.assertEqual(res_add["decision"], "allow", f"{sw_m}: {res_add}")
                res_mod = guard("swarm", {"action": "model", "name": "leaf1", "model": sw_m})
                self.assertEqual(res_mod["decision"], "allow", f"{sw_m}: {res_mod}")

            # Strict block on non-Google models remains enforced
            for bad in ("claude-3-7-sonnet", "anthropic/haiku", "gpt-4o", "openai/o3-mini", "chatgpt-4o-latest", "o4"):
                self.assertEqual(
                    guard("invoke_subagent", {"TypeName": "self", "Role": "Sub-Subagent", "Prompt": "check", "Model": bad})["decision"],
                    "deny",
                    bad,
                )
                self.assertEqual(
                    guard("swarm", {"action": "add", "name": "w1", "model": bad, "brief": "slice"})["decision"],
                    "deny",
                    bad,
                )

            # allow_medium_tier=false blocks medium on non-complex swarm slices while allowing low/flash_lite
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "delegation": {"sub_subagent": {"allow_medium_tier": False}}}),
                encoding="utf-8",
            )
            deny_med = guard("swarm", {"action": "add", "name": "w1", "model": "gemini-4-hydrogen-medium", "brief": "simple file scan"})
            self.assertEqual(deny_med["decision"], "deny")
            self.assertIn("allow_medium_tier=false", deny_med["reason"])
            self.assertEqual(
                guard("swarm", {"action": "add", "name": "w1", "model": "gemini-4-hydrogen-low", "brief": "simple file scan"})["decision"],
                "allow",
            )

            # allow_nesting=false blocks nested sub-subagent marker
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "delegation": {"sub_subagent": {"allow_nesting": False}}}),
                encoding="utf-8",
            )
            deny_nest = guard(
                "invoke_subagent",
                {"TypeName": "self", "Role": "Nested Sub-Subagent", "Prompt": "run nested subagent", "Model": "flash_lite"},
            )
            self.assertEqual(deny_nest["decision"], "deny")
            self.assertIn("allow_nesting=false", deny_nest["reason"])

    def test_v216_pre_invocation_delegate_nesting_cue_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )
            prompt = "modo normal: 1. Refactor state machine\n2. Add unit tests\n3. Update docs"
            with tempfile.TemporaryDirectory() as art1:
                _, _, pre1 = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art1, "lastUserInput": prompt, "invocationNum": 1},
                )
                msg1 = pre1["injectSteps"][0]["systemMessage"]["systemMessage"]
                del_lines = [ln for ln in msg1.splitlines() if ln.startswith("DELEGATE|")]
                self.assertEqual(len(del_lines), 1, msg1)
                self.assertIn("sub-sub:flash_lite+swarm-med/low", del_lines[0])
                self.assertLessEqual(len(del_lines[0]), 200, del_lines[0])

            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "delegation": {"sub_subagent": {"allow_nesting": False}}}),
                encoding="utf-8",
            )
            with tempfile.TemporaryDirectory() as art2:
                _, _, pre2 = run_hook(
                    "planner_pre_invocation.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art2, "lastUserInput": prompt, "invocationNum": 1},
                )
                msg2 = pre2["injectSteps"][0]["systemMessage"]["systemMessage"]
                del_lines2 = [ln for ln in msg2.splitlines() if ln.startswith("DELEGATE|")]
                self.assertEqual(len(del_lines2), 1, msg2)
                self.assertNotIn("sub-sub:", del_lines2[0])
                self.assertLessEqual(len(del_lines2[0]), 200, del_lines2[0])


class TestAntiPollingGuard(unittest.TestCase):
    """Tests for v2.17.0 anti-polling PreToolUse guards, --fast auto-rewrite, and DELEGATE| cues."""

    def test_v217_schedule_and_manage_task_polling_guard(self) -> None:
        hooks_cfg = json.loads((PLUGIN_ROOT / "hooks.json").read_text(encoding="utf-8"))
        pre_hooks = hooks_cfg["dynamic-fractal-planner"]["PreToolUse"]
        self.assertTrue(
            any(
                h.get("matcher") == "schedule|manage_task"
                and any("planner_guard_polling.py" in sub.get("command", "") for sub in h.get("hooks", []))
                for h in pre_hooks
            )
        )

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / ".agents").mkdir(parents=True)

            def poll_guard(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
                rc, err, parsed = run_hook(
                    "planner_guard_polling.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art,
                        "toolCall": {"name": tool_name, "args": args},
                    },
                )
                self.assertEqual(rc, 0, err)
                return parsed

            # schedule: polling prompts or short unconditional timers denied w/ reactive/watch guidance
            deny_prompt = poll_guard("schedule", {"DurationSeconds": 120, "Prompt": "check build status"})
            self.assertEqual(deny_prompt["decision"], "deny")
            self.assertIn("DENY|anti_poll", deny_prompt["reason"])
            self.assertIn("NotificationTimeoutSeconds", deny_prompt["reason"])
            self.assertIn("gh run watch --exit-status", deny_prompt["reason"])

            deny_short = poll_guard("schedule", {"DurationSeconds": 15, "Prompt": "ping me", "TimerCondition": "never"})
            self.assertEqual(deny_short["decision"], "deny")
            self.assertIn("DENY|anti_poll", deny_short["reason"])

            deny_short_none = poll_guard("schedule", {"DurationSeconds": 30, "Prompt": "resume"})
            self.assertEqual(deny_short_none["decision"], "deny")

            # schedule: daemon, cron, and genuine >30s non-polling reminders allowed
            self.assertEqual(
                poll_guard("schedule", {"CronExpression": "*/15 * * * *", "Prompt": "check inbox", "IsDaemon": True})["decision"],
                "allow",
            )
            self.assertEqual(
                poll_guard("schedule", {"CronExpression": "0 9 * * 1", "Prompt": "weekly summary"})["decision"],
                "allow",
            )
            self.assertEqual(
                poll_guard("schedule", {"DurationSeconds": 120, "Prompt": "Remind user about lunch break"})["decision"],
                "allow",
            )

            # manage_task: 1st status/list allowed, 2nd within 90s denied; kill & send_input allowed
            self.assertEqual(poll_guard("manage_task", {"Action": "status", "TaskId": "t-1"})["decision"], "allow")
            deny_status = poll_guard("manage_task", {"Action": "status", "TaskId": "t-1"})
            self.assertEqual(deny_status["decision"], "deny")
            self.assertIn("DENY|anti_poll", deny_status["reason"])

            self.assertEqual(poll_guard("manage_task", {"Action": "list"})["decision"], "allow")
            deny_list = poll_guard("manage_task", {"Action": "list"})
            self.assertEqual(deny_list["decision"], "deny")
            self.assertIn("DENY|anti_poll", deny_list["reason"])

            self.assertEqual(poll_guard("manage_task", {"Action": "kill", "TaskId": "t-1"})["decision"], "allow")
            self.assertEqual(poll_guard("manage_task", {"Action": "send_input", "TaskId": "t-1", "Input": "y\n"})["decision"], "allow")

    def test_v217_command_polling_guards_and_fast_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            (repo / ".agents").mkdir(parents=True)

            def cmd_guard(cmd: str) -> dict[str, Any]:
                rc, err, parsed = run_hook(
                    "planner_guard_command.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art,
                        "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}},
                    },
                )
                self.assertEqual(rc, 0, err)
                return parsed

            # ps aux | grep repeat poll within 120s denied
            self.assertEqual(cmd_guard("ps aux | grep dotnet")["decision"], "allow")
            deny_ps = cmd_guard("ps aux | grep dotnet")
            self.assertEqual(deny_ps["decision"], "deny")
            self.assertIn("DENY|anti_poll", deny_ps["reason"])

            # shell while/for + sleep polling gh run list/view denied
            for loop_cmd in (
                "while true; do gh run list --limit 1; sleep 10; done",
                "for i in 1 2 3; do gh run view 12345; sleep 5; done",
            ):
                deny_gh = cmd_guard(loop_cmd)
                self.assertEqual(deny_gh["decision"], "deny", loop_cmd)
                self.assertIn("DENY|anti_poll", deny_gh["reason"])
                self.assertIn("gh run watch --exit-status --compact", deny_gh["reason"])

            # run_plugin_tests.py auto-rewrites to --fast when bare
            rw_bare = cmd_guard("python3 -B tests/run_plugin_tests.py")
            self.assertEqual(rw_bare["decision"], "allow")
            self.assertIn("run_plugin_tests.py --fast", rw_bare.get("overwrite", {}).get("CommandLine", ""))

            for keep_cmd in (
                "python3 -B tests/run_plugin_tests.py --fast",
                "python3 -B tests/run_plugin_tests.py --full",
                "python3 -B tests/run_plugin_tests.py -h",
                "python3 -B tests/run_plugin_tests.py TestAntiPollingGuard",
            ):
                rw_keep = cmd_guard(keep_cmd)
                self.assertEqual(rw_keep["decision"], "allow")
                self.assertNotIn("--fast --fast", rw_keep.get("overwrite", {}).get("CommandLine", ""))
                self.assertNotIn(" --fast", rw_keep.get("overwrite", {}).get("CommandLine", "").replace(keep_cmd, ""))

    def test_v217_delegate_cue_contains_anti_poll_and_single_verify_owner(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean"}),
                encoding="utf-8",
            )
            prompt = "1. Refactor state machine\n2. Add unit tests\n3. Update docs"
            _, _, pre = run_hook(
                "planner_pre_invocation.py",
                {"workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": prompt, "invocationNum": 1},
            )
            msg = pre["injectSteps"][0]["systemMessage"]["systemMessage"]
            del_lines = [ln for ln in msg.splitlines() if ln.startswith("DELEGATE|")]
            self.assertEqual(len(del_lines), 1, msg)
            self.assertIn("!poll(!schedule/!manage_task-loop)", del_lines[0])
            self.assertIn("1-verify-owner", del_lines[0])
            self.assertLessEqual(len(del_lines[0]), 200, del_lines[0])


class TestPlanGovernanceAndHub(unittest.TestCase):
    """Tests for v2.18.0 Plan Governance, Central Plans Hub auto-sync, hygiene audit, and Blueprint-First default."""

    def _valid_bp_text(self, bp_id: str, status: str = "ACTIVE-v1", extra_meta: str = "") -> str:
        extra = f"{extra_meta}\n" if extra_meta else ""
        return (
            f"# SOTA Blueprint — {bp_id}\n\n"
            f"> STYLE|lossless symbolic telegraphic EN ONLY.\n"
            f"BP_META|id={bp_id}|domain_vec=software:0.90|door=2-way|tier=T2|status={status}|updated=2026-11-25\n"
            f"{extra}\n"
            "## 1. Strategic Frame\n"
            "VALUE|Measurable governance improvement >=95% compliance.\n"
            "WARDLEY|Build custom lint engine | reuse stdlib python3.\n"
            "NON_GOAL|No C# DSP rewrites.\n"
            "NON_GOAL|No third-party pip deps.\n\n"
            "## 2. Blind Morphogenesis & TRIZ\n"
            "TRIZ_CONTRADICTION|Freshness vs manual doc maintenance.\n"
            "CANDIDATE|PureManual|Hand-edited markdown hub only.\n"
            "CANDIDATE|HybridMarker|Curated prose + machine-synced marker inventory.\n"
            "WINNER|Hybrid curated + auto-synced marker hub.\n"
            "REJECTED_BECAUSE|PureManual|Drifts within 2 commits (>15% stale).\n"
            "REJECTED_BECAUSE|FullAutoGen|Destroys curated priority ordering (0% human narrative).\n\n"
            "## 3. Cold-Context Red-Team & Kill Criteria\n"
            "PREMORTEM|T+6mo|Hub drifts silently causing stale plan execution.\n"
            "THREAT|Stale superseded directive re-enabled -> mitigated by hygiene gate.\n"
            "KILL|K1|Hub drift|hub_check_exit!=0|every PR|Run blueprint hub --sync.\n"
            "KILL|K2|Runtime slow|hygiene_ms>250ms|every run|Skip heavy scans.\n\n"
            "## 4. Wave-0 Spikes\n"
            "SPIKE|S1|1-way|Verify marker preservation|0 B outside diff|python3 -c 'print(0)'|0 B diff\n\n"
            "## 5. Budgets & QAS\n"
            "BUDGET|latency<=150ms|mem<=32MB|cost=$0\n"
            "PLANES|offline=CLI | rt=hook <=10ms | async=CI\n"
            "QAS|Q1|Sync hub|Repo w/ blueprints|Updates marker block|0 B diff outside markers|python3 -B scripts/plan_tool.py blueprint hub --check\n\n"
            "## 6. Rolling-Wave DSM\n"
            "### H1 Atomic Slices\n"
            "SLICE|W1|Sync hub engine|scripts/blueprint_engine.py|<=150ms|tests pass\n"
            "### H2+ Outcome Stubs\n"
            "STUB|W2|Optional future extension <=10ms\n\n"
            "## 7. Fitness\n"
            "FITNESS|F1|Hub check stays green|0 drift|plan_tool.py blueprint hub --check\n"
        )

    def test_v218_hygiene_audit_and_hub_sync_preserves_curated_prose(self) -> None:
        import blueprint_engine

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            bp_dir = repo / ".agents" / "docs" / "blueprints"
            bp_dir.mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "planner_mode": "lean", "blueprint": {"hub_file": ".agents/docs/PLANS_HUB.md"}}),
                encoding="utf-8",
            )
            # 1. Conflict + stale SUPERSEDED_BY + missing hub -> hygiene fails
            (bp_dir / "BP_ALPHA.md").write_text(
                self._valid_bp_text("BP_ALPHA", status="ACTIVE-v1", extra_meta="CONFLICTS_WITH|BP_BETA|Blur conflict"),
                encoding="utf-8",
            )
            (bp_dir / "BP_BETA.md").write_text(
                self._valid_bp_text("BP_BETA", status="ACTIVE-v1", extra_meta="SUPERSEDED_BY|BP_GAMMA"),
                encoding="utf-8",
            )
            res_bad = blueprint_engine.hygiene_audit(repo)
            self.assertFalse(res_bad["ok"])
            joined_bad = "\n".join(res_bad["issues"])
            self.assertIn("Unresolved CONFLICTS_WITH|", joined_bad)
            self.assertIn("declares SUPERSEDED_BY|BP_GAMMA", joined_bad)
            self.assertIn("Central Plans Hub missing", joined_bad)

            # 2. Resolve conflicts + create curated hub w/ markers -> sync preserves curated text 100%
            (bp_dir / "BP_ALPHA.md").write_text(
                self._valid_bp_text("BP_ALPHA", status="DONE", extra_meta="CONFLICT_RESOLVED|Blur disabled by W51.4"),
                encoding="utf-8",
            )
            (bp_dir / "BP_BETA.md").write_text(
                self._valid_bp_text("BP_BETA", status="SUPERSEDED", extra_meta="SUPERSEDED_BY|BP_GAMMA"),
                encoding="utf-8",
            )
            curated_head = "# Central Plans Hub\n\nCURATED_TOP_KEEP_EXACT\n\n"
            curated_tail = "\n\nCURATED_BOTTOM_KEEP_EXACT\n"
            hub_path = repo / ".agents" / "docs" / "PLANS_HUB.md"
            hub_path.write_text(
                f"{curated_head}{blueprint_engine.HUB_AUTO_START}\nstale\n{blueprint_engine.HUB_AUTO_END}{curated_tail}",
                encoding="utf-8",
            )

            # Check detects drift before sync
            rc_drift = blueprint_engine.main(["--workspace", str(repo), "hub", "--check"])
            self.assertEqual(rc_drift, 1)

            # Sync updates only inside markers and preserves curated sections verbatim
            sync_info = blueprint_engine.sync_plans_hub(repo)
            self.assertTrue(sync_info["changed"])
            hub_txt = hub_path.read_text(encoding="utf-8")
            self.assertTrue(hub_txt.startswith(curated_head))
            self.assertTrue(hub_txt.endswith(curated_tail))
            self.assertIn("`BP_ALPHA`", hub_txt)
            self.assertIn("`BP_BETA`", hub_txt)

            # Idempotent re-sync & clean hub --check + hygiene --strict
            sync_again = blueprint_engine.sync_plans_hub(repo)
            self.assertFalse(sync_again["changed"])
            self.assertEqual(blueprint_engine.main(["--workspace", str(repo), "hub", "--check"]), 0)
            self.assertEqual(blueprint_engine.main(["--workspace", str(repo), "hygiene", "--strict"]), 0)

    def test_v218_blueprint_first_default_opt_out_and_resume_hygiene_gate(self) -> None:
        # Pure helper checks (PT-BR & EN)
        for p_opt in (
            "Implementa o filtro de audio sem blueprint para economizar token",
            "nao faz blueprint, corrige o bug direto",
            "pula o blueprint e faz logo",
            "Add logging helper, skip blueprint to save tokens",
            "no blueprint needed, just patch auth.py",
        ):
            self.assertTrue(complexity_triage.detect_blueprint_opt_out(p_opt), p_opt)
        self.assertFalse(complexity_triage.detect_blueprint_opt_out("Cria um novo filtro de audio espacial"))

        for p_res in (
            "Vamos retomar o plano da fase 3 W31",
            "continua o roadmap de modularizacao",
            "Resume the deferred Phase B blueprint now",
            "executa o plano W4 que deixamos pendente",
        ):
            self.assertTrue(complexity_triage.detect_plan_resume_intent(p_res), p_res)
        self.assertFalse(complexity_triage.detect_plan_resume_intent("Como funciona o filtro de audio?"))

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents" / "docs" / "blueprints").mkdir(parents=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps(
                    {
                        "schema_version": 5,
                        "planner_mode": "lean",
                        "blueprint": {
                            "hub_file": ".agents/docs/PLANS_HUB.md",
                            "require_by_default": True,
                            "default_min_tier": 1,
                            "require_pre_resume_hygiene": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            import blueprint_engine

            (repo / ".agents" / "docs" / "blueprints" / "BP_ONE.md").write_text(
                self._valid_bp_text("BP_ONE", status="PARTIAL"),
                encoding="utf-8",
            )
            blueprint_engine.sync_plans_hub(repo)

            # 1. Feature task w/ require_by_default=True injects [BLUEPRINT_FIRST|SOTA]
            with tempfile.TemporaryDirectory() as art1:
                _, _, pre1 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art1,
                        "lastUserInput": "Adiciona um novo modulo de cache de sessao no backend e testes unitarios",
                        "invocationNum": 1,
                    },
                )
                msg1 = pre1["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[BLUEPRINT_FIRST|SOTA]", msg1)

            # 2. Explicit token-saving opt-out suppresses [BLUEPRINT_FIRST|SOTA]
            with tempfile.TemporaryDirectory() as art2:
                _, _, pre2 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art2,
                        "lastUserInput": "Adiciona um novo modulo de cache no backend sem blueprint para economizar token",
                        "invocationNum": 1,
                    },
                )
                msg2 = pre2.get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
                self.assertNotIn("[BLUEPRINT_FIRST|SOTA]", msg2)
                self.assertNotIn("[BLUEPRINT_MODE|SOTA]", msg2)

            # 3. Resuming an older plan injects [PLAN_HYGIENE_GATE]
            with tempfile.TemporaryDirectory() as art3:
                _, _, pre3 = run_hook(
                    "planner_pre_invocation.py",
                    {
                        "workspacePaths": [str(repo)],
                        "artifactDirectoryPath": art3,
                        "lastUserInput": "Vamos retomar o plano da fase 3 W31 agora",
                        "invocationNum": 1,
                    },
                )
                msg3 = pre3["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("[PLAN_HYGIENE_GATE]", msg3)
                self.assertIn(".agents/docs/PLANS_HUB.md", msg3)

            # 4. SessionStart surfaces PLANS_HUB.md & compact PLANS_HUB| line
            with tempfile.TemporaryDirectory() as art4:
                _, _, ss = run_hook(
                    "planner_session_start.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art4},
                )
                ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("PLANS_HUB.md", ss_msg)
                self.assertIn("PLANS_HUB|bp=1(done=0,partial=1,active=0,def=0)|sync=OK|bp_default=ON", ss_msg)


class TestBlueprintArchitectureV2(unittest.TestCase):
    """v2.19.0: Tiered mini|compact|full formats, Anti-Gaming Linter v2, lossless DONE compaction, match, transition & triage alignment."""

    def test_tiered_format_classify_scaffold_lint_and_topology(self) -> None:
        import blueprint_engine

        # T1/2-way -> mini format, 0 subagents, zero dead boilerplate
        c_mini = blueprint_engine.classify_blueprint_request("Add a small toggle switch to disable album art blur in Settings")
        self.assertEqual(c_mini["recommended_format"], "mini")
        self.assertIn("ux-product", c_mini["domain_vector"])
        self.assertEqual(c_mini["subagent_topology"]["stage_1_scouts"]["count"], 0)
        self.assertEqual(c_mini["subagent_topology"]["stage_2_blind_solvers"]["count"], 0)
        self.assertEqual(c_mini["subagent_topology"]["stage_3_cold_redteam"]["count"], 0)

        # T3/1-way -> full format, full pro [L3] topology
        c_full = blueprint_engine.classify_blueprint_request(
            "Architect a new zero-alloc real-time neural ONNX DSP pipeline with SELinux security migration"
        )
        self.assertEqual(c_full["recommended_format"], "full")
        self.assertEqual(c_full["subagent_topology"]["stage_2_blind_solvers"]["model"], "pro")

        # Scaffold mini -> no EVOLVE_RULE or PARENT_ADR|none boilerplate, <=35L
        with tempfile.TemporaryDirectory() as td_sc:
            sc_res = blueprint_engine.scaffold_blueprint(
                "Album Art Blur Toggle",
                "ALBUM_BLUR_TOGGLE",
                Path(td_sc),
                prompt="Add a toggle switch to disable album art blur in Settings",
                fmt="mini",
            )
            mini_scaffold = Path(sc_res["path"]).read_text(encoding="utf-8")
        self.assertIn("|format=mini|", mini_scaffold)
        self.assertNotIn("EVOLVE_RULE|", mini_scaffold)
        self.assertNotIn("PARENT_ADR|none", mini_scaffold)
        self.assertLessEqual(len(mini_scaffold.splitlines()), 35)

        # Complete mini blueprint passes --strict >=95 in <=30L & <=3KB
        valid_mini = "\n".join([
            "# Album Art Blur Toggle (`BP_ALBUM_BLUR_TOGGLE`)",
            "",
            "## 0. Meta & Lineage",
            "BP_META|id=BP_ALBUM_BLUR_TOGGLE|format=mini|domain_vec=ux-product:1.00|door=2-way|tier=T1|status=ACTIVE|h1_wave=W1",
            "",
            "## 1. Value & Non-Goals",
            "VALUE|Allow disabling album art blur in Settings in <=2 taps, cutting GPU overdraw >=20% on scrolling.",
            "NON_GOAL|Zero redesign of NowPlaying layout or custom shader rewrite.",
            "",
            "## 2. Decision, Trade-off & Kill Guard",
            "CANDIDATE|A|Runtime Toggle Flag|Single SharedPreferences boolean checked before blur pass.",
            "CANDIDATE|B|Compile-Time Build Flavor|Separate APK build variant without blur.",
            "REJECTED_BECAUSE|B|Doubles CI APK matrix (+100% build cost) and forces uninstall/reinstall to change preference.",
            "WINNER|Candidate A: single persisted boolean toggle in Settings w/ immediate invalidation.",
            "KILL|R1|Visual Glitch or Jank|frame_time_p99 > 16.6ms or l10n_missing > 0|Unit & UI toggle check|Default blur enabled and revert view branch.",
            "",
            "## 3. Invariants, QAS & H1 Slices",
            "BUDGET|latency=<=16.6ms/frame|mem=<=1MB|cost=<=100L diff|degraded_floor=Fallback to default blur=true if pref corrupt.",
            "QAS|Q1|Toggle blur switch OFF in Settings|Android Settings screen|Blur layer skipped immediately|<=16.6ms && 0 crashes|python3 -B .agents/scripts/check_android_cs.py --ci-parity",
            "SLICE|W1|Add pref key, Settings toggle, EN/PT-BR strings, & draw guard|Toggle persists across restart|python3 -B .agents/scripts/check_android_cs.py --ci-parity",
            "FITNESS|python3 -B .agents/scripts/check_android_cs.py --ci-parity",
            "",
        ])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "BP_ALBUM_BLUR_TOGGLE.md"
            p.write_text(valid_mini, encoding="utf-8")
            res = blueprint_engine.lint_blueprint(p, strict=True)
            self.assertEqual(res["format"], "mini")
            self.assertTrue(res["passed"], f"Expected mini BP to pass, got {res}")
            self.assertGreaterEqual(res["score"], 95)
            self.assertLessEqual(len(valid_mini.splitlines()), 30)
            self.assertLessEqual(len(valid_mini.encode("utf-8")), 3072)

    def test_anti_gaming_linter_v2(self) -> None:
        import blueprint_engine

        # 1. Adversarial 1-word tautology blueprint MUST FAIL (<50)
        garbage = "\n".join([
            "# BP_GARBAGE",
            "BP_META|id=BP_GARBAGE|format=full|domain_vec=software:1.00|door=1-way|tier=T3|status=ACTIVE|h1_wave=W1",
            "## 1. Value & Non-Goals",
            "VALUE|1ms startup " + ("x " * 70) + "makes the system super fast and scalable.",
            "WARDLEY|Commodity=a | Product=b | Custom=c",
            "NON_GOAL|none1",
            "NON_GOAL|none2",
            "## 2. Live SOTA & Blind Morphogenesis",
            "AUDIT_EXISTING|checked",
            "SOTA_SOURCES|web",
            "TRIZ_CONTRADICTION|a vs b",
            "CANDIDATE|A|optA|desc",
            "REJECTED_BECAUSE|A|bad",
            "CANDIDATE|B|optB|desc",
            "REJECTED_BECAUSE|B|slow",
            "WINNER|C",
            "## 3. Cold-Ctx Red-Team, Pre-Mortem & Kill Criteria",
            "PREMORTEM|T+6mo|fails",
            "THREAT|bad -> fix",
            "KILL|R1|fail|bad|1d|revert",
            "KILL|R2|fail|worse|1d|revert",
            "## 4. Invariants, Budgets & QAS",
            "BUDGET|latency=1ms|mem=1MB|cost=1$|degraded_floor=ok",
            "PLANES|offline=a|rt=b|async=c",
            "QAS|Q1|s|e|r|1ms|echo ok",
            "## 5. Epistemic Wave-0 Spikes",
            "SPIKE|S1|VoI=1|check|done",
            "## 6. Rolling-Wave DSM Graph",
            "### H1 Atomic Slices",
            "SLICE|W1|do|done|echo ok",
            "### H2+ Outcome Stubs",
            "STUB|W2|later",
            "## 7. Fitness Functions & Evolution",
            "FITNESS|echo ok",
        ])
        with tempfile.TemporaryDirectory() as td:
            gp = Path(td) / "BP_GARBAGE.md"
            gp.write_text(garbage, encoding="utf-8")
            gres = blueprint_engine.lint_blueprint(gp, strict=True)
            self.assertFalse(gres["passed"])
            self.assertLessEqual(gres["score"], 50, f"Garbage BP scored too high: {gres}")
            joined = " ".join(gres["issues"] + gres["blockers"])
            self.assertIn("ORI|", joined)
            self.assertIn("KCC|", joined)
            self.assertIn("IDS|", joined)
            self.assertIn("WBS|", joined)
            self.assertIn("CTB|", joined)

            # 2. `--fast` CLI flag and `||` / backticked pipes MUST NOT false-positive
            valid_pipes = "\n".join([
                "# Pipe Safe (`BP_PIPE_SAFE`)",
                "## 0. Meta & Lineage",
                "BP_META|id=BP_PIPE_SAFE|format=mini|domain_vec=software:1.00|door=2-way|tier=T1|status=ACTIVE|h1_wave=W1",
                "## 1. Value & Non-Goals",
                "VALUE|Ensure `a || b` and `--fast` tests pass in <=5s with 0 regressions.",
                "NON_GOAL|No shell rewrites outside test runner.",
                "## 2. Decision, Trade-off & Kill Guard",
                "CANDIDATE|A|Code-aware parser|Track backticks and double-pipe operators.",
                "CANDIDATE|B|Regex split|Naive split on single pipe character.",
                "REJECTED_BECAUSE|B|Mangles `a || b` boolean logic and backticked pipes causing >10% false lint failures.",
                "WINNER|Candidate A: deterministic backtick-aware pipe splitter.",
                "KILL|R1|Parser Regression|`err_count > 0 || test_fails > 0`|`<=10s` suite|Revert parser helper and restore prior split.",
                "## 3. Invariants, QAS & H1 Slices",
                "BUDGET|latency=<=10ms|mem=<=8MB|cost=<=50L|degraded_floor=Fallback to line-level report on parse error.",
                "QAS|Q1|Run plugin suite w/ `--fast` & `a || b` expressions|Local Python 3|All assertions pass|0 errors && <=10s|python3 -B tests/run_plugin_tests.py --fast",
                "SLICE|W1|Implement `_split_pipe_fields` and unit test|Zero split bugs|python3 -B tests/run_plugin_tests.py --fast",
                "FITNESS|python3 -B tests/run_plugin_tests.py --fast",
            ])
            pp = Path(td) / "BP_PIPE_SAFE.md"
            pp.write_text(valid_pipes, encoding="utf-8")
            pres = blueprint_engine.lint_blueprint(pp, strict=True)
            self.assertTrue(pres["passed"], f"Valid pipe/--fast BP failed: {pres}")
            self.assertEqual(pres["score"], 100)

    def test_compact_done_blueprints_lossless_sha256_and_hub_sync(self) -> None:
        import blueprint_engine
        import hashlib
        import tarfile

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            bp_dir = repo / ".agents" / "docs" / "blueprints"
            bp_dir.mkdir(parents=True, exist_ok=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "blueprint": {"require_by_default": True}}),
                encoding="utf-8",
            )

            def _mk(bp_id: str, status: str) -> str:
                return "\n".join([
                    f"# {bp_id}",
                    f"BP_META|id={bp_id}|format=mini|domain_vec=software:1.00|door=2-way|tier=T1|status={status}|h1_wave=W1",
                    "VALUE|Cut latency >=20% with 0 regressions.",
                    "NON_GOAL|Zero third-party deps.",
                    "CANDIDATE|A|Keep|Baseline.",
                    "CANDIDATE|B|Upgrade|Target.",
                    "REJECTED_BECAUSE|A|Fails latency budget by >25ms under peak load.",
                    "WINNER|Candidate B with deterministic checks.",
                    "KILL|R1|Regression|fail_count > 0|Unit test run|Revert commit immediately.",
                    "BUDGET|latency=<=10ms|mem=<=8MB|cost=<=50L|degraded_floor=Safe fallback.",
                    "QAS|Q1|Run suite|Local|Passes|0 errors|python3 -B tests/run_plugin_tests.py --fast",
                    "SLICE|W1|Apply change|Verified|python3 -B tests/run_plugin_tests.py --fast",
                    "FITNESS|python3 -B tests/run_plugin_tests.py --fast",
                ]) + "\n"

            done1_txt = _mk("BP_OLD_ONE", "DONE")
            done2_txt = _mk("BP_OLD_TWO", "DONE")
            keep_txt = _mk("BP_KEEP_DONE", "DONE")
            act_txt = _mk("BP_LIVE_WORK", "ACTIVE")
            (bp_dir / "BP_OLD_ONE.md").write_text(done1_txt, encoding="utf-8")
            (bp_dir / "BP_OLD_TWO.md").write_text(done2_txt, encoding="utf-8")
            (bp_dir / "BP_KEEP_DONE.md").write_text(keep_txt, encoding="utf-8")
            (bp_dir / "BP_LIVE_WORK.md").write_text(act_txt, encoding="utf-8")
            blueprint_engine.sync_plans_hub(repo)

            # Dry-run first must not delete anything
            dry = blueprint_engine.compact_done_blueprints(repo, apply=False, keep_ids={"BP_KEEP_DONE"})
            self.assertEqual(sorted(dry["candidates"]), ["BP_OLD_ONE", "BP_OLD_TWO"])
            self.assertTrue((bp_dir / "BP_OLD_ONE.md").is_file())

            # Apply compaction
            res = blueprint_engine.compact_done_blueprints(repo, apply=True, keep_ids={"BP_KEEP_DONE"})
            self.assertTrue(res["applied"], res)
            self.assertTrue(res["verified_sha256"], res)
            self.assertEqual(sorted(res["compacted"]), ["BP_OLD_ONE", "BP_OLD_TWO"])
            self.assertFalse((bp_dir / "BP_OLD_ONE.md").is_file())
            self.assertFalse((bp_dir / "BP_OLD_TWO.md").is_file())
            self.assertTrue((bp_dir / "BP_KEEP_DONE.md").is_file())
            self.assertTrue((bp_dir / "BP_LIVE_WORK.md").is_file())

            # Verify verbatim bytes & SHA-256 in tarball
            tar_p = repo / ".agents" / "docs" / "archive" / "blueprints_done.tar.gz"
            self.assertTrue(tar_p.is_file())
            with tarfile.open(tar_p, "r:gz") as tf:
                b1 = tf.extractfile("BP_OLD_ONE.md").read()
                b2 = tf.extractfile("BP_OLD_TWO.md").read()
            self.assertEqual(b1.decode("utf-8"), done1_txt)
            self.assertEqual(b2.decode("utf-8"), done2_txt)
            ledger_txt = (repo / ".agents" / "docs" / "archive" / "BLUEPRINTS_DONE_LEDGER.md").read_text(encoding="utf-8")
            self.assertIn(hashlib.sha256(b1).hexdigest()[:12], ledger_txt)
            self.assertIn("BP_OLD_TWO", ledger_txt)

            # PLANS_HUB & SessionStart reflect archived_done=2
            hub_txt = (repo / ".agents" / "docs" / "PLANS_HUB.md").read_text(encoding="utf-8")
            self.assertIn("ARCHIVED_DONE|count=2|", hub_txt)
            hyg = blueprint_engine.hygiene_audit(repo)
            self.assertTrue(hyg["ok"], hyg)
            self.assertEqual(hyg["counts"]["archived_done"], 2)

            with tempfile.TemporaryDirectory() as art:
                _, _, ss = run_hook(
                    "planner_session_start.py",
                    {"workspacePaths": [str(repo)], "artifactDirectoryPath": art},
                )
                ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("arch=2", ss_msg)

    def test_match_and_transition_blueprints(self) -> None:
        import blueprint_engine

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            bp_dir = repo / ".agents" / "docs" / "blueprints"
            bp_dir.mkdir(parents=True, exist_ok=True)
            bp_path = bp_dir / "BP_AUDIO_CACHE_ENGINE.md"
            bp_path.write_text(
                "\n".join([
                    "# Audio Cache Engine (`BP_AUDIO_CACHE_ENGINE`)",
                    "BP_META|id=BP_AUDIO_CACHE_ENGINE|format=mini|domain_vec=software:1.00|door=2-way|tier=T1|status=ACTIVE|h1_wave=W1",
                    "VALUE|Cache decoded audio frames for <=5ms lookup latency.",
                    "NON_GOAL|No network sync.",
                    "CANDIDATE|A|LRU Cache|Bounded memory cache.",
                    "CANDIDATE|B|No Cache|Decode every seek.",
                    "REJECTED_BECAUSE|B|Adds >150ms seek latency on every scrubber move.",
                    "WINNER|Candidate A bounded LRU cache.",
                    "KILL|R1|OOM|cache_mb > 64MB|Runtime monitor|Evict oldest entries.",
                    "BUDGET|latency=<=5ms|mem=<=64MB|cost=<=100L|degraded_floor=Bypass cache on pressure.",
                    "QAS|Q1|Seek cached track|Player|Frame ready|<=5ms|python3 -B tests/run_plugin_tests.py --fast",
                    "SLICE|W1|Build cache|Verified|python3 -B tests/run_plugin_tests.py --fast",
                    "FITNESS|python3 -B tests/run_plugin_tests.py --fast",
                ]) + "\n",
                encoding="utf-8",
            )
            blueprint_engine.sync_plans_hub(repo)

            m_hit = blueprint_engine.match_blueprints("Fix seek latency bug in audio cache engine LRU", repo)
            self.assertEqual(m_hit["action"], "AMEND")
            self.assertEqual(m_hit["top_match"]["id"], "BP_AUDIO_CACHE_ENGINE")

            m_miss = blueprint_engine.match_blueprints("Design new OAuth2 billing webhook service", repo)
            self.assertEqual(m_miss["action"], "CREATE")

            tr = blueprint_engine.transition_blueprint(
                bp_path,
                status="DONE",
                note="Shipped W1 LRU cache w/ 0 regressions.",
                sync_hub=True,
                repo=repo,
            )
            self.assertEqual(tr["status"], "DONE")
            self.assertTrue(tr["hub_synced"])
            updated_txt = bp_path.read_text(encoding="utf-8")
            self.assertIn("|status=DONE|", updated_txt)
            self.assertIn("HYGIENE_NOTE|Shipped W1 LRU cache w/ 0 regressions.", updated_txt)
            self.assertTrue(blueprint_engine.hygiene_audit(repo)["hub_in_sync"])

    def test_t0_tiny_copy_fix_and_domain_filename_stripping(self) -> None:
        import blueprint_engine
        import complexity_triage

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".agents").mkdir(parents=True, exist_ok=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "blueprint": {"require_by_default": True, "default_min_tier": 1}}),
                encoding="utf-8",
            )

            # 1-line UI label change is T0 tiny_fix and MUST NOT force a blueprint
            t_copy = complexity_triage.analyze_prompt(
                "Muda o texto do botão de exportar para Salvar em FLAC",
                workspace_root=repo,
            )
            self.assertEqual(t_copy["tier"], 0)
            self.assertEqual(t_copy["intent"], "tiny_fix")
            self.assertFalse(t_copy["blueprint_required_by_policy"])

            # Code filename containing 'Studio' must not misclassify software refactor as ux-product
            c_ref = blueprint_engine.classify_blueprint_request(
                "Refactor StudioBatchExporter.cs into 3 smaller partial files"
            )
            self.assertIn("software", c_ref["domain_vector"])
            self.assertNotIn("ux-product", c_ref["domain_vector"])


class TestAdversarialQaGate(unittest.TestCase):
    def test_schema_config_validation_offline_contracts_and_tamper_resistance(self) -> None:
        import qa_gate
        import schema_migrations

        cfg_default = schema_migrations.resolve_adversarial_qa_config({"schema_version": 5})
        self.assertFalse(cfg_default["enabled"])
        cfg = schema_migrations.resolve_adversarial_qa_config({"schema_version": 5, "adversarial_qa": {"enabled": True}})
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["require_cold_context_subagent"])
        self.assertTrue(cfg["strict_verify"])
        self.assertEqual(cfg["contracts_file"], ".agents/qa/regression_contracts.json")

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "theme.cs").write_text("public static bool EnableBlur = false;\n", encoding="utf-8")
            (repo / "cfg.json").write_text('{"mode": "strict", "limits": {"maxMb": 2400}}\n', encoding="utf-8")

            valid_doc = {
                "version": 1,
                "policy": "1-bug=1-permanent-automated-test-first",
                "contracts": [
                    {
                        "id": "REG-001",
                        "bug_ref": "H1",
                        "title": "Blur disabled",
                        "domain": "ui",
                        "severity": "HIGH",
                        "status": "frozen",
                        "immutable": True,
                        "mode": "offline",
                        "test_ref": "tests/t.py::test_blur",
                        "offline_checks": [
                            {"kind": "file_contains_regex", "path": "src/theme.cs", "pattern": r"EnableBlur\s*=\s*false"},
                        ],
                    },
                    {
                        "id": "REG-002",
                        "bug_ref": "H2",
                        "title": "No forbidden hack",
                        "domain": "runtime",
                        "severity": "CRITICAL",
                        "status": "frozen",
                        "immutable": True,
                        "mode": "offline",
                        "test_ref": "tests/t.py::test_clean",
                        "offline_checks": [
                            {"kind": "file_forbids_regex", "path": "src/theme.cs", "pattern": r"EnableBlur\s*=\s*true"},
                        ],
                    },
                    {
                        "id": "REG-003",
                        "bug_ref": "H3",
                        "title": "JSON limit matches",
                        "domain": "config",
                        "severity": "MEDIUM",
                        "status": "frozen",
                        "immutable": True,
                        "mode": "offline",
                        "test_ref": "tests/t.py::test_json",
                        "offline_checks": [
                            {"kind": "json_key_equals", "path": "cfg.json", "key": "limits.maxMb", "expected": 2400},
                        ],
                    },
                ],
            }

            self.assertEqual(qa_gate.validate_contracts_registry(valid_doc), [])
            ev = qa_gate.evaluate_offline_contracts(repo, valid_doc)
            self.assertTrue(ev["ok"], ev)
            self.assertEqual(ev["passed"], 3)
            self.assertEqual(ev["failed"], 0)

            # Tamper: disabling a frozen contract MUST fail schema validation
            tampered = json.loads(json.dumps(valid_doc))
            tampered["contracts"][0]["status"] = "disabled"
            val_bad = qa_gate.validate_contracts_registry(tampered)
            self.assertTrue(any("disabled" in e for e in val_bad), val_bad)

            # Waived skip without substantive reason fails; with waiver_reason passes validation
            tampered["contracts"][0]["status"] = "waived"
            self.assertTrue(len(qa_gate.validate_contracts_registry(tampered)) > 0)
            tampered["contracts"][0]["waiver_reason"] = "Superseded by v3 architecture"
            self.assertEqual(qa_gate.validate_contracts_registry(tampered), [])

            # Break the source invariant -> offline contracts MUST fail
            (repo / "src" / "theme.cs").write_text("public static bool EnableBlur = true;\n", encoding="utf-8")
            ev_broken = qa_gate.evaluate_offline_contracts(repo, valid_doc)
            self.assertFalse(ev_broken["ok"])
            self.assertEqual(ev_broken["failed"], 2)

    def test_blocking_gate_verdict_cold_prompt_cli_verify_and_pre_invocation_cue(self) -> None:
        import qa_gate

        # 1. Blocking gate ignores historical_archive but fails on live HIGH/CRITICAL/GEOM/P95/CONTRACTS
        clean_rep = {
            "findings": [{"severity": "CRITICAL", "origin": "historical_archive", "source": "exit_info_q2", "category": "exit_info_historical"}],
            "q2GateSummary": {"cpuP95Ms": 7.5, "newFatalCount": 0},
            "q3SoakSummary": {"passed": True},
        }
        g_ok = qa_gate.evaluate_qa_gate(clean_rep, contracts_eval={"ok": True, "failed": 0})
        self.assertTrue(g_ok["ok"], g_ok)
        self.assertEqual(g_ok["verdict"], "PASS")
        self.assertIn("QA_GATE|VERDICT=PASS|", g_ok["summaryLine"])

        bad_rep = {
            "findings": [
                {"severity": "HIGH", "source": "ui_tree_q1", "category": "ui_sibling_overlap"},
            ],
            "q2GateSummary": {"frameCpuP95Ms": 16.4, "exitNewFatal": 1, "totalFrames": 60},
            "q3SoakSummary": {"cycles": 8, "passed": False},
        }
        g_fail = qa_gate.evaluate_qa_gate(bad_rep, contracts_eval={"ok": False, "failed": 2})
        self.assertFalse(g_fail["ok"])
        self.assertEqual(g_fail["verdict"], "FAIL")
        self.assertGreaterEqual(len(g_fail["reasons"]), 5, g_fail["reasons"])

        # 2. Cold-context destroyer prompt strips builder rationale & embeds 1-bug=1-test + gate line
        built = qa_gate.build_cold_context_qa_prompt(
            goal="Verify Audio FX sheet under 120Hz",
            changed_files=["src/AudioSheet.cs"],
            acceptance_criteria=["Zero overlaps", "P95 <= 12ms"],
            profile={"schema_version": 5, "adversarial_qa": {"gate_command": "python3 -B .agents/agent/cavern_bug_hunter.py --vectors V12 --gate"}},
            contracts_count=12,
        )
        self.assertTrue(built["isolated"])
        self.assertTrue(built["stripBuilderHistory"])
        prompt = built["prompt"]
        self.assertIn("COLD_CONTEXT_ADVERSARIAL_QA_DESTROYER", prompt)
        self.assertIn("1-bug=1-test", prompt)
        self.assertIn("QA_GATE|VERDICT=", prompt)
        self.assertIn("src/AudioSheet.cs", prompt)

        # 3. CLI `plan_tool.py qa` + `verify_universal.py` enforcement + PreInvocation cue
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            (repo / ".agents").mkdir(parents=True, exist_ok=True)
            (repo / ".agents" / "planner_profile.json").write_text(
                json.dumps({"schema_version": 5, "adversarial_qa": {"enabled": True, "cold_context_qa_subagent": True}}),
                encoding="utf-8",
            )
            (repo / "app.py").write_text("SAFE_MODE = True\n", encoding="utf-8")

            add_res = run_script("plan_tool.py", [
                "--workspace", str(repo), "qa", "add-contract",
                "--id", "REG-APP-SAFE",
                "--bug", "B1",
                "--title", "SAFE_MODE stays True",
                "--domain", "runtime",
                "--severity", "HIGH",
                "--test-ref", "tests/test_app.py::test_safe",
                "--file", "app.py",
                "--must-match", r"SAFE_MODE\s*=\s*True",
            ])
            self.assertEqual(add_res.returncode, 0, add_res.stdout + add_res.stderr)
            self.assertIn("REG-APP-SAFE", add_res.stdout)

            st_res = run_script("plan_tool.py", ["--workspace", str(repo), "qa", "status"])
            self.assertEqual(st_res.returncode, 0, st_res.stdout + st_res.stderr)
            self.assertIn("QA_STATUS|enabled=True|contracts=1/1", st_res.stdout)

            vc_res = run_script("plan_tool.py", ["--workspace", str(repo), "qa", "verify-contracts", "--strict"])
            self.assertEqual(vc_res.returncode, 0, vc_res.stdout + vc_res.stderr)
            self.assertIn("QA_CONTRACTS|PASS|total=1|offline_pass=1|fail=0", vc_res.stdout)

            # verify_universal.py passes while contract holds, and FAILS when contract breaks!
            vu_ok = run_script(
                "verify_universal.py",
                ["--workspace", str(repo)],
                cwd=repo,
                env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1", "PLANNER_ARTIFACT_DIR": art},
            )
            self.assertEqual(vu_ok.returncode, 0, vu_ok.stdout + vu_ok.stderr)

            (repo / "app.py").write_text("SAFE_MODE = False\n", encoding="utf-8")
            vu_fail = run_script(
                "verify_universal.py",
                ["--workspace", str(repo)],
                cwd=repo,
                env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1", "PLANNER_ARTIFACT_DIR": art},
            )
            self.assertNotEqual(vu_fail.returncode, 0)
            self.assertIn("REG-APP-SAFE", vu_fail.stdout)

            # PreInvocation injects [QA_GATE|COLD_CTX] on T2+ turn
            _, _, pre_t2 = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": "1. Refactor audio pipeline\n2. Add regression tests\n3. Update docs",
                "invocationNum": 1,
            })
            self.assertIn("[QA_GATE|COLD_CTX]", json.dumps(pre_t2))


def _stamp_plugin_tests_passed() -> None:
    art_dir = (os.environ.get("PLANNER_ARTIFACT_DIR") or os.environ.get("CAVERN_ARTIFACT_DIR") or "").strip()
    if not art_dir or os.environ.get("PLANNER_NO_HOOK_STAMP"):
        return
    hook_file = Path(art_dir) / "scratch" / ".hook_state.json"
    try:
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = json.loads(hook_file.read_text(encoding="utf-8")) if hook_file.is_file() else {}
        now = time.time()
        last_pe = float(data.get("last_plugin_edit_ts") or 0.0)
        data["last_plugin_test_ts"] = max(now, last_pe)
        data["needs_plugin_tests"] = False
        hook_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    global FAST_MODE
    parser = argparse.ArgumentParser(description="Run Dynamic Fractal Planner regression tests.")
    parser.add_argument("--fast", action="store_true", help="Run in-memory single-process tests (<5s).")
    parser.add_argument("--full", action="store_true", help="Run full subprocess integration tests (default).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose per-test output (verbosity=2).")
    args, remaining = parser.parse_known_args()
    FAST_MODE = bool(args.fast and not args.full)

    loader = unittest.TestLoader()
    targets = [r for r in remaining if not r.startswith("-")]
    if targets:
        suite = loader.loadTestsFromNames(targets, sys.modules[__name__])
    else:
        suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    if result.wasSuccessful():
        _stamp_plugin_tests_passed()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
