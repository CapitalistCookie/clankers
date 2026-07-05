"""Remote system sampler — reads /proc twice over a short delta for instantaneous
rates, emits JSON with everything btop shows. Runs on the monitored host via
`ssh host python3 -` (fed on stdin). Pure stdlib; no deps.

Modes:
  (default)   one snapshot → print one JSON object, exit.
  `loop [s]`  stream a JSON object every iteration forever (for the real-time
              WebSocket view); exits cleanly when its stdout pipe closes (the SSH
              connection drops). Optional float arg overrides the sample delta.
"""
import glob, json, os, pwd, re, subprocess, sys, time

# Container/bridge plumbing that double-counts physical traffic (Proxmox veth pairs).
_SKIP_NET = ("veth", "fwbr", "fwpr", "fwln", "tap", "lo", "bonding_masters")
# Partition names (we keep the whole device, which already sums its partitions).
_PART = re.compile(r"(?:sd[a-z]+\d+|nvme\d+n\d+p\d+|mmcblk\d+p\d+|vd[a-z]+\d+)$")

CLK = os.sysconf("SC_CLK_TCK") or 100
PAGE = os.sysconf("SC_PAGE_SIZE") or 4096


def read_cpu():
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                p = line.split()
                out[p[0]] = list(map(int, p[1:]))
    return out


def cpu_pct(a, b):
    ta, tb = sum(a), sum(b)
    ia = a[3] + (a[4] if len(a) > 4 else 0)
    ib = b[3] + (b[4] if len(b) > 4 else 0)
    dt, di = tb - ta, ib - ia
    return max(0.0, min(100.0, 100.0 * (dt - di) / dt)) if dt > 0 else 0.0


def read_net():
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            iface, rest = line.split(":", 1)
            c = rest.split()
            out[iface.strip()] = (int(c[0]), int(c[8]))
    return out


def read_disk():
    out = {}
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            out[p[2]] = (int(p[5]) * 512, int(p[9]) * 512)  # bytes r/w (sector=512)
    return out


def read_pids():
    """pid -> (cpu_ticks, comm, rss_pages). One pass over /proc/[pid]/stat."""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                line = f.read()
        except OSError:
            continue
        rp = line.rfind(")")
        if rp < 0:
            continue
        comm = line[line.find("(") + 1:rp]
        rest = line[rp + 2:].split()
        try:
            utime, stime, rss = int(rest[11]), int(rest[12]), int(rest[21])
        except (IndexError, ValueError):
            continue
        out[int(d)] = (utime + stime, comm, rss)
    return out


