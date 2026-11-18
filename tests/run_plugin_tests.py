#!/usr/bin/env python3
"""Comprehensive regression suite for the Dynamic Fractal Planner global plugin.

Covers:
1. Pure-Python chained schema migration (v1->v2 on frozen generic & Cavern fixtures,
   synthetic v1->v10 multi-hop chain, v99 graceful warning, in-memory non-mutation,
   and `plan_tool.py migrate` CLI).
2. Automatic SessionStart project bootstrap (`is_real_project_root`, shared
   `bootstrap_workspace` v2, no overwrite of existing docs, zero pollution of
   `/`, `/tmp`, `~`, `~/.gemini`, and empty folders).
3. Tolerant `EXTRA_REQUESTS.md` heading detection across hooks & verifier.
4. Plugin self-protection gate (`complexity_triage`, `planner_guard_files`,
   `planner_post_tool`, `planner_stop`, `planner_pre_invocation`, `verify_universal --plugin-tests`).
5. Full Cavern v1 fixture parity + live Cavern v2 profile parity across every script & hook.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
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
import planner_state  # noqa: E402
import schema_migrations  # noqa: E402


def run_hook(script_name: str, payload: dict[str, Any], env_extra: dict[str, str] | None = None) -> tuple[int, str, dict[str, Any]]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
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
    parsed: dict[str, Any] = {}
    if out_txt:
        try:
            parsed = json.loads(out_txt)
        except Exception:
            parsed = {"_raw": out_txt}
    return res.returncode, res.stderr.strip(), parsed


def run_script(script_name: str, args: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *args],
        cwd=str(cwd) if cwd else str(PLUGIN_ROOT),
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
    def test_v1_generic_fixture_migrates_losslessly(self) -> None:
        raw = json.loads((FIXTURES_DIR / "profile_v1_generic.json").read_text(encoding="utf-8"))
        snapshot = copy.deepcopy(raw)
        migrated, ver, warnings = schema_migrations.migrate_profile(raw)

        self.assertEqual(raw, snapshot, "migrate_profile must never mutate input dict")
        self.assertEqual(ver, 2)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(warnings, [])
        self.assertEqual(migrated["custom_owner_note"], "preserve-me-losslessly")

        # Verify legacy commands normalized into build_rules
        rules = migrated.get("build_rules") or []
        cmds = {r.get("label"): r for r in rules}
        self.assertIn("verify_command", cmds)
        self.assertEqual(cmds["verify_command"]["command"], "npm test")
        self.assertEqual(cmds["verify_command"]["trigger_extensions"], [".ts", ".tsx"])
        self.assertIn("verify_non_code_command", cmds)
        self.assertTrue(cmds["verify_non_code_command"].get("run_without_build"))

        # Verify memory_feedback defaults & domains preservation
        mf = migrated["memory_feedback"]
        self.assertTrue(mf["enabled"])
        self.assertEqual(mf["min_code_files"], 2)
        self.assertTrue(len(mf["docs"]) >= 1)
        self.assertIn("payments", migrated["domains"])
        self.assertFalse(migrated["domains"]["payments"]["is_ship"])
        self.assertTrue(migrated["domains"]["release-ops"]["is_ship"])
        self.assertEqual(migrated["domains"]["payments"]["keywords"], ["stripe*", "webhook*", "billing"])

    def test_v1_cavern_fixture_migrates_losslessly(self) -> None:
        raw = json.loads((FIXTURES_DIR / "profile_v1_cavern.json").read_text(encoding="utf-8"))
        self.assertNotIn("schema_version", raw)
        migrated, ver, warnings = schema_migrations.migrate_profile(raw)

        self.assertEqual(ver, 2)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(warnings, [])
        self.assertEqual(len(migrated["build_rules"]), len(raw["build_rules"]))
        self.assertEqual(migrated["build_rules"], raw["build_rules"])
        self.assertEqual(migrated["memory_feedback"], raw["memory_feedback"])
        self.assertEqual(migrated["domains"], raw["domains"])
        self.assertEqual(migrated["file_guards"], raw["file_guards"])
        self.assertEqual(migrated["command_guards"], raw["command_guards"])
        self.assertEqual(migrated["diff_rules"], raw["diff_rules"])
        self.assertEqual(migrated["key_parity_rules"], raw["key_parity_rules"])

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
        self.assertEqual(out["keep"], "yes")
        self.assertEqual(out["hops"], [f"{i}->{i + 1}" for i in range(1, 10)])
        for v in range(2, 11):
            self.assertTrue(out[f"field_v{v}"])

    def test_newer_schema_warns_without_crashing(self) -> None:
        future = {"schema_version": 99, "project_name": "FutureRepo", "future_field": [1, 2, 3]}
        out, ver, warnings = schema_migrations.migrate_profile(future)
        self.assertEqual(ver, 99)
        self.assertEqual(out["schema_version"], 99)
        self.assertEqual(out["future_field"], [1, 2, 3])
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
            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(prof_file.read_text(encoding="utf-8"), v1_text, "load_project_profile must NOT modify disk")

            res = run_script("plan_tool.py", ["--workspace", str(repo), "migrate"])
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue((agents_dir / "planner_profile.json.bak").is_file())
            disk_after = json.loads(prof_file.read_text(encoding="utf-8"))
            self.assertEqual(disk_after["schema_version"], 2)

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

        with tempfile.TemporaryDirectory() as pkg_td:
            pkg_dir = Path(pkg_td)
            (pkg_dir / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
            self.assertTrue(planner_state.is_real_project_root(pkg_dir))

        with tempfile.TemporaryDirectory() as src_td:
            src_dir = Path(src_td)
            sub = src_dir / "src" / "core"
            sub.mkdir(parents=True)
            (sub / "app.py").write_text("print('ok')\n", encoding="utf-8")
            self.assertTrue(planner_state.is_real_project_root(src_dir))

    def test_session_start_auto_bootstrap_and_non_overwrite(self) -> None:
        # 1. Fresh package.json project
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
            for rel in planner_state.BOOTSTRAP_REQUIRED_FILES:
                self.assertTrue((repo / rel).is_file(), f"Missing {rel}")
            prof = json.loads((repo / ".agents" / "planner_profile.json").read_text(encoding="utf-8"))
            self.assertEqual(prof["schema_version"], 2)
            self.assertTrue(any(r["command"] == "npm test" for r in prof["build_rules"]))

        # 2. Existing files never overwritten; only missing files created
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo = Path(td)
            init_git_repo(repo)
            docs_dir = repo / ".agents" / "docs"
            docs_dir.mkdir(parents=True)
            sentinel_hist = "# Custom History\nKeep forever.\n"
            sentinel_extra = "# Queue\n\n## Pending\n- Fix issue #42\n"
            (docs_dir / "PROJECT_HISTORY.md").write_text(sentinel_hist, encoding="utf-8")
            (docs_dir / "EXTRA_REQUESTS.md").write_text(sentinel_extra, encoding="utf-8")

            rc, err, parsed = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            msg = parsed["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("SAVED_IDEAS.md", msg)
            self.assertIn("planner_profile.json", msg)
            self.assertIn("ATTENTION — Pending extra requests", msg)
            self.assertEqual((docs_dir / "PROJECT_HISTORY.md").read_text(encoding="utf-8"), sentinel_hist)
            self.assertEqual((docs_dir / "EXTRA_REQUESTS.md").read_text(encoding="utf-8"), sentinel_extra)

        # 3. Empty non-project folder never polluted
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
            ("## pending\n1. Item C\n", ["1. Item C"]),
            ("### Queue:\n- Item D\n## Completed\n- Done E\n", ["Item D"]),
            ("## extra requests\n- Item F\n", ["Item F"]),
            ("# Pending\n- Item G\n", ["Item G"]),
            ("# Extra Request Queue (Parallel `/btw` Threads)\n\n## Pending Extra Requests\n(No pending requests.)\n", []),
            ("## Queue\n(none)\n<!-- comment -->\n", []),
        ]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            docs = repo / ".agents" / "docs"
            docs.mkdir(parents=True)
            extra = docs / "EXTRA_REQUESTS.md"
            for content, expected_sub in variants:
                extra.write_text(content, encoding="utf-8")
                _, items = planner_state.get_pending_extra_requests(repo, {})
                self.assertEqual(len(items), len(expected_sub), f"Failed for heading content:\n{content}")
                for exp, got in zip(expected_sub, items):
                    self.assertIn(exp, got)


class TestSelfProtectionGate(unittest.TestCase):
    def test_triage_detects_plugin_self(self) -> None:
        res = complexity_triage.analyze_prompt(
            "Update ~/.gemini/config/plugins/dynamic-fractal-planner/hooks/planner_stop.py and schema_migrations.py",
            PLUGIN_ROOT,
        )
        self.assertIn("plugin-self", res["detected_domains"])
        self.assertGreaterEqual(res["tier"], 2)

    def test_plugin_edit_gate_post_tool_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            ws = Path(td)
            init_git_repo(ws)
            target_plugin_file = str(PLUGIN_ROOT / "hooks" / "planner_stop.py")

            # 1. Without plan -> planner_guard_files denies outside-repo plugin edit
            rc, _, resp = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Edit planner_stop.py in dynamic-fractal-planner plugin",
                "toolCall": {"name": "edit_file", "args": {"Path": target_plugin_file, "Target": "a", "Replacement": "b"}},
            })
            self.assertEqual(rc, 0)
            self.assertEqual(resp.get("decision"), "deny")
            self.assertIn("PLAN GATE", resp.get("reason", ""))

            # 2. Frozen fixture protected with force_ask
            rc, _, resp_fix = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(FIXTURES_DIR / "profile_v1_generic.json"), "Target": "a", "Replacement": "b"}},
            })
            self.assertEqual(rc, 0)
            self.assertEqual(resp_fix.get("decision"), "force_ask")

            # 3. With active plan -> guard allows
            init_res = run_script("plan_tool.py", [
                "--dir", art, "--workspace", str(ws), "init",
                "--tier", "2", "--goal", "Update plugin", "--req", "REQ-01: Update stop hook",
            ])
            self.assertEqual(init_res.returncode, 0, init_res.stderr)

            rc, _, resp_ok = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "lastUserInput": "Update plugin",
                "toolCall": {"name": "edit_file", "args": {"Path": target_plugin_file, "Target": "a", "Replacement": "b"}},
            })
            self.assertEqual(rc, 0)
            self.assertEqual(resp_ok.get("decision"), "allow")

            # 4. PostToolUse marks needs_plugin_tests=True on plugin .py/.json/.md edit
            rc, _, _ = run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": "~/.gemini/config/plugins/dynamic-fractal-planner/hooks/planner_stop.py"}},
            })
            self.assertEqual(rc, 0)
            hook_file = Path(art) / "scratch" / ".hook_state.json"
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertTrue(hs.get("needs_plugin_tests"))
            self.assertGreater(float(hs.get("last_plugin_edit_ts") or 0), 0.0)

            # 5. Running verify_universal.py WITHOUT --plugin-tests does NOT clear needs_plugin_tests
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"python3 {SCRIPTS_DIR / 'verify_universal.py'}"}},
            })
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertTrue(hs.get("needs_plugin_tests"))

            # 6. PreInvocation preserves needs_plugin_tests across new turns
            run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "lastUserInput": "New turn question?",
                "invocationNum": 1,
            })
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertTrue(hs.get("needs_plugin_tests"))

            # 7. Stop hook: fullyIdle=false allows stop; otherwise blocks stop when needs_plugin_tests=True
            _, _, stop_idle_false = run_hook("planner_stop.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "fullyIdle": False,
                "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(stop_idle_false, {})

            _, _, stop_blocked = run_hook("planner_stop.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(stop_blocked.get("decision"), "continue")
            self.assertIn("run_plugin_tests.py", stop_blocked.get("reason", ""))

            # 8. PostToolUse clears needs_plugin_tests ONLY on run_plugin_tests.py or verify_universal.py --plugin-tests
            time.sleep(0.01)
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"python3 {TESTS_DIR / 'run_plugin_tests.py'}"}},
            })
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertFalse(hs.get("needs_plugin_tests"))
            self.assertGreaterEqual(float(hs.get("last_plugin_test_ts") or 0), float(hs.get("last_plugin_edit_ts") or 0))

            # Clear plan open checks cleanly to verify stop allows once tests & plan done
            plan_data = json.loads((Path(art) / "scratch" / "plan_state.json").read_text(encoding="utf-8"))
            for m in plan_data.get("modules") or []:
                run_script("plan_tool.py", ["--dir", art, "--workspace", str(ws), "done", m["id"]])
            run_script("plan_tool.py", ["--dir", art, "--workspace", str(ws), "req-verify", "REQ-01", "--evidence", "tested"])
            _, _, stop_ok = run_hook("planner_stop.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "fullyIdle": True,
                "terminationReason": "NO_TOOL_CALL",
            })
            self.assertEqual(stop_ok, {})

            # Also verify PostToolUse clears needs_plugin_tests on `verify_universal.py --plugin-tests`
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "edit_file", "args": {"Path": str(PLUGIN_ROOT / "README.md")}},
            })
            self.assertTrue(json.loads(hook_file.read_text(encoding="utf-8")).get("needs_plugin_tests"))
            time.sleep(0.01)
            run_hook("planner_post_tool.py", {
                "workspacePaths": [str(ws)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": f"python3 {SCRIPTS_DIR / 'verify_universal.py'} --plugin-tests"}},
            })
            self.assertFalse(json.loads(hook_file.read_text(encoding="utf-8")).get("needs_plugin_tests"))

    def test_verify_universal_plugin_tests_flag(self) -> None:
        if os.environ.get("PLANNER_PLUGIN_TEST_DEPTH") == "1":
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
                env_extra={
                    "PLANNER_ARTIFACT_DIR": art,
                    "PLANNER_PLUGIN_TEST_DEPTH": "1",
                },
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("plugin tests passed", res.stdout)
            hs = json.loads(hook_file.read_text(encoding="utf-8"))
            self.assertFalse(hs.get("needs_plugin_tests"))
            self.assertGreaterEqual(float(hs.get("last_plugin_test_ts") or 0), float(hs.get("last_plugin_edit_ts") or 0))


class TestCavernV1AndV2Parity(unittest.TestCase):
    LIVE_CAVERN_PROFILE = Path("/root/.gemini/antigravity/scratch/Cavern/.agents/planner_profile.json")

    def _make_cavern_repo(self, td: str, profile_data: dict[str, Any], use_temp_dotnet_guard: bool = True) -> tuple[Path, Path]:
        repo = Path(td)
        init_git_repo(repo)
        (repo / ".agents" / "docs").mkdir(parents=True)
        (repo / ".agents" / "docs" / "PROJECT_HISTORY.md").write_text("# History\n", encoding="utf-8")
        (repo / ".agents" / "docs" / "SAVED_IDEAS.md").write_text("# Ideas\n", encoding="utf-8")
        (repo / ".agents" / "docs" / "EXTRA_REQUESTS.md").write_text(
            "## Pending Extra Requests\n(No pending requests.)\n", encoding="utf-8"
        )
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
            'class AndroidStrings {\n'
            '  static Dictionary<string, string> CreateEnglish() => new() {\n'
            '    ["Hello"] = "Hello",\n'
            '  };\n'
            '  static void ApplyPortuguese(Dictionary<string, string> d) {\n'
            '    d["Hello"] = "Olá";\n'
            '  }\n'
            '}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

        prof = copy.deepcopy(profile_data)
        dotnet_marker = repo / ".fake_dotnet"
        if use_temp_dotnet_guard:
            dotnet_marker.write_text("#!/bin/sh\n", encoding="utf-8")
            for g in prof.get("command_guards") or []:
                cond = g.get("conditions") or {}
                if "exists_path" in cond:
                    cond["exists_path"] = str(dotnet_marker)
                if "not_exists_glob" in cond:
                    cond["not_exists_glob"] = str(repo / ".nonexistent_android_pack*")

        (repo / ".agents" / "planner_profile.json").write_text(
            json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return repo, dotnet_marker

    def _assert_parity_for_profile(self, label: str, profile_data: dict[str, Any]) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as art:
            repo, _ = self._make_cavern_repo(td, profile_data)

            # 1. Complexity Triage parity
            tri = complexity_triage.analyze_prompt(
                "Corrigir layout One UI na tela Play, ajustar decoder TrueHD em Cavern.Format, resolver AudioTrack e gerar APK release",
                repo,
            )
            self.assertEqual(tri["tier"], 3, label)
            for dom in ("ui-oneui9", "dsp-format", "media-native", "build-deliver"):
                self.assertIn(dom, tri["detected_domains"], f"{label} missing {dom}")
            self.assertEqual(tri["recommended_modules"][-1]["domain"], "build-deliver", label)

            # 2. SessionStart parity
            rc, err, ss = run_hook("planner_session_start.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
            })
            self.assertEqual(rc, 0, err)
            ss_msg = ss["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("csproj=2.8.28 | CI=2.8.${{ github.run_number }}", ss_msg)
            self.assertNotIn("Schema warning", ss_msg)

            # 3. PreInvocation parity (not plan_ready -> injects Phase 0 triage + runaway guard)
            multi_prompt = (
                "1. Ajustar decoder em Cavern.Format\n"
                "2. Corrigir layout One UI na tela Play\n"
                "3. Gerar APK release no GitHub Actions"
            )
            rc, _, pre = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": multi_prompt,
                "invocationNum": 1,
            })
            self.assertEqual(rc, 0)
            self.assertIn("injectSteps", pre, f"{label}: unexpected PreInvocation output: {pre}")
            pre_msg = pre["injectSteps"][0]["systemMessage"]["systemMessage"]
            self.assertIn("Dynamic Planner Triage", pre_msg)

            # 4. Guard Files: targetSdkVersion != 28 -> force_ask, == 28 -> allow, Plan Gate -> deny
            manifest = str(repo / "CavernSamples" / "CavernizeAndroid" / "Properties" / "AndroidManifest.xml")
            _, _, fg_bad = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {
                    "name": "edit_file",
                    "args": {
                        "Path": manifest,
                        "Target": 'android:targetSdkVersion="28"',
                        "Replacement": 'android:targetSdkVersion="34"',
                    },
                },
            })
            self.assertEqual(fg_bad.get("decision"), "force_ask", label)

            with tempfile.TemporaryDirectory() as art_fg:
                _, _, fg_good = run_hook("planner_guard_files.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art_fg,
                    "toolCall": {
                        "name": "edit_file",
                        "args": {
                            "Path": manifest,
                            "Target": 'android:targetSdkVersion="28"',
                            "Replacement": 'android:targetSdkVersion="28" android:label="Cavern"',
                        },
                    },
                })
                self.assertEqual(fg_good.get("decision"), "allow", label)

            _, _, fg_gate = run_hook("planner_guard_files.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": multi_prompt,
                "toolCall": {
                    "name": "edit_file",
                    "args": {
                        "Path": str(repo / "Cavern.Format" / "Sample.cs"),
                        "Target": "a",
                        "Replacement": "b",
                    },
                },
            })
            self.assertEqual(fg_gate.get("decision"), "deny", label)
            self.assertIn("PLAN GATE", fg_gate.get("reason", ""))

            # Runaway read containment check (>=4 reads without plan)
            for idx in range(4):
                run_hook("planner_post_tool.py", {
                    "workspacePaths": [str(repo)],
                    "artifactDirectoryPath": art,
                    "toolCall": {"name": "read_file", "args": {"Path": str(repo / "Cavern.Format" / f"File{idx}.cs")}},
                })
            _, _, pre_runaway = run_hook("planner_pre_invocation.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "lastUserInput": multi_prompt,
                "invocationNum": 2,
            })
            self.assertIn("PLANNER GUARD", pre_runaway["injectSteps"][0]["systemMessage"]["systemMessage"])

            # 5. Guard Command parity
            _, _, cg_apk = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "DOTNET_GCHeapHardLimit=20000000 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 dotnet build CavernSamples/CavernizeAndroid/CavernizeAndroid.csproj"}},
            })
            self.assertEqual(cg_apk.get("decision"), "deny", label)

            _, _, cg_env = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "dotnet build CavernSamples/Reusable/Cavernize.Logic/Cavernize.Logic.csproj"}},
            })
            self.assertEqual(cg_env.get("decision"), "deny", label)

            _, _, cg_ok = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "DOTNET_GCHeapHardLimit=20000000 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 /root/.dotnet/dotnet build CavernSamples/Reusable/Cavernize.Logic/Cavernize.Logic.csproj"}},
            })
            self.assertEqual(cg_ok.get("decision"), "allow", label)

            # Command rewrite check
            _, _, cg_rw = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "conversationId": "conv123",
                "toolCall": {"name": "run_command", "args": {"CommandLine": "python3 .agents/scripts/plan_tool.py status"}},
            })
            self.assertEqual(cg_rw.get("decision"), "allow")
            self.assertIn("PLANNER_ARTIFACT_DIR=", cg_rw.get("overwrite", {}).get("CommandLine", ""))

            # Pending EXTRA_REQUESTS blocks git commit/push
            extra_file = repo / ".agents" / "docs" / "EXTRA_REQUESTS.md"
            extra_file.write_text("## Pending Extra Requests\n- Handle btw task\n", encoding="utf-8")
            _, _, cg_extra = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'test'"}},
            })
            self.assertEqual(cg_extra.get("decision"), "deny", label)
            self.assertIn("pending extra requests", cg_extra.get("reason", "").lower())
            extra_file.write_text("## Pending Extra Requests\n(No pending requests.)\n", encoding="utf-8")

            # Active plan blocks commit until non-ship modules & REQs verified; allows when only is_ship remains
            init_res = run_script("plan_tool.py", [
                "--dir", art, "--workspace", str(repo), "init", "--json", "-",
            ], cwd=repo) if False else subprocess.run(
                [
                    sys.executable, str(SCRIPTS_DIR / "plan_tool.py"),
                    "--dir", art, "--workspace", str(repo), "init", "--json", "-",
                ],
                input=json.dumps({
                    "goal": "Test Cavern Parity",
                    "tier": 2,
                    "requirements": [
                        {"id": "REQ-01", "summary": "Test DSP"},
                        {"id": "REQ-02", "summary": "Commit & Push"},
                    ],
                    "modules": [
                        {"id": "MOD-DSP-FORMAT", "title": "DSP", "domain": "dsp-format", "linked_reqs": ["REQ-01"]},
                        {"id": "MOD-AUDIT", "title": "Audit", "domain": "audit", "linked_reqs": ["REQ-01", "REQ-02"]},
                        {"id": "MOD-SHIP", "title": "Ship", "domain": "build-deliver", "linked_reqs": ["REQ-02"]},
                    ],
                }),
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(init_res.returncode, 0, init_res.stderr)

            _, _, cg_plan_block = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'wip'"}},
            })
            self.assertEqual(cg_plan_block.get("decision"), "deny", label)

            run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "done", "MOD-DSP-FORMAT"])
            run_script("plan_tool.py", ["--dir", art, "--workspace", str(repo), "req-verify", "REQ-01", "--evidence", "ok"])
            _, _, cg_ship_allow = run_hook("planner_guard_command.py", {
                "workspacePaths": [str(repo)],
                "artifactDirectoryPath": art,
                "toolCall": {"name": "run_command", "args": {"CommandLine": "git commit -m 'ready'"}},
            })
            self.assertEqual(cg_ship_allow.get("decision"), "allow", label)

            # 6. verify_universal parity (diff_rules & key_parity_rules)
            (repo / "Cavern.Format" / "Bad.cs").write_text("namespace Foo;\nclass Bad { void F(object x) { if (x is not null) {} } }\n", encoding="utf-8")
            (repo / "CavernSamples" / "CavernizeAndroid" / "BadUi.cs").write_text("class BadUi { void F() { var d = new AlertDialog(); var s = new Spinner(); } }\n", encoding="utf-8")
            (repo / "CavernSamples" / "CavernizeAndroid" / "Language" / "AndroidStrings.cs").write_text(
                'class AndroidStrings {\n'
                '  static Dictionary<string, string> CreateEnglish() => new() {\n'
                '    ["Hello"] = "Hello",\n'
                '    ["MissingPtKey"] = "Missing",\n'
                '  };\n'
                '  static void ApplyPortuguese(Dictionary<string, string> d) {\n'
                '    d["Hello"] = "Olá";\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            v_fail = run_script(
                "verify_universal.py",
                ["--workspace", str(repo)],
                cwd=repo,
                env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"},
            )
            self.assertNotEqual(v_fail.returncode, 0, label)
            self.assertIn("is not", v_fail.stdout)
            self.assertIn("File-scoped namespace", v_fail.stdout)
            self.assertIn("AlertDialog", v_fail.stdout)
            self.assertIn("Spinner", v_fail.stdout)
            self.assertIn("MissingPtKey", v_fail.stdout)

            # Fix violations and verify clean pass
            (repo / "Cavern.Format" / "Bad.cs").unlink()
            (repo / "CavernSamples" / "CavernizeAndroid" / "BadUi.cs").unlink()
            (repo / "CavernSamples" / "CavernizeAndroid" / "Language" / "AndroidStrings.cs").write_text(
                'class AndroidStrings {\n'
                '  static Dictionary<string, string> CreateEnglish() => new() {\n'
                '    ["Hello"] = "Hello",\n'
                '    ["MissingPtKey"] = "Missing",\n'
                '  };\n'
                '  static void ApplyPortuguese(Dictionary<string, string> d) {\n'
                '    d["Hello"] = "Olá";\n'
                '    d["MissingPtKey"] = "Faltando";\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )
            v_pass = run_script(
                "verify_universal.py",
                ["--workspace", str(repo)],
                cwd=repo,
                env_extra={"PLANNER_RUNNING_PLUGIN_TESTS": "1"},
            )
            self.assertEqual(v_pass.returncode, 0, v_pass.stdout + v_pass.stderr)
            self.assertIn("UNIVERSAL VERIFY PASS", v_pass.stdout)

    def test_cavern_v1_fixture_and_live_v2_parity(self) -> None:
        v1_cavern = json.loads((FIXTURES_DIR / "profile_v1_cavern.json").read_text(encoding="utf-8"))
        self._assert_parity_for_profile("v1_cavern_fixture", v1_cavern)

        if self.LIVE_CAVERN_PROFILE.is_file():
            v2_cavern = json.loads(self.LIVE_CAVERN_PROFILE.read_text(encoding="utf-8"))
            self.assertEqual(v2_cavern.get("schema_version"), 2)
            self._assert_parity_for_profile("live_v2_cavern", v2_cavern)


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
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        _stamp_plugin_tests_passed()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
