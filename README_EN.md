# Dev Console: edit code in a sandbox, verify with a probe, then apply

> AstrBot plugin · Staging, probe instance, fingerprint-gated apply

## The problem

The code being edited is the process the model itself is running in. Editing a `.py` doesn't hot-reload; if a restart crashes, there is nothing left to report the failure.

So writing and applying must be separated by a live observer — a probe instance.

## Features

- `dev_read_file` — read-only, supports offset for large files
- `dev_write_file` — writes to `staging/` only
- `dev_list_staging` — list candidates
- `dev_probe` — compileall + start on another port + health check + produce a patch
- `dev_apply` — apply, requires the last probe to PASS and the candidate fingerprint unchanged
- `dev_status` / `dev_journal`

## Install

### Option 1: Plugin marketplace (recommended)

Search "开发工位" in the AstrBot WebUI plugin marketplace.

### Option 2: Download from Releases

Grab the zip from [Releases](../../releases), then WebUI → Plugins → Upload.

### Option 3: Clone manually

```bash
cd AstrBot/data/plugins
git clone https://github.com/RandyLuaW/astrbot_plugin_dev_console.git
```

No extra Python dependencies.

## Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | See Chinese README |
| `inject_hint` | bool | `true` | See Chinese README |
| `max_read_lines` | int | `600` | See Chinese README |
| `max_write_bytes` | int | `204800` | See Chinese README |
| `probe_timeout` | int | `180` | See Chinese README |

## Notes

- Probe timeout defaults to 180s
- Single write capped at 200KB
- Admin-only, candidates must stay under `astrbot/` or `main.py`

## License

MIT
