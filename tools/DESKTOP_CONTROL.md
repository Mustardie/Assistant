# JARVIS desktop control

The general JARVIS runtime now has a verification-first Windows desktop layer.
It does not use blind keyboard or mouse automation for app/window management.

## Architecture

- `desktop_models.py` contains JSON-safe schemas for app identities, windows,
  desktop state, actions, risk, plans, and results.
- `app_catalog.py` normalizes common app names and aliases.
- `app_discovery.py` performs bounded discovery through configured aliases,
  Start Menu shortcuts, known install paths, PATH, Windows App Execution
  Aliases, and the Windows `App Paths` registry keys. It never recursively
  scans all of Program Files.
- `desktop_windows.py` wraps top-level Win32 window enumeration and state
  changes through `ctypes`. It does not synthesize keys or mouse input.
- `desktop_file_intent.py` chooses an app from a file type and identifies
  executable, installer, script, temporary/cache, and network-path risk.
- `desktop_planner.py` converts natural-language desktop requests into a
  non-executing action plan.
- `desktop_control.py` focuses existing windows, launches apps, verifies
  windows, opens/reveals files, and binds confirmations to exact handles/PIDs.

Optional configured aliases can be supplied without writing user data:

```powershell
$env:JARVIS_APP_ALIASES='{"My App":"D:\\Apps\\MyApp.exe"}'
```

Start Menu `.lnk` resolution uses optional `pywin32`. If it is missing, app
discovery continues with every other source and includes the missing capability
in not-found diagnostics.

## Tool surface

Read-only and low-risk tools:

- `desktop_get_state`, `desktop_active_window`, `desktop_list_windows`
- `desktop_plan`, `app_find`, `app_open`, `app_focus`
- `app_open_file`, `app_open_folder`, `app_show_in_folder`
- `app_minimize`, `app_maximize`, `app_restore`

Plan/confirm tools:

- `app_close_plan` → `app_close_confirmed`
- `process_kill_plan` → `process_kill_confirmed`

Close uses `WM_CLOSE`, which lets the app show its own save prompt. It never
falls through to process termination. Kill is a separate, high-risk action.
Confirmation IDs expire after five minutes and are bound to the exact selected
window handles or PID.

## File choices

- PDF → Microsoft Edge
- code/config → Visual Studio Code
- `.prproj` → Adobe Premiere Pro
- `.blend` → Blender
- text/log → Notepad
- image/video/audio → registered Windows default
- folder → File Explorer
- executable/installer → high-risk confirmation plan
- script → Visual Studio Code for inspection; risky locations require confirmation

Windows shell acknowledgements are not treated as verified success. If no
matching/new window can be observed, the result is `uncertain` with evidence
and suggested next steps.

## Real Windows validation still required

Automated tests use fake discovery, launchers, process termination, and window
APIs. A Windows smoke pass should validate:

1. Start Menu resolution both with and without `pywin32`.
2. Foreground activation restrictions across normal and elevated apps.
3. Packaged Store apps and App Execution Alias behavior.
4. Slow/splash-screen apps such as Premiere, Resolve, Blender, and Minecraft.
5. Explorer `/select,` behavior for paths containing spaces and Unicode.
6. Normal close behavior when an app presents a save/discard dialog.
7. Exact PID termination after a human reviews and confirms the plan.

