from textual import events
from textual.app import App,ComposeResult
from textual.widgets import Header,Footer,Static
from textual.containers import Horizontal
from textual.widget import Widget
from textual.strip import Strip
from rich.segment import Segment
from rich.style import Style
from textual.reactive import reactive
import mido
import asyncio
# atm lets work on block placement I'll think about the rest later
ticks_per_quarter_note=480
ticks_per_16th=ticks_per_quarter_note//4
blocks_per_column=2
beats_per_minute=120
seconds_per_beat=60/beats_per_minute
seconds_per_tick=seconds_per_beat/ticks_per_quarter_note
#Currently I wanna cover 4 octaves from f#6 to f#2
top_pitch = 90     # F#6
bottom_pitch = 42  # F#2
COLS_PER_BEAT = 4          # 4 sixteenth-columns per quarter note
COLS_PER_MEASURE = 16      # assuming 4/4 time
GATE_RATIO = 0.85
# setting up mido
mido.set_backend('mido.backends.pygame')
port_name = "VirtualMIDISynth #1"
port = mido.open_output(port_name)
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
def clamp(value, min_val, max_val):
    return max(min_val, min(value, max_val))
def ticks_to_seconds(ticks:int):
    return ticks*seconds_per_tick
def pitch_to_name(pitch: int) -> str:
    name = NOTE_NAMES[pitch % 12]
    octave = pitch // 12 - 1  # MIDI standard: C4 = 60
    return f"{name}{octave}"
def build_song():
    melody = [64,62,60,62, 64,64,64, 62,62,64,62, 64,67,67, 64,62,60,62, 64,64,64,64, 62,64,62,60]
    grid = []
    note_events = []
    for i, pitch in enumerate(melody):
        col = i * COLS_PER_BEAT          # quarter note = 4 columns, laid out left to right
        row = top_pitch - pitch
        note_start = col * ticks_per_16th
        note_duration = ticks_per_16th * 4
        note = Note(pitch, note_start, note_duration, 100)
        grid.append((col, row, note))
        note_events.append((note_start, 1, note))
        note_events.append((note_start + int(note_duration*GATE_RATIO), 0, note))
    return grid, note_events
class PitchGutter(Widget):
    def render_line(self, y: int) -> Strip:
        max_row = top_pitch - bottom_pitch
        if y > max_row:
            return Strip([Segment(" " * self.size.width)])
        pitch = top_pitch - y
        label = pitch_to_name(pitch).rjust(4) + " "
        is_sharp = "#" in label
        style = Style(bgcolor="grey11") if is_sharp else None
        return Strip([Segment(label, style)])
#Currently I'll say a column should represent 1/16 of a note
class Note:
    def __init__(self, pitch, start, duration, velocity):
        self.pitch = pitch
        self.start = start
        self.duration = duration
        self.velocity = velocity
        self.state = 0           # 0 = idle, 1 = currently being resized
        self.resize_edge = None  # "start" or "end"


