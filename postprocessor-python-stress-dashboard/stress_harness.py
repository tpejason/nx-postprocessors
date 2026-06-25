#!/usr/bin/env python3
"""
Stress-test harness — headless model × camera-count × device sweep for Nx AI Manager
====================================================================================
Drives the stress-dashboard postprocessor through a full sweep with ZERO GUI clicks
and saves one HTML + CSV report per step.

What it automates (all verified against Nx Witness 6.1.2 on the DELL box):
  • camera count   — PATCH /rest/v3/devices/{id} userEnabledAnalyticsEngineIds
                     (enable the Nx AI Manager engine on exactly the first N cameras,
                     disable it on every other compatible camera → clean count)
  • drive inference — ensure those N cameras are recording (PATCH schedule), so the
                     stream is consumed and the engine runs without anyone viewing
  • inference device — (optional) edit bin/runtime_args.json + mediaserver restart
  • MODEL (low/high/…) — (optional) PATCH each camera's device-agent settings
                     (parameters.nxAI: models + pipelines[].modelUUID + selectedPipeline)
                     then mediaserver restart so the runtime loads it. The model catalog
                     is harvested from the plugin logs (no clean REST/file lists it).
  • run + export    — web_app API: /api/session/start, /stop, /api/report.{html,csv}

The harness SNAPSHOTS each touched camera's engine list, recording schedule, and (when
sweeping models) its full nxAI config, and RESTORES them on exit — so the box returns
to its prior state. Model/device changes require --ssh-user/--ssh-pass (mediaserver
restart applies them).

Examples:
  # camera-count sweep on the currently-configured model, CPU
  python3 stress_harness.py --host <SERVER_IP> --nx-pass <NX_PASSWORD> \
      --counts 4,8,12,16,20,24 --model-label ppl-vehicle-low \
      --label DELL-Core-Ultra-7-265 --seconds 300 --outdir ~/Downloads

  # full matrix: two models × camera counts, switching models automatically (CPU)
  python3 stress_harness.py --host <SERVER_IP> --nx-pass <NX_PASSWORD> \
      --ssh-user nx --ssh-pass <NX_PASSWORD> \
      --models people-vehicle-low,people-vehicle-high --device cpu \
      --counts 4,8,12,16,20,24 --label DELL-Core-Ultra-7-265 \
      --seconds 300 --outdir ~/Downloads
"""
import argparse, json, os, ssl, sys, time, subprocess
import urllib.request, urllib.error, urllib.parse

AI_ENGINE_ID = "{cd976852-0a24-3774-823a-a8e222c551f9}"   # Nx AI Manager engine
# Server install differs by product: Nx Witness = /opt/networkoptix (+ networkoptix-mediaserver
# service); Nx Meta = /opt/networkoptix-metavms (+ networkoptix-metavms-mediaserver). The SSH-side
# helpers below probe Meta first, then fall back to Witness, so the harness works on either.
RUNTIME_ARGS = "/opt/networkoptix/mediaserver/var/nx_ai_manager/nxai_manager/bin/runtime_args.json"
_RUNTIME_ARGS_REL = "mediaserver/var/nx_ai_manager/nxai_manager/bin/runtime_args.json"

_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


# ──────────────────────────── small HTTP helpers ────────────────────────────
def _req(url, method="GET", token=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)


class Nx:
    def __init__(self, host, user, pw, port=7001):
        self.base = f"https://{host}:{port}"
        st, j = _req(f"{self.base}/rest/v3/login/sessions", "POST",
                     body={"username": user, "password": pw, "setCookie": False})
        self.token = j["token"]

    def devices(self):
        _, j = _req(f"{self.base}/rest/v3/devices"
                    "?_with=id,name,compatibleAnalyticsEngineIds,"
                    "userEnabledAnalyticsEngineIds,schedule", token=self.token)
        return j

    def patch_device(self, dev_id, payload):
        enc = urllib.parse.quote(dev_id)
        st, j = _req(f"{self.base}/rest/v3/devices/{enc}", "PATCH",
                     token=self.token, body=payload)
        return st

    def get_nxai(self, dev_id):
        """The device-agent settings VALUES for the AI Manager engine
        (parameters.nxAI): {models, pipelines, selectedPipeline, …}."""
        enc = urllib.parse.quote(dev_id)
        _, j = _req(f"{self.base}/rest/v3/devices/{enc}?_with=parameters", token=self.token)
        return ((j.get("parameters") or {}).get("nxAI")) or {}

    def patch_nxai(self, dev_id, nxai):
        return self.patch_device(dev_id, {"parameters": {"nxAI": nxai}})


