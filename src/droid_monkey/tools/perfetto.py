"""Perfetto: record a system trace on the device while an attack runs, then analyse the app's CPU, threads and memory.

Recording uses the device's own `perfetto` binary (Android 9+): scheduler events, thread states and blocking reasons,
atrace slices (view, gfx, input, dalvik, binder) for the app, and process memory counters. Analysis runs Google's
trace_processor locally (the `perfetto` Python package downloads it on first use) with fixed SQL, and the diagnoser
can run its own read-only queries on the same trace.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb, AdbError

DEVICE_CONFIG = "/data/misc/perfetto-configs/droid-monkey.pbtxt"
DEVICE_TRACE = "/data/misc/perfetto-traces/droid-monkey.perfetto-trace"

CONFIG = """buffers {{ size_kb: 65536 fill_policy: RING_BUFFER }}
buffers {{ size_kb: 4096 fill_policy: RING_BUFFER }}
data_sources {{ config {{ name: "linux.ftrace" target_buffer: 0 ftrace_config {{
  ftrace_events: "sched/sched_switch" ftrace_events: "sched/sched_waking" ftrace_events: "sched/sched_blocked_reason"
  ftrace_events: "task/task_newtask" ftrace_events: "task/task_rename"
  atrace_categories: "view" atrace_categories: "gfx" atrace_categories: "am" atrace_categories: "wm"
  atrace_categories: "dalvik" atrace_categories: "input" atrace_categories: "binder_driver" atrace_categories: "res"
  atrace_apps: "{package}" }} }} }}
data_sources {{ config {{ name: "linux.process_stats" target_buffer: 1
  process_stats_config {{ scan_all_processes_on_start: true proc_stats_poll_ms: 250 }} }} }}
data_sources {{ config {{ name: "linux.sys_stats" target_buffer: 1 sys_stats_config {{
  meminfo_period_ms: 250 meminfo_counters: MEMINFO_MEM_AVAILABLE }} }} }}
