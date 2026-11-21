#!/usr/bin/env python3
import argparse
import copy
import datetime
import fcntl
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path


PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "osama-usuf.pane-marks")
SOURCE = "pane-marks"
FLAG = "⚑"
HERDR = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


class PluginError(RuntimeError):
    pass


def herdr(*args):
    result = subprocess.run(
        [HERDR, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        code = None
        try:
            error = json.loads(message).get("error", {})
            code = error.get("code")
            message = error.get("message", message)
        except (AttributeError, json.JSONDecodeError):
            pass
        raise PluginError(message or f"herdr {' '.join(args)} failed")
    output = result.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"raw": output}


def result_of(response, key):
    return response.get("result", {}).get(key, [])


def state_paths():
    directory = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not directory:
        raise PluginError("HERDR_PLUGIN_STATE_DIR is not set")
    path = Path(directory)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path / "marks.json", path / "marks.json.lock"


def load_state(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise PluginError("marks.json must contain an object")
    return value


def save_state(path, state):
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def inventory():
    workspace_data = result_of(herdr("workspace", "list"), "workspaces")
    pane_data = result_of(herdr("pane", "list"), "panes")
    return {
        "workspaces": {
            item["workspace_id"]: item
            for item in workspace_data
            if item.get("workspace_id")
        },
        "panes": {
            item["pane_id"]: item
            for item in pane_data
            if item.get("pane_id")
        },
    }


def pane_info(pane_id):
    response = herdr("pane", "get", pane_id)
    return response.get("result", {}).get("pane")


def clean_note(value):
    value = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(value.split())


def shorten(value, limit):
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def border_label(record):
    note = record.get("note") or "revisit"
    return f"{FLAG} {shorten(note, 40)}"


def pane_token(record):
    note = record.get("note") or "marked"
    return f"{FLAG} {shorten(note, 20)}"


def workspace_token(count):
    return f"{FLAG} {count} marked"


def prune(state, live):
    for pane_id in list(state):
        if pane_id not in live["panes"] or not isinstance(state[pane_id], dict):
            del state[pane_id]
            continue
        state[pane_id]["workspace_id"] = live["panes"][pane_id].get("workspace_id")


def apply_event_state(state, event, payload):
    if event == "pane.closed":
        data = payload.get("data", payload)
        pane_id = data.get("pane_id")
        if pane_id:
            state.pop(pane_id, None)
    elif event == "pane.moved":
        data = payload.get("data", payload)
        old_id = data.get("previous_pane_id")
        pane = data.get("pane") or {}
        new_id = pane.get("pane_id")
        if old_id in state and new_id:
            record = state.pop(old_id)
            record["workspace_id"] = pane.get("workspace_id")
            state[new_id] = record


def set_pane_mark_token(pane_id, value=None):
    args = ["pane", "report-metadata", pane_id, "--source", SOURCE]
    if value is None:
        args.extend(["--clear-token", "mark"])
    else:
        args.extend(["--token", f"mark={value}"])
    herdr(*args)


def set_workspace_mark_token(workspace_id, count):
    args = ["workspace", "report-metadata", workspace_id, "--source", SOURCE]
    if count:
        args.extend(["--token", f"marks={workspace_token(count)}"])
    else:
        args.extend(["--clear-token", "marks"])
    herdr(*args)


def restore_label_if_marked(pane_id, record):
    try:
        info = pane_info(pane_id)
    except PluginError:
        return
    if not info:
        return
    current = info.get("label")
    if not isinstance(current, str) or not current.startswith(FLAG):
        return
    original = record.get("original_label")
    if original is None:
        herdr("pane", "rename", pane_id, "--clear")
    else:
        herdr("pane", "rename", pane_id, original)


def sync_effects(state, live):
    workspace_counts = {}
    for pane_id, record in state.items():
        if pane_id not in live["panes"]:
            continue
        workspace_id = live["panes"][pane_id].get("workspace_id")
        record["workspace_id"] = workspace_id
        workspace_counts[workspace_id] = workspace_counts.get(workspace_id, 0) + 1
        try:
            info = pane_info(pane_id)
        except PluginError:
            continue
        if not info:
            continue
        expected_label = border_label(record)
        if info.get("label") != expected_label:
            herdr("pane", "rename", pane_id, expected_label)
        set_pane_mark_token(pane_id, pane_token(record))

    for workspace_id, workspace in live["workspaces"].items():
        count = workspace_counts.get(workspace_id, 0)
        if count or "marks" in workspace.get("tokens", {}):
            set_workspace_mark_token(workspace_id, count)


def transact(mutator=None, event=None, payload=None):
    path, lock_path = state_paths()
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(path)
        if event:
            apply_event_state(state, event, payload or {})
        live = inventory()
        prune(state, live)
        result = mutator(state, live) if mutator else None
        save_state(path, state)
        sync_effects(state, live)
        return result, copy.deepcopy(state), live


def mark_pane(pane_id, note):
    note = clean_note(note)

    def mutate(state, live):
        if pane_id not in live["panes"]:
            raise PluginError(f"pane does not exist: {pane_id}")
        record = state.get(pane_id)
        if record is None:
            info = pane_info(pane_id) or {}
            state[pane_id] = {
                "note": note,
                "original_label": info.get("label"),
                "workspace_id": live["panes"][pane_id].get("workspace_id"),
                "marked_at": time.time_ns(),
            }
        else:
            record["note"] = note
        return state[pane_id]

    transact(mutate)


def unmark_pane(pane_id):
    def mutate(state, live):
        record = state.pop(pane_id, None)
        if record and pane_id in live["panes"]:
            restore_label_if_marked(pane_id, record)
            set_pane_mark_token(pane_id)
        return bool(record)

    result, _, _ = transact(mutate)
    return result


def context_pane_id():
    pane_id = os.environ.get("PANE_MARKS_TARGET") or os.environ.get("HERDR_PANE_ID")
    if pane_id:
        return pane_id
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "")
    try:
        context = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def find(value):
        if isinstance(value, dict):
            for key in ("focused_pane_id", "pane_id"):
                candidate = value.get(key)
                if isinstance(candidate, str) and ":p" in candidate:
                    return candidate
            for key in ("pane", "focused_pane", "focus", "context"):
                candidate = find(value.get(key))
                if candidate:
                    return candidate
            for child in value.values():
                candidate = find(child)
                if candidate:
                    return candidate
        elif isinstance(value, list):
            for child in value:
                candidate = find(child)
                if candidate:
                    return candidate
        return None

    return find(context)


def socket_request(method, params):
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        raise PluginError("HERDR_SOCKET_PATH is not set")
    request = {"id": f"pane-marks-{os.getpid()}", "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(socket_path)
        client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        response = client.makefile("rb").readline()
    if not response:
        raise PluginError(f"Herdr returned no response to {method}")
    data = json.loads(response.decode("utf-8"))
    if "error" in data:
        raise PluginError(data["error"].get("message", method + " failed"))
    return data.get("result", {})


def focus_pane(pane_id):
    socket_request("pane.focus", {"pane_id": pane_id})


def open_popup(entrypoint, env=None):
    args = [
        "plugin",
        "pane",
        "open",
        "--plugin",
        PLUGIN_ID,
        "--entrypoint",
        entrypoint,
    ]
    for key, value in (env or {}).items():
        args.extend(["--env", f"{key}={value}"])
    herdr(*args)


def show_notification(title, body=None):
    args = ["notification", "show", title]
    if body:
        args.extend(["--body", body])
    herdr(*args)


def toggle():
    pane_id = context_pane_id()
    if not pane_id:
        raise PluginError("Could not identify the focused pane from plugin context")

    def mutate(state, live):
        if pane_id in state:
            record = state.pop(pane_id)
            if pane_id in live["panes"]:
                restore_label_if_marked(pane_id, record)
                set_pane_mark_token(pane_id)
            return True
        return False

    was_marked, _, _ = transact(mutate)
    if was_marked:
        show_notification("Mark cleared")
    else:
        open_popup("note", {"PANE_MARKS_TARGET": pane_id})


def marked_order(state):
    return sorted(
        state.items(),
        key=lambda item: (int(item[1].get("marked_at", 0)), item[0]),
    )


def next_marked():
    current = context_pane_id()

    def snapshot(state, live):
        return [pane_id for pane_id, _ in marked_order(state)]

    ids, _, _ = transact(snapshot)
    if not ids:
        show_notification("No marked panes")
        return
    if current in ids:
        target = ids[(ids.index(current) + 1) % len(ids)]
    else:
        target = ids[0]
    focus_pane(target)


def open_list():
    transact()
    open_popup("list")


def age_label(marked_at):
    seconds = max(0, (time.time_ns() - int(marked_at)) // 1_000_000_000)
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def list_entries():
    _, state, live = transact()
    workspace_labels = {
        workspace_id: workspace.get("label", workspace_id)
        for workspace_id, workspace in live["workspaces"].items()
    }
    tabs_by_workspace = {}
    entries = []
    for pane_id, record in marked_order(state):
        pane = live["panes"].get(pane_id, {})
        workspace_id = pane.get("workspace_id") or record.get("workspace_id")
        tab_id = pane.get("tab_id")
        if workspace_id not in tabs_by_workspace:
            tabs = result_of(herdr("tab", "list", "--workspace", workspace_id), "tabs")
            tabs_by_workspace[workspace_id] = {
                tab.get("tab_id"): tab.get("label", tab.get("tab_id", ""))
                for tab in tabs
            }
        try:
            details = pane_info(pane_id) or {}
        except PluginError:
            details = {}
        label = details.get("label") or pane_id
        if label.startswith(FLAG):
            label = label[len(FLAG) :].strip() or pane_id
        entries.append(
            {
                "pane_id": pane_id,
                "workspace": workspace_labels.get(workspace_id, workspace_id or "?"),
                "tab": tabs_by_workspace.get(workspace_id, {}).get(tab_id, tab_id or "?"),
                "label": label,
                "note": record.get("note") or "no note",
                "age": age_label(record.get("marked_at", time.time_ns())),
            }
        )
    return entries


def prompt_note():
    prompt = "Reminder note (Enter to skip, Esc to cancel): "
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty():
        value = sys.stdin.read()
        if value.startswith("\x1b"):
            sys.stdout.write("\nCancelled.\n")
            return None
        return clean_note(value.rstrip("\r\n")[:500])

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    value = []
    tty.setraw(fd)
    try:
        while True:
            char = sys.stdin.read(1)
            if not char:
                break
            if char in ("\r", "\n"):
                break
            if char == "\x1b":
                sys.stdout.write("\r\nCancelled.\r\n")
                return None
            if char in ("\x7f", "\x08"):
                if value:
                    value.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char == "\x15":
                value.clear()
                sys.stdout.write("\r" + " " * (len(prompt) + 500) + "\r" + prompt)
                sys.stdout.flush()
                continue
            if char.isprintable() and len(value) < 500:
                value.append(char)
                sys.stdout.write(char)
                sys.stdout.flush()
        sys.stdout.write("\r\n")
        return clean_note("".join(value))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def read_key():
    if not sys.stdin.isatty():
        char = sys.stdin.read(1)
        if char in ("\r", "\n"):
            return "ENTER"
        if char == "\x1b":
            return "ESC"
        return char or None
    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    if not first:
        return None
    if first == b"\r" or first == b"\n":
        return "ENTER"
    if first == b"\x1b":
        ready, _, _ = select.select([fd], [], [], 0.04)
        if not ready:
            return "ESC"
        second = os.read(fd, 1)
        if second == b"[":
            third = os.read(fd, 1)
            return {"A": "UP", "B": "DOWN"}.get(third.decode("ascii", "ignore"), "ESC")
        return "ESC"
    return first.decode("utf-8", "ignore")


def render_list(entries, selected):
    sys.stdout.write("\033[2J\033[H")
    print(f"Marked panes ({len(entries)})")
    print("─" * min(shutil.get_terminal_size((80, 24)).columns, 70))
    if not entries:
        print("No marked panes.")
    else:
        width = max(24, shutil.get_terminal_size((80, 24)).columns - 2)
        for index, entry in enumerate(entries):
            marker = ">" if index == selected else " "
            title = (
                f"{FLAG} {entry['workspace']} › {entry['tab']} › {entry['label']} "
                f"— {entry['note']}  ({entry['age']})"
            )
            print(f"{marker} {index + 1}. {shorten(title, width)}")
    print("\n↑/↓ or j/k move · Enter/digit jump · d delete · q/Esc close")
    sys.stdout.flush()


def list_ui():
    entries = list_entries()
    selected = 0
    render_list(entries, selected)
    while True:
        key = read_key()
        if key is None or key in ("q", "ESC"):
            return
        if key in ("UP", "k") and entries:
            selected = (selected - 1) % len(entries)
            render_list(entries, selected)
        elif key in ("DOWN", "j") and entries:
            selected = (selected + 1) % len(entries)
            render_list(entries, selected)
        elif key == "d" and entries:
            unmark_pane(entries[selected]["pane_id"])
            entries = list_entries()
            selected = min(selected, max(0, len(entries) - 1))
            render_list(entries, selected)
        elif key == "ENTER" and entries:
            focus_pane(entries[selected]["pane_id"])
            return
        elif isinstance(key, str) and key.isdigit() and entries:
            index = int(key) - 1
            if 0 <= index < len(entries):
                focus_pane(entries[index]["pane_id"])
                return


def event_from_environment():
    event = os.environ.get("HERDR_PLUGIN_EVENT", "")
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise PluginError(f"Invalid HERDR_PLUGIN_EVENT_JSON: {error}")
    if not event:
        event = payload.get("event") or payload.get("type", "")
    event = event.replace("_", ".")
    if event in ("pane.closed", "pane.moved"):
        return event, payload
    nested = payload.get("data", {})
    nested_type = nested.get("type", "") if isinstance(nested, dict) else ""
    nested_type = nested_type.replace("_", ".")
    if nested_type in ("pane.closed", "pane.moved"):
        return nested_type, payload
    raise PluginError(f"Unsupported pane event: {event or nested_type or 'unknown'}")


def run_note():
    transact()
    pane_id = context_pane_id()
    if not pane_id:
        raise PluginError("No target pane was provided")
    note = prompt_note()
    if note is None:
        return
    mark_pane(pane_id, note)
    print(f"Marked {pane_id}")


def run_mark(pane_id, note):
    mark_pane(pane_id, note or "")
    print(f"Marked {pane_id}")


def run_unmark(pane_id):
    if unmark_pane(pane_id):
        print(f"Cleared mark on {pane_id}")
    else:
        print(f"No mark on {pane_id}")


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("resync", "toggle", "next", "open-list", "note", "list-ui", "event"):
        subparsers.add_parser(name)
    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("pane_id")
    mark_parser.add_argument("--note", default="")
    unmark_parser = subparsers.add_parser("unmark")
    unmark_parser.add_argument("pane_id")
    args = parser.parse_args()

    if args.command == "resync":
        _, state, _ = transact()
        print(f"Resynced {len(state)} pane mark(s)")
    elif args.command == "toggle":
        toggle()
    elif args.command == "next":
        next_marked()
    elif args.command == "open-list":
        open_list()
    elif args.command == "note":
        run_note()
    elif args.command == "list-ui":
        list_ui()
    elif args.command == "event":
        event, payload = event_from_environment()
        _, state, _ = transact(event=event, payload=payload)
        print(f"Handled {event}; {len(state)} pane mark(s) remain")
    elif args.command == "mark":
        run_mark(args.pane_id, args.note)
    elif args.command == "unmark":
        run_unmark(args.pane_id)


if __name__ == "__main__":
    try:
        main()
    except (OSError, PluginError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
