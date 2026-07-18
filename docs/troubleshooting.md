# Troubleshooting

## MCP tools hang forever while every health check passes

**Symptom:** `rag_search` / `rag_add_issue` / `rag_index_project` calls from an MCP
client hang indefinitely (Claude Code backgrounds them at 120 s and gives up at
1800 s with "sent no response or progress"). Meanwhile everything *looks* healthy:

- `curl http://localhost:3000/` → 200 in milliseconds
- `docker ps` → `open-webui` healthy, `qdrant-vector-db` up
- `curl http://localhost:11434/api/version` → answers fine
- `systemctl is-active ollama` → active

**Root cause (case observed 2026-07-18):** the *inference* path is dead while the
*control* plane still answers. Ollama had the embedding model pinned to a GPU that
the NVIDIA driver had lost (`nvidia-smi`: "Unable to determine the device handle
for GPU0 … Unknown Error"), so every embedding request blocked forever — and every
RAG operation embeds. Health checks never exercise the embed path, so nothing
flagged it.

### Diagnosis (in order, ~1 minute)

```bash
# 1. Does an actual embedding complete? This is the real health check.
curl -m 10 http://localhost:11434/api/embed \
     -d '{"model":"nomic-embed-text","input":"ping"}'
# hang/timeout = inference path dead, keep going

# 2. Is the model pinned to a GPU?
ollama ps          # "100% GPU" + UNTIL "Forever" = pinned

# 3. Is the GPU alive?
nvidia-smi         # "Unknown Error" / "No devices were found" = wedged driver

# 4. What killed it?
journalctl -k --since "-24 hours" | grep -iE "xid|nvrm" | head
journalctl --since "-24 hours" -u systemd-suspend.service | tail
# Continuous "NVRM: _issueRpcAndWait: rpcSendMessage failed" = GSP RPC channel
# dead. A preceding failed suspend/hibernate is the usual trigger on this
# hardware (see the nvidia-*-gsp-* entries in common-issues).
```

### Recovery

1. **Reset the GPU without rebooting** — viable on hybrid laptops where the
   display runs on the iGPU (`cat /sys/module/nvidia_drm/refcnt` must be 0;
   if the display holds the dGPU, reboot instead):
   ```bash
   sudo systemctl stop ollama nvidia-persistenced   # release CUDA handles
   # wait for /sys/module/nvidia_uvm/refcnt to reach 0 — if a client is stuck
   # in the kernel it never releases, and reboot is the only way out
   sudo modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia
   echo 1 | sudo tee /sys/bus/pci/devices/0000:64:00.0/remove   # dGPU PCI addr
   echo 1 | sudo tee /sys/bus/pci/rescan   # re-enumerate → clean GSP re-init
   sudo modprobe nvidia_uvm nvidia_drm
   nvidia-smi                              # GPU should be back
   sudo systemctl start ollama
   ```
   (Scripted with guard rails in `~/.claude/scripts/nvidia-gpu-reset.sh` on
   this machine.) The PCI remove/rescan matters: it re-initializes the GSP
   firmware from scratch instead of reusing the dead RPC channel.
2. **If any step refuses** (module in use, uvm refcnt never drains, device
   doesn't reappear): reboot — a GSP wedge does not self-heal.
3. **Re-run anything that was dropped:** MCP writes that timed out were never
   stored — check the calling project's `docs/issues-outbox/` for entries
   written under the outage and flush them.

### Prevention ideas (not yet implemented)

- Health probe that does an end-to-end embed with a timeout (step-1 curl)
  instead of trusting HTTP 200s — candidates: compose healthcheck on a sidecar,
  or a `rag doctor` subcommand in `scripts/`.
- (CPU-only embedding was considered and rejected — inference on CPU is far too
  slow on this machine's models; the fix is GPU recovery, not GPU avoidance.)

### Incident log

- **2026-07-18** — suspend attempt failed 23:01 the night before
  (`systemd-suspend.service` FAILURE after ~1 min); kernel NVRM assertions from
  09:41; all MCP writes hung from late morning (two 1800 s timeouts observed in
  a Claude Code session on prism). Captures diverted to prism
  `docs/issues-outbox/`. GPU family bug refs in `common-issues`:
  `nvidia-595-open-gsp-hibernate-unload-timeout-battery-drain`,
  `nvidia-610-still-fails-gsp-hibernate-unload-driver-update-ruled-out`.
