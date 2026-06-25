#!/usr/bin/env python3
"""
System metric collectors for the Stress Dashboard web app.
=========================================================

Whole-machine CPU / RAM / GPU / NPU sampling, every source auto-detected at
startup and degrading gracefully when unavailable. Designed for Linux Nx
servers; on the Intel Core Ultra reference box specifically:

  * CPU / RAM  -> psutil
  * Intel NPU  -> intel_vpu sysfs (/sys/class/accel/accel*/device/):
                  npu_busy_time_us (cumulative -> util%), npu_memory_utilization,
                  npu_current/max_frequency_mhz. Readable without root.
  * Intel iGPU -> 'xe' (or i915) DRM driver. True engine utilisation via
                  per-client drm-engine-* fdinfo counters (needs root to read
                  the mediaserver's fds); falls back to act/max frequency ratio
                  (no root) when fdinfo is unreadable.
  * NVIDIA     -> pynvml (NVML) if a GPU and the library are present.

Each collector exposes:
  - .available  : bool
  - .label      : human-readable name
  - .sample()   : dict of current readings (or {} if unavailable)

A reading dict uses these conventional keys where applicable:
  util_pct, util_mode ("engine"|"freq-proxy"|...), mem_used_mb, mem_total_mb,
  mem_pct, freq_mhz, freq_max_mhz, power_w, temp_c, name.
"""
import os
import glob
import time
import shutil
import logging
import re
import subprocess
import functools

logger = logging.getLogger("stress-dashboard.metrics")

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


# ───────────────────────────────────────────────────────────────────────────
#  Static hardware spec (for the report's Run Configuration). Computed once;
#  every field is a human-readable string or "n/a" when undetectable.
# ───────────────────────────────────────────────────────────────────────────

def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _lspci_desc(line):
    """Pull the human device name out of an `lspci -nn` line, dropping the
    leading slot/class and the trailing [vendor:device] id and (rev ..).
    e.g. '00:02.0 VGA ... [0300]: Intel Corporation Arc Graphics [8086:7d67] (rev 06)'
         -> 'Intel Corporation Arc Graphics'"""
    if not line:
        return None
    desc = line.split("]: ", 1)[1] if "]: " in line else line
    desc = re.sub(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]", "", desc)  # drop [vendor:device]
    desc = re.sub(r"\s*\(rev [0-9a-fx]+\)\s*$", "", desc)            # drop (rev xx)
    return desc.strip() or None


def _spec_cpu():
    try:
        for ln in open("/proc/cpuinfo"):
            if ln.lower().startswith("model name"):
                name = ln.split(":", 1)[1].strip()
                threads = os.cpu_count() or 0
                return f"{name} ({threads} threads)" if threads else name
    except Exception:
        pass
    return "n/a"


def _spec_ram():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemTotal"):
                return f"{int(ln.split()[1]) / 1048576:.1f} GB"
    except Exception:
        pass
    return "n/a"


def _spec_gpu():
    out = _run(["bash", "-c", "lspci -nn 2>/dev/null | grep -iE 'VGA compatible controller|Display controller|3D controller'"])
    if out.strip():
        descs = [_lspci_desc(l) for l in out.strip().splitlines()]
        descs = [d for d in descs if d]
        if descs:
            return " / ".join(dict.fromkeys(descs))  # dedupe, keep order
    nv = _run(["bash", "-c", "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null"])
    if nv.strip():
        return nv.strip().splitlines()[0].strip()
    return "n/a"


def _spec_npu():
    out = _run(["bash", "-c", "lspci -nn 2>/dev/null | grep -iE 'neural|processing accelerator|\\bNPU\\b|\\bVPU\\b'"])
    if out.strip():
        d = _lspci_desc(out.strip().splitlines()[0])
        if d:
            return d
    # Fallback: intel_vpu exposes an accel device even when lspci lacks a clear name
    for accel in glob.glob("/sys/class/accel/accel*"):
        drv = os.path.join(accel, "device", "driver")
        name = os.path.basename(os.path.realpath(drv)) if os.path.exists(drv) else ""
        return f"Intel NPU ({name})" if name else "Intel NPU"
    return "n/a"


@functools.lru_cache(maxsize=1)
def get_hardware_specs():
    """Static host spec for the Run Configuration table. Each value is a string
    or 'n/a'. Cached — hardware does not change while the app runs."""
    specs = {"cpu": _spec_cpu(), "gpu": _spec_gpu(), "npu": _spec_npu(), "ram": _spec_ram()}
    logger.info("Hardware specs: %s", specs)
    return specs