duration_ms: {duration_ms}
"""

STATE_NAMES = {"Running": "running", "R": "waiting for a CPU", "R+": "waiting for a CPU (preempted)",
               "S": "sleeping/blocked (lock, sleep, wait)", "D": "blocked on I/O", "DK": "blocked on I/O"}


class Recording:
    def __init__(self, adb: Adb, package: str, pid: str):
        self.adb, self.package, self.pid = adb, package, pid

    def stop(self, out: Path, timeout: float = 20.0) -> Path:
        """Stop tracing and pull the trace file to `out`."""
        self.adb.shell(f"kill -TERM {self.pid}", check=False)
        deadline = time.time() + timeout
        while time.time() < deadline and self.adb.shell(f"ps -p {self.pid} -o PID=", check=False).strip():
            time.sleep(0.5)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.adb.run("pull", DEVICE_TRACE, str(out), timeout=120)
        self.adb.shell(f"rm -f {DEVICE_TRACE}", check=False)
        return out


def start(adb: Adb, package: str, local_tmp: Path, max_seconds: int = 180) -> Recording:
    """Start a background trace for `package`. Stop it with Recording.stop()."""
    local_tmp.parent.mkdir(parents=True, exist_ok=True)
    local_tmp.write_text(CONFIG.format(package=package, duration_ms=max_seconds * 1000))
    adb.run("push", str(local_tmp), DEVICE_CONFIG, timeout=30)
    adb.shell("pkill -f droid-monkey.pbtxt", check=False)  # a trace left running by an interrupted attack
    adb.shell(f"rm -f {DEVICE_TRACE}", check=False)
    pid = adb.shell(f"perfetto --background --txt -c {DEVICE_CONFIG} -o {DEVICE_TRACE}").strip().splitlines()
    if not pid or not pid[-1].strip().isdigit():
        raise AdbError(f"perfetto did not start: {' '.join(pid)[:200]}")
    time.sleep(1)  # let the data sources start before the attack does anything
    return Recording(adb, package, pid[-1].strip())


# ---- analysis ------------------------------------------------------------------------------------

@dataclass
class TraceAnalysis:
    seconds: float
    main_thread_states: dict[str, float]       # state -> ms
    long_main_slices: list[dict]               # {name, ms, states: {state: ms}, blocked_in: str}
    main_blocks_over_700ms: int                # top-level main-thread slices (input, frames, lifecycle) over 700 ms
    frames_over_16ms: int
    busiest_threads: list[dict]                # {thread, cpu_ms}
    app_cpu_ms: float
    gc: dict                                   # {count, ms}
    binder_on_main: list[dict]                 # {ms, reply_process}
    memory_mb: dict                            # counter -> {min, max, end}
    device_mem_available_mb: dict
    startups: list[dict] = field(default_factory=list)        # {type, ms} for each launch of the app
    slowest_startup: dict = field(default_factory=dict)       # {type, ms, main_thread: {state: ms}, slices: [...]}

    def to_dict(self) -> dict:
        return self.__dict__

    def summary(self) -> str:
        """A few lines for a person or a model: where the main thread's time went and what memory did."""
        lines = [f"trace: {self.seconds:.1f}s; app CPU {self.app_cpu_ms:.0f} ms; "
                 f"main-thread blocks over 700 ms: {self.main_blocks_over_700ms}; frames over 16 ms: {self.frames_over_16ms}"]
        total = sum(self.main_thread_states.values()) or 1
        lines.append("main thread: " + ", ".join(
            f"{STATE_NAMES.get(s, s)} {ms:.0f} ms ({100 * ms / total:.0f}%)"
            for s, ms in sorted(self.main_thread_states.items(), key=lambda kv: -kv[1])[:4]))
        for s in self.long_main_slices[:5]:
            states = ", ".join(f"{STATE_NAMES.get(k, k)} {v:.0f} ms" for k, v in s["states"].items())
            lines.append(f"long main-thread slice: '{s['name']}' {s['ms']:.0f} ms ({states})"
                         + (f"; blocked in {s['blocked_in']}" if s.get("blocked_in") else ""))
        if self.binder_on_main:
            lines.append("binder calls on the main thread over 20 ms: " + ", ".join(
                f"{b['ms']:.0f} ms to {b['reply_process']}" for b in self.binder_on_main[:5]))
        lines.append("busiest app threads (CPU): " + ", ".join(f"{t['thread']} {t['cpu_ms']:.0f} ms" for t in self.busiest_threads[:5]))
        if self.gc["count"]:
            lines.append(f"GC: {self.gc['count']} collections, {self.gc['ms']:.0f} ms")
        for name, v in self.memory_mb.items():
            lines.append(f"{name}: {v['min']:.1f} -> {v['max']:.1f} MB (end {v['end']:.1f} MB)")
        if self.startups:
            lines.append("launches: " + ", ".join(f"{x['type']} {x['ms']:.0f} ms" for x in self.startups))
        if self.slowest_startup:
            st = self.slowest_startup
            states = ", ".join(f"{STATE_NAMES.get(k, k)} {v:.0f} ms" for k, v in st["main_thread"].items())
            lines.append(f"slowest launch ({st['type']}, {st['ms']:.0f} ms), main thread: {states}")
            lines += [f"  {x['ms']:.0f} ms '{x['name']}'" for x in st["slices"]]
        if self.device_mem_available_mb:
            d = self.device_mem_available_mb
            lines.append(f"device MemAvailable: min {d['min']:.0f} MB, max {d['max']:.0f} MB")
        return "\n".join(lines)


def _rows(tp, sql: str) -> list:
    return list(tp.query(sql))


def analyse(trace: Path, package: str) -> TraceAnalysis:
    from perfetto.trace_processor import TraceProcessor
    tp = TraceProcessor(trace=str(trace))
    try:
        return _analyse(tp, package)
    finally:
        tp.close()


