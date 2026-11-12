#!/usr/bin/env python3
"""
Turn the raw D2 runs into tables and graphs.

  python3 experiments/analyze.py

Reads results/d2/raw/<scenario>/<mode>_r<rep>/ and writes to results/d2/:
  summary.csv            one row per scenario and mode (mean of reps)
  runs.csv               one row per run
  detection.csv          how fast the controller reacted to each event
  summary.md             the tables in Markdown
  graphs/*.png           charts (needs matplotlib)
"""

import csv
import json
import os
import statistics

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
D2 = os.path.join(ROOT, "results", "d2")
RAW = os.path.join(D2, "raw")
GRAPHS = os.path.join(D2, "graphs")

ORDER = ["e1_low", "e1_med", "e1_high", "e2_hetero", "e3_overload",
         "e4_netload", "e5_crash", "e6_linkdown"]
LABEL = {"e1_low": "E1 low", "e1_med": "E1 medium", "e1_high": "E1 high",
         "e2_hetero": "E2 unequal", "e3_overload": "E3 overload",
         "e4_netload": "E4 net load", "e5_crash": "E5 crash",
         "e6_linkdown": "E6 link down"}
MODE_LABEL = {"rr": "Static round robin", "adaptive": "Adaptive (SDN)"}
COLORS = {"rr": "#9aa5b1", "adaptive": "#0f766e"}

# What controller event each scenario action should cause.
DETECT = {
    "hog_start": ("overloaded", "h1"),
    "flood_start": ("overloaded", "h1"),
    "server_kill": ("server_down", None),
    "server_restart": ("server_up", None),
    "link_down": ("server_down", None),
    "link_up": ("server_up", None),
}


def read_csv(path):

    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_events(path):

    out = []

    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass

    return out


def pct(sorted_vals, p):

    if not sorted_vals:
        return 0.0

    i = int(round(p / 100.0 * (len(sorted_vals) - 1)))
    return sorted_vals[min(i, len(sorted_vals) - 1)]


def load_runs():

    runs = []

    if not os.path.isdir(RAW):
        return runs

    for scenario in sorted(os.listdir(RAW)):

        sdir = os.path.join(RAW, scenario)

        for run in sorted(os.listdir(sdir)):

            rdir = os.path.join(sdir, run)
            meta_path = os.path.join(rdir, "run.json")

            if not os.path.exists(meta_path):
                continue

            with open(meta_path) as f:
                meta = json.load(f)

            rows = []

            for name in sorted(os.listdir(rdir)):
                if name.startswith("client_") and name.endswith(".csv"):
                    rows += read_csv(os.path.join(rdir, name))

            meta["rows"] = rows
            meta["events"] = read_events(os.path.join(
                rdir, "controller_events.jsonl"))
            meta["dir"] = rdir
            runs.append(meta)

    return runs


def run_metrics(run):

    rows = run["rows"]
    ok = [r for r in rows if r["ok"] == "1"]
    lat = sorted(float(r["latency_ms"]) for r in ok)
    per_server = {}

    for r in ok:
        per_server[r["server"]] = per_server.get(r["server"], 0) + 1

    total = len(rows)

    return {
        "scenario": run["scenario"], "mode": run["mode"], "rep": run["rep"],
        "requests": total,
        "ok": len(ok),
        "failed": total - len(ok),
        "failed_pct": 100.0 * (total - len(ok)) / total if total else 0.0,
        "retried_pct": 100.0 * sum(1 for r in rows if r["attempts"] != "1")
                       / total if total else 0.0,
        "avg_ms": statistics.mean(lat) if lat else 0.0,
        "p95_ms": pct(lat, 95),
        "p99_ms": pct(lat, 99),
        "throughput_rps": len(ok) / float(run["duration"]),
        "h1": per_server.get("h1", 0),
        "h2": per_server.get("h2", 0),
        "h3": per_server.get("h3", 0),
    }