_device_cache = {"ts": 0.0, "val": "n/a"}


def get_inference_device():
    """The AI Manager inference device (CPU/GPU/NPU) read from runtime_args.json.
    Lightly cached (5s) so a device_type change is reflected without restarting the
    web app. Returns 'n/a' if the file is absent/empty (engine default = CPU)."""
    import json as _j
    now = time.time()
    if now - _device_cache["ts"] < 5.0:
        return _device_cache["val"]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "bin", "runtime_args.json"),
        "/opt/networkoptix/mediaserver/var/nx_ai_manager/nxai_manager/bin/runtime_args.json",
        "/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/bin/runtime_args.json",
    ]
    val = "n/a"
    for p in candidates:
        try:
            with open(p) as f:
                dt = (_j.load(f) or {}).get("device_type")
            if dt:
                val = str(dt).upper()
                break
        except Exception:
            continue
    _device_cache.update(ts=now, val=val)
    return val


# ───────────────────────────────────────────────────────────────────────────
#  Per-camera stream info from the Nx Server REST API (primary/secondary
#  resolution + fps). Used to enrich the per-camera report table. Best-effort:
#  returns {} if the API is unreachable, so the report degrades to n/a.
# ───────────────────────────────────────────────────────────────────────────

import json as _json
import ssl as _ssl
import urllib.request as _urlreq

_nx_cfg = {"url": "https://127.0.0.1:7001", "user": "admin", "password": "admin"}
_nx_cache = {"ts": 0.0, "data": {}}
_NX_TTL = 30.0  # seconds


def set_nx_credentials(url=None, user=None, password=None):
    if url:
        _nx_cfg["url"] = url.rstrip("/")
    if user:
        _nx_cfg["user"] = user
    if password is not None:
        _nx_cfg["password"] = password


def _nx_ctx():
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