# ──────────────────────────── camera selection ──────────────────────────────
def pick_test_cameras(devs, explicit):
    """Return [(id, name)] of cameras to use, in a stable order."""
    by_name = {d.get("name"): d for d in devs}
    compatible = [d for d in devs
                  if AI_ENGINE_ID in (d.get("compatibleAnalyticsEngineIds") or [])]
    if explicit:
        out = []
        for tok in explicit:
            d = by_name.get(tok) or next((x for x in devs if x["id"].strip("{}") == tok.strip("{}")), None)
            if not d:
                sys.exit(f"camera not found: {tok}")
            out.append(d)
        chosen = out
    else:
        chosen = sorted(compatible, key=lambda d: d["id"])
    return [(d["id"], d.get("name") or d["id"]) for d in chosen]


# ──────────────────────────── SSH-side controls ─────────────────────────────
def ssh(host, user, pw, cmd, timeout=120):
    # When the harness runs ON the target box (--host 127.0.0.1/localhost), execute the
    # command locally instead of via sshpass+ssh. This lets the whole sweep run server-side
    # (Mac-independent) on a box without sshpass/internet — the sudo -S commands work the
    # same locally. Remote hosts (e.g. Mac → DELL) still go over sshpass+ssh unchanged.
    if host in ("127.0.0.1", "localhost", "::1"):
        return subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    full = ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10", f"{user}@{host}", cmd]
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def restart_mediaserver(host, user, pw, wait=60):
    """Full VMS restart — needed for the inference runtime to (re)load the model
    configured per camera. Waits `wait` s for the server + model load to settle."""
    ssh(host, user, pw,
        f"echo '{pw}' | sudo -S sh -c 'service networkoptix-metavms-mediaserver restart "
        f"2>/dev/null || service networkoptix-mediaserver restart'",
        timeout=180)
    print(f"  [vms] mediaserver restarting, waiting {wait}s for model load …")
    time.sleep(wait)


def set_runtime_device(host, user, pw, device):
    """Write bin/runtime_args.json (CPU/GPU/NPU). Caller restarts the VMS."""
    import base64
    payload = json.dumps({"device_type": device.upper()})
    # Write runtime_args.json as ROOT under whichever server root exists (Meta first, then Witness),
    # creating it if absent. The JSON content is base64-encoded so its double-quotes can't collide
    # with the shell quoting (earlier bugs: piping JSON into `sudo -S` stdin where the password was
    # expected left the file empty; embedding raw JSON in a double-quoted `sh -c` broke on its quotes
    # — both silently left the runtime on CPU). base64 is shell-safe; password goes to `sudo -S`.
    b64 = base64.b64encode(payload.encode()).decode()
    remote = (
        "R=''; for r in networkoptix-metavms networkoptix; do "
        "[ -d /opt/$r/mediaserver ] && R=$r && break; done; "
        "[ -z \"$R\" ] && R=networkoptix; "
        f"echo '{pw}' | sudo -S sh -c 'echo {b64} | base64 -d > /opt/'$R'/{_RUNTIME_ARGS_REL}'"
    )
    ssh(host, user, pw, remote)
    print(f"  [device] inference device set to {device.upper()}")


# ──────────────────────────── model catalog + switching ─────────────────────
_HARVEST = r'''python3 - <<"PY"
import json
cat={}
for fn in ["/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_plugin_start.log",
           "/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_plugin.log",
           "/opt/networkoptix/mediaserver/var/nx_ai_manager/nxai_plugin_start.log",
           "/opt/networkoptix/mediaserver/var/nx_ai_manager/nxai_plugin.log"]:
    try: lines=open(fn,errors="replace")
    except: continue
    for line in lines:
        line=line.strip()
        if chr(34)+"models"+chr(34) not in line: continue
        try: d=json.loads(line)
        except: continue
        nx=(d.get("parameters") or {}).get("nxAI") or {}
        for m in nx.get("models") or []:
            u=m.get("UUID")
            if u and u not in cat: cat[u]=m
print(json.dumps(cat))
PY'''


