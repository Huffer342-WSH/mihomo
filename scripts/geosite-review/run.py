#!/usr/bin/env python3
"""Compare standard, original memconservative and buffered memconservative."""

import argparse
import collections
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
import uuid

sys.dont_write_bytecode = True
from review_support import digest, environment_record, plotting_python, save, summarize_loaders
from process_metrics import measure_process, metric_description

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path("scripts/geosite-review")
DECODER = Path("component/geodata/memconservative/decode.go")
MEASUREMENT = "loader-process-resources-v1"
BUILD_FLAGS = ["-mod=readonly", "-trimpath", "-buildvcs=false"]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def download(url, target, refresh=False):
    """Reuse verified cached files; never promote partial downloads to cache."""
    metadata = target.with_name(target.name + ".json")
    if target.is_file() and metadata.is_file() and not refresh:
        try:
            info = json.loads(metadata.read_text())
            if info["url"] == url and info["size"] > 0 and target.stat().st_size == info["size"] and digest(target) == info["sha256"]:
                print(f"Using cached {target.name}", flush=True)
                return target
        except (ValueError, KeyError):
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + f".{os.getpid()}.part")
    for attempt in range(1, 4):
        try:
            print(f"Downloading {target.name} (attempt {attempt}/3)", flush=True)
            request = urllib.request.Request(url, headers={"User-Agent": "mihomo-geosite-review"})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as stream:
                expected_size = response.headers.get("Content-Length")
                copied, last_report = 0, time.monotonic()
                while block := response.read(1024 * 1024):
                    stream.write(block)
                    copied += len(block)
                    if time.monotonic() - last_report >= 10:
                        print(f"  {target.name}: {copied / 1048576:.1f} MiB", flush=True)
                        last_report = time.monotonic()
            if copied == 0 or (expected_size and copied != int(expected_size)):
                raise OSError(f"incomplete download: {target.name} ({copied} bytes)")
            info = {"url": url, "size": copied, "sha256": digest(partial)}
            partial.replace(target)
            save(metadata, info)
            return target
        except (OSError, ValueError, http.client.HTTPException) as error:
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise RuntimeError(f"could not download {url}: {error}") from error
            print(f"  Retry after download failure: {error}", flush=True)
            time.sleep(attempt)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise


def resolve_go(explicit, cache):
    if explicit:
        executable = shutil.which(explicit)
        if not executable:
            raise RuntimeError(f"Go executable not found: {explicit}")
        return str(Path(executable).resolve())
    executable = shutil.which("go")
    if executable:
        try:
            version = subprocess.check_output([executable, "version"], text=True, env={**os.environ, "GOTOOLCHAIN": "local"})
            release = version.split()[2]
            if release == "go1.26" or release.startswith("go1.26."):
                return str(Path(executable).resolve())
        except (OSError, subprocess.CalledProcessError, IndexError):
            pass
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}.get(platform.system())
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if system is None or arch is None:
        raise RuntimeError("automatic Go setup supports Linux/macOS/Windows on amd64/arm64; pass --go for this platform")
    package = f"go1.26.{system}-{arch}"
    install = cache / "toolchains" / package
    binary = install / "go/bin" / ("go.exe" if system == "windows" else "go")
    if binary.is_file():
        print(f"Using cached toolchain: {binary}", flush=True)
        return str(binary)
    suffix = ".zip" if system == "windows" else ".tar.gz"
    archive = download("https://github.com/MetaCubeX/go/releases/download/build/" + package + suffix, cache / "downloads" / (package + suffix))
    install.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="unpack-", dir=install) as temporary:
        if suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(temporary)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(temporary, filter="data")
        (Path(temporary) / "go").rename(install / "go")
    if not binary.is_file():
        raise RuntimeError(f"Go executable missing from downloaded archive: {archive}")
    return str(binary)