def detection(run):
    """Time between each scenario action and the controller noticing it."""

    out = []

    for a in run["actions"]:

        if a["action"] not in DETECT:
            continue

        ev_name, server = DETECT[a["action"]]
        target = server
        scenario = run["scenario"]

        if target is None:
            target = "h2" if scenario == "e5_crash" else "h3"

        hits = [e for e in run["events"]
                if e.get("ev") == ev_name and e.get("server") == target
                and e["t"] >= a["t"] - 0.05]

        delay = round(hits[0]["t"] - a["t"], 2) if hits else None

        out.append({"scenario": scenario, "mode": run["mode"],
                    "rep": run["rep"], "action": a["action"],
                    "server": target, "controller_event": ev_name,
                    "detected_after_s": delay,
                    "reason": hits[0].get("reason", "") if hits else ""})

    return out


def aggregate(metrics):

    groups = {}

    for m in metrics:
        groups.setdefault((m["scenario"], m["mode"]), []).append(m)

    rows = []
    keys = ["requests", "failed", "failed_pct", "retried_pct", "avg_ms",
            "p95_ms", "p99_ms", "throughput_rps", "h1", "h2", "h3"]

    for scenario in ORDER:
        for mode in ("rr", "adaptive"):

            g = groups.get((scenario, mode))

            if not g:
                continue

            row = {"scenario": scenario, "mode": mode, "reps": len(g)}

            for k in keys:
                vals = [x[k] for x in g]
                row[k] = statistics.mean(vals)
                row[k + "_sd"] = statistics.stdev(vals) if len(vals) > 1 \
                    else 0.0

            rows.append(row)

    return rows


def write_csv(path, rows):

    if not rows:
        return

    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in r.items()})


def improvement(rr, ad, key, lower_is_better=True):

    if not rr or rr[key] == 0:
        return ""

    change = (rr[key] - ad[key]) / rr[key] * 100.0

    if not lower_is_better:
        change = -change

    return "%+.0f%%" % change


def write_markdown(summary, det_rows, scenarios):

    by = {(r["scenario"], r["mode"]): r for r in summary}
    lines = ["# D2 results summary (generated by experiments/analyze.py)", "",
             "Each value is the mean over the repetitions. Latency is the "
             "time a client waited for a full reply, including retries.",
             "", "## Scenarios", "", "| Scenario | What happens |",
             "|---|---|"]

    for s in ORDER:
        if s in scenarios:
            lines.append("| %s | %s |" % (LABEL[s], scenarios[s]["title"]))

    lines += ["", "## Static round robin vs adaptive", "",
              "| Scenario | Mode | Reps | Avg ms | p95 ms | Throughput req/s "
              "| Failed % | Retried % | h1 / h2 / h3 requests |",
              "|---|---|---|---|---|---|---|---|---|"]

    for s in ORDER:
        for mode in ("rr", "adaptive"):
            r = by.get((s, mode))
            if not r:
                continue
            lines.append(
                "| %s | %s | %d | %.1f | %.1f | %.1f | %.2f | %.2f | "
                "%.0f / %.0f / %.0f |" % (
                    LABEL[s], MODE_LABEL[mode], r["reps"], r["avg_ms"],
                    r["p95_ms"], r["throughput_rps"], r["failed_pct"],
                    r["retried_pct"], r["h1"], r["h2"], r["h3"]))

    lines += ["", "## Adaptive compared with round robin", "",
              "Positive = adaptive is better.", "",
              "| Scenario | Avg latency | p95 latency | Throughput |",
              "|---|---|---|---|"]

    for s in ORDER:
        rr, ad = by.get((s, "rr")), by.get((s, "adaptive"))
        if rr and ad:
            lines.append("| %s | %s | %s | %s |" % (
                LABEL[s], improvement(rr, ad, "avg_ms"),
                improvement(rr, ad, "p95_ms"),
                improvement(rr, ad, "throughput_rps", False)))

    if det_rows:

        lines += ["", "## How fast the controller noticed each event", "",
                  "Measured from the controller's event log "
                  "(mean over repetitions; '-' means not detected).", "",
                  "| Scenario | Event | Mode | Controller event | "
                  "Detected after (s) | Reason logged |",
                  "|---|---|---|---|---|---|"]

        groups = {}

        for d in det_rows:
            groups.setdefault((d["scenario"], d["action"], d["mode"]),
                              []).append(d)

        for (s, action, mode), g in sorted(
                groups.items(), key=lambda kv: (ORDER.index(kv[0][0]),
                                                kv[0][1], kv[0][2])):
            vals = [x["detected_after_s"] for x in g
                    if x["detected_after_s"] is not None]
            delay = "%.2f" % statistics.mean(vals) if vals else "-"
            reason = next((x["reason"] for x in g if x["reason"]), "")
            lines.append("| %s | %s | %s | %s %s | %s | %s |" % (
                LABEL[s], action, MODE_LABEL[mode], g[0]["server"],
                g[0]["controller_event"], delay, reason))

        lines += ["", "In round robin mode the controller still monitors "
                  "and logs these events, but the static policy ignores "
                  "them. Only adaptive mode acts on them: a failed "
                  "server gets no new connections and its flow rules are "
                  "deleted; an overloaded server's cost is tripled, so new "
                  "connections go elsewhere unless every server is busy."]

    lines.append("")

    with open(os.path.join(D2, "summary.md"), "w") as f:
        f.write("\n".join(lines))