def harvest_model_catalog(host, user, pw):
    """{uuid: model_object} for every model the AI Manager has seen — parsed from the
    plugin logs (the live catalog is cloud-synced; no clean local file/REST lists it)."""
    r = ssh(host, user, pw, _HARVEST, timeout=60)
    line = next((l for l in r.stdout.splitlines() if l.strip().startswith("{")), "{}")
    return json.loads(line)


def prime_catalog(nx, compat_ids, host, user, pw):
    """The AI plugin only writes the model catalog to its log once it initializes models for an
    engine-enabled camera; a mediaserver restart (or reboot) truncates that log, leaving harvest
    empty. Enable the engine + recording on ONE compatible camera and restart so the plugin
    re-initializes and re-logs the full catalog. The camera's ORIGINAL state is restored later by
    the harness's normal restore() (the snapshot is taken from the pre-prime device list)."""
    cid = sorted(compat_ids)[0]
    print(f"  [prime] enabling inference on one camera ({cid[:10]}…) + restart to re-log catalog")
    nx.patch_device(cid, {"userEnabledAnalyticsEngineIds": [AI_ENGINE_ID],
                          "schedule": {"isEnabled": True}})
    restart_mediaserver(host, user, pw, wait=60)


def resolve_models(tokens, catalog):
    """Map each --models token to a unique catalog model. A token is `selector` or
    `label=selector`, where selector is a UUID or a keyword string (all '-'/'_'-split
    words must appear in the model Name). The optional `label=` overrides the filename
    slug — use it for names that can't be disambiguated by keyword (e.g. 'People
    Detection (Low)' is a strict word-subset of 'People and Vehicles Detection (Low)',
    so pin it by UUID: `ppl-low=c1f893ab-...`).
    Returns [(slug, uuid, model_object)] in the given order. Exits on no/ambiguous match."""
    out = []
    for tok in tokens:
        label, _, sel = tok.strip().partition("=") if "=" in tok else ("", "", tok.strip())
        label, sel = label.strip(), sel.strip()
        if sel in catalog:                                 # exact UUID
            out.append((label or sel[:8], sel, catalog[sel])); continue
        words = [w for w in sel.lower().replace("_", "-").split("-") if w]
        hits = [(u, m) for u, m in catalog.items()
                if all(w in (m.get("Name") or "").lower() for w in words)]
        if not hits:
            sys.exit(f"--models: no model matches '{sel}'. Available: "
                     + "; ".join(sorted(m.get('Name') for m in catalog.values())))
        if len(hits) > 1:
            sys.exit(f"--models: '{sel}' is ambiguous -> "
                     + "; ".join(m.get('Name') for _, m in hits)
                     + ". Be more specific or pin it as label=<UUID>.")
        out.append((label or sel, hits[0][0], hits[0][1]))
    return out


def switch_model(nx, cams, nxai_snap, uuid, obj):
    """Set every pool camera to `uuid` by writing its device-agent nxAI: swap models +
    pipelines[].modelUUID + selectedPipeline, preserving the camera's Postprocessor.
    Needs a mediaserver restart afterwards for the runtime to load it."""
    for cid, _ in cams:
        base = nxai_snap.get(cid) or nx.get_nxai(cid)
        new = dict(base)
        new["models"] = [obj]
        pls = [dict(p) for p in (base.get("pipelines") or [])]
        if not pls:
            pls = [{"Postprocessor": "Stress Dashboard", "Preprocessor": "",
                    "chains": [], "modelNMS": 0.42, "resizingMethod": "Letterbox"}]
        pls[0]["modelUUID"] = uuid
        new["pipelines"] = pls
        new["selectedPipeline"] = uuid
        nx.patch_nxai(cid, new)


# ──────────────────────────── web_app run control ───────────────────────────
def start_run(webapp, name, model, cameras, resolution="vga", notes=""):
    st, j = _req(f"{webapp}/api/session/start", "POST",
                 body={"name": name, "model": model, "camera_count": cameras,
                       "resolution": resolution, "notes": notes})
    return j.get("id")


