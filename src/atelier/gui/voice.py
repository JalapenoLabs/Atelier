"""Push-to-talk voice notes in the review window, recorded and transcribed on the reviewer's own machine.

Holding the talk key (or pressing it once, in toggle mode) records a **take** into the note on screen: the shown
ticket's note in focus mode (`focus.py`), or the grid's bulk note while its popup is open (`review.py`). Letting go
ends the take, and it is transcribed off the UI thread into words. The take holds its place in the note from the
moment recording starts, so takes keep the order they were spoken in. **Retake** discards the newest take after the
**checkpoint**, with its recording; checkpoint locks the takes so far. A take that fails to transcribe keeps its
recording and shows the error until it is retried or discarded, so nothing said is ever lost silently, and a decision
waits until every take is words.

The keys are caught by an event filter on the window rather than by QML, so they work whichever item has the keyboard:
the talk key is never typed into a field, while retake and checkpoint type their letters in a text field and act there
with Ctrl. Everything is configured in `[review.voice]` of `atelier.toml` (`atelierd.config.VoiceConfig`), read when a
project opens and again whenever its files change.

Recording is `pw-record` (PipeWire) streaming 16 kHz mono samples, which `PipeWireRecorder` writes as a WAV into the
note's draft folder while measuring the level for the meter. Transcribing runs a command (`voxtype transcribe` by
default) that prints the words. Both sit behind small protocols, so the tests use fakes and never touch a microphone.
"""

from __future__ import annotations

import array
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtQuick import QQuickWindow

from atelier.gui.backend import StudioBackend
from atelier.gui.focus import ReviewFocus
from atelier.gui.review import DecisionNote, ReviewBoard, TakeState
from atelier.gui.tasks import TaskRunner
from atelierd.config import ConfigError, TalkMode, VoiceConfig, load_config
from atelierd.constants import CONFIG_FILE_NAME, DEFAULT_TRANSCRIBE_COMMAND

logger = logging.getLogger(__name__)

# What a take is recorded as: what Whisper wants.
SAMPLE_RATE = 16_000
_SAMPLE_BYTES = 2
# The recorder reads, and measures the level of, 50 ms at a time.
_CHUNK_BYTES = SAMPLE_RATE * _SAMPLE_BYTES // 20
# The meter shows -60 dBFS to full scale.
_METER_FLOOR_DB = -60.0
_PW_RECORD_COMMAND = (
    "pw-record",
    "--rate",
    str(SAMPLE_RATE),
    "--channels",
    "1",
    "--format",
    "s16",
    "--raw",
    "-",
)
# How long pw-record gets to stop before it is killed.
_RECORDER_STOP_TIMEOUT_S = 3.0
# A transcription still running after this long fails, and its recording is kept for a retry.
_TRANSCRIBE_TIMEOUT_S = 120
# A tap of the talk key this short is a slip, not a take.
_SHORTEST_TAKE_S = 0.3
# How often the level meter is read while recording.
_LEVEL_INTERVAL_MS = 50
_RECORDING_PREFIX = "take-"

# A transcriber's own log lines and progress notes around the words it prints: voxtype prints `Loading audio file:`,
# `Audio format:` and `Processing ...` lines, then timestamped log records, on stdout. Colour codes are removed first.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TRANSCRIBER_NOISE = (
    re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+(TRACE|DEBUG|INFO|WARN|WARNING|ERROR)\b"),
    re.compile(r"^(Loading audio file|Audio format):"),
    re.compile(r"^Processing \d+ samples"),
)


class VoiceError(Exception):
    """A take could not be recorded or transcribed; the message says why, for the reviewer."""


class VoicePlace(StrEnum):
    """Where in the window a take is spoken into."""

    # Focus mode: the shown ticket's own note.
    FOCUS = "focus"
    # The grid's bulk note, while its popup is open.
    BULK = "bulk"


# ========================================================================================
# Recording and transcribing
# ========================================================================================


