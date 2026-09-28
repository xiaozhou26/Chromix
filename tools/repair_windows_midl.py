"""Repair byte-pinned Windows restored MIDL wrappers without touching source TLBs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
from types import MappingProxyType


SCRIPT = "build/toolchain/win/midl.py"
ORIGINAL_SHA256 = "0c0062cc6b73e1d0874e2a3304498f48ad1cb552d11d49eea223319051dde831"
REPAIRED_SHA256 = "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde"
# The transform is architecture-independent; each donor still needs an exact pin.
IDENTITIES = {
    "x64": {
        "chromium_version": "153.0.8010.47",
        "ungoogled_commit": "31e6f2dd3bb2f113800d25ae359f024684addb51",
        "head_sha": "657b9731b68aae35d4ee02428684ab8bdceb9181",
        "platform": "windows", "arch": "x64",
        "repository": "ungoogled-software/ungoogled-chromium-windows",
        "repository_id": 177210827,
        "head_branch": "153.0.8010.47-1.1", "event": "push",
        "workflow_path": ".github/workflows/build-x64.yml",
        "run_id": 35059013905,
        "artifact_id": 10523508661, "artifact_name": "build-artifact",
        "artifact_digest": "sha256:d5ae2b64ba9f819613482a9321107946b60b8d44bc2adad13ee9206c9dab238c",
        "artifact_size_in_bytes": 15713545950,
    },
}
# Artifact 10915484727 has the same patched/domain-substituted bytes as 153.
WINDOWS154_ORIGINAL_SHA256 = "0c0062cc6b73e1d0874e2a3304498f48ad1cb552d11d49eea223319051dde831"
WINDOWS154_REPAIRED_SHA256 = "466824620fd2348229c24d477d114e4c7acc0a5e75a8f1b162729809a0dfccde"
WINDOWS154_RECORDS = (
    MappingProxyType({
        "identity": MappingProxyType({
            "chromium_version": "154.0.8037.57",
            "ungoogled_commit": "800d0bb5078472e4442c1fd73373172754a60939",
            "head_sha": "fc387c7527f875ca73c82ed4907fccaa86808c9a",
            "platform": "windows", "arch": "x64",
            "repository": "ungoogled-software/ungoogled-chromium-windows",
            "repository_id": 177210827,
            "head_branch": "154.0.8037.57-1.1", "event": "push",
            "workflow_path": ".github/workflows/build-x64.yml",
            "run_id": 36093095228,
            "artifact_id": 10915484727, "artifact_name": "build-artifact",
            "artifact_digest": "sha256:7a6ba27fa2d056759d1e635f486e68cbfed36ef2d73ee201527e1ddb52d0d4a4",
            "artifact_size_in_bytes": 15716545319,
        }),
        "before_sha256": WINDOWS154_ORIGINAL_SHA256,
        "after_sha256": WINDOWS154_REPAIRED_SHA256,
    }),
    MappingProxyType({
        "identity": MappingProxyType({
            "chromium_version": "154.0.8037.57",
            "ungoogled_commit": "800d0bb5078472e4442c1fd73373172754a60939",
            "head_sha": "fc387c7527f875ca73c82ed4907fccaa86808c9a",
            "platform": "windows", "arch": "arm64",
            "repository": "ungoogled-software/ungoogled-chromium-windows",
            "repository_id": 177210827,
            "head_branch": "154.0.8037.57-1.1", "event": "push",
            "workflow_path": ".github/workflows/build-arm.yml",
            "run_id": 36093095856,
            "artifact_id": 10939470078, "artifact_name": "build-artifact-arm",
            "artifact_digest": "sha256:4f6e341e4a9dec0b2ffc5e7dab81d8bebc4074b91b55acfa78f6e9b443c25133",
            "artifact_size_in_bytes": 16009896290,
        }),
        "before_sha256": WINDOWS154_ORIGINAL_SHA256,
        "after_sha256": WINDOWS154_REPAIRED_SHA256,
    }),
)
BEFORE = b"""            open(file_path, 'wb').close()
        shutil.copy(file_path, outdir)
"""
AFTER = b"""            open(file_path, 'wb').close()
        # Restored zero-byte TLBs need native MIDL, not GUID substitution.
        if (sys.platform == 'win32' and source_file == tlb
                and os.path.getsize(file_path) == 0):
            source_exists = False
        shutil.copy(file_path, outdir)