def resolve_alpha(base):
    result = subprocess.run(["git", "rev-parse", "--verify", (base or "origin/Alpha") + "^{commit}"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    if base:
        raise RuntimeError(f"comparison ref not found: {base}")
    print("Fetching official Alpha (local origin/Alpha is missing)", flush=True)
    subprocess.run(["git", "fetch", "--no-tags", "https://github.com/MetaCubeX/mihomo.git", "Alpha"], cwd=ROOT, check=True)
    return git("rev-parse", "FETCH_HEAD^{commit}").decode().strip()


VARIANTS = ("standard", "memconservative", "optimized_memconservative")


def write_config(text, loader, destination):
    pattern = r"(?m)^geodata-loader:[^\n]*$"
    if len(re.findall(pattern, text)) != 1:
        raise ValueError("configuration must contain exactly one top-level geodata-loader")
    destination.write_text(re.sub(pattern, "geodata-loader: " + loader, text), encoding="utf-8", newline="\n")


def loading_signature(log):
    text = log.read_text(encoding="utf-8", errors="replace")
    if "test is successful" not in text:
        raise RuntimeError(f"missing successful configuration result: {log}")
    # Compare the configured rules and their reported record counts. This is a
    # loading consistency check, not a packet-level matcher equivalence test.
    records = re.findall(r"Finished initial (GeoSite|GeoIP) rule ([^\r\n]*?records: \d+)", text)
    if not records:
        raise RuntimeError(f"no GeoData rule records in {log}")
    return collections.Counter(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="official baseline (default: origin/Alpha)")
    parser.add_argument("--optimized-ref", help="read decode.go from this ref instead of the working file")
    parser.add_argument("--rounds", type=positive, default=30)
    parser.add_argument("--buffer-sizes", type=positive, nargs="+", metavar="KiB", help="compare standard, original memconservative and optimized buffer sizes")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / HARNESS / "test.yaml")
    parser.add_argument("--go")
    parser.add_argument("--tags", default="with_gvisor")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/geosite-review")
    args = parser.parse_args()
    if args.buffer_sizes and len(set(args.buffer_sizes)) != len(args.buffer_sizes):
        parser.error("buffer sizes must be unique")
    sizes = args.buffer_sizes or []
    variants = ("standard", "memconservative", *(f"buffer_{size}k" for size in sizes)) if sizes else VARIANTS
    measurement = "buffer-size-process-resources-v2" if sizes else MEASUREMENT
    memory_metric = metric_description()
    output_root = args.output_dir.resolve()
    cache = output_root / "cache"
    output = output_root / (("buffers-" if sizes else "loaders-") + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    print(f"Results: {output}", flush=True)
    manifest = {"schema": 5, "measurement": measurement, "status": "preparing", "memory_metric": memory_metric,
                "rounds": args.rounds, "warmups": 1, "source_scope": "official baseline plus optimized decode.go only", "instrumented": False}
    save(output / "manifest.json", manifest)
    env = {**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOWORK": "off"}
    for name in ("MIHOMO_DECODE_TIMINGS", "MIHOMO_GEOSITE_TIMINGS"):
        env.pop(name, None)
    redactions = {str(output): "<run>", str(cache): "<cache>", str(ROOT): "<workspace>", str(Path.home()): "<home>"}

    def redact(text):
        for prefix, token in sorted(redactions.items(), key=lambda item: -len(item[0])):
            for spelling in (prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\"), prefix.replace("\\", "\\\\")):
                text = text.replace(spelling, token)
        return text

    def scrub(path):
        path.write_text(redact(path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")

    def command(argv, source, log):
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(argv, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT)
        scrub(log)
        if result.returncode:
            raise RuntimeError(f"command failed; see {log}")

    try:
        base = resolve_alpha(args.base)
        optimized_ref = git("rev-parse", args.optimized_ref).decode().strip() if args.optimized_ref else None
        optimized = git("show", optimized_ref + ":" + DECODER.as_posix()).decode("utf-8") if optimized_ref else (ROOT / DECODER).read_text(encoding="utf-8")
        buffer_pattern = r"bufio\.NewReaderSize\(f,\s*(\d+)\s*\*\s*1024\)"
        allocations = re.findall(buffer_pattern, optimized)
        if len(allocations) != 1:
            raise ValueError("expected exactly one fixed-KiB reader allocation in optimized decoder")
        default_buffer_kib = int(allocations[0])
        manifest["optimized_default_buffer_kib"] = default_buffer_kib
        original = git("show", base + ":" + DECODER.as_posix()).decode("utf-8")
        if optimized == original:
            raise ValueError("optimized decoder is identical to baseline")
        data = output / "data"
        data.mkdir()
        manifest["inputs"] = {}
        for name in ("geosite.dat", "geoip.dat"):
            if args.data_dir:
                src = args.data_dir.resolve() / name
            else:
                src = download("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/" + name, cache / "data" / name, args.refresh_data)
            shutil.copyfile(src, data / name)
            manifest["inputs"][name] = {"sha256": digest(data / name)}
        go = resolve_go(args.go, cache)
        redactions[str(Path(go).parent.parent)] = "<go>"
        env["PATH"] = str(Path(go).parent) + os.pathsep + env.get("PATH", "")
        plot_python = plotting_python(cache)
        env["MPLCONFIGDIR"] = str(cache / "matplotlib")
        manifest.update(base=base, optimized_ref=optimized_ref, working_head=git("rev-parse", "HEAD").decode().strip(),
                        environment=environment_record(go, env, output), build_flags=BUILD_FLAGS, tags=args.tags)
        optimized_labels = [f"buffer_{size}k" for size in sizes] if sizes else ["optimized"]
        sources = {label: output / label / "source" for label in ("baseline", *optimized_labels)}
        archive = output / "baseline.tar"
        subprocess.run(["git", "archive", "--output", str(archive), base], cwd=ROOT, check=True)
        for source in sources.values():
            source.mkdir(parents=True)
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter="data")
        archive.unlink()
        for label in optimized_labels:
            content = optimized
            if sizes:
                size = sizes[optimized_labels.index(label)]
                content = re.sub(buffer_pattern, f"bufio.NewReaderSize(f, {size}*1024)", content)
            (sources[label] / DECODER).write_text(content, encoding="utf-8", newline="\n")
        manifest["buffer_sizes_kib"] = sizes
        manifest["decoder_sha256"] = {label: digest(source / DECODER) for label, source in sources.items()}
        # All other source files are exactly the same extracted baseline.
        manifest["binary_sha256"] = {}
        binaries = {}
        for label, source in sources.items():
            print(f"Building uninstrumented {label}", flush=True)
            binary = output / label / ("mihomo.exe" if os.name == "nt" else "mihomo")
            command([go, "build", *BUILD_FLAGS, *(["-tags", args.tags] if args.tags else []), "-o", str(binary), "."], source, output / label / "build.log")
            binaries[label] = binary
            manifest["binary_sha256"][label] = digest(binary)
        config_text = args.config.read_text(encoding="utf-8")
        configs = {}
        manifest["variants"] = {}
        for variant in variants:
            folder = output / variant
            folder.mkdir(exist_ok=True)
            loader = "standard" if variant == "standard" else "memconservative"
            binary_label = variant if variant.startswith("buffer_") else ("optimized" if variant == "optimized_memconservative" else "baseline")
            configs[variant] = folder / "config.yaml"
            write_config(config_text, loader, configs[variant])
            manifest["variants"][variant] = {"loader": loader, "binary": binary_label, "config_sha256": digest(configs[variant])}
        if sizes:
            assert len({p.read_bytes() for name, p in configs.items() if name != "standard"}) == 1
        else:
            assert configs["memconservative"].read_bytes() == configs["optimized_memconservative"].read_bytes()
        manifest["status"] = "running"
        save(output / "manifest.json", manifest)
        measurements = []
        expected = None

        def measure(variant, number):
            nonlocal expected
            binary = binaries[manifest["variants"][variant]["binary"]]
            log = output / variant / f"load-{number}.log"
            with log.open("w", encoding="utf-8") as stream:
                record = measure_process([str(binary), "-t", "-d", str(data), "-f", str(configs[variant])], cwd=output, env=env, stdout=stream)
            scrub(log)
            if record["returncode"]:
                raise RuntimeError(f"configuration loading failed: {log}")
            signature = loading_signature(log)
            if expected is None:
                expected = signature
            elif signature != expected:
                raise RuntimeError(f"GeoData rule counts differ: {log}")
            if number != "warmup":
                measurements.append({"version": variant, "round": number, **record})
                save(output / "measurements.json", measurements)

        for variant in variants:
            print(f"Warming {variant}", flush=True)
            measure(variant, "warmup")
        for number in range(args.rounds):
            offset = number % len(variants)
            order = variants[offset:] + variants[:offset]
            print(f"Round {number + 1}/{args.rounds}: {' '.join(order)}", flush=True)
            for variant in order:
                measure(variant, number)
        comparisons = None
        if sizes:
            comparisons = [("standard", "memconservative")]
            comparisons += [(reference, label) for reference in ("standard", "memconservative") for label in optimized_labels]
            default_label = f"buffer_{default_buffer_kib}k"
            if default_label in optimized_labels:
                comparisons += [(default_label, label) for label in optimized_labels if label != default_label]
        summary = summarize_loaders(measurements, variants=variants, comparisons=comparisons)
        save(output / "summary.json", summary)
        command([plot_python, "-I", str(ROOT / HARNESS / "plot_results.py"), str(output)], output, output / "plot.log")
        manifest["plot"] = json.loads((output / "manifest.json").read_text(encoding="utf-8"))["plot"]
        for variant in variants:
            item = summary["variants"][variant]
            print(f"{variant}: wall median {item['load_ms']['median']:.2f} ms; peak resident median {item['peak_resident_bytes']['median'] / 1048576:.2f} MiB", flush=True)
        manifest["status"] = "passed"
    except BaseException as error:
        manifest.update(status="failed", error=redact(str(error)))
        raise
    finally:
        save(output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
