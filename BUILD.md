# Building the executable

    .venv/Scripts/python.exe -m PyInstaller technical_drawing_sheet.spec --noconfirm

Output: `dist/TechnicalDrawingSheet.exe` — one file, copy it anywhere.

## Variants

| command | result |
|---|---|
| `... technical_drawing_sheet.spec` | single exe (default) |
| `... technical_drawing_sheet.spec -- --onedir` | folder build, starts instantly |
| `... technical_drawing_sheet.spec -- --console` | keeps a console window for diagnosis |

A onefile exe unpacks itself into a temp folder on every launch, which is
why the first window takes a few seconds. The folder build skips that.

## What the machine still needs

* **Microsoft Excel** — the side panel is real Excel embedded in the
  window. Without it the panel shows a message and PDF generation falls
  back to the in-process renderer.
* **ODA File Converter** — only to open `.dwg`. `.dxf` works without it.

Neither can be redistributed inside the bundle.

## Where to look when something goes wrong

`%LOCALAPPDATA%\TechnicalDrawingSheet\`

* `session.log` — everything the app logged, rotated
* `crash.log` — native faults, written by faulthandler
* `recovery.tdsheet` — the rolling autosave
* `recent.json` — recently opened projects

Help ▸ Open the Diagnostics Folder opens it from inside the app.
