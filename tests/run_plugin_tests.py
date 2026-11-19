#!/usr/bin/env python3
"""Comprehensive regression suite for Dynamic Fractal Planner v3.

Supports:
  --fast : Single-process in-memory execution (<5s on Termux ARM64 PRoot)
  --full : Full subprocess integration execution (default)
"""

from __future__ import annotations

import argparse
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

import complexity_triage  # noqa: E402
import log_digest  # noqa: E402
import model_mode_cache  # noqa: E402
import planner_state  # noqa: E402
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
) -> tuple[int, str, dict[str, Any]]:
    merged_env = {"PLANNER_DISABLE_TELEMETRY": "1"}
    if env_extra:
        merged_env.update(env_extra)
    if FAST_MODE:
        mod = _load_mod(HOOKS_DIR / script_name)
        rc = 0
        with _patched_env(merged_env, stdin_txt=json.dumps(payload)) as (out_buf, err_buf):
            try:
                mod.main()
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            out_txt = out_buf.getvalue().strip()
            err_txt = err_buf.getvalue().strip()
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
        [sys.executable, str(HOOKS_DIR / script_name)],
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


def init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)


class TestSchemaMigrations(unittest.TestCase):
    def test_v1_generic_fixture_migrates_losslessly_to_v3(self) -> None:
        raw = json.loads((FIXTURES_DIR / "profile_v1_generic.json").read_text(encoding="utf-8"))
        snapshot = copy.deepcopy(raw)
        migrated, ver, warnings = schema_migrations.migrate_profile(raw)

        self.assertEqual(raw, snapshot, "migrate_profile must never mutate input dict")
        self.assertEqual(ver, 3)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(warnings, [])
        self.assertEqual(migrated["custom_owner_note"], "preserve-me-losslessly")
        self.assertEqual(migrated["planner_mode"], "lean")
        self.assertEqual(migrated["read_budget"], {"max_full_read_lines": 300, "max_slice_lines": 250})
        self.assertEqual(migrated["secret_guard"], {"enabled": True})
        self.assertEqual(migrated["blast_radius"], {"simple_tier_max_files": 3})

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
        self.assertEqual(ver1, 3)
        self.assertEqual(w1, [])
        self.assertEqual(m1["build_rules"], v1["build_rules"])
        self.assertEqual(m1["file_guards"], v1["file_guards"])
        self.assertEqual(m1["diff_rules"], v1["diff_rules"])
        self.assertEqual(m1["planner_mode"], "lean")

        v2 = json.loads((FIXTURES_DIR / "profile_v2_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v2.get("schema_version"), 2)
        m2, ver2, w2 = schema_migrations.migrate_profile(v2)
        self.assertEqual(ver2, 3)
        self.assertEqual(w2, [])
        self.assertEqual(m2["schema_version"], 3)
        self.assertEqual(m2["planner_mode"], "lean")
        self.assertEqual(m2["build_rules"], v2["build_rules"])
        self.assertEqual(m2["command_guards"], v2["command_guards"])
        self.assertEqual(m2["key_parity_rules"], v2["key_parity_rules"])

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
            self.assertEqual(loaded["schema_version"], 3)
            self.assertEqual(prof_file.read_text(encoding="utf-8"), v1_text)

            res = run_script("plan_tool.py", ["--workspace", str(repo), "migrate"])
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue((agents_dir / "planner_profile.json.bak").is_file())
            disk_after = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_after["schema_version"], 3)
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
            self.assertEqual(prof["schema_version"], 3)
            self.assertEqual(prof["planner_mode"], "lean")
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
            self.assertEqual(prof["schema_version"], 3)

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

            # Circuit Breaker on 4 edits to same file without passing build
            for _ in range(4):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "edit_file", "args": {"Path": str(repo / "app.py")}},
                })
            _, _, cb_edit = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art, "lastUserInput": "Fix test", "invocationNum": 3,
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
        (repo / "CavernSamples" / "CavernizeAndroid" / "Properties" / "AndroidManifest.xml").write_text(
            '<manifest><uses-sdk android:targetSdkVersion="28" /></manifest>\n',
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

            # 5. File guard: targetSdkVersion="28"
            manifest = str(repo / "CavernSamples" / "CavernizeAndroid" / "Properties" / "AndroidManifest.xml")
            _, _, fg_bad = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)], "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": manifest, "Target": 'android:targetSdkVersion="28"', "Replacement": 'android:targetSdkVersion="34"'}},
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
        v1_cavern = json.loads((FIXTURES_DIR / "profile_v1_cavern.json").read_text(encoding="utf-8"))
        self._assert_parity_for_profile("v1_cavern_fixture", v1_cavern)

        v2_cavern = json.loads((FIXTURES_DIR / "profile_v2_cavern.json").read_text(encoding="utf-8"))
        self.assertEqual(v2_cavern.get("schema_version"), 2)
        self._assert_parity_for_profile("v2_cavern_fixture", v2_cavern)

        if self.LIVE_CAVERN_PROFILE.is_file():
            live_cavern = json.loads(self.LIVE_CAVERN_PROFILE.read_text(encoding="utf-8"))
            self.assertIn(live_cavern.get("schema_version"), (2, 3))
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
    args, remaining = parser.parse_known_args()
    FAST_MODE = bool(args.fast and not args.full)

    loader = unittest.TestLoader()
    targets = [r for r in remaining if not r.startswith("-")]
    if targets:
        suite = loader.loadTestsFromNames(targets, sys.modules[__name__])
    else:
        suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        _stamp_plugin_tests_passed()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