def _analyse(tp, package: str) -> TraceAnalysis:
    proc = _rows(tp, f"select upid, pid from process where name = '{package}' order by start_ts desc limit 1")
    bounds = _rows(tp, "select (end_ts - start_ts) / 1e9 as s from trace_bounds")
    seconds = bounds[0].s if bounds else 0.0
    if not proc:
        return TraceAnalysis(seconds, {}, [], 0, 0, [], 0.0, {"count": 0, "ms": 0.0}, [], {}, {})
    upid, pid = proc[0].upid, proc[0].pid
    main = _rows(tp, f"select utid from thread where upid = {upid} and tid = {pid}")
    utid = main[0].utid if main else -1

    states = {r.state: r.ms for r in _rows(tp, f"""
        select state, sum(dur) / 1e6 as ms from thread_state where utid = {utid} group by state""")}

    long_slices = []
    for r in _rows(tp, f"""
            select s.ts, s.dur, s.name, s.dur / 1e6 as ms from slice s join thread_track tt on s.track_id = tt.id
            where tt.utid = {utid} and s.depth = 0 and s.dur > 100e6 order by s.dur desc limit 8"""):
        within = {x.state: x.ms for x in _rows(tp, f"""
            select state, sum(min(ts + dur, {r.ts + r.dur}) - max(ts, {r.ts})) / 1e6 as ms from thread_state
            where utid = {utid} and ts < {r.ts + r.dur} and ts + dur > {r.ts} group by state order by ms desc""")}
        blocked = _rows(tp, f"""
            select blocked_function from thread_state where utid = {utid} and ts < {r.ts + r.dur} and ts + dur > {r.ts}
            and blocked_function is not null order by dur desc limit 1""")
        inner = _rows(tp, f"""
            select s.name, s.dur / 1e6 as ms from slice s join thread_track tt on s.track_id = tt.id
            where tt.utid = {utid} and s.depth > 0 and s.ts >= {r.ts} and s.ts < {r.ts + r.dur}
            order by s.dur desc limit 1""")
        name = r.name[:70] + (f" > {inner[0].name[:60]} ({inner[0].ms:.0f} ms)" if inner and inner[0].ms > 50 else "")
        long_slices.append({"name": name, "ms": round(r.ms, 1), "states": {k: round(v, 1) for k, v in within.items()},
                            "blocked_in": blocked[0].blocked_function if blocked else ""})

    frames = _rows(tp, f"""
        select sum(s.dur > 16.7e6) as slow from slice s
        join thread_track tt on s.track_id = tt.id where tt.utid = {utid} and s.name like 'Choreographer#doFrame%'""")
    busiest = [{"thread": r.name or "?", "cpu_ms": round(r.ms, 1)} for r in _rows(tp, f"""
        select t.name, sum(ts.dur) / 1e6 as ms from thread_state ts join thread t using (utid)
        where t.upid = {upid} and ts.state = 'Running' group by ts.utid order by ms desc limit 8""")]
    cpu = _rows(tp, f"""select sum(ts.dur) / 1e6 as ms from thread_state ts join thread t using (utid)
                         where t.upid = {upid} and ts.state = 'Running'""")
    gc = _rows(tp, f"""select count(*) as n, coalesce(sum(s.dur), 0) / 1e6 as ms from slice s
        join thread_track tt on s.track_id = tt.id join thread t using (utid)
        where t.upid = {upid} and (s.name like '%GC%' or s.name like 'concurrent copying%')""")
    binder = [{"ms": round(r.ms, 1), "reply_process": r.reply or "?"} for r in _rows(tp, f"""
        select s.dur / 1e6 as ms, (select p.name from flow f join slice rs on f.slice_in = rs.id
            join thread_track rt on rs.track_id = rt.id join thread th using (utid) join process p using (upid)
            where f.slice_out = s.id limit 1) as reply
        from slice s join thread_track tt on s.track_id = tt.id
        where tt.utid = {utid} and s.name = 'binder transaction' and s.dur > 20e6 order by s.dur desc limit 5""")]
    memory = {}
    for r in _rows(tp, f"""
            select ct.name, min(c.value) / 1048576.0 as mn, max(c.value) / 1048576.0 as mx,
                   (select c2.value from counter c2 where c2.track_id = ct.id order by c2.ts desc limit 1) / 1048576.0 as last
            from counter c join process_counter_track ct on c.track_id = ct.id
            where ct.upid = {upid} and ct.name in ('mem.rss', 'mem.rss.anon', 'mem.swap') group by ct.name"""):
        memory[r.name] = {"min": round(r.mn, 1), "max": round(r.mx, 1), "end": round(r.last, 1)}
    heap = _rows(tp, f"""select min(c.value) / 1024.0 as mn, max(c.value) / 1024.0 as mx,
            (select c2.value from counter c2 where c2.track_id = ct.id order by c2.ts desc limit 1) / 1024.0 as last
        from counter c join process_counter_track ct on c.track_id = ct.id
        where ct.upid = {upid} and ct.name = 'Heap size (KB)' group by ct.id""")
    if heap and heap[0].mx is not None:
        memory["java heap"] = {"min": round(heap[0].mn, 1), "max": round(heap[0].mx, 1), "end": round(heap[0].last, 1)}
    avail = _rows(tp, """select min(c.value) / 1048576.0 as mn, max(c.value) / 1048576.0 as mx from counter c
        join counter_track ct on c.track_id = ct.id where ct.name = 'MemAvailable'""")
    device = {"min": round(avail[0].mn, 1), "max": round(avail[0].mx, 1)} if avail and avail[0].mn is not None else {}

    startups, slowest = _startups(tp, package)
    return TraceAnalysis(
        startups=startups, slowest_startup=slowest,
        seconds=round(seconds, 1), main_thread_states={k: round(v, 1) for k, v in states.items()},
        long_main_slices=long_slices, main_blocks_over_700ms=sum(1 for x in long_slices if x["ms"] > 700),
        frames_over_16ms=int(frames[0].slow or 0) if frames else 0, busiest_threads=busiest,
        app_cpu_ms=round(cpu[0].ms or 0.0, 1) if cpu else 0.0,
        gc={"count": int(gc[0].n or 0), "ms": round(gc[0].ms or 0.0, 1)} if gc else {"count": 0, "ms": 0.0},
        binder_on_main=binder, memory_mb=memory, device_mem_available_mb=device)