"""


COMPARE_BEFORE = b"        midl_output_dir, outdir, common_files\n"
COMPARE_AFTER = b"        midl_output_dir, outdir, common_files, shallow=False\n"
RECORD = ".chromix-windows-midl-repair.json"
WINDOWS = os.name == "nt"
_STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


def _transform(payload: bytes, before_sha256: str, after_sha256: str) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    if digest == after_sha256:
        return payload
    if digest != before_sha256:
        raise ValueError(f"unknown restored Windows MIDL script: sha256={digest}")
    if payload.count(BEFORE) != 1 or payload.count(COMPARE_BEFORE) != 1:
        raise ValueError("ambiguous restored Windows MIDL replacement")
    repaired = payload.replace(BEFORE, AFTER, 1).replace(COMPARE_BEFORE, COMPARE_AFTER, 1)
    if hashlib.sha256(repaired).hexdigest() != after_sha256:
        raise ValueError("unexpected repaired Windows MIDL hash")
    return repaired


def transform(payload: bytes) -> bytes:
    """Accept only the exact Windows153 upstream or already-repaired bytes."""
    return _transform(payload, ORIGINAL_SHA256, REPAIRED_SHA256)


def transform154(payload: bytes) -> bytes:
    """Accept the verified Windows154 donor bytes, not raw Chromium tag bytes."""
    return _transform(payload, WINDOWS154_ORIGINAL_SHA256, WINDOWS154_REPAIRED_SHA256)


def _exact_identity(identity: dict, expected) -> bool:
    return (isinstance(identity, dict) and identity == expected
            and all(type(identity[key]) is type(value) for key, value in expected.items()))


def _windows154_record(identity: dict, arch: str, records: tuple):
    # Records are reviewed code pins, never derived from the restore receipt.
    if type(records) is not tuple:
        raise ValueError("Windows154 MIDL pins must be immutable explicit records")
    matches = []
    for record in records:
        if (not isinstance(record, MappingProxyType)
                or set(record) != {"identity", "before_sha256", "after_sha256"}
                or not isinstance(record["identity"], MappingProxyType)
                or set(record["identity"]) != set(IDENTITIES["x64"])
                or any(type(record["identity"][key]) is not type(value)
                       for key, value in IDENTITIES["x64"].items())
                or record["identity"]["chromium_version"] != "154.0.8037.57"
                or record["before_sha256"] != WINDOWS154_ORIGINAL_SHA256
                or record["after_sha256"] != WINDOWS154_REPAIRED_SHA256):
            raise ValueError("unknown Windows154 MIDL repair record")
        if (record["identity"]["platform"] == "windows"
                and record["identity"]["arch"] == arch
                and _exact_identity(identity, record["identity"])):
            matches.append(record)
    if len(matches) != 1:
        raise ValueError("unverified Windows154 MIDL upstream identity")
    return matches[0]


def _check_info(path: Path, info, *, directory=False) -> None:
    mode = getattr(info, "st_mode", None)
    nlink = getattr(info, "st_nlink", None)
    attributes = getattr(info, "st_file_attributes", 0)
    if (type(mode) is not int or stat.S_ISLNK(mode)
            or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode))
            or not directory and nlink != 1):
        raise ValueError(f"linked or unsafe Windows MIDL path: {path}: "
                         f"st_mode={mode!r}, st_nlink={nlink!r}, st_file_attributes={attributes!r}")


def _safe_stat(path: Path):
    if ".." in path.parts:
        raise ValueError(f"unsafe Windows MIDL traversal: {path}")
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            return None
        _check_info(parent, info, directory=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _check_info(path, info)
    return info


def _identity(info):
    return tuple(getattr(info, key) for key in _STAT_FIELDS)


def _read_identity(path: Path, info, *, cross_api=False):
    if not WINDOWS:
        return _identity(info)
    for key in (*_STAT_FIELDS, "st_birthtime_ns"):
        value = getattr(info, key, None)
        if (type(value) is not int
                or key in ("st_dev", "st_ino", "st_mode", "st_nlink") and value <= 0
                or key == "st_size" and value < 0):
            raise ValueError(f"incomplete Windows MIDL file identity: {path}: {key}={value!r}")
    identity = _identity(info)
    # Windows path ctime can be BirthTime while handle ctime is ChangeTime.
    return (identity[:-1] if cross_api else identity) + (info.st_birthtime_ns,)


def _check_identity(path: Path, left, right, phase: str, *, cross_api=False) -> None:
    before = _read_identity(path, left, cross_api=cross_api)
    after = _read_identity(path, right, cross_api=cross_api)
    fields = _STAT_FIELDS
    if WINDOWS:
        fields = (fields[:-1] if cross_api else fields) + ("st_birthtime_ns",)
    differences = [f"{key}={a!r}->{b!r}" for key, a, b in zip(fields, before, after) if a != b]
    if differences:
        raise ValueError(f"Windows MIDL script changed {phase}: {path}: " + ", ".join(differences))


def _read(path: Path, *, limit=65536):
    before = _safe_stat(path)
    if before is None:
        raise ValueError(f"Windows MIDL script disappeared: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        _check_info(path, opened)
        _check_identity(path, before, opened, "before reading (P0/H0)", cross_api=True)
        payload = stream.read(limit + 1)
        after = _safe_stat(path)
        final = os.fstat(stream.fileno())
        _check_info(path, final)
        if len(payload) > limit:
            raise ValueError(f"Windows MIDL script oversized during reading: {path}: "
                             f"bytes_read={len(payload)}, limit={limit}")
        if after is None:
            raise ValueError(f"Windows MIDL script disappeared during reading: {path}")
        _check_identity(path, before, after, "during reading (P0/P1)")
        _check_identity(path, opened, final, "during reading (H0/H1)")
        _check_identity(path, after, final, "during reading (P1/H1)", cross_api=True)
    return payload, before


def _replace(path: Path, original: bytes, repaired: bytes, info) -> bool:
    fd, name = tempfile.mkstemp(prefix=".midl-repair-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(repaired)
            stream.flush()
            os.fsync(stream.fileno())
        written, temporary_info = _read(temporary)
        if written != repaired:
            raise ValueError("Windows MIDL temporary content changed")
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        current, current_info = _read(path)
        if current == repaired:
            return False
        if current != original or _identity(current_info) != _identity(info):
            raise ValueError("Windows MIDL script changed before replacement")
        written, final_info = _read(temporary)
        if (written != repaired or (final_info.st_dev, final_info.st_ino)
                != (temporary_info.st_dev, temporary_info.st_ino)):
            raise ValueError("Windows MIDL temporary content changed before replacement")
        _safe_stat(path)
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _local(root: Path, value: str) -> Path:
    if (not value or "\0" in value or "\\" in value or ":" in value
            or "$" in value or Path(value).is_absolute() or PureWindowsPath(value).drive
            or ".." in Path(value).parts):
        raise ValueError(f"unsafe Windows MIDL graph path: {value}")
    return root / value


def _tokens(value: str) -> list[str]:
    # GN escapes literal spaces, colons and dollars with '$'. Variables are not paths.
    return [re.sub(r"\$([ $:])", r"\1", token)
            for token in re.findall(r"(?:\$[ $:]|[^ \t])+", value)]


def _output(out: Path, name: str) -> Path:
    target = _local(out, name)
    if "gen" not in target.relative_to(out).parts or target.suffix not in (".h", ".c", ".tlb"):
        raise ValueError(f"unexpected Windows MIDL declared output: {name}")
    return target


def _literal_tokens(value: str) -> list[str]:
    if re.search(r"\$(?![ $:])", value):
        raise ValueError("unsupported variable in Windows MIDL Ninja path")
    return _tokens(value)


def _midl_rules(lines: list[str]) -> set[str]:
    """Recognize local GN MIDL action rules without evaluating donor commands."""
    rules, names = set(), set()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"rule[ \t]+([A-Za-z0-9_]+)", line)
        if not match:
            continue
        name = match[1]
        if name in names:
            raise ValueError("duplicate Windows MIDL Ninja rule")
        names.add(name)
        fields, duplicate = {}, False
        for offset in range(index + 1, len(lines)):
            binding = lines[offset]
            if not binding.strip() or binding.lstrip().startswith("#"):
                continue
            if not binding.startswith((" ", "\t")):
                break
            key, separator, value = binding.strip().partition(" = ")
            duplicate |= key in fields or not separator
            fields[key] = value
        if "_idl_action___" not in name and "midl.py" not in fields.get("command", ""):
            continue
        description = re.fullmatch(
            r"ACTION (//[A-Za-z0-9_/]+:[A-Za-z0-9_]+_idl_action)"
            r"\((//build/toolchain/win:win_clang_(x64|x86|arm64))\)", fields.get("description", ""))
        if (duplicate or set(fields) != {"command", "description", "restat", "pool"}
                or not description or fields["restat"] != "1" or fields["pool"] != "build_toolchain_action_pool"):
            raise ValueError(f"unverified Windows MIDL action rule: {name}")
        expected = re.sub(r"[^A-Za-z0-9_]", "_", description[1] + "_" + description[2]) + "__rule"
        command = _tokens(fields["command"])
        if (name != expected or len(command) < 4
                or re.fullmatch(r"[^ $]*python3?(?:\.exe)?", command[0]) is None
                or command[1:3] != ["../../" + SCRIPT, "environment." + description[3]]):
            raise ValueError(f"unverified Windows MIDL command ownership: {name}")
        rules.add(name)
    return rules


def _graph(src: Path) -> tuple[list[str], dict]:
    out = src / "out/Default"
    root = out / "build.ninja"
    if _safe_stat(root) is None:
        generated = out / "gen"
        if generated.exists():
            raise ValueError("Windows MIDL generated tree exists without a Ninja graph")
        return [], {}
    pending, seen, outputs = [root], {}, set()
    while pending:
        path = pending.pop()
        relative = path.relative_to(out).as_posix()
        if relative in seen:
            continue
        raw, _ = _read(path, limit=256 * 1024 * 1024)
        seen[relative] = hashlib.sha256(raw).hexdigest()
        lines = re.sub(rb"\$\r?\n[ \t]*", b"", raw).decode("utf-8").splitlines()
        rules = _midl_rules(lines)
        midl_edge = False
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            directive = re.match(r"(include|subninja|build|rule)(?:[ \t]|$)", line)
            if not directive:
                if re.match(r"[ \t]+(?:include|subninja|build|rule)(?:[ \t]|$)", line):
                    raise ValueError("unsupported indented Windows MIDL Ninja directive")
                if midl_edge and line.startswith((" ", "\t")):
                    if not re.fullmatch(r"[ \t]+source_name_part = [A-Za-z0-9_]+", line):
                        raise ValueError("unsupported Windows MIDL edge override")
                elif not line.startswith((" ", "\t")):
                    midl_edge = False
                continue
            midl_edge = False
            if directive[1] in ("include", "subninja"):
                names = _literal_tokens(line[directive.end():].strip())
                if len(names) != 1:
                    raise ValueError("ambiguous Windows MIDL Ninja include")
                pending.append(_local(out, names[0]))
                continue
            if directive[1] != "build":
                continue
            if "$" in line:
                _literal_tokens(line)
            if "midl.py" not in line and "_idl_action___" not in line:
                continue
            match = re.fullmatch(r"build[ \t]+(.+?)(?<!\$):[ \t]+(.+)", line)
            if not match:
                raise ValueError("unrecognized Windows MIDL Ninja edge")
            inputs = _tokens(match[2])
            if not inputs:
                raise ValueError("missing Windows MIDL Ninja rule")
            if "midl.py" not in line and inputs[0] not in rules and "_idl_action___" not in inputs[0]:
                continue
            if (inputs[0] not in rules or inputs.count("../../" + SCRIPT) != 1
                    or "|" not in inputs or inputs.index("../../" + SCRIPT) <= inputs.index("|")
                    or "||" in inputs or "|@" in inputs):
                raise ValueError("unverified Windows MIDL Ninja edge ownership")
            midl_edge = True
            for name in _tokens(match[1]):
                if name == "|":
                    continue
                target = _output(out, name)
                _safe_stat(target)
                outputs.add(name)
    return sorted(outputs), {"file_count": len(seen), "sha256": hashlib.sha256(
        json.dumps(seen, sort_keys=True).encode()).hexdigest()}


def _record(src: Path, value: dict) -> None:
    path = src / RECORD
    if _safe_stat(path) is not None:
        raise ValueError("Windows MIDL repair record already exists")
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    if len(raw) > 65536:
        raise ValueError("oversized Windows MIDL repair record")
    fd, name = tempfile.mkstemp(prefix=".midl-record-", suffix=".tmp", dir=src)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _read(temporary)[0] != raw or _safe_stat(path) is not None:
            raise ValueError("Windows MIDL repair record changed before publication")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _proof(src: Path, identity: dict, *, before_sha256=ORIGINAL_SHA256,
           after_sha256=REPAIRED_SHA256) -> dict:
    path = src / RECORD
    if _safe_stat(path) is None:
        raise ValueError("repaired Windows MIDL script lacks one-time output invalidation record")
    value = json.loads(_read(path)[0])
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not _exact_identity(value.get("identity"), identity)
            or value.get("after_sha256") != after_sha256
            or value.get("before_sha256") != before_sha256
            or not isinstance(value.get("outputs"), list) or not isinstance(value.get("graphs"), dict)):
        raise ValueError("invalid Windows MIDL output invalidation record")
    if (not all(isinstance(name, str) for name in value["outputs"])
            or value["outputs"] != sorted(set(value["outputs"]))
            or value["graphs"] and (set(value["graphs"]) != {"file_count", "sha256"}
                or type(value["graphs"]["file_count"]) is not int or value["graphs"]["file_count"] < 1
                or not isinstance(value["graphs"]["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", value["graphs"]["sha256"]) is None)):
        raise ValueError("invalid Windows MIDL output invalidation inventory")
    for name in value["outputs"]:
        _output(src / "out/Default", name)
    current_outputs, _ = _graph(src)
    if set(current_outputs) - set(value["outputs"]):
        raise ValueError("Windows MIDL graph has outputs without one-time invalidation proof")
    return value


def apply(src: Path, platform: str, arch: str, identity: dict, *,
          records=WINDOWS154_RECORDS) -> dict:
    """Match a strict-restore identity against independent, reviewed donor pins."""
    if platform != "windows":
        return {"status": "not_applicable"}
    path = src.absolute() / SCRIPT
    if _safe_stat(path) is None:
        return {"status": "no_repair_targets", "path": SCRIPT}
    version = str(identity.get("chromium_version", ""))
    if version.startswith("153."):
        if arch not in IDENTITIES or not _exact_identity(identity, IDENTITIES[arch]):
            raise ValueError("unverified Windows153 MIDL upstream identity")
        before_sha256, after_sha256 = ORIGINAL_SHA256, REPAIRED_SHA256
        repair = transform
    elif version.startswith("154."):
        record = _windows154_record(identity, arch, records)
        before_sha256, after_sha256 = record["before_sha256"], record["after_sha256"]
        repair = transform154
    else:
        return {"status": "not_applicable", "path": SCRIPT}
    original, info = _read(path)
    repaired = repair(original)
    if original == repaired:
        proof = _proof(src, identity, before_sha256=before_sha256, after_sha256=after_sha256)
        changed, removed = False, []
    else:
        if _safe_stat(src / RECORD) is not None:
            raise ValueError("Windows MIDL script reverted after recorded repair")
        outputs, graphs = _graph(src)
        # Only declared MIDL action outputs are invalidated, never source placeholders.
        removed = []
        for name in outputs:
            target = _local(src / "out/Default", name)
            if _safe_stat(target) is not None:
                target.unlink()
                removed.append(name)
        changed = _replace(path, original, repaired, info)
        if not changed:
            proof = _proof(src, identity, before_sha256=before_sha256, after_sha256=after_sha256)
        else:
            proof = {"schema_version": 1, "identity": identity,
                     "before_sha256": before_sha256, "after_sha256": after_sha256,
                     "outputs": outputs, "graphs": graphs}
            _record(src, proof)
    return {"status": "repaired" if changed else "verified", "path": SCRIPT,
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "after_sha256": hashlib.sha256(repaired).hexdigest(), "changed": changed,
            "invalidated_outputs": removed, "invalidation_record": RECORD,
            "declared_outputs": len(proof["outputs"])}
