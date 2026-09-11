"""Plot loader speed and OS peak-memory comparisons."""
import argparse
import hashlib
import json
from pathlib import Path


def render(directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))["variants"]
    buffers = manifest["measurement"].startswith("buffer-size-process-resources-")
    names = tuple(manifest["variants"])
    label_map = {"standard": "Standard", "memconservative": "Original memconservative", "optimized_memconservative": "Buffered memconservative"}
    labels = tuple(label_map.get(name, name.removeprefix("buffer_").removesuffix("k") + " KiB") for name in names)
    default_buffer = f"buffer_{manifest.get("optimized_default_buffer_kib", 64)}k"
    colors = ["#2563eb" if name in (default_buffer, "optimized_memconservative") else "#94a3b8" if name in ("standard", "memconservative") else "#0f766e" for name in names]
    columns = [("load_ms", "Full loading time", "ms", 1), ("peak_resident_bytes", "Peak resident memory", "MiB", 1048576)]
    if "peak_commit_bytes" in summary[names[0]]:
        columns.append(("peak_commit_bytes", "Peak committed memory", "MiB", 1048576))
    fig, axes = plt.subplots(1, len(columns), figsize=(13, max(5.6, 0.5 * len(names) + 3)))
    fig.subplots_adjust(left=0.20, right=0.97, top=0.68, bottom=0.21, wspace=0.30)
    fig.suptitle("Read buffer sizes: speed and peak memory" if buffers else "GeoData loaders: speed and peak memory", x=0.04, y=0.98, ha="left", fontsize=18, fontweight="bold")
    environment = manifest["environment"]
    info = environment["os"]
    version = environment["go"]["effective_env"]["GOVERSION"]
    fig.text(0.04, 0.90, f"{info['system']} / {info['machine']} | {version} | {manifest['rounds']} runs per loader | Warm cache", fontsize=10, color="#475569")
    fig.text(0.04, 0.84, f"CPU: {environment['cpu'].get('model', 'unknown')} | No timing or memory instrumentation in mihomo", fontsize=9, color="#475569")
    for col, (metric, title, unit, scale) in enumerate(columns):
        ax = axes[col]
        values = [summary[name][metric]["median"] / scale for name in names]
        lower = [values[i] - summary[name][metric]["p25"] / scale for i, name in enumerate(names)]
        upper = [summary[name][metric]["p75"] / scale - values[i] for i, name in enumerate(names)]
        ax.barh(range(len(names)), values, color=colors, height=0.48, xerr=[lower, upper], capsize=3, error_kw={"elinewidth": 1})
        ax.set_yticks(range(len(names)), labels if col == 0 else [""] * len(names))
        ax.invert_yaxis()
        ax.set_title(title, fontsize=11, pad=18)
        maximum = max(value + error for value, error in zip(values, upper))
        ax.set_xlim(0, maximum * 1.32)
        for i, value in enumerate(values):
            ax.text(value + upper[i] + maximum * 0.025, i, f"{value:.1f}", va="center", fontsize=10)
        ax.set_xlabel(unit)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.set_axisbelow(True)
        ax.grid(axis="x", color="#e2e8f0")
    fig.text(0.04, 0.09, "Bars: medians; whiskers: P25-P75. Memory is the OS lifetime high-water mark for each mihomo process.", fontsize=9, color="#475569")
    fig.text(0.04, 0.04, "Resident and committed memory are distinct metrics; compare versions on the same device.", fontsize=9, color="#475569")
    for extension in ("png", "svg"):
        fig.savefig(directory / f"timing-comparison.{extension}", dpi=160, facecolor="white")
    plt.close(fig)
    svg = directory / "timing-comparison.svg"
    svg.write_text("\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n", encoding="utf-8")
    manifest["plot"] = {"matplotlib": matplotlib.__version__, "numpy": numpy.__version__,
                        "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        "sha256": {ext: hashlib.sha256((directory / f"timing-comparison.{ext}").read_bytes()).hexdigest() for ext in ("png", "svg")}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="completed run directory")
    render(parser.parse_args().directory.resolve())
