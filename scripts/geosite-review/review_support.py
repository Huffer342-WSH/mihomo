"""Environment capture and statistics for loader resource comparisons."""

import collections
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
import venv

from hardware_info import collect_hardware

def plotting_python(cache):
    """Keep plotting dependencies out of the user's existing Python environment."""
    directory = cache / "plotting" / f"{sys.platform}-py{sys.version_info.major}.{sys.version_info.minor}"
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = "import matplotlib, numpy; assert matplotlib.__version__ == '3.10.8'"
    if python.is_file() and subprocess.run([str(python), "-I", "-c", probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return str(python)
    print(f"Preparing isolated plotting environment: {directory}", flush=True)
    if directory.exists():
        shutil.rmtree(directory)
    # Relocatable Python distributions need the original interpreter location
    # to find their standard library; use symlinks on POSIX.
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(directory)
    subprocess.run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "matplotlib==3.10.8"], check=True)
    subprocess.run([str(python), "-I", "-c", probe], check=True)
    return str(python)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def save(path, data):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def timing_stats(values):
    ordered = sorted(values)
    if not ordered or not all(math.isfinite(value) for value in ordered):
        raise ValueError("timing samples must be nonempty and finite")

    def percentile(fraction):
        position = fraction * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    p25, p75 = percentile(0.25), percentile(0.75)
    return {
        "n": len(ordered), "median": statistics.median(ordered),
        "p25": p25, "p75": p75, "iqr": p75 - p25,
        "min": ordered[0], "max": ordered[-1],
        "stdev": statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
    }


def public_environment(record):
    """Keep comparable performance facts, not local paths or arbitrary env text."""
    def pick(value, keys):
        return {key: value[key] for key in keys if key in value}

    cpu = pick(record.get("cpu", {}), ("model", "logical_count", "available_count"))
    if "affinity" in record.get("cpu", {}):
        cpu["available_count"] = len(record["cpu"]["affinity"])
    raw_go = record.get("go", {})
    go_env = raw_go.get("effective_env", {})
    raw_vars = record.get("environment_variables", {})
    variables = {}
    for key in ("GOMAXPROCS", "GOGC", "GOMEMLIMIT"):
        value = raw_vars.get(key)
        if value is not None:
            variables[key] = value if re.fullmatch(r"(?:off|\d+(?:[KMGT]i?B)?)", str(value)) else "<set>"
    disk = record.get("storage", {})
    return {
        "os": pick(record.get("os", {}), ("system", "release", "machine")),
        "cpu": cpu,
        "memory": pick(record.get("memory", {}), ("os_total_bytes", "modules_status")),
        "storage": {
            **pick(disk, ("filesystem", "device_status")),
            "devices": [pick(item, ("model", "size_bytes", "transport")) for item in disk.get("devices", [])],
            "filesystem_capacity": pick(disk.get("filesystem_capacity", {}), ("total_bytes",)),
        },
        "python": pick(record.get("python", {}), ("version",)),
        "go": {
            **pick(raw_go, ("sha256", "runtime_probe", "selection")),
            "effective_env": pick(go_env, ("GOVERSION", "GOOS", "GOARCH", "GOHOSTOS", "GOHOSTARCH", "GOAMD64", "GOARM64", "GOEXPERIMENT", "CGO_ENABLED", "GOTOOLCHAIN", "GOWORK")),
            "custom_goflags": bool(go_env.get("GOFLAGS")) or raw_go.get("custom_goflags", False),
        },
        "environment_variables": variables,
        "custom_godebug": bool(raw_vars.get("GODEBUG")) or record.get("custom_godebug", False),
        "timer": pick(record.get("timer", {}), ("implementation", "resolution_seconds", "monotonic")),
    }