class Recording(Protocol):
    """A take being recorded."""

    def level(self) -> float:
        """How loud the last moment was, from 0 (silence) to 1 (full scale), as the meter shows it."""
        ...

    def stop(self) -> None:
        """Stop recording and finish the WAV. Raises `VoiceError` when nothing could be recorded."""
        ...

    def cancel(self) -> None:
        """Stop recording and delete what was recorded."""
        ...


class Recorder(Protocol):
    """Starts recordings from the microphone."""

    def start(self, path: Path) -> Recording:
        """Start recording a 16 kHz mono WAV to `path`. Raises `VoiceError` when recording cannot start."""
        ...


class Transcriber(Protocol):
    """Turns a recording into the words said in it."""

    def transcribe(self, recording: Path) -> str:
        """The words in a recording; empty when none were heard. Raises `VoiceError` when transcribing fails."""
        ...


class VoiceDevices(NamedTuple):
    """What voice notes record and transcribe with; the tests pass fakes."""

    recorder: Recorder
    # Makes the transcriber for the command `[review.voice] transcribe` names.
    make_transcriber: Callable[[tuple[str, ...]], Transcriber]


class PipeWireRecorder:
    """Records with `pw-record`, which streams raw samples that are written as a WAV here."""

    def start(self, path: Path) -> Recording:
        """Start `pw-record` and write what it hears to `path`."""
        return _PipeWireRecording(path)