def collect(delta=1.0):
    """One snapshot: two /proc samples `delta`s apart for instantaneous rates."""
    c1, n1, d1, p1 = read_cpu(), read_net(), read_disk(), read_pids()
    time.sleep(delta)
    c2, n2, d2, p2 = read_cpu(), read_net(), read_disk(), read_pids()

    overall = cpu_pct(c1["cpu"], c2["cpu"])
    cores, i = [], 0
    while f"cpu{i}" in c1 and f"cpu{i}" in c2:
        cores.append(round(cpu_pct(c1[f"cpu{i}"], c2[f"cpu{i}"]), 1))
        i += 1

    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.strip().split()[0]) * 1024
    mem_total = mem["MemTotal"]
    mem_avail = mem.get("MemAvailable", mem["MemFree"])
    swap_total = mem.get("SwapTotal", 0)

    load = open("/proc/loadavg").read().split()
    uptime = float(open("/proc/uptime").read().split()[0])

    # temp via k10temp (Tctl/Tdie preferred)
    temp = None
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(os.path.join(hw, "name")).read().strip() != "k10temp":
                continue
        except OSError:
            continue
        for ti in sorted(glob.glob(os.path.join(hw, "temp*_input"))):
            lp = ti.replace("_input", "_label")
            lbl = open(lp).read().strip() if os.path.exists(lp) else ""
            try:
                val = int(open(ti).read().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if lbl in ("Tctl", "Tdie") or temp is None:
                temp = val
                if lbl == "Tctl":
                    break

    freqs = []
    for fp in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            freqs.append(int(open(fp).read().strip()) / 1000.0)
        except (OSError, ValueError):
            pass
    freq_avg = round(sum(freqs) / len(freqs)) if freqs else None

    model = ""
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break

    # net rates — physical NICs + main bridges only (skip veth/fw* container
    # plumbing that double-counts the same packets).
    net = []
    for iface, (rx2, tx2) in n2.items():
        if iface.startswith(_SKIP_NET):
            continue
        if not iface.startswith(("en", "eth", "wl", "vmbr", "bond")):
            continue
        rx1, tx1 = n1.get(iface, (rx2, tx2))
        net.append({"iface": iface, "rx": max(0, rx2 - rx1), "tx": max(0, tx2 - tx1)})
    net.sort(key=lambda x: -(x["rx"] + x["tx"]))
    net = net[:6]

    # disk IO — whole physical devices only (skip partitions/loop/ram/dm/sr).
    dio = []
    for dev, (r2, w2) in d2.items():
        if dev.startswith(("loop", "ram", "dm-", "sr")) or _PART.match(dev):
            continue
        r1, w1 = d1.get(dev, (r2, w2))
        dr, dw = max(0, r2 - r1), max(0, w2 - w1)
        if dr or dw:
            dio.append({"dev": dev, "read": dr, "write": dw})
    dio.sort(key=lambda x: -(x["read"] + x["write"]))
    dio = dio[:6]

    # filesystems
    fs = []
    try:
        out = subprocess.check_output(
            ["df", "-PB1", "-x", "tmpfs", "-x", "devtmpfs", "-x", "overlay", "-x", "squashfs"],
            text=True, timeout=4)
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) < 6:
                continue
            total, used, mount = int(p[1]), int(p[2]), p[5]
            if mount.startswith(("/sys", "/proc", "/dev", "/run")):
                continue
            if total:
                fs.append({"mount": mount, "used": used, "total": total,
                           "pct": round(100 * used / total, 1)})
        fs.sort(key=lambda x: -x["pct"])
        fs = fs[:8]
    except Exception:
        pass

    # per-process instantaneous CPU% from the tick delta over the sample window
    rows = []
    for pid, (t2, comm, rss) in p2.items():
        t1 = p1.get(pid, (t2,))[0]
        dcpu = max(0, t2 - t1)
        pct = 100.0 * dcpu / CLK / max(delta, 0.001)
        rows.append((pct, pid, comm, rss))
    rows.sort(reverse=True)
    top = []
    for pct, pid, comm, rss in rows[:24]:
        try:
            user = pwd.getpwuid(os.stat(f"/proc/{pid}").st_uid).pw_name
        except (OSError, KeyError):
            user = "?"
        top.append({"pid": pid, "user": user, "cpu": round(pct, 1),
                    "mem": round(100 * rss * PAGE / mem_total, 1),
                    "rss": rss * PAGE, "cmd": comm})

    return {
        "host": os.uname().nodename, "model": model,
        "cpu": round(overall, 1), "cores": cores, "ncpu": len(cores),
        "freq": freq_avg, "temp": round(temp, 1) if temp else None,
        "load": [float(load[0]), float(load[1]), float(load[2])],
        "tasks": load[3], "uptime": uptime,
        "mem": {"used": mem_total - mem_avail, "total": mem_total, "avail": mem_avail,
                "cached": mem.get("Cached", 0), "buffers": mem.get("Buffers", 0)},
        "swap": {"used": swap_total - mem.get("SwapFree", 0), "total": swap_total},
        "net": net, "disk_io": dio, "fs": fs, "top": top,
    }


def _emit(obj):
    """Print one JSON line; return False if the pipe has closed (consumer gone)."""
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


if __name__ == "__main__":
    loop = "loop" in sys.argv[1:]
    delta = 1.0
    for a in sys.argv[1:]:
        try:
            delta = float(a)
        except ValueError:
            pass
    if not loop:
        _emit(collect(delta))
    else:
        # Stream forever; stop the moment our stdout pipe breaks (SSH dropped) so
        # we never leave an orphan sampler running on the host.
        while True:
            try:
                obj = collect(delta)
            except Exception as e:
                if not _emit({"error": f"{type(e).__name__}: {e}"}):
                    break
                time.sleep(delta)
                continue
            if not _emit(obj):
                break
