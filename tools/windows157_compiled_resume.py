#!/usr/bin/env python3
"""Migrate only the latest Windows157 x64 compiled stage3 tree from232 to235."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import apply_restored_patches as patches
import migrate_windows_snapshot as files
import windows157_tools as tools
import windows157_ninja_state as ninja_state
import windows157_crubit_resume as origin

MANIFEST = "build/windows/windows157-stage3-checkpoint.json"
MANIFEST_SHA256 = "f097a99e383d855987eac7e9c9fe5c66d353abfbb155d3949ee2ca38256c0d71"
WORKFLOW = "build-win-x64-157-compiled-resume"
RECEIPT = ".chromix-windows157-compiled-migration.json"
PENDING = ".chromix-windows157-compiled-in-progress"
BLOCKER = ".chromix-layer-in-progress"
VIEW = "windows157-compiled-recipe"
RECIPE_BASE = "8147b39e782ed933ee2e9cb8b8e3fa734c53399b"
TARGETS = ("base/i18n/icubridge/default_icu_locale.h",
           "third_party/blink/renderer/core/timezone/timezone_controller.cc",
           "components/embedder_support/user_agent_utils.cc")
RECOVERY_CODE = {
    "tools/windows157_ninja_state.py": "3709b6dd13acd547637c880ad46de3a46cbb3d1b97ca5e1dfdcb02cb3c284d9f",
    "build/windows/ci-stage.ps1": "33ed7d7ee77573dc9aa5269a5599b31311cda454468a43046ed92651f6d9b438",
    "tools/gen_windows_arm64_workflow.py": "76b16bb6ce60f0014ac71a8c161534d033aefbacc90aadba146c59c13c78e899",
    ".github/workflows/build-win-x64-157-compiled-resume.yml": "b626dd46ddb75d767bbfca485dfff6ad19d0999a0c665615ea8244c8506de7ee"
}
RECIPE_FILES = ("build/args.windows.gn", "build/windows/lite-tarball-files/v8/test/torque/test-torque.tq",
                "CHROMIUM_WINDOWS_VERSION",
                "UNGOOGLED_WINDOWS_VERSION", "tools/windows157_fresh.py", "tools/windows157_tools.py",
                "tools/install_windows_metagen_bindings.py", "tools/merge_gn_args.py",
                "tools/verify_patch_stack.py", "tools/apply_restored_patches.py",
                "tools/migrate_windows_snapshot.py", "tools/windows157_crubit_resume.py",
                "tools/verify_windows_snapshot_source.py", "tools/platform_pins.py",
                "tools/validate_windows_snapshot.py", "tools/validate_posix_snapshot.py",
                "tools/download_windows_snapshot.py", "tools/download_posix_snapshot.py")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(repo):
    raw = files._file(repo, MANIFEST).read_bytes()
    if digest(raw) != MANIFEST_SHA256:
        raise ValueError("compiled checkpoint manifest changed")
    return json.loads(raw)


def expected(m):
    return dict(repository=m["repository"], head_sha=m["head_sha"], run_id=m["run_id"],
                stage=3, attempt=1, job_id=m["job_id"], arch="x64", workflow=Path(m["workflow_path"]).stem,
                artifacts=[{k: v for k, v in a.items() if k != "expires_at"} | {"expired": False}
                           for a in m["artifacts"]])


def metadata(client, m, now=None):
    now = now or datetime.now(timezone.utc)
    run = client.get(f'/actions/runs/{m["run_id"]}')
    required = dict(id=m["run_id"], head_sha=m["head_sha"], head_branch="main", event="workflow_dispatch",
                    status="completed", conclusion="failure", run_attempt=1, workflow_id=m["workflow_id"], path=m["workflow_path"])
    if (any(type(run.get(k)) is not type(v) or run.get(k) != v for k, v in required.items())
            or any(run.get(k, {}).get("id") != m["repository_id"] or run.get(k, {}).get("full_name") != m["repository"]
                   for k in ("repository", "head_repository"))):
        raise ValueError("compiled donor run mismatch")
    jobs = client.items(f'/actions/runs/{m["run_id"]}/attempts/1/jobs', "jobs")
    selected = [j for j in jobs if j.get("id") == m["job_id"] or j.get("name") == "Windows157 x64 stage 3"]
    want = dict(id=m["job_id"], run_id=m["run_id"], head_sha=m["head_sha"], run_attempt=1,
                name="Windows157 x64 stage 3", status="completed", conclusion="failure")
    if len(selected) != 1 or any(selected[0].get(k) != v for k, v in want.items()):
        raise ValueError("compiled donor job/attempt mismatch")
    for job in jobs:
        match = re.fullmatch(r"Windows157 x64 stage (\d+)", job.get("name", ""))
        if match and int(match[1]) > 3 and job.get("conclusion") != "skipped":
            raise ValueError("a later compiled donor stage exists")
    for name, conclusion in {"Run stage 3": "failure", "Ensure build tree snapshot": "success",
                             "Upload fingerprint diagnostics": "success", "Upload Crubit admission evidence": "success",
                             "Upload final bundle": "skipped", **{f"Upload tree part {i}": "success" for i in range(1, 5)}}.items():
        steps = [s for s in selected[0]["steps"] if s.get("name") == name]
        if len(steps) != 1 or steps[0].get("status") != "completed" or steps[0].get("conclusion") != conclusion:
            raise ValueError("compiled checkpoint upload incomplete: " + name)
    listed = client.items(f'/actions/runs/{m["run_id"]}/artifacts', "artifacts")
    trees = [a for a in listed if "tree-s3-" in a.get("name", "")]
    if len(trees) != 2 or any(re.search(r"tree-s(?:[4-9]|1[0-2])-", a.get("name", "")) for a in listed):
        raise ValueError("latest compiled tree set changed")
    for pin in m["artifacts"] + m["evidence_artifacts"]:
        matches = [a for a in listed if a.get("id") == pin["id"]]
        if (len(matches) != 1 or any(matches[0].get(k) != v for k, v in pin.items())
                or matches[0].get("expired") is not False
                or datetime.fromisoformat(pin["expires_at"].replace("Z", "+00:00")) <= now
                or any(matches[0].get("workflow_run", {}).get(k) != v for k, v in
                       dict(id=m["run_id"], head_sha=m["head_sha"], repository_id=m["repository_id"],
                            head_repository_id=m["repository_id"], head_branch="main").items())):
            raise ValueError("compiled artifact identity/expiry mismatch")
    return expected(m)


def checked_download(m, origin_report, download):
    if origin_report != expected(m):
        raise ValueError("not the latest stage3 compiled donor")
    want = dict(status="success", phase="complete", publication="published", repository=m["repository"],
                run_id=m["run_id"], head_sha=m["head_sha"])
    if any(download.get(k) != v for k, v in want.items()):
        raise ValueError("compiled download not authenticated/published")
    pins = expected(m)["artifacts"]
    if [{k: a.get(k) for k in pins[0]} for a in download.get("artifacts", [])] != pins:
        raise ValueError("compiled downloaded artifact set mismatch")
    volumes = download.get("volumes", [])
    if (len(volumes) != 2 or [v.get("name") for v in volumes] != ["tree.7z.001", "tree.7z.002"]
            or [v.get("artifact_id") for v in volumes] != [a["id"] for a in pins]
            or any(type(v.get("size_in_bytes")) is not int or v["size_in_bytes"] <= 0
                   or not re.fullmatch("[0-9a-f]{64}", v.get("sha256", "")) for v in volumes)
            or download.get("total_size_in_bytes") != sum(v["size_in_bytes"] for v in volumes)):
        raise ValueError("compiled volume verification incomplete")


def selected_recipe(repo):
    result = subprocess.run([sys.executable, str(repo / "tools/patch_selection.py"), "--repo", str(repo),
                             "--platform", "windows", "--json"], check=True, capture_output=True, text=True)
    selected = json.loads(result.stdout)
    return selected, [(entry["path"], files._file(repo, entry["path"]).read_bytes()) for entry in selected["patches"]]


def preparation_key(repo):
    result = subprocess.run([sys.executable, str(repo / "tools/patch_selection.py"), "--repo", str(repo),
                             "--platform", "windows", "--key", "windows"], check=True, capture_output=True, text=True)
    key = result.stdout.strip()
    if not re.fullmatch("[0-9a-f]{64}", key):
        raise ValueError("invalid Windows preparation key")
    return key


def validate_selection(repo, m):
    selected, raw = selected_recipe(repo)
    old = m["source_receipt"]["identity"]
    if (len(raw) != 235 or selected["patches"] != m["selected_patches"]
            or selected["patches"][:232] != old["series"]["patches"]
            or digest(files._file(repo, "patches/series").read_bytes()) != m["root_series_sha256"]):
        raise ValueError("require exact root224 and unchanged232 prefix plus reviewed235")
    root_names = [l.split("#", 1)[0].strip() for l in files._file(repo, "patches/series").read_text().splitlines()]
    if len([n for n in root_names if n]) != 224:
        raise ValueError("root series is not224")
    root_series = files._file(repo, "patches/series").read_bytes()
    def canonical(entries, selection):
        replacements = {"patches/" + Path(e["path"]).name: e["path"] for e in selection["overrides"]}
        lines = root_series.decode().splitlines(keepends=True)
        mapped = []
        names = []
        for line in lines:
            name = line.split("#", 1)[0].strip()
            if name:
                names.append(replacements.get(name, name))
                line = line.replace(name, replacements.get(name, name), 1)
            mapped.append(line)
        if [e["path"] for e in entries[:224]] != names:
            raise ValueError("canonical root224 override order mismatch")
        text = "".join(mapped)
        if not text.endswith("\n"):
            text += "\n"
        text += "".join(e["path"] + "\n" for e in entries[224:])
        return digest(text.encode())
    if (canonical(old["series"]["patches"], old["selection"]) != old["series"]["sha256"]
            or canonical(selected["patches"], selected["selection"]) != selected["series_sha256"]):
        raise ValueError("canonical effective Windows series digest mismatch")
    for key in ("version", "platform", "core_commit", "core_gitlink", "platform_commit", "overrides"):
        if selected["selection"].get(key) != old["selection"].get(key):
            raise ValueError("Windows source recipe changed: " + key)
    if selected["selection"]["additions"]["patches"][:-3] != old["selection"]["additions"]["patches"]:
        raise ValueError("Windows additions prefix changed")
    for (name, data), pin in zip(raw[-3:], m["additions"], strict=True):
        _, entries = patches.transform_patch(data, set(), [])
        if name != pin["patch"] or digest(data) != pin["sha256"] or entries != [(pin["target"], "modify", None)]:
            raise ValueError("unreviewed Windows migration patch surface")
    return selected, raw


def checkout(repo):
    git = files._host_git((repo,))
    head, tree = files._tree(git, repo, os.environ.get("GITHUB_SHA") or None)
    files._tracked(repo, tree)
    # Scoped dependencies are pinned to exact reviewed blobs, not a repository-wide path exemption.
    base = origin.git_tree(git, repo, RECIPE_BASE)
    scoped = set(RECIPE_FILES) | {name for name in base if name.startswith("build/windows/")
                                 and name != "build/windows/ci-stage.ps1"}
    for name in scoped:
        if tree.get(name) != base.get(name):
            raise ValueError("unreviewed Windows preparation tool input: " + name)
    from platform_pins import load_pins
    current = load_pins(repo, "windows")
    base_pins = dict(re.findall(r'(?m)^\s*(\w+) = "([^"\r\n]+)"', origin.blob(git, repo, RECIPE_BASE, "build/ungoogled-revisions.psd1").decode()))
    for key in ("WindowsChromiumVersion", "WindowsUngoogledVersion", "WindowsUngoogledCommit",
                "UngoogledWindowsVersion", "UngoogledWindowsCommit"):
        actual = current[key]
        if actual != base_pins[key]:
            raise ValueError("Windows platform pin changed: " + key)
    if current["ChromiumVersion"] != "157.0.8103.78":
        raise ValueError("compiled platform version mismatch")
    for name, expected_hash in RECOVERY_CODE.items():
        if digest(files._file(repo, name).read_bytes().replace(b"\r\n", b"\n")) != expected_hash:
            raise ValueError("compiled recovery orchestration bytes changed: " + name)
    # Source validation uses a pinned private Windows recipe; unrelated platform files are not executable inputs.
    return git, head


def recipe_view(repo, view, git):
    import io
    import zipfile
    tree = origin.git_tree(git, repo, RECIPE_BASE)
    if not view.exists():
        view.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(files._git(git, repo, "archive", "--format=zip", RECIPE_BASE))) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                name = patches._relative(item.filename)
                path = view / name
                path.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item)
                if name == "build/windows/lite-tarball-files/v8/test/torque/test-torque.tq":
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                path.write_bytes(data)
                if os.name != "nt":
                    mode = 0o666 if name == "build/windows/lite-tarball-files/v8/test/torque/test-torque.tq" else int(tree[name][0], 8) & 0o777
                    path.chmod(mode)
    from verify_windows_snapshot_source import _inventory
    if _inventory(view) != set(tree):
        raise ValueError("compiled Windows recipe view inventory changed")
    for name, (_, kind, oid) in tree.items():
        raw = files._file(view, name).read_bytes()
        if kind != "blob" or oid not in {hashlib.sha1(f"blob {len(b)}\0".encode() + b).hexdigest()
                                         for b in (raw, raw.replace(b"\r\n", b"\n"))}:
            raise ValueError("compiled Windows recipe view content changed: " + name)
    return preparation_key(view)


def output_state(root):
    result = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            files._safe(Path(directory) / name, directory=True)
        for name in names:
            path = files._file(Path(directory), name)
            info = path.stat()
            with path.open("rb") as stream:
                value = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError("compiled output changed while hashing")
            result[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns, info.st_mode, value)
    return result


def retained_gn_contract(view, work):
    """The Windows checkpoint is never relocated; inspect its canonical paths offline too."""
    import merge_gn_args
    prefix = "C:/c/chromix/src"
    if os.name == "nt" and work.resolve().as_posix().casefold() != "c:/c/chromix":
        raise ValueError("compiled Windows checkpoint root must remain C:/c/chromix")
    overlay = ('# Windows157 fresh native build; never disable PGO for tarball sources.\n'
               'chrome_pgo_phase = 2\nhost_cpu = "x64"\n'
               f'v8_metagen_libclang_so = "{prefix}/third_party/llvm-build/Release+Asserts/bin/libclang.dll"\n'
               f'v8_metagen_libclang_bindings_dir = "{prefix}/third_party/llvm-build/Release+Asserts/bindings/python"\n')
    if files._file(work, "windows157-native.gn").read_text() != overlay:
        raise ValueError("retained compiled GN canonical overlay differs from donor")
    inputs = [work / "tooling/ungoogled-chromium/flags.gn",
              work / "tooling/ungoogled-chromium-windows/flags.windows.gn",
              view / "build/args.windows.gn", work / "windows157-native.gn"]
    keys, values = [], {}
    for path in inputs:
        order, parsed = merge_gn_args.parse(path)
        keys.extend(key for key in order if key not in keys)
        values.update(parsed)
    expected = "\n".join([merge_gn_args.GENERATED_HEADER, *[values[key] for key in keys]]) + "\n"
    if files._file(work / "src", "out/Chromix/args.gn").read_text() != expected:
        raise ValueError("retained compiled GN arguments differ from exact recipe")


def compiled_contract(work, m, *, migrated=False):
    src = work / "src"
    for name in (PENDING, BLOCKER, ".chromix-patch-in-progress", ".chromix-domain-substitution-in-progress",
                 ".chromix-upstream-restored.json"):
        if (src / name).exists():
            raise ValueError("interrupted or wrong compiled source state: " + name)
    for name, value in {".chromix-source-unpacked": m["version"], ".chromix-ungoogled-core": m["core"],
                        ".chromix-ungoogled-windows": m["windows"], ".chromix-binaries-pruned": m["core"],
                        ".chromix-domain-substituted": m["core"]}.items():
        if files._file(src, name).read_text().strip() != value:
            raise ValueError("compiled source pin mismatch: " + name)
    if not migrated:
        for name in (".chromix-patches", ".chromix-patch-selection"):
            if files._file(src, name).read_text().strip() != m["old_preparation_key"]:
                raise ValueError("compiled old232 preparation key mismatch")
        ready = "|".join((m["version"], m["core"], m["windows"], m["old_preparation_key"]))
        if files._file(src, ".chromix-source-ready").read_text().strip() != ready:
            raise ValueError("compiled donor ready marker changed")
        receipt = json.loads(files._file(work, ".chromix-windows157-crubit-win32-resume.json").read_text())
        if receipt != m["parent_admission"]:
            raise ValueError("complete compiled parent admission mismatch")
    if files._file(src, "build/toolchain/win/midl.py").stat().st_size == 0:
        raise ValueError("compiled MIDL script missing")
    if tools.sha(src / "build/toolchain/win/midl.py") != "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde":
        raise ValueError("compiled repaired/domain-substituted MIDL mismatch")
    tools.metagen.validate_sources(src, m["version"])
    out = files._safe(src / "out/Chromix", directory=True)
    for name in ("build.ninja", "args.gn", "gn.exe", ".ninja_log", ".ninja_deps"):
        if files._file(out, name).stat().st_size == 0:
            raise ValueError("compiled checkpoint lacks retained GN/Ninja state")
    ninja_state.validate(out, files._file(src, "third_party/ninja/ninja.exe"))
    if not any(p.is_file() and p.stat().st_size > 0 for p in (out / "obj").rglob("*.obj")):
        raise ValueError("compiled checkpoint has no objects")
    args = files._file(out, "args.gn").read_text()
    for key, expected_value in (("target_cpu", '"x64"'), ("host_cpu", '"x64"'), ("chrome_pgo_phase", "2")):
        values = re.findall(r"(?m)^\s*" + key + r"\s*=\s*([^\r\n#]+)", args)
        if [v.strip() for v in values] != [expected_value]:
            raise ValueError("compiled native PGO2 argument mismatch: " + key)
    for relative in ("out/Chromix/gn.exe", "third_party/ninja/ninja.exe", "third_party/rust-toolchain/bin/bindgen.exe",
                     tools.CRUBIT_ROOT + "/bin/cc_bindings_from_rs.exe"):
        tools.pe_executable(src / relative)
    profile = src / "chrome/build/pgo_profiles" / tools.PGO["x64"]
    if tools.file(src / "chrome/build/win64.pgo.txt").read_text().strip() != profile.name:
        raise ValueError("compiled PGO selector mismatch")
    if tools.file(Path(str(profile) + ".chromix-sha256")).read_text().strip() != tools.sha(profile):
        raise ValueError("compiled PGO bytes/proof mismatch")
    return out


def verify_hashes(src, expected_hashes):
    for name, expected_hash in expected_hashes.items():
        path = files._file(src, name, missing=expected_hash is None)
        actual = digest(path.read_bytes()) if path.exists() else None
        if actual != expected_hash:
            raise ValueError("complete source preimage/postimage mismatch: " + name)


def apply_one(stage, item, reverse, root):
    patch = root / "stack.patch"
    patch.write_bytes(item[1])
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CEILING_DIRECTORIES=str(root))
    command = ["git", "-c", "core.autocrlf=false", "apply", "--no-index", "--whitespace=nowarn"]
    result = subprocess.run(command + (["--reverse"] if reverse else []) + [str(patch)], cwd=stage,
                            env=env, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("private complete stack replay failed: " + item[0] + ": " + result.stderr.decode(errors="replace"))


def private_migration(src, stack):
    if len(stack) != 235 or tuple(x[2][0][0] for x in stack[-3:]) != TARGETS:
        raise ValueError("private replay requires complete Windows235 stack")
    names = {n for _, _, entries in stack for n, _, _ in entries}
    before = patches._snapshot(src, names)
    with tempfile.TemporaryDirectory(prefix="windows157-compiled-private-") as temporary:
        root = Path(temporary).resolve()
        stage = root / "src"
        stage.mkdir()
        for name, (raw, _) in before.items():
            if raw is not None:
                p = stage / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw.replace(b"\r\n", b"\n"))
        def snapshot():
            return {n: (stage / n).read_bytes() if (stage / n).exists() else None for n in names}
        old = snapshot()
        for reverse, sequence in ((True, reversed(stack[:232])), (False, stack[:232])):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        if snapshot() != old:
            raise ValueError("complete old232 roundtrip changed source")
        for item in stack[-3:]:
            apply_one(stage, item, False, root)
        revised = snapshot()
        if {n for n in names if revised[n] != old[n]} != set(TARGETS):
            raise ValueError("migration changed files outside the exact three sources")
        for reverse, sequence in ((True, reversed(stack)), (False, stack)):
            for item in sequence:
                apply_one(stage, item, reverse, root)
        if snapshot() != revised:
            raise ValueError("complete new235 roundtrip changed source")
        for name, (raw, info) in before.items():
            path = src / name
            if (path.read_bytes() if path.exists() else None) != raw or info and path.stat().st_mtime_ns != info.st_mtime_ns:
                raise ValueError("source changed during private migration")
        return before, revised


def atomic_bytes(path, raw, mode=None):
    files._safe(path, missing=True)
    fd, temporary = tempfile.mkstemp(prefix=".compiled-publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def publish_sources(src, before, revised):
    if not (src / PENDING).is_file() or not (src / BLOCKER).is_file():
        raise ValueError("compiled publication requires both transaction blockers")
    for name in TARGETS:
        raw, info = before[name]
        if files._file(src, name).read_bytes() != raw or (src / name).stat().st_mtime_ns != info.st_mtime_ns:
            raise ValueError("compiled source changed before publication")
    for name in TARGETS:
        atomic_bytes(src / name, revised[name], stat.S_IMODE(before[name][1].st_mode))


def source_stack(repo, work, m):
    validate_selection(repo, m)
    core = work / "tooling/ungoogled-chromium"
    tooling = work / "tooling/ungoogled-chromium-windows"
    git = files._host_git((repo, work))
    for root, commit, submodule in ((core, m["core"], None), (tooling, m["windows"], m["core"])):
        _, tree = files._tree(git, root, commit)
        files._tracked(root, tree, core_pin=submodule)
    # Run the fixed recipe's selector in its own process; main's POSIX-only manifest changes are not Windows inputs.
    command = [sys.executable, "-c", "import json,sys; from pathlib import Path; import apply_restored_patches as p; "
               "i,s,l=p._load(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),'windows',src=Path(sys.argv[4])); "
               "print(json.dumps({'identity':i,'stack':[(n,b.hex(),e) for n,b,e in s],"
               "'lite':{n:(b.hex(),m) for n,(b,m) in l.items()}}))",
               str(repo), str(core), str(tooling), str(work / "src")]
    env = dict(os.environ, PYTHONPATH=str(repo / "tools"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(command, env=env, cwd=repo / "tools", check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    identity = data["identity"]
    stack = [(n, bytes.fromhex(b), [tuple(e) for e in entries]) for n, b, entries in data["stack"]]
    lite = {n: (bytes.fromhex(b), mode) for n, (b, mode) in data["lite"].items()}
    old = m["source_receipt"]["identity"]
    if identity["lite"] != old["lite"]:
        raise ValueError("compiled domain/lite inputs changed: lite")
    for key, root, filename in (("regex", core, "domain_regex.list"),
                                ("list", tooling, "domain_substitution.list")):
        expected_path = "C:/c/chromix/tooling/" + root.name + "/" + filename
        if (old[key]["path"].replace("\\", "/") != expected_path
                or identity[key]["path"] != str(root / filename)
                or identity[key]["sha256"] != old[key]["sha256"]):
            raise ValueError("compiled domain tooling path/hash changed: " + key)
        identity[key] = dict(old[key])
    if identity["series"]["patches"][:232] != old["series"]["patches"]:
        raise ValueError("effective domain-substituted Windows232 prefix changed")
    for name, (raw, _) in lite.items():
        if files._file(work / "src", name).read_bytes() != raw:
            raise ValueError("compiled lite payload changed")
    return identity, stack, core, tooling


def preflight(repo, view, work, m):
    src = work / "src"
    if (work / RECEIPT).exists():
        raise ValueError("compiled migration already applied")
    out = compiled_contract(work, m)
    retained_gn_contract(view, work)
    state = output_state(src / "out")
    host_paths = ("out/Chromix/gn.exe", "third_party/ninja/ninja.exe", "third_party/rust-toolchain/bin/bindgen.exe",
                  tools.CRUBIT_ROOT + "/bin/cc_bindings_from_rs.exe", "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
                  "third_party/llvm-build/Release+Asserts/bin/llvm-profdata.exe")
    host_state = files._capture(src, host_paths)
    host_hashes = {name: tools.sha(src / name) for name in host_paths}
    protected = files._capture(src, {"out/Chromix/args.gn", "out/Chromix/build.ninja", "out/Chromix/.ninja_log",
                                    "out/Chromix/.ninja_deps", "chrome/build/pgo_profiles/" + tools.PGO["x64"],
                                    "chrome/build/pgo_profiles/" + tools.PGO["x64"] + ".chromix-sha256"})
    original_state = files._capture(src, set(m["source_receipt"]["outputs"]) | set(TARGETS)
                                   | {".chromix-source-ready", ".chromix-patches", ".chromix-patch-selection"})
    verify_hashes(src, m["source_receipt"]["outputs"])
    verify_hashes(src, {a["target"]: a["before"] for a in m["additions"]})
    identity, stack, core, tooling = source_stack(view, work, m)
    # Full source receipts bind every old affected file; the newly touched header has its own preimage.
    observed_receipts = []
    for path in (work / "fingerprint-diagnostics").glob("source-*.json"):
        if json.loads(files._file(path.parent, path.name).read_text()) == m["source_receipt"]:
            observed_receipts.append(path.name)
    if not observed_receipts:
        raise ValueError("original full compiled232 source receipt missing")
    before, revised = private_migration(src, stack)
    for pin in m["additions"]:
        if digest(revised[pin["target"]]) != pin["after"]:
            raise ValueError("private postimage differs from reviewed patch")
    if output_state(src / "out") != state:
        raise ValueError("compiled outputs changed during private replay")
    files._unchanged(src, original_state)
    return {"before": before, "revised": revised, "identity": identity, "state": state,
            "host_state": host_state, "host_hashes": host_hashes, "protected": protected,
            "original_state": original_state, "observed_receipts": observed_receipts,
            "ninja_state": ninja_state.validate(out, src / "third_party/ninja/ninja.exe")}


def migrate(repo, view, work, m, target_sha):
    verified = preflight(repo, view, work, m)
    src = work / "src"
    out = src / "out/Chromix"
    before, revised = verified["before"], verified["revised"]
    identity, state = verified["identity"], verified["state"]
    host_state, host_hashes = verified["host_state"], verified["host_hashes"]
    protected, observed_receipts = verified["protected"], verified["observed_receipts"]
    files._unchanged(src, verified["original_state"])
    backup = work / "windows157-compiled-origin"
    backup.mkdir()
    for name in TARGETS:
        path = backup / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before[name][0])
    for name in (".chromix-source-ready", ".chromix-patches", ".chromix-patch-selection"):
        (backup / name).write_bytes(files._file(src, name).read_bytes())
    for name in (PENDING, BLOCKER):
        with (src / name).open("x", encoding="utf-8") as stream:
            stream.write("windows157-compiled232-to235\n")
    publish_sources(src, before, revised)
    verify_hashes(src, {n: digest(raw) if raw is not None else None for n, raw in revised.items()})
    key = preparation_key(view)
    report = {"schema_version": 1, "status": "verified", "method": "full-old232-new235-private-reverse-forward",
              "qualification": "Complete affected source bytes and full patch stack; not full Chromium attestation",
              "domain_substituted": True, "patch_count": 235, "identity": identity,
              "outputs": {n: digest(raw) if raw is not None else None for n, raw in revised.items()}}
    receipt = dict(schema_version=1, status="migrated", phase="compiled-pgo2-gn-ninja-retained", arch="x64",
                   version=m["version"], donor=expected(m), target_sha=target_sha, preparation_key=key,
                   previous_preparation_key=m["old_preparation_key"], changed_source_files=list(TARGETS),
                   source=report, output_files=len(state), outputs_preserved=True, host_hashes=host_hashes,
                   prior_source_receipts=observed_receipts,
                   ninja_state=verified["ninja_state"],
                   consumer_run_id=os.environ.get("GITHUB_RUN_ID"), consumer_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"))
    atomic_bytes(work / RECEIPT, (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode())
    diagnostic = work / "fingerprint-diagnostics/windows157-compiled-source.json"
    atomic_bytes(diagnostic, (json.dumps(report, sort_keys=True, indent=2) + "\n").encode())
    for name in (".chromix-patches", ".chromix-patch-selection"):
        atomic_bytes(src / name, (key + "\n").encode())
    atomic_bytes(src / ".chromix-source-ready", ("|".join((m["version"], m["core"], m["windows"], key)) + "\n").encode())
    files._unchanged(src, protected)
    files._unchanged(src, host_state)
    if output_state(src / "out") != state:
        raise ValueError("compiled output changed during atomic publication")
    (src / PENDING).unlink()
    (src / BLOCKER).unlink()
    return receipt


def resume(repo, view, work, m, sha):
    compiled_contract(work, m, migrated=True)
    retained_gn_contract(view, work)
    receipt = json.loads(files._file(work, RECEIPT).read_text())
    key = preparation_key(view)
    if (receipt.get("status") != "migrated" or receipt.get("target_sha") != sha or receipt.get("donor") != expected(m)
            or receipt.get("preparation_key") != key or receipt.get("changed_source_files") != list(TARGETS)
            or receipt.get("source", {}).get("patch_count") != 235
            or receipt.get("consumer_run_id") != os.environ.get("GITHUB_RUN_ID")
            or receipt.get("consumer_attempt") != os.environ.get("GITHUB_RUN_ATTEMPT")):
        raise ValueError("compiled continuation identity mismatch")
    for name in (".chromix-patches", ".chromix-patch-selection"):
        if files._file(work / "src", name).read_text().strip() != key:
            raise ValueError("compiled continuation marker mismatch")
    ready = "|".join((m["version"], m["core"], m["windows"], key))
    if files._file(work / "src", ".chromix-source-ready").read_text().strip() != ready:
        raise ValueError("compiled ready identity changed")
    verify_hashes(work / "src", receipt["source"]["outputs"])
    verify_hashes(work / "src", receipt["host_hashes"])
    source_stack(view, work, m)
    with tempfile.TemporaryDirectory(prefix="windows157-compiled-proof-") as temporary:
        report = Path(temporary) / "source.json"
        subprocess.run([sys.executable, str(view / "tools/verify_patch_stack.py"), "--src", str(work / "src"),
                        "--repo", str(view), "--core", str(work / "tooling/ungoogled-chromium"),
                        "--platform-tooling", str(work / "tooling/ungoogled-chromium-windows"),
                        "--platform", "windows", "--output", str(report)], check=True, timeout=600)
        actual = json.loads(report.read_text())
    for key, folder, filename in (("regex", "ungoogled-chromium", "domain_regex.list"),
                                   ("list", "ungoogled-chromium-windows", "domain_substitution.list")):
        old = receipt["source"]["identity"][key]
        if (actual["identity"][key]["sha256"] != old["sha256"]
                or actual["identity"][key]["path"] != str(work / "tooling" / folder / filename)
                or old["path"].replace("\\", "/") != "C:/c/chromix/tooling/" + folder + "/" + filename):
            raise ValueError("continuation tooling identity path/hash changed")
        actual["identity"][key] = dict(old)
    if actual["outputs"] != receipt["source"]["outputs"] or actual["identity"] != receipt["source"]["identity"]:
        raise ValueError("complete continuation source proof mismatch")
    return receipt


def consumer():
    if (os.environ.get("GITHUB_REPOSITORY") != "xiaozhou26/Chromix" or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_WORKFLOW_REF") != f"xiaozhou26/Chromix/.github/workflows/{WORKFLOW}.yml@refs/heads/main"
            or not re.fullmatch(r"build-(?:[4-9]|1[0-2])", os.environ.get("GITHUB_JOB", ""))
            or not re.fullmatch("[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[1-9][0-9]*", os.environ.get("GITHUB_RUN_ATTEMPT", ""))
            or os.environ.get("GITHUB_RUN_ID") == "44591948738"):
        raise ValueError("compiled recovery requires its main-only stage4..12 workflow")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("metadata", "migrate", "resume"), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--origin", type=Path)
    parser.add_argument("--download", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        consumer()
        repo = args.repo.resolve()
        tools.metagen.validate_runtime(tools.metagen.runtime_info())
        m = load(repo)
        git, sha = checkout(repo)
        validate_selection(repo, m)
        if args.phase == "metadata":
            from validate_windows_snapshot import Client
            result = metadata(Client(m["repository"], os.environ.get("GH_TOKEN", "")), m)
        else:
            if not args.workdir:
                raise ValueError("compiled workdir required")
            work = args.workdir.resolve()
            view = work.parent / VIEW
            recipe_view(repo, view, git)
            if args.phase == "migrate":
                if os.environ.get("GITHUB_JOB") != "build-4" or not args.origin or not args.download:
                    raise ValueError("compiled migration requires exact donor download at stage4")
                checked_download(m, json.loads(args.origin.read_text()), json.loads(args.download.read_text()))
                result = migrate(repo, view, work, m, sha)
            else:
                result = resume(repo, view, work, m, sha)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        failure = {"status": "rejected", "phase": args.phase, "error_type": type(error).__name__,
                   "error": str(error)}
        if args.workdir:
            log = args.workdir / "src/out/Chromix/.ninja_log"
            if log.is_file() and not log.is_symlink():
                with log.open("rb") as stream:
                    prefix = stream.read(64)
                failure["ninja_log_header"] = ninja_state.header_diagnostic(prefix)
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(failure, stream, sort_keys=True, indent=2)
        except OSError:
            pass
        print(f"Windows157 compiled checkpoint rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