class _PipeWireRecording:
    """One `pw-record` run, with a thread writing its samples to the WAV and measuring their level."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(_PW_RECORD_COMMAND, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError as error:
            raise VoiceError("pw-record (PipeWire) is not installed, so nothing can be recorded") from error
        self._wave = wave.open(str(path), "wb")
        self._wave.setnchannels(1)
        self._wave.setsampwidth(_SAMPLE_BYTES)
        self._wave.setframerate(SAMPLE_RATE)
        self._frame_count = 0
        self._level = 0.0
        self._pump = threading.Thread(target=self._write_samples, name="voice-recording", daemon=True)
        self._pump.start()

    def level(self) -> float:
        """The last 50 ms's loudness on the meter's scale."""
        return self._level

    def stop(self) -> None:
        """Stop `pw-record`, write out the WAV, and fail when it heard nothing."""
        stderr = self._end()
        if self._frame_count == 0:
            self._path.unlink(missing_ok=True)
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else "no sound arrived"
            raise VoiceError(f"pw-record recorded nothing: {detail}")
        logger.debug(f"[Voice] Recorded {self._frame_count / SAMPLE_RATE:.1f} s into {self._path}")

    def cancel(self) -> None:
        """Stop `pw-record` and delete the WAV."""
        self._end()
        self._path.unlink(missing_ok=True)

    def _end(self) -> str:
        """Stop the process and the writing thread, close the WAV, and return what `pw-record` said on stderr."""
        self._process.terminate()
        try:
            self._process.wait(timeout=_RECORDER_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("[Voice] pw-record did not stop when asked; killing it")
            self._process.kill()
            self._process.wait()
        self._pump.join(timeout=_RECORDER_STOP_TIMEOUT_S)
        self._wave.close()
        stderr = self._process.stderr.read() if self._process.stderr else b""
        return stderr.decode(errors="replace")

    def _write_samples(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            logger.error("[Voice] pw-record has no output to read")
            return
        while chunk := stdout.read(_CHUNK_BYTES):
            whole = chunk[: len(chunk) - len(chunk) % _SAMPLE_BYTES]
            self._wave.writeframes(whole)
            samples = array.array("h", whole)
            self._frame_count += len(samples)
            if samples:
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
                decibels = 20 * math.log10(rms) if rms > 0 else _METER_FLOOR_DB
                self._level = min(1.0, max(0.0, 1 - decibels / _METER_FLOOR_DB))


class CommandTranscriber:
    """Transcribes by running a command with the recording's path as its last argument and reading what it prints."""

    def __init__(self, command: tuple[str, ...]) -> None:
        self._command = command

    def transcribe(self, recording: Path) -> str:
        """Run the command on the recording and return its words."""
        name = self._command[0]
        try:
            completed = subprocess.run(
                [*self._command, str(recording)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=_TRANSCRIBE_TIMEOUT_S,
                # Plain text is easier to read words out of; tools that colour their logs mostly honour this.
                env={**os.environ, "NO_COLOR": "1"},
                check=False,
            )
        except FileNotFoundError as error:
            raise VoiceError(f"{name} is not installed") from error
        except subprocess.TimeoutExpired as error:
            raise VoiceError(f"{name} took longer than {_TRANSCRIBE_TIMEOUT_S} s") from error
        if completed.returncode != 0:
            said = completed.stderr.strip().splitlines() or completed.stdout.strip().splitlines() or ["no output"]
            raise VoiceError(f"{name} failed (exit {completed.returncode}): {_ANSI_ESCAPE.sub('', said[-1])}")
        return transcript_text(completed.stdout)


def transcript_text(output: str) -> str:
    """The words in a transcriber's output: every line that is not one of its log lines or progress notes."""
    words: list[str] = []
    for line in _ANSI_ESCAPE.sub("", output).splitlines():
        stripped = line.strip()
        if stripped and not any(pattern.match(stripped) for pattern in _TRANSCRIBER_NOISE):
            words.append(stripped)
    return " ".join(words)


def default_voice_devices() -> VoiceDevices:
    """The microphone through PipeWire, and the configured transcription command."""
    return VoiceDevices(recorder=PipeWireRecorder(), make_transcriber=CommandTranscriber)


# ========================================================================================
# Voice notes in the window
# ========================================================================================


class VoiceKeys(BaseModel):
    """The three keys, as Qt key codes, and their names for the window's hints."""

    model_config = ConfigDict(frozen=True)

    talk: int
    retake: int
    checkpoint: int
    talk_label: str
    retake_label: str
    checkpoint_label: str


def parse_voice_keys(config: VoiceConfig) -> VoiceKeys:
    """Read the configured key names as Qt keys. Raises `VoiceError` for a name that is not one plain key."""
    codes: dict[str, int] = {}
    for setting, name in (
        ("talk_key", config.talk_key),
        ("retake_key", config.retake_key),
        ("checkpoint_key", config.checkpoint_key),
    ):
        sequence = QKeySequence.fromString(name)
        # PySide's stubs miss QKeySequence's indexing, which it has at run time.
        combination = sequence[0] if sequence.count() == 1 else None  # type: ignore[index]
        is_plain_key = (
            combination is not None
            and combination.key() != Qt.Key.Key_unknown
            and combination.keyboardModifiers() == Qt.KeyboardModifier.NoModifier
        )
        if combination is None or not is_plain_key:
            raise VoiceError(f"[review.voice] {setting} '{name}' is not one key without modifiers")
        codes[setting] = combination.key().value
    return VoiceKeys(
        talk=codes["talk_key"],
        retake=codes["retake_key"],
        checkpoint=codes["checkpoint_key"],
        talk_label=config.talk_key,
        retake_label=config.retake_key,
        checkpoint_label=config.checkpoint_key,
    )


class _Target(NamedTuple):
    """The note a take goes into, the folder its recording goes into, and what to call it in messages."""

    label: str
    note: DecisionNote
    folder: Path


class _ActiveTake(NamedTuple):
    """The take being recorded now."""

    target: _Target
    take_id: int
    path: Path
    recording: Recording
    started_at: float


class VoiceNotes(QObject):
    """Records takes into the note on screen while the talk key is down, and transcribes them in the background."""

    changed = Signal()
    recordingChanged = Signal()
    levelChanged = Signal()

    def __init__(
        self,
        backend: StudioBackend,
        board: ReviewBoard,
        focus: ReviewFocus,
        tasks: TaskRunner,
        show_message: Callable[[str], None],
        devices: VoiceDevices,
    ) -> None:
        """Create voice notes for the review grid and focus mode.

        Args:
            backend: where the open project's `atelier.toml` and state folder are.
            board: the grid, whose bulk note takes are spoken into while its popup is open.
            focus: focus mode, whose shown ticket's note takes are spoken into.
            tasks: runs finishing and transcribing off the UI thread.
            show_message: shows a status message.
            devices: what to record and transcribe with.
        """
        super().__init__()
        self._backend = backend
        self._board = board
        self._focus = focus
        self._tasks = tasks
        self._show_message = show_message
        self._devices = devices

        self._config = VoiceConfig()
        self._keys: VoiceKeys | None = parse_voice_keys(self._config)
        self._transcriber: Transcriber | None = None
        self._unavailable_reason = "No project is open"
        self._window: QQuickWindow | None = None
        self._is_focus_shown = False
        self._is_bulk_note_open = False
        self._active: _ActiveTake | None = None
        self._level = 0.0
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(_LEVEL_INTERVAL_MS)
        self._level_timer.timeout.connect(self._read_level)

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_is_available(self) -> bool:
        return not self._unavailable_reason

    def _get_unavailable_reason(self) -> str:
        return self._unavailable_reason

    def _get_is_recording(self) -> bool:
        return self._active is not None

    def _get_recording_place(self) -> str:
        if self._active is None:
            return ""
        if self._active.target.note is self._board.note:
            return VoicePlace.BULK.value
        return VoicePlace.FOCUS.value

    def _get_level(self) -> float:
        return self._level

    def _get_is_toggle(self) -> bool:
        return self._config.talk_mode is TalkMode.TOGGLE

    def _get_talk_key_label(self) -> str:
        return self._keys.talk_label if self._keys else ""

    def _get_retake_key_label(self) -> str:
        return self._keys.retake_label if self._keys else ""

    def _get_checkpoint_key_label(self) -> str:
        return self._keys.checkpoint_label if self._keys else ""

    def _get_is_focus_shown(self) -> bool:
        return self._is_focus_shown

    def _set_is_focus_shown(self, is_shown: bool) -> None:
        if is_shown == self._is_focus_shown:
            return
        self._is_focus_shown = is_shown
        self.changed.emit()

    def _get_is_bulk_note_open(self) -> bool:
        return self._is_bulk_note_open

    def _set_is_bulk_note_open(self, is_open: bool) -> None:
        if is_open == self._is_bulk_note_open:
            return
        self._is_bulk_note_open = is_open
        self.changed.emit()

    isAvailable = Property(bool, _get_is_available, notify=changed)
    # Why voice notes are off: no transcriber, a key that is not a key, an unreadable atelier.toml.
    unavailableReason = Property(str, _get_unavailable_reason, notify=changed)
    isRecording = Property(bool, _get_is_recording, notify=recordingChanged)
    # Where the take being recorded goes: "focus", "bulk", or empty.
    recordingPlace = Property(str, _get_recording_place, notify=recordingChanged)
    level = Property(float, _get_level, notify=levelChanged)
    isToggle = Property(bool, _get_is_toggle, notify=changed)
    talkKeyLabel = Property(str, _get_talk_key_label, notify=changed)
    retakeKeyLabel = Property(str, _get_retake_key_label, notify=changed)
    checkpointKeyLabel = Property(str, _get_checkpoint_key_label, notify=changed)
    # Set by QML: focus mode is on screen, and the grid's bulk note popup is open.
    isFocusShown = Property(bool, _get_is_focus_shown, _set_is_focus_shown, notify=changed)
    isBulkNoteOpen = Property(bool, _get_is_bulk_note_open, _set_is_bulk_note_open, notify=changed)

    # ####################### #
    #      CONFIGURATION      #
    # ####################### #

    def reload_config(self) -> None:
        """Read `[review.voice]` from the open project's `atelier.toml` again, and the transcriber it names."""
        try:
            config = load_config(self._backend.resolve(CONFIG_FILE_NAME))
        except ConfigError as error:
            logger.warning(f"[Voice] Voice notes are off: {error}")
            self._set_config(VoiceConfig(), f"atelier.toml could not be read: {error}")
            return

        voice_config = config.review.voice if config.review else VoiceConfig()
        command = voice_config.transcribe
        if command is None and shutil.which(DEFAULT_TRANSCRIBE_COMMAND[0]):
            command = DEFAULT_TRANSCRIBE_COMMAND
        if command is None:
            self._set_config(
                voice_config,
                f"{DEFAULT_TRANSCRIBE_COMMAND[0]} is not on PATH; name a transcriber in [review.voice] transcribe",
            )
            return
        self._transcriber = self._devices.make_transcriber(command)
        self._set_config(voice_config, "")

    def _set_config(self, config: VoiceConfig, unavailable_reason: str) -> None:
        self._config = config
        try:
            self._keys = parse_voice_keys(config)
        except VoiceError as error:
            self._keys = None
            unavailable_reason = str(error)
        if unavailable_reason:
            self._transcriber = None
        self._unavailable_reason = unavailable_reason
        self.changed.emit()

    # ####################### #
    #          KEYS           #
    # ####################### #

    @Slot(QObject)
    def watchKeys(self, window: QObject) -> None:
        """Catch the voice keys on the window, whichever of its items has the keyboard."""
        if not isinstance(window, QQuickWindow):
            logger.warning(f"[Voice] Voice keys need the Qt Quick window, not {type(window).__name__}")
            return
        self._window = window
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt's name)
        """Act on the talk, retake and checkpoint keys where a note is on screen; let every other event through."""
        if isinstance(event, QKeyEvent) and event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return self._on_key(event)
        # The window losing the keyboard mid-take would never hear the talk key let go.
        is_deactivated = event.type() == QEvent.Type.WindowDeactivate
        if is_deactivated and self._active is not None and self._config.talk_mode is TalkMode.HOLD:
            logger.info("[Voice] The window lost the keyboard while the talk key was held; ending the take")
            self._finish_take()
        return False

    def _on_key(self, event: QKeyEvent) -> bool:
        place = self._place()
        if self._keys is None or place is None:
            return False
        modifiers = event.modifiers() & ~(Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.KeypadModifier)
        is_plain = modifiers == Qt.KeyboardModifier.NoModifier
        is_press = event.type() == QEvent.Type.KeyPress

        if event.key() == self._keys.talk and is_plain:
            # Held keys repeat; only the first press and the real release count.
            if not event.isAutoRepeat():
                self._on_talk_key(place, is_press)
            return True

        actions = {self._keys.retake: self._retake, self._keys.checkpoint: self._checkpoint}
        action = actions.get(event.key())
        if action is None or not is_press:
            return False
        is_ctrl = modifiers == Qt.KeyboardModifier.ControlModifier
        # In a text field the plain letter is typed; Ctrl acts there instead.
        if not (is_ctrl or (is_plain and not self._is_typing())):
            return False
        if not event.isAutoRepeat():
            action(place)
        return True

    def _on_talk_key(self, place: VoicePlace, is_press: bool) -> None:
        if self._config.talk_mode is TalkMode.TOGGLE:
            if not is_press:
                return
            if self._active is None:
                self._start_take(place)
            else:
                self._finish_take()
            return
        if is_press:
            self._start_take(place)
        elif self._active is not None:
            self._finish_take()

    def _place(self) -> VoicePlace | None:
        """Where a take would go now: the bulk note while its popup is open, else focus mode's ticket, else nowhere."""
        if self._is_bulk_note_open:
            return VoicePlace.BULK
        if self._is_focus_shown and self._focus.shown_ticket:
            return VoicePlace.FOCUS
        return None

    def _is_typing(self) -> bool:
        item = self._window.activeFocusItem() if self._window else None
        return item is not None and (item.inherits("QQuickTextEdit") or item.inherits("QQuickTextInput"))

    # ####################### #
    #    RECORDING A TAKE     #
    # ####################### #

    @Slot(str)
    def startTake(self, place: str) -> None:
        """Start recording a take into the note at a place, as pressing the talk key does."""
        self._start_take(VoicePlace(place))

    @Slot()
    def finishTake(self) -> None:
        """End the take being recorded and transcribe it, as letting go of the talk key does."""
        if self._active is None:
            logger.debug("[Voice] No take is being recorded to finish")
            return
        self._finish_take()

    def _start_take(self, place: VoicePlace) -> None:
        if self._active is not None:
            logger.debug("[Voice] A take is already being recorded")
            return
        if self._unavailable_reason or self._transcriber is None:
            self._show_message(f"Voice notes are off: {self._unavailable_reason}")
            return
        target = self._target(place)
        if target is None:
            self._show_message("Open a ticket to talk into its note")
            return

        # Named by the moment it started, so a new take never reuses the name of one still being transcribed.
        path = target.folder / f"{_RECORDING_PREFIX}{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
        try:
            recording = self._devices.recorder.start(path)
        except VoiceError as error:
            self._show_message(f"Could not record: {error}")
            return
        take_id = target.note.start_spoken_take(path)
        self._active = _ActiveTake(target, take_id, path, recording, time.monotonic())
        self._level_timer.start()
        self.recordingChanged.emit()
        logger.info(f"[Voice] Recording a take for {target.label} into {path}")

    def _finish_take(self) -> None:
        active = self._end_recording()
        if active is None:
            return
        target, take_id = active.target, active.take_id
        if time.monotonic() - active.started_at < _SHORTEST_TAKE_S:
            target.note.remove_take(take_id)
            self._run_voice(active.recording.cancel, lambda _: None)
            self._show_message(f"{target.label}: that take was too short to transcribe")
            return
        transcriber = self._transcriber
        if transcriber is None:
            # Only reachable when the config changed under a take; it waits as a failure for a retry.
            target.note.update_spoken_take(take_id, TakeState.FAILED, error=self._unavailable_reason)
            self._run_voice(active.recording.stop, lambda _: None)
            return

        def stop_and_transcribe() -> str:
            active.recording.stop()
            return transcriber.transcribe(active.path)

        target.note.update_spoken_take(take_id, TakeState.TRANSCRIBING)
        self._run_voice(
            stop_and_transcribe,
            lambda text: self._on_transcribed(target, take_id, active.path, text),
            lambda error: self._on_failed(target, take_id, active.path, error),
        )

    def _end_recording(self) -> _ActiveTake | None:
        active = self._active
        self._active = None
        self._level_timer.stop()
        self._level = 0.0
        self.levelChanged.emit()
        self.recordingChanged.emit()
        return active

    def _on_transcribed(self, target: _Target, take_id: int, path: Path, text: str) -> None:
        # A take retaken (or sent with a decision) while it was transcribed has left the note; so does its recording.
        if not text:
            if target.note.remove_take(take_id) is not None:
                self._show_message(f"{target.label}: no words were heard in that take")
            path.unlink(missing_ok=True)
            return
        if not target.note.update_spoken_take(take_id, TakeState.WORDS, text=text):
            path.unlink(missing_ok=True)

    def _on_failed(self, target: _Target, take_id: int, path: Path, error: Exception) -> None:
        if not path.is_file():
            # Nothing was recorded (or a retake deleted it), so there is nothing to transcribe again.
            if target.note.remove_take(take_id) is not None:
                self._show_message(f"{target.label}: {error}")
            return
        if target.note.update_spoken_take(take_id, TakeState.FAILED, error=str(error)):
            self._show_message(f"{target.label}: a take failed to transcribe ({error}); it is kept to retry")
            return
        path.unlink(missing_ok=True)

    def _read_level(self) -> None:
        if self._active is None:
            return
        self._level = self._active.recording.level()
        self.levelChanged.emit()

    # ####################### #
    #   RETAKE, CHECKPOINT    #
    # ####################### #

    @Slot(str)
    def retake(self, place: str) -> None:
        """Discard the newest take after the checkpoint, and its recording."""
        self._retake(VoicePlace(place))

    @Slot(str)
    def checkpoint(self, place: str) -> None:
        """Lock the takes so far, so a retake cannot remove them."""
        self._checkpoint(VoicePlace(place))

    @Slot(str, int)
    def retryTake(self, place: str, index: int) -> None:
        """Transcribe a take that failed again, from its kept recording."""
        target = self._target(VoicePlace(place))
        take = target.note.take_at(index) if target else None
        if target is None or take is None or take.state is not TakeState.FAILED or take.recording is None:
            logger.debug(f"[Voice] No failed take {index} to retry at {place}")
            return
        if self._transcriber is None:
            self._show_message(f"Voice notes are off: {self._unavailable_reason}")
            return
        if not take.recording.is_file():
            target.note.remove_take(take.take_id)
            self._show_message(f"{target.label}: that take's recording is gone, so it was dropped; say it again")
            return
        transcriber, path = self._transcriber, take.recording
        target.note.update_spoken_take(take.take_id, TakeState.TRANSCRIBING)
        self._run_voice(
            lambda: transcriber.transcribe(path),
            lambda text: self._on_transcribed(target, take.take_id, path, text),
            lambda error: self._on_failed(target, take.take_id, path, error),
        )

    @Slot(str, int)
    def discardTake(self, place: str, index: int) -> None:
        """Drop a take that failed to transcribe, and its recording."""
        target = self._target(VoicePlace(place))
        take = target.note.take_at(index) if target else None
        if target is None or take is None or take.state is not TakeState.FAILED:
            logger.debug(f"[Voice] No failed take {index} to discard at {place}")
            return
        target.note.remove_take(take.take_id)
        if take.recording:
            take.recording.unlink(missing_ok=True)
        self._show_message(f"{target.label}: discarded the take that failed")

    def _retake(self, place: VoicePlace) -> None:
        target = self._target(place)
        if target is None:
            self._show_message("Open a ticket to retake its note")
            return
        active = self._active
        if active is not None and active.target.note is target.note:
            self._end_recording()
            target.note.remove_take(active.take_id)
            self._run_voice(active.recording.cancel, lambda _: None)
            self._show_message(f"{target.label}: recording discarded")
            return
        removed = target.note.remove_last_take()
        if removed is None:
            is_locked = target.note.locked_count > 0
            self._show_message(
                f"{target.label}: the takes before the checkpoint are kept"
                if is_locked
                else f"{target.label}: no take to retake"
            )
            return
        if removed.recording:
            removed.recording.unlink(missing_ok=True)
        self._show_message(f"{target.label}: take discarded")

    def _checkpoint(self, place: VoicePlace) -> None:
        target = self._target(place)
        if target is None:
            self._show_message("Open a ticket to checkpoint its note")
            return
        target.note.checkpoint()
        count = target.note.locked_count
        self._show_message(f"{target.label}: {count} take{'' if count == 1 else 's'} locked; a retake keeps them")

    # ####################### #
    #         HELPERS         #
    # ####################### #

    def _target(self, place: VoicePlace) -> _Target | None:
        if place is VoicePlace.BULK:
            return _Target("The bulk note", self._board.note, self._board.bulk_draft_folder)
        ticket = self._focus.shown_ticket
        if not ticket:
            return None
        return _Target(ticket, self._focus.note_for(ticket), self._focus.draft_folder(ticket))

    def _run_voice(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        def log_failure(error: Exception) -> None:
            logger.warning(f"[Voice] Finishing a recording failed: {error}")

        self._tasks.run_voice(work, on_done, on_error or log_failure)