class Workstation(Widget, can_focus=True):
    _initial_grid, _initial_events = build_song()
    grid: reactive[list[tuple[int, int, Note]]] = reactive(_initial_grid)
    note_events: reactive[list[tuple[int, bool, Note]]] = reactive(_initial_events)
    undo_stack: reactive[list[tuple[str, object]]] = reactive([])
    redo_stack: reactive[list[tuple[str, object]]] = reactive([])
    mode = reactive(0)  # 0 = placement, 1 = resize
    position = reactive("0,0")
    BINDINGS = [
        ("space", "play", "Play"),
        ("c", "clear", "Clear"),
        ("r", "toggle_mode", "Toggle placement/resize"),
        ("enter", "finish_resize", "Finish resize"),
        ("ctrl+z", "undo", "Undo"),
        ("ctrl+y", "redo", "Redo"),
    ]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._resize_snapshot = []  # list of (note, orig_start, orig_duration) for the row being resized

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        col_note = [None] * width
        note_ends = [False] * width
        for (start_x, py, note) in self.grid:
            if py != y:
                continue
            num_chars = int(note.duration // ticks_per_16th)
            for i in range(num_chars):
                col = start_x + i
                if 0 <= col < width:
                    col_note[col] = note
            end_col = start_x + num_chars - 1
            if 0 <= end_col < width:
                note_ends[end_col] = True

        segments = []
        for x in range(width):
            note = col_note[x]
            if note is not None:
                if note.state == 1:
                    segments.append(Segment("█", Style(color="green")))
                elif note_ends[x]:
                    segments.append(Segment("█", Style(color="yellow")))
                else:
                    segments.append(Segment("█"))
            elif x % COLS_PER_MEASURE == 0:
                segments.append(Segment(" ", Style(bgcolor="grey27")))
            elif x % COLS_PER_BEAT == 0:
                segments.append(Segment(" ", Style(bgcolor="grey15")))
            else:
                segments.append(Segment(" "))
        return Strip(segments)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.mode != 1:
            return
        row = clamp(event.y, 0, top_pitch - bottom_pitch)
        y = top_pitch - row
        click_tick = event.x * ticks_per_16th
        for (start_x, py, note) in self.grid:
            if py != row:
                continue
            if note.start <= click_tick < note.start + note.duration:
                self.position = f"{event.x},{y} <->"
                return
        self.position = f"{event.x},{y}"

    def _resolve_row_pushes(self, note: Note) -> None:
        """Recompute every other note's position on this row from the ORIGINAL
        snapshot taken at resize-start, based on note's current start/duration.
        Because this always starts from the snapshot (not the previous frame),
        shrinking the resizing note naturally pulls pushed notes back."""
        edge = note.resize_edge
        orig_note_start = next(o for (n, o, d) in self._resize_snapshot if n is note)
        others = [(n, o, d) for (n, o, d) in self._resize_snapshot if n is not note]

        if edge == "end":
            after = sorted([t for t in others if t[1] >= orig_note_start], key=lambda t: t[1])
            prev_end = note.start + note.duration
            for n, orig_start, orig_duration in after:
                n.start = prev_end if orig_start < prev_end else orig_start
                prev_end = n.start + n.duration
        else:  # "start"
            before = sorted([t for t in others if t[1] <= orig_note_start], key=lambda t: -t[1])
            prev_start = note.start
            for n, orig_start, orig_duration in before:
                orig_end = orig_start + orig_duration
                n.start = max(0, prev_start - n.duration) if orig_end > prev_start else orig_start
                prev_start = n.start

    def on_mouse_down(self, event: events.MouseDown) -> None:
        row = clamp(event.y, 0, top_pitch - bottom_pitch)
        y = top_pitch - row

        if event.button == 1:
            if self.mode == 0:
                self.position = f"{event.x},{y}"
                note_start = event.x * ticks_per_16th
                note_duration = ticks_per_16th * 4
                note_end = note_start + note_duration

                for (start_x, py, existing_note) in self.grid:
                    if py != row:
                        continue
                    existing_start = existing_note.start
                    existing_end = existing_note.start + existing_note.duration
                    if note_start < existing_end and existing_start < note_end:
                        return

                note = Note(y, note_start, note_duration, 100)
                entry = (event.x, row, note)
                self.grid = [*self.grid, entry]
                self.note_events = [*self.note_events, (note_start, 1, note), (note_start + note_duration, 0, note)]
                self.undo_stack = [*self.undo_stack, ("add", entry)]
                self.redo_stack = []

            elif self.mode == 1:
                self.position = f"{event.x},{y}"
                click_tick = event.x * ticks_per_16th

                resizing_note = None
                for (start_x, py, existing_note) in self.grid:
                    if existing_note.state == 1:
                        resizing_note = existing_note
                        resizing_row = py
                        break

                if resizing_note is None:
                    for (start_x, py, existing_note) in self.grid:
                        if py != row:
                            continue
                        existing_start = existing_note.start
                        existing_end = existing_note.start + existing_note.duration
                        if existing_start <= click_tick < existing_end:
                            existing_note.state = 1
                            existing_note.resize_edge = (
                                "start" if (click_tick - existing_start) < (existing_end - click_tick) else "end"
                            )
                            self._resize_snapshot = [
                                (n, n.start, n.duration) for (sx2, py2, n) in self.grid if py2 == row
                            ]
                            self.grid = list(self.grid)
                            break
                else:
                    existing_start = resizing_note.start
                    existing_end = resizing_note.start + resizing_note.duration

                    if resizing_note.resize_edge == "end":
                        new_end = max(click_tick + ticks_per_16th, existing_start + ticks_per_16th)
                        resizing_note.duration = new_end - existing_start
                    else:
                        new_start = max(0, min(click_tick, existing_end - ticks_per_16th))
                        resizing_note.duration = existing_end - new_start
                        resizing_note.start = new_start

                    self._resolve_row_pushes(resizing_note)

                    updated = {n: n.start // ticks_per_16th for (n, _o, _d) in self._resize_snapshot}
                    self.grid = [
                        (updated[n], py2, n) if n in updated else (sx2, py2, n)
                        for (sx2, py2, n) in self.grid
                    ]

        if event.button == 3:
            click_tick = event.x * ticks_per_16th
            target = None
            for g in self.grid:
                start_x, py, existing_note = g
                if py != row:
                    continue
                existing_start = existing_note.start
                existing_end = existing_note.start + existing_note.duration
                if existing_start <= click_tick < existing_end:
                    target = g
                    break

            if target is None:
                return

            self.grid = [g for g in self.grid if g is not target]
            self.note_events = [e for e in self.note_events if e[2] is not target[2]]
            self.undo_stack = [*self.undo_stack, ("delete", target)]
            self.redo_stack = []

    def watch_position(self, old_value: str, new_value: str) -> None:
        self.app.query_one("#position_display", Static).update(new_value)

    def action_play(self) -> None:
        self.run_worker(self.play_events())

    def action_toggle_mode(self) -> None:
        if self.mode == 1:
            self.action_finish_resize()
        self.mode = 0 if self.mode == 1 else 1

    def _rebuild_grid_positions(self, changed_notes: set) -> None:
        updated = {n: n.start // ticks_per_16th for n in changed_notes}
        self.grid = [
            (updated[n], py, n) if n in updated else (sx, py, n)
            for (sx, py, n) in self.grid
        ]

    def _rebuild_note_events(self, changed_notes: set) -> None:
        self.note_events = [e for e in self.note_events if e[2] not in changed_notes] + [
            ev for n in changed_notes for ev in ((n.start, 1, n), (n.start + n.duration, 0, n))
        ]

    def action_finish_resize(self) -> None:
        resizing_note = None
        for (sx, py, n) in self.grid:
            if n.state == 1:
                resizing_note = n
                break
        if resizing_note is None:
            return

        resizing_note.state = 0
        resizing_note.resize_edge = None

        changes = [
            (n, orig_start, orig_duration, n.start, n.duration)
            for (n, orig_start, orig_duration) in self._resize_snapshot
            if n.start != orig_start or n.duration != orig_duration
        ]
        self._resize_snapshot = []
        self.grid = list(self.grid)

        if not changes:
            return

        changed_notes = {c[0] for c in changes}
        self._rebuild_note_events(changed_notes)
        self.undo_stack = [*self.undo_stack, ("resize", changes)]
        self.redo_stack = []

    def action_undo(self) -> None:
        if not self.undo_stack:
            return
        action, entry = self.undo_stack[-1]
        self.undo_stack = self.undo_stack[:-1]

        if action == "add":
            start_x, row, note = entry
            self.grid = [g for g in self.grid if g is not entry]
            self.note_events = [e for e in self.note_events if e[2] is not note]
        elif action == "delete":
            start_x, row, note = entry
            self.grid = [*self.grid, entry]
            self.note_events = [*self.note_events, (note.start, 1, note), (note.start + note.duration, 0, note)]
        elif action == "clear":
            self.grid = entry
            self.note_events = []
            for start_x, row, note in entry:
                self.note_events.append((note.start, 1, note))
                self.note_events.append((note.start + note.duration, 0, note))
        elif action == "resize":
            for n, old_start, old_duration, new_start, new_duration in entry:
                n.start, n.duration = old_start, old_duration
            changed_notes = {c[0] for c in entry}
            self._rebuild_grid_positions(changed_notes)
            self._rebuild_note_events(changed_notes)

        self.redo_stack = [*self.redo_stack, (action, entry)]

    def action_redo(self) -> None:
        if not self.redo_stack:
            return
        action, entry = self.redo_stack[-1]
        self.redo_stack = self.redo_stack[:-1]

        if action == "add":
            start_x, row, note = entry
            self.grid = [*self.grid, entry]
            self.note_events = [*self.note_events, (note.start, 1, note), (note.start + note.duration, 0, note)]
        elif action == "delete":
            start_x, row, note = entry
            self.grid = [g for g in self.grid if g is not entry]
            self.note_events = [e for e in self.note_events if e[2] is not note]
        elif action == "clear":
            self.grid = []
            self.note_events = []
        elif action == "resize":
            for n, old_start, old_duration, new_start, new_duration in entry:
                n.start, n.duration = new_start, new_duration
            changed_notes = {c[0] for c in entry}
            self._rebuild_grid_positions(changed_notes)
            self._rebuild_note_events(changed_notes)

        self.undo_stack = [*self.undo_stack, (action, entry)]

    def action_clear(self) -> None:
        if not self.grid:
            return
        snapshot = self.grid
        self.undo_stack = [*self.undo_stack, ("clear", snapshot)]
        self.redo_stack = []
        self.grid = []
        self.note_events = []

    async def play_events(self):
        previous_tick = 0
        sorted_events = sorted(self.note_events, key=lambda e: e[0])
        for tick, action, note in sorted_events:
            gap_ticks = tick - previous_tick
            gap_seconds = ticks_to_seconds(gap_ticks)
            await asyncio.sleep(gap_seconds)

            if action:
                port.send(mido.Message('note_on', note=note.pitch, velocity=note.velocity))
            else:
                port.send(mido.Message('note_off', note=note.pitch))

            previous_tick = tick
class PianoRoll(App):
    CSS_PATH="roll.tcss"
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("0,0", id="position_display")
        with Horizontal():
            yield PitchGutter()
            yield Workstation()
        yield Footer()
    def on_mount(self) -> None:
        self.query_one(Workstation).focus()
if __name__=="__main__":
    app=PianoRoll()
    app.run()