# ------------------------------------------------------------ graphs

def make_graphs(summary, runs):

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping graphs")
        return

    os.makedirs(GRAPHS, exist_ok=True)
    by = {(r["scenario"], r["mode"]): r for r in summary}
    present = [s for s in ORDER if (s, "rr") in by and (s, "adaptive") in by]

    def bars(key, ylabel, title, fname):

        fig, ax = plt.subplots(figsize=(10, 4.2))
        width = 0.38
        xs = range(len(present))

        for i, mode in enumerate(("rr", "adaptive")):
            vals = [by[(s, mode)][key] for s in present]
            errs = [by[(s, mode)][key + "_sd"] for s in present]
            pos = [x + (i - 0.5) * width for x in xs]
            ax.bar(pos, vals, width, yerr=errs, capsize=3,
                   color=COLORS[mode], label=MODE_LABEL[mode])

        ax.set_xticks(list(xs))
        ax.set_xticklabels([LABEL[s] for s in present])
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.legend(frameon=False)
        ax.grid(axis="y", alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(os.path.join(GRAPHS, fname), dpi=140)
        plt.close(fig)

    bars("avg_ms", "Average response time (ms)",
         "Average response time: static vs adaptive", "avg_latency.png")
    bars("p95_ms", "95th percentile response time (ms)",
         "Tail (p95) response time: static vs adaptive", "p95_latency.png")
    bars("throughput_rps", "Successful requests per second",
         "Throughput: static vs adaptive", "throughput.png")
    bars("retried_pct", "Requests needing a retry (%)",
         "Requests that hit a bad server first", "retried.png")

    # Load level chart for E1.
    levels = [("e1_low", 8), ("e1_med", 16), ("e1_high", 32)]
    levels = [(s, t) for s, t in levels if (s, "rr") in by]

    if levels:
        fig, ax = plt.subplots(figsize=(6, 4))
        for mode in ("rr", "adaptive"):
            ax.plot([t for _, t in levels],
                    [by[(s, mode)]["avg_ms"] for s, _ in levels],
                    marker="o", color=COLORS[mode], label=MODE_LABEL[mode])
        ax.set_xlabel("Concurrent client threads")
        ax.set_ylabel("Average response time (ms)")
        ax.set_title("E1: response time as load grows (equal servers)")
        ax.legend(frameon=False)
        ax.grid(alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(os.path.join(GRAPHS, "e1_load_levels.png"), dpi=140)
        plt.close(fig)

    # Requests per server.
    fig, axes = plt.subplots(1, len(present), figsize=(2.1 * len(present), 3.6),
                             sharey=False)
    if len(present) == 1:
        axes = [axes]
    for ax, s in zip(axes, present):
        bottom = [0, 0]
        for srv, col in (("h1", "#0f766e"), ("h2", "#60a5fa"),
                         ("h3", "#f59e0b")):
            vals = [by[(s, m)][srv] for m in ("rr", "adaptive")]
            ax.bar(["RR", "Adapt."], vals, bottom=bottom, color=col,
                   label=srv)
            bottom = [b + v for b, v in zip(bottom, vals)]
        ax.set_title(LABEL[s], fontsize=9)
        ax.tick_params(labelsize=8)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Requests served")
    axes[-1].legend(frameon=False, fontsize=8)
    fig.suptitle("Which server handled the requests")
    fig.tight_layout()
    fig.savefig(os.path.join(GRAPHS, "distribution.png"), dpi=140)
    plt.close(fig)

    # Timelines for the dynamic scenarios (first repetition).
    for s in ("e3_overload", "e4_netload", "e5_crash", "e6_linkdown"):

        rs = {r["mode"]: r for r in runs if r["scenario"] == s
              and r["rep"] == 1}

        if len(rs) < 2:
            continue

        fig, ax = plt.subplots(figsize=(9, 3.8))

        for mode in ("rr", "adaptive"):
            run = rs[mode]
            t0 = run["t0"]
            bins = {}
            for r in run["rows"]:
                b = int(float(r["t_start"]) - t0)
                bins.setdefault(b, []).append(float(r["latency_ms"]))
            xs = sorted(b for b in bins if 0 <= b < run["duration"])
            ax.plot(xs, [statistics.mean(bins[b]) for b in xs],
                    marker=".", color=COLORS[mode], label=MODE_LABEL[mode])

        for a in rs["adaptive"]["actions"]:
            if a["action"] != "dump_flows":
                ax.axvline(a["at"], color="#b91c1c", ls="--", lw=1)
                ax.text(a["at"] + 0.15, ax.get_ylim()[1] * 0.92,
                        a["action"].replace("_", " "), color="#b91c1c",
                        fontsize=8)

        ax.set_xlabel("Time since clients started (s)")
        ax.set_ylabel("Avg response time per second (ms)")
        ax.set_title("%s - %s" % (LABEL[s], rs["rr"]["title"]), fontsize=9)
        ax.legend(frameon=False, loc="upper left")
        ax.grid(alpha=0.3)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(os.path.join(GRAPHS, "timeline_%s.png" % s), dpi=140)
        plt.close(fig)

    print("graphs written to", GRAPHS)


def main():

    global D2, RAW, GRAPHS

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--d2", default=D2, help="results/d2 folder")
    args = parser.parse_args()
    D2 = os.path.abspath(args.d2)
    RAW = os.path.join(D2, "raw")
    GRAPHS = os.path.join(D2, "graphs")

    runs = load_runs()

    if not runs:
        print("no runs found in", RAW)
        return

    metrics = [run_metrics(r) for r in runs]
    det_rows = [d for r in runs for d in detection(r)]
    summary = aggregate(metrics)

    scenarios = {}
    sc_path = os.path.join(D2, "scenarios.json")
    if os.path.exists(sc_path):
        with open(sc_path) as f:
            scenarios = json.load(f)

    os.makedirs(D2, exist_ok=True)
    write_csv(os.path.join(D2, "runs.csv"), metrics)
    write_csv(os.path.join(D2, "summary.csv"), summary)
    write_csv(os.path.join(D2, "detection.csv"), det_rows)
    write_markdown(summary, det_rows, scenarios)
    make_graphs(summary, runs)

    print("%d runs analysed; summary in %s" % (len(runs),
                                               os.path.join(D2, "summary.md")))


if __name__ == "__main__":
    main()