def _startups(tp, package: str) -> tuple[list[dict], dict]:
    """Every launch of the app (Perfetto's startup module), and what the main thread did in the slowest one:
    its states and the longest named work (bindApplication, activityStart, inflate, the app's own trace sections)."""
    try:
        rows = _rows(tp, f"""INCLUDE PERFETTO MODULE android.startup.startups;
            select s.startup_id, s.startup_type, s.ts, s.dur, s.dur / 1e6 as ms, p.upid, p.pid
            from android_startups s left join android_startup_processes p using (startup_id)
            where s.package = '{package}' order by s.ts""")
    except Exception:  # older trace_processor without the module
        return [], {}
    startups = [{"type": r.startup_type, "ms": round(r.ms, 1)} for r in rows]
    if not rows:
        return startups, {}
    worst = max(rows, key=lambda r: r.dur)
    utid = _rows(tp, f"select utid from thread where upid = {worst.upid} and tid = {worst.pid}") if worst.upid else []
    if not utid:
        return startups, {"type": worst.startup_type, "ms": round(worst.ms, 1), "main_thread": {}, "slices": []}
    u, a, b = utid[0].utid, worst.ts, worst.ts + worst.dur
    states = {x.state: round(x.ms, 1) for x in _rows(tp, f"""
        select state, sum(min(ts + dur, {b}) - max(ts, {a})) / 1e6 as ms from thread_state
        where utid = {u} and ts < {b} and ts + dur > {a} group by state order by ms desc""")}
    slices = [{"name": x.name[:90], "ms": round(x.ms, 1), "depth": x.depth} for x in _rows(tp, f"""
        select s.name, s.dur / 1e6 as ms, s.depth from slice s join thread_track tt on s.track_id = tt.id
        where tt.utid = {u} and s.ts >= {a} and s.ts < {b} and s.depth <= 4 and s.dur > 5e6
        order by s.dur desc limit 10""")]
    return startups, {"type": worst.startup_type, "ms": round(worst.ms, 1), "main_thread": states, "slices": slices}


def query(trace: Path, sql: str, limit: int = 50) -> str:
    """Run one read-only SQL query on a trace (for the diagnoser). Returns rows as text."""
    head = sql.strip().split(None, 1)[0].lower() if sql.strip() else ""
    if head not in ("select", "with"):
        return "only SELECT/WITH queries are allowed"
    from perfetto.trace_processor import TraceProcessor
    tp = TraceProcessor(trace=str(trace))
    try:
        rows = list(tp.query(sql))
        if not rows:
            return "no rows"
        cols = list(rows[0].__dict__.keys())
        out = [" | ".join(cols)]
        for r in rows[:limit]:
            out.append(" | ".join(str(getattr(r, c))[:120] for c in cols))
        if len(rows) > limit:
            out.append(f"... {len(rows) - limit} more rows")
        return "\n".join(out)
    except Exception as e:  # a bad query is the model's to fix
        return f"query failed: {e}"[:600]
    finally:
        tp.close()
