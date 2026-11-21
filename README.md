# Pane Marks

Pane Marks lets you flag panes to revisit, optionally with a reminder note. It uses a `⚑` pane-border label and sidebar tokens rather than a border color.

## Features

- `prefix+m` toggles a mark on the focused pane. A new mark opens a note popup; Enter skips the note and Escape cancels.
- Marked panes show a `⚑ <note>` border label (`⚑ revisit` without a note), a `$mark` pane token for agent sidebar rows, and a `$marks` workspace token such as `⚑ 2 marked`.
- `prefix+shift+m` opens a numbered list popup. Navigate with Up/Down or `j`/`k`; Enter or a number jumps to a pane; `d` removes the selected mark; `q` or Escape closes the list.
- `prefix+backtick` cycles through marked panes in creation order, wrapping at the end.
- Marks are removed when a pane closes and remapped when it moves. Startup resync reapplies labels and tokens and prunes pane IDs that no longer exist.

## Requirements

- Herdr 0.9.5 or newer
- Python 3.9 or newer; standard library only
- Linux or macOS

## Install

Install the latest version:

```sh
herdr plugin install osama-usuf/herdr-pane-marks
```

Install the tagged 0.1.0 release:

```sh
herdr plugin install osama-usuf/herdr-pane-marks --ref v0.1.0
```

Installation does not add keybindings or sidebar rows; configure them below.

## Required configuration

Merge these settings into `~/.config/herdr/config.toml`. If a table already exists, add the setting or rows to it rather than creating a duplicate table.

```toml
[ui]
pane_borders = "always"

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [{ token = "$marks", fg = "#ff9e64", bold = true }],
]

[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent"],
  [{ token = "$mark", fg = "#ff9e64", bold = true }],
]

[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "osama-usuf.pane-marks.toggle"
description = "mark pane for later"

[[keys.command]]
key = "prefix+shift+m"
type = "plugin_action"
command = "osama-usuf.pane-marks.open-list"
description = "list marked panes"

[[keys.command]]
key = "prefix+backtick"
type = "plugin_action"
command = "osama-usuf.pane-marks.next"
description = "next marked pane"
```

Custom `rows` replace the defaults. Keep the default rows first, as shown, and keep any other custom rows already in your config. The agent rows include the defaults before `$mark`; `$mark` is available in agent rows, while `$marks` is a workspace token. `pane_borders = "always"` also frames a lone pane when outer pane borders are enabled.

After editing the config, reload it:

```sh
herdr server reload-config
```

## Remote machines

Plugin actions and custom-command keybindings run on the Herdr server that owns the panes. Install Pane Marks on each remote host itself by SSHing into that host and running the `herdr plugin install` command there; plugin installation is not forwarded by `herdr --machine`.

Put the `[[keys.command]]` bindings in that remote host's own `~/.config/herdr/config.toml`. Sidebar rows and colors come from the local client config. After changing the remote host config, reload it with:

```sh
herdr --machine <machine> server reload-config
```

Marks are per machine. The list and next action only cover the currently selected machine.

## What it runs

The startup hook resyncs saved marks. Actions implement toggle, list, and next. The note and list interfaces are popup panes. `pane.closed` and `pane.moved` event hooks remove or remap marks and resync their display effects. The plugin writes its state, including notes and original pane labels, to:

```text
$HERDR_PLUGIN_STATE_DIR/marks.json
```

## Limitations

- Herdr has no per-pane border color, so the mark is shown with the border label.
- Renaming a marked pane causes the mark label to be reapplied on the next sync.
- A mark is dropped if its pane ID changes across a server restart.

## Uninstall

```sh
herdr plugin uninstall osama-usuf.pane-marks
```