def stop_run(webapp):
    _req(f"{webapp}/api/session/stop", "POST", body={})


def export(webapp, sid, outdir, label, n, model_label, device):
    base = f"{label}-{n}CH-{model_label}-{device}".replace(" ", "_")
    for ext in ("html", "csv"):
        st, body = _req(f"{webapp}/api/report.{ext}?id={sid}")
        path = os.path.join(os.path.expanduser(outdir), base + "." + ext)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))
        print(f"  [export] {path}")


def live_total_fps(webapp):
    try:
        _, j = _req(f"{webapp}/api/live")
        return (j.get("now") or {}).get("total_fps")
    except Exception:
        return None


# ───────────────────────────── one camera-count sweep ───────────────────────
def count_sweep(nx, a, webapp, cams, compat_ids, snap, counts, model_label, device_label):
    for n in counts:
        on = cams[:n]
        on_ids = {cid for cid, _ in on}
        print(f"\n=== model={model_label} | {n} cameras ===")
        # enable engine on exactly the chosen N; disable on EVERY other AI-compatible
        # camera so only N channels report (clean count)
        for cid, _ in on:
            nx.patch_device(cid, {"userEnabledAnalyticsEngineIds": [AI_ENGINE_ID]})
        for cid in compat_ids - on_ids:
            nx.patch_device(cid, {"userEnabledAnalyticsEngineIds": []})
        # make sure the N cameras are recording so inference is driven
        for cid, _ in on:
            if not (snap[cid].get("sched") or {}).get("isEnabled"):
                nx.patch_device(cid, {"schedule": {"isEnabled": True}})
        print(f"  [warmup] {a.warmup}s for inference to ramp …"); time.sleep(a.warmup)
        print(f"  [warmup] live total FPS ≈ {live_total_fps(webapp)}")

        run_name = f"{a.label}-{n}CH-{model_label}-{device_label}"
        sid = start_run(webapp, run_name, model_label, n)
        print(f"  [run] session {sid} started; measuring {a.seconds}s …")
        time.sleep(a.seconds)
        stop_run(webapp)
        print(f"  [run] session {sid} stopped")
        export(webapp, sid, a.outdir, a.label, n, model_label, device_label)


