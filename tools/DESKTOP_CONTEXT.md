# JARVIS desktop context and habit learning

This layer adds opt-in, local desktop awareness on top of the safe app/window
control service. Monitoring is disabled by default. When disabled, event calls
return `stored: false` and do not create activity records.

## Collected while enabled

- active application and a privacy-filtered foreground title;
- important visible applications and coarse idle duration;
- files opened or revealed through JARVIS and candidates returned by an
  explicit bounded JARVIS Downloads scan;
- coarse JARVIS intent terms, tool/skill outcome, widget lifecycle,
  confirmation outcome, connector status, and app lifecycle metadata;
- bounded routine summaries, mode predictions, suggestion feedback, and
  configuration needed to suppress unwanted suggestions.

The JSON store is capped and atomically replaced. The debug summary contains
counts and top application names, never the raw event list.

## Deliberately excluded

There is no keyboard capture, password or token collection, screen or audio
recording, browser-history scan, chat scraping, clipboard monitoring, message
sending, or hidden automation. Standard privacy mode removes titles for
browsers, communication clients, credential tools, and sensitive title terms.
Strict mode removes every window title and suppresses proactive suggestions.

## Monitoring and startup

`desktop_monitor_start` and `desktop_monitor_stop` require explicit confirmation.
Pause/resume preserves the user's opt-in without silently changing it. The
monitor samples at a conservative interval, stores snapshots only after an app
change or a five-minute heartbeat, rotates history, and contains poll failures.

Windows startup is a separate opt-in. `desktop_startup_enable_plan` produces an
expiring plan. Only `desktop_startup_enable_confirmed` writes the visible,
current-user Startup-folder command, which starts JARVIS minimized. Disable
removes that exact file after confirmation. If a system tray is unavailable,
JARVIS refuses to start hidden and shows the main window.

## Learning and suggestions

The deterministic learner groups safe events into sessions and only creates a
routine after it appears in at least two separate sessions. Predictions combine
active/running apps, files/downloads, open widgets, coarse command terms, idle
state, and modest time-of-day evidence. Suggestions require running monitoring,
respect cooldowns and disabled types, stop after repeated dismissals, are
suppressed in strict privacy mode and during Minecraft by default, and never
execute their action plan.

Skill conversion returns a review-only plan. It neither writes a skill nor runs
one; both operations remain subject to user approval and the existing skill
safety checks.

## Main tools

- Context: `desktop_context_snapshot`, `desktop_mode_predict`,
  `desktop_activity_list`, `desktop_context_debug_summary`
- Monitor/privacy: `desktop_monitor_start`, `desktop_monitor_stop`,
  `desktop_monitor_pause`, `desktop_monitor_resume`, `desktop_monitor_status`,
  `desktop_privacy_set`
- Startup: `desktop_startup_status`, `desktop_startup_enable_plan`,
  `desktop_startup_enable_confirmed`, `desktop_startup_disable`
- Habits: `desktop_habits_list`, `desktop_habit_explain`,
  `desktop_habit_disable`, `desktop_habit_delete`,
  `desktop_create_skill_from_routine_plan`
- Suggestions: `desktop_suggestions_list`, `desktop_suggestion_accept`,
  `desktop_suggestion_dismiss`, `desktop_suggestion_type_disable`,
  `desktop_gaming_suggestions_set`, `desktop_prediction_mark_wrong`
- History: `desktop_activity_clear`

All normal tests use fake desktop/startup services; no test launches Windows
applications, changes real startup state, captures input/media, or contacts an
external account.