def environment_record(go, env, output):
    hardware = collect_hardware(output / "data")
    keys = ("GOOS", "GOARCH", "GOHOSTOS", "GOHOSTARCH", "GOVERSION", "GOAMD64", "GOARM64", "GOEXPERIMENT", "GOROOT", "CGO_ENABLED", "GOFLAGS", "GOTOOLCHAIN", "GOWORK")
    go_env = json.loads(subprocess.check_output([go, "env", "-json", *keys], env=env, text=True))
    if go_env["GOOS"] != go_env["GOHOSTOS"] or go_env["GOARCH"] != go_env["GOHOSTARCH"]:
        raise ValueError("timing requires a native executable; remove cross-compilation GOOS/GOARCH overrides")
    probe = output / "environment-probe.go"
    probe.write_text('''package main
import ("encoding/json"; "os"; "runtime"; "runtime/debug")
func main() { _ = json.NewEncoder(os.Stdout).Encode(map[string]any{
"num_cpu": runtime.NumCPU(), "gomaxprocs": runtime.GOMAXPROCS(0),
"memory_limit_bytes": debug.SetMemoryLimit(-1),
}) }
''', encoding="utf-8")
    try:
        runtime_probe = json.loads(subprocess.check_output([go, "run", str(probe)], cwd=output, env=env, text=True))
    finally:
        probe.unlink(missing_ok=True)
    selected = ("CGO_ENABLED", "GOTOOLCHAIN", "GOFLAGS", "GOWORK", "GOOS", "GOARCH", "GOAMD64", "GOARM64", "GOEXPERIMENT", "GOMAXPROCS", "GOGC", "GOMEMLIMIT", "GODEBUG")
    clock = time.get_clock_info("perf_counter")
    return public_environment({
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(), "machine": platform.machine()},
        **hardware,
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "go": {"executable": go, "sha256": digest(Path(go)), "effective_env": go_env, "runtime_probe": runtime_probe},
        "environment_variables": {key: env.get(key) for key in selected},
        "timer": {"implementation": clock.implementation, "resolution_seconds": clock.resolution, "monotonic": clock.monotonic},
    })


def summarize_loaders(measurements, *, variants=None, comparisons=None):
    variants = variants or ("standard", "memconservative", "optimized_memconservative")
    rounds = collections.defaultdict(dict)
    for row in measurements:
        name = row["version"]
        if name not in variants or name in rounds[row["round"]] or row["returncode"] != 0:
            raise ValueError("invalid, failed or duplicate loader measurement")
        rounds[row["round"]][name] = row
    if not rounds or any(set(values) != set(variants) for values in rounds.values()):
        raise ValueError("each round must contain all expected variants")
    metrics = ["load_ms", "cpu_ms", "peak_resident_bytes"]
    commit = ["peak_commit_bytes" in row for row in measurements]
    if any(commit) and not all(commit):
        raise ValueError("mixed peak-commit metric availability")
    if all(commit):
        metrics.append("peak_commit_bytes")
    summary = {"variants": {}, "paired_differences": {},
               "definition": "reference - candidate; positive means less time or memory for candidate",
               "quantile_method": "linear interpolation at (n - 1) * p"}
    for name in variants:
        rows = [row for row in measurements if row["version"] == name]
        summary["variants"][name] = {"rounds": len(rows), **{metric: timing_stats([row[metric] for row in rows]) for metric in metrics}}
    if comparisons is None:
        comparisons = (("standard", "memconservative"), ("memconservative", "optimized_memconservative"), ("standard", "optimized_memconservative"))
    for reference, candidate in comparisons:
        result = {"reference": reference, "candidate": candidate, "metrics": {}}
        for metric in metrics:
            deltas = [rows[reference][metric] - rows[candidate][metric] for rows in rounds.values()]
            ratios = [100 * delta / rows[reference][metric] if rows[reference][metric] else 0 for delta, rows in zip(deltas, rounds.values())]
            result["metrics"][metric] = {"saved": timing_stats(deltas), "saved_percent": timing_stats(ratios),
                                           "lower_rounds": sum(value > 0 for value in deltas), "higher_rounds": sum(value < 0 for value in deltas), "equal_rounds": sum(value == 0 for value in deltas)}
        summary["paired_differences"][reference + "_vs_" + candidate] = result
    return summary