# ─────────────────────────────────── main ───────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Headless model × camera-count × device sweep for Nx AI Manager stress tests")
    ap.add_argument("--host", required=True, help="Nx mediaserver IP/host")
    ap.add_argument("--nx-user", default="admin")
    ap.add_argument("--nx-pass", required=True)
    ap.add_argument("--webapp", help="stress-dashboard URL (default http://<host>:8120)")
    ap.add_argument("--ssh-user", help="SSH user (needed for --device and --models)")
    ap.add_argument("--ssh-pass", help="SSH password (needed for --device and --models)")
    ap.add_argument("--counts", default="4,8,12,16,20", help="comma list of camera counts to sweep")
    ap.add_argument("--device", choices=["cpu", "gpu", "npu"], help="set inference device before the sweep")
    ap.add_argument("--models", help="comma list of model keywords/UUIDs to sweep, e.g. "
                    "'people-vehicle-low,people-vehicle-high'. Each is PATCHed onto the pool "
                    "cameras + a mediaserver restart loads it. Omit to use the model already configured.")
    ap.add_argument("--model-label", default="model", help="filename label when --models is NOT used")
    ap.add_argument("--label", default="nx", help="host label prefix for report filenames")
    ap.add_argument("--cameras", help="explicit comma list of camera names/UUIDs (default: all AI-compatible, sorted)")
    ap.add_argument("--seconds", type=int, default=300, help="measurement duration per step")
    ap.add_argument("--warmup", type=int, default=30, help="seconds to let inference ramp before measuring")
    ap.add_argument("--outdir", default="~/Downloads")
    ap.add_argument("--no-restore", action="store_true", help="do NOT restore engine/model/schedule on exit")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    a = ap.parse_args()

    webapp = a.webapp or f"http://{a.host}:8120"
    counts = [int(x) for x in a.counts.split(",") if x.strip()]
    explicit = [x.strip() for x in a.cameras.split(",")] if a.cameras else None
    model_tokens = [x.strip() for x in a.models.split(",") if x.strip()] if a.models else None

    need_ssh = bool(a.device or model_tokens)
    if need_ssh and not (a.ssh_user and a.ssh_pass):
        sys.exit("--device / --models require --ssh-user and --ssh-pass (a mediaserver restart applies them)")

    nx = Nx(a.host, a.nx_user, a.nx_pass)
    devs = nx.devices()
    pool = pick_test_cameras(devs, explicit)        # rotation pool (explicit set, or all compatible sorted)
    maxn = max(counts)
    if len(pool) < maxn:
        sys.exit(f"only {len(pool)} cameras in the pool, need {maxn}")
    cams = pool[:maxn]                              # the cameras the sweep cycles through
    # Disable universe = EVERY AI-compatible camera on the system, so an N-camera step
    # reports exactly N even when --cameras pins a subset.
    compat_ids = {d["id"] for d in devs if AI_ENGINE_ID in (d.get("compatibleAnalyticsEngineIds") or [])}

    # Resolve the model sweep. Default = a single pass on whatever model is already set.
    model_plan = [(a.model_label, None, None)]
    if model_tokens:
        catalog = harvest_model_catalog(a.host, a.ssh_user, a.ssh_pass)
        if not catalog and not a.dry_run:
            print("[prime] model catalog empty (plugin logs truncated by a restart) — priming …")
            prime_catalog(nx, compat_ids, a.host, a.ssh_user, a.ssh_pass)
            catalog = harvest_model_catalog(a.host, a.ssh_user, a.ssh_pass)
        if not catalog:
            sys.exit("could not read the model catalog from the plugin logs"
                     + (" — dry-run skips auto-priming; run for real to populate it"
                        if a.dry_run else ""))
        model_plan = resolve_models(model_tokens, catalog)

    device_label = a.device or "asis"
    print(f"Target {a.host} | webapp {webapp} | device {device_label}")
    print(f"Models: {[s for s,_,_ in model_plan]}")
    print(f"Counts: {counts} over {len(cams)} cameras; {a.seconds}s each (+{a.warmup}s warmup)")
    for i, (cid, nm) in enumerate(cams, 1):
        print(f"  {i:>2}. {nm}  {cid}")
    if a.dry_run:
        if model_tokens:
            for s, u, o in model_plan:
                print(f"  model '{s}' -> {o.get('Name')} ({u})")
        print("\n[dry-run] no changes made."); return

    # snapshot for restore: engine + schedule for all; nxAI for the pool (model switching)
    snap = {d["id"]: {"eng": d.get("userEnabledAnalyticsEngineIds") or [],
                      "sched": d.get("schedule")} for d in devs}
    nxai_snap = {}
    if model_tokens:
        print("[snapshot] reading current per-camera model config …")
        for cid, _ in cams:
            nxai_snap[cid] = nx.get_nxai(cid)

    def restore():
        if a.no_restore:
            print("[restore] skipped (--no-restore)"); return
        print("[restore] restoring engine assignments …")
        for cid in compat_ids:
            nx.patch_device(cid, {"userEnabledAnalyticsEngineIds": snap[cid]["eng"]})
        if model_tokens:
            print("[restore] restoring per-camera model config …")
            for cid, _ in cams:
                if cid in nxai_snap:
                    nx.patch_nxai(cid, nxai_snap[cid])
            restart_mediaserver(a.host, a.ssh_user, a.ssh_pass, wait=30)

    try:
        if a.device:
            set_runtime_device(a.host, a.ssh_user, a.ssh_pass, a.device)
            if not model_tokens:
                # no per-model restart will happen, so apply the device now
                restart_mediaserver(a.host, a.ssh_user, a.ssh_pass)

        for slug, uuid, obj in model_plan:
            if uuid:
                print(f"\n##### MODEL: {slug} -> {obj.get('Name')} #####")
                switch_model(nx, cams, nxai_snap, uuid, obj)
                restart_mediaserver(a.host, a.ssh_user, a.ssh_pass)
            count_sweep(nx, a, webapp, cams, compat_ids, snap, counts, slug, device_label)
    finally:
        restore()
        print("\nDone.")


if __name__ == "__main__":
    main()