def _nx_token(ctx):
    body = _json.dumps({"username": _nx_cfg["user"], "password": _nx_cfg["password"]}).encode()
    req = _urlreq.Request(_nx_cfg["url"] + "/rest/v3/login/sessions", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
    with _urlreq.urlopen(req, timeout=4, context=ctx) as r:
        return _json.load(r)["token"]


def _norm_id(dev_id):
    return (dev_id or "").strip("{}").lower()


def get_camera_streams():
    """{normalized_device_id: {'primary': {'res','fps'}, 'secondary': {'res','fps'}}}.
    Cached for _NX_TTL seconds. Empty dict if the Nx API can't be reached."""
    now = time.time()
    if _nx_cache["data"] and now - _nx_cache["ts"] < _NX_TTL:
        return _nx_cache["data"]
    out = {}
    try:
        ctx = _nx_ctx()
        tok = _nx_token(ctx)
        req = _urlreq.Request(_nx_cfg["url"] + "/rest/v3/devices",
                              headers={"Authorization": "Bearer " + tok})
        with _urlreq.urlopen(req, timeout=6, context=ctx) as r:
            devs = _json.load(r)
        for d in devs if isinstance(devs, list) else []:
            did = _norm_id(d.get("id"))
            if not did:
                continue
            info = {"primary": None, "secondary": None, "model": None}
            # Per-camera AI model, from parameters.nxAI (device-agent settings values).
            # Active model = selectedPipeline if set, else the first configured pipeline's
            # modelUUID; resolve UUID -> human name via the device's own nxAI.models list.
            try:
                nxai = (d.get("parameters") or {}).get("nxAI") or {}
                if isinstance(nxai, str):
                    nxai = _json.loads(nxai)
                sel = nxai.get("selectedPipeline")
                pls = nxai.get("pipelines") or []
                if not sel and pls:
                    sel = pls[0].get("modelUUID")
                for m in nxai.get("models") or []:
                    if m.get("UUID") == sel:
                        info["model"] = m.get("Name"); break
                if info["model"] is None and sel:
                    info["model"] = str(sel)[:8]
            except Exception:
                pass
            streams = (((d.get("parameters") or {}).get("bitrateInfos") or {}).get("streams")) or []
            for s in streams:
                idx = s.get("encoderIndex")
                key = ("primary" if idx in ("primary", 0, "0")
                       else "secondary" if idx in ("secondary", 1, "1") else None)
                if not key:
                    continue
                actual = s.get("actualFps")
                fps = int(round(actual)) if actual else (s.get("fps") or None)
                info[key] = {"res": s.get("resolution") or "?", "fps": fps}
            # Fall back to mediaStreams (resolution only) for any stream params missed.
            for ms in d.get("mediaStreams") or []:
                ei = ms.get("encoderIndex")
                key = "primary" if ei == 0 else "secondary" if ei == 1 else None
                if key and not info[key]:
                    info[key] = {"res": ms.get("resolution") or "?", "fps": None}
            out[did] = info
    except Exception as e:
        logger.warning("Nx stream query failed: %s", e)
    if out:
        _nx_cache.update(ts=now, data=out)
    return out


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
#  CPU / RAM
# ════════════════════════════════════════════════════════════════════════════

class CpuRamCollector:
    label = "CPU / RAM"

    def __init__(self):
        self.available = psutil is not None
        if self.available:
            # Prime cpu_percent so the first real sample is meaningful.
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                self.available = False

    def sample(self):
        if not self.available:
            return {}
        try:
            vm = psutil.virtual_memory()
            return {
                "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
                "cpu_per_core": [round(x, 1) for x in psutil.cpu_percent(interval=None, percpu=True)],
                "ram_pct": round(vm.percent, 1),
                "ram_used_mb": round(vm.used / 1024 / 1024, 1),
                "ram_total_mb": round(vm.total / 1024 / 1024, 1),
            }
        except Exception as e:
            logger.debug("CPU/RAM sample failed: %s", e)
            return {}


# ════════════════════════════════════════════════════════════════════════════
#  Intel NPU (intel_vpu)
# ════════════════════════════════════════════════════════════════════════════

class IntelNpuCollector:
    label = "Intel NPU"

    def __init__(self):
        self.device_dir = None
        self.available = False
        for accel in sorted(glob.glob("/sys/class/accel/accel*")):
            dev = os.path.join(accel, "device")
            if os.path.exists(os.path.join(dev, "npu_busy_time_us")):
                self.device_dir = dev
                self.available = True
                break
        self._last_busy_us = None
        self._last_t = None
        if self.available:
            self._last_busy_us = _read_int(os.path.join(self.device_dir, "npu_busy_time_us"))
            self._last_t = time.monotonic()
            logger.info("Intel NPU detected at %s", self.device_dir)

    def sample(self):
        if not self.available:
            return {}
        d = self.device_dir
        out = {}
        busy = _read_int(os.path.join(d, "npu_busy_time_us"))
        now = time.monotonic()
        if busy is not None and self._last_busy_us is not None and self._last_t is not None:
            dt = now - self._last_t
            dbusy = busy - self._last_busy_us
            if dt > 0 and dbusy >= 0:
                util = (dbusy / 1e6) / dt * 100.0
                out["util_pct"] = round(min(max(util, 0.0), 100.0), 1)
                out["util_mode"] = "busy-time"
        self._last_busy_us = busy if busy is not None else self._last_busy_us
        self._last_t = now

        mem = _read_int(os.path.join(d, "npu_memory_utilization"))
        if mem is not None:
            out["mem_used_mb"] = round(mem / 1024 / 1024, 1)
        cur = _read_int(os.path.join(d, "npu_current_frequency_mhz"))
        mx = _read_int(os.path.join(d, "npu_max_frequency_mhz"))
        if cur is not None:
            out["freq_mhz"] = cur
        if mx is not None:
            out["freq_max_mhz"] = mx
        return out


# ════════════════════════════════════════════════════════════════════════════
#  Intel iGPU (xe / i915)
# ════════════════════════════════════════════════════════════════════════════

class IntelIgpuCollector:
    """Intel integrated GPU utilisation.

    Strategy:
      1. If we can read DRM per-client fdinfo (root, and the driver exposes
         drm-engine-<class> busy nanoseconds), aggregate busy time across all
         clients of this GPU -> true engine utilisation %.
      2. Otherwise fall back to the act/max GT frequency ratio as a coarse
         load proxy (always readable, no root).
    """
    label = "Intel iGPU"

    # DRM render-capable Intel drivers, in card scan order.
    _INTEL_DRIVERS = ("xe", "i915")

    def __init__(self):
        self.available = False
        self.card = None          # e.g. /sys/class/drm/card1
        self.driver = None
        self.pdev = None          # PCI address string, e.g. 0000:00:02.0
        self.freq_act_path = None
        self.freq_max_path = None
        self._fdinfo_ok = False
        self._last_engine_busy = {}   # engine class -> last busy ns
        self._last_t = None

        self._detect()
        if self.available:
            logger.info("Intel iGPU detected: card=%s driver=%s pdev=%s fdinfo=%s",
                        self.card, self.driver, self.pdev, self._fdinfo_ok)

    def _detect(self):
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if "-" in os.path.basename(card):   # skip connectors like card1-HDMI-A-1
                continue
            drv_link = os.path.join(card, "device", "driver")
            driver = os.path.basename(os.path.realpath(drv_link)) if os.path.exists(drv_link) else None
            if driver not in self._INTEL_DRIVERS:
                continue
            self.card = card
            self.driver = driver
            self.pdev = os.path.basename(os.path.realpath(os.path.join(card, "device")))
            self.available = True
            self._find_freq_paths()
            self._probe_fdinfo()
            break

    def _find_freq_paths(self):
        dev = os.path.join(self.card, "device")
        candidates_act = [
            os.path.join(dev, "drm", os.path.basename(self.card), "gt", "gt0", "rps_act_freq_mhz"),
            os.path.join(dev, "tile0", "gt0", "freq0", "act_freq_mhz"),
        ]
        candidates_max = [
            os.path.join(dev, "drm", os.path.basename(self.card), "gt", "gt0", "rps_max_freq_mhz"),
            os.path.join(dev, "tile0", "gt0", "freq0", "max_freq_mhz"),
        ]
        # Also try the direct drm path (kernel 6.x xe layout).
        candidates_act.insert(0, os.path.join(self.card, "gt", "gt0", "rps_act_freq_mhz"))
        candidates_max.insert(0, os.path.join(self.card, "gt", "gt0", "rps_max_freq_mhz"))
        for c in candidates_act:
            if os.path.exists(c):
                self.freq_act_path = c
                break
        for c in candidates_max:
            if os.path.exists(c):
                self.freq_max_path = c
                break

    def _iter_fdinfo_for_pdev(self):
        """Yield parsed fdinfo dicts for DRM clients on this GPU's pdev."""
        for fdinfo in glob.glob("/proc/[0-9]*/fdinfo/*"):
            try:
                with open(fdinfo) as f:
                    text = f.read()
            except Exception:
                continue
            if "drm-pdev" not in text or self.pdev not in text:
                continue
            info = {}
            for line in text.splitlines():
                if line.startswith("drm-"):
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
            if info.get("drm-pdev") == self.pdev:
                yield info

    def _probe_fdinfo(self):
        """Decide whether fdinfo-based utilisation is usable."""
        try:
            for info in self._iter_fdinfo_for_pdev():
                if any(k.startswith("drm-engine-") for k in info):
                    self._fdinfo_ok = True
                    break
        except Exception as e:
            logger.debug("fdinfo probe failed: %s", e)
        # We may have found no active client yet; still mark usable if we are
        # root (counters will appear once the GPU is used).
        if not self._fdinfo_ok and os.geteuid() == 0:
            self._fdinfo_ok = True

    @staticmethod
    def _engine_class(key):
        # drm-engine-render / -compute / -copy / -video / -video-enhance
        return key[len("drm-engine-"):]

    def _sample_fdinfo(self):
        busy_by_engine = {}
        for info in self._iter_fdinfo_for_pdev():
            for k, v in info.items():
                if k.startswith("drm-engine-"):
                    ns = self._parse_ns(v)
                    if ns is not None:
                        eng = self._engine_class(k)
                        busy_by_engine[eng] = busy_by_engine.get(eng, 0) + ns
        now = time.monotonic()
        out = {}
        if self._last_t is not None and busy_by_engine:
            dt = now - self._last_t
            if dt > 0:
                per_engine = {}
                for eng, ns in busy_by_engine.items():
                    last = self._last_engine_busy.get(eng, ns)
                    dns = ns - last
                    if dns < 0:
                        dns = 0
                    pct = (dns / 1e9) / dt * 100.0
                    per_engine[eng] = round(min(max(pct, 0.0), 100.0), 1)
                if per_engine:
                    # Report the busiest engine as the headline utilisation.
                    out["util_pct"] = max(per_engine.values())
                    out["util_mode"] = "engine"
                    out["engines"] = per_engine
        self._last_engine_busy = busy_by_engine
        self._last_t = now
        return out

    @staticmethod
    def _parse_ns(v):
        # value looks like "123456 ns"
        try:
            return int(v.split()[0])
        except Exception:
            return None

    def _sample_freq_proxy(self):
        act = _read_int(self.freq_act_path) if self.freq_act_path else None
        mx = _read_int(self.freq_max_path) if self.freq_max_path else None
        out = {}
        if act is not None:
            out["freq_mhz"] = act
        if mx is not None:
            out["freq_max_mhz"] = mx
        if act is not None and mx and mx > 0:
            out["util_pct"] = round(min(act / mx * 100.0, 100.0), 1)
            out["util_mode"] = "freq-proxy"
        return out

    def sample(self):
        if not self.available:
            return {}
        out = {}
        if self._fdinfo_ok:
            out = self._sample_fdinfo()
        if "util_pct" not in out:
            # No engine data (idle/no client/no perms) -> frequency proxy.
            proxy = self._sample_freq_proxy()
            # Keep engine breakdown if we had one but no headline.
            proxy.update({k: v for k, v in out.items() if k == "engines"})
            out = proxy
        else:
            # Augment engine reading with frequency info too.
            out.update({k: v for k, v in self._sample_freq_proxy().items()
                        if k in ("freq_mhz", "freq_max_mhz")})
        out["driver"] = self.driver
        return out


# ════════════════════════════════════════════════════════════════════════════
#  NVIDIA (NVML)
# ════════════════════════════════════════════════════════════════════════════

class NvidiaCollector:
    label = "NVIDIA GPU"

    def __init__(self):
        self.available = False
        self._nvml = None
        self._handles = []
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            count = pynvml.nvmlDeviceGetCount()
            self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
            self.available = count > 0
            if self.available:
                logger.info("NVIDIA detected: %d GPU(s)", count)
        except Exception as e:
            logger.debug("NVIDIA/NVML not available: %s", e)
            self.available = False

    def sample(self):
        if not self.available:
            return {}
        p = self._nvml
        gpus = []
        for i, h in enumerate(self._handles):
            g = {"index": i}
            try:
                g["name"] = p.nvmlDeviceGetName(h)
                if isinstance(g["name"], bytes):
                    g["name"] = g["name"].decode()
            except Exception:
                pass
            try:
                util = p.nvmlDeviceGetUtilizationRates(h)
                g["util_pct"] = float(util.gpu)
                g["mem_util_pct"] = float(util.memory)
            except Exception:
                pass
            try:
                mem = p.nvmlDeviceGetMemoryInfo(h)
                g["mem_used_mb"] = round(mem.used / 1024 / 1024, 1)
                g["mem_total_mb"] = round(mem.total / 1024 / 1024, 1)
            except Exception:
                pass
            try:
                g["power_w"] = round(p.nvmlDeviceGetPowerUsage(h) / 1000.0, 1)
            except Exception:
                pass
            try:
                g["temp_c"] = p.nvmlDeviceGetTemperature(h, p.NVML_TEMPERATURE_GPU)
            except Exception:
                pass
            gpus.append(g)
        # Headline = busiest GPU.
        head = {}
        utils = [g["util_pct"] for g in gpus if "util_pct" in g]
        if utils:
            head["util_pct"] = max(utils)
            head["util_mode"] = "nvml"
        head["gpus"] = gpus
        return head


# ════════════════════════════════════════════════════════════════════════════
#  Aggregator
# ════════════════════════════════════════════════════════════════════════════

class MetricsManager:
    """Detects and samples all available metric sources."""

    def __init__(self):
        self.cpu = CpuRamCollector()
        self.npu = IntelNpuCollector()
        self.igpu = IntelIgpuCollector()
        self.nvidia = NvidiaCollector()

    def sources(self):
        """Report which sources are live, for the UI to lay out panels."""
        return {
            "cpu_ram": self.cpu.available,
            "intel_npu": self.npu.available,
            "intel_igpu": self.igpu.available,
            "nvidia": self.nvidia.available,
            "igpu_mode": ("engine" if self.igpu.available and self.igpu._fdinfo_ok else
                          ("freq-proxy" if self.igpu.available else None)),
            "root": (os.geteuid() == 0),
        }

    def sample(self):
        """One combined reading. Missing sources are simply absent."""
        out = {}
        cpu = self.cpu.sample()
        if cpu:
            out["cpu"] = cpu
        npu = self.npu.sample()
        if npu:
            out["npu"] = npu
        igpu = self.igpu.sample()
        if igpu:
            out["igpu"] = igpu
        nv = self.nvidia.sample()
        if nv:
            out["nvidia"] = nv
        return out


if __name__ == "__main__":
    # Quick self-test: print sources and two samples a second apart.
    logging.basicConfig(level=logging.INFO)
    m = MetricsManager()
    import json
    print("SOURCES:", json.dumps(m.sources(), indent=2))
    m.sample()
    time.sleep(1.0)
    print("SAMPLE:", json.dumps(m.sample(), indent=2))
