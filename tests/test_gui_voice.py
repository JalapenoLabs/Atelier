"""Voice notes against a fake recorder and transcriber: takes, retakes, checkpoints, failures, config, and the keys.

Nothing here records from a microphone. The PipeWire recorder is driven by a stand-in `pw-record` on PATH that streams
a tone, and the command transcriber by a stand-in script.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fake_backend import CARBINE, FakeBackend
from pydantic import ValidationError
from PySide6.QtCore import QEvent, QSettings, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from test_gui_focus import make_focus_queue
from test_gui_review import carbine_history, make_image

import atelier.gui.voice as voice_module
from atelier.gui.app import _MAIN_QML
from atelier.gui.review import BULK_NOTE_DIR_NAME, DecisionNote
from atelier.gui.studio import Studio
from atelier.gui.tasks import TaskRunner
from atelier.gui.voice import (
    CommandTranscriber,
    PipeWireRecorder,
    Transcriber,
    VoiceDevices,
    VoiceError,
    VoiceNotes,
    transcript_text,
)
from atelierd.config import VoiceConfig
from atelierd.constants import REVIEW_DIR_NAME

WaitUntil = Callable[[Callable[[], bool]], None]

REVIEW_TABLE = """
[review.tracker]
kind = "youtrack"
url = "https://example.youtrack.cloud"
project = "WAY"
reviewer = "youtrack-test"

[review.queues]
Art = "tag: {Art asset}"

[review.stages]
approve = "Done"
changes = "Develop"
"""

# What `voxtype transcribe` printed on stdout for a take on this machine, around the words: its progress notes, then
# its coloured log records, then the words and blank lines.
VOXTYPE_STDOUT = (
    'Loading audio file: "take.wav"\n'
    "Audio format: 16000 Hz, 1 channel(s), Int\n"
    "Processing 32000 samples (2.00s)...\n"
    "\x1b[2m2026-11-03T02:52:51.219412Z\x1b[0m \x1b[32m INFO\x1b[0m Using local whisper transcription mode\n"
    "\x1b[2m2026-11-03T02:52:51.788443Z\x1b[0m \x1b[32m INFO\x1b[0m Model loaded in 0.57s\n"
    "\x1b[2m2026-11-03T02:52:52.108645Z\x1b[0m \x1b[33m WARN\x1b[0m Whisper retry also returned degenerate transcript\n"
    '\x1b[2m2026-11-03T02:52:52.108658Z\x1b[0m \x1b[32m INFO\x1b[0m Transcription completed: "Grip too thin."\n'
    "Grip too thin.\n"
    "\n"
    "\n"
)


def write_wav(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * round(16_000 * seconds))


def write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class FakeRecording:
    """A take that is 'recorded' as half a second of silence when it stops."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.outcome = "recording"

    def level(self) -> float:
        return 0.6

    def stop(self) -> None:
        write_wav(self.path, 0.5)
        self.outcome = "stopped"

    def cancel(self) -> None:
        self.path.unlink(missing_ok=True)
        self.outcome = "cancelled"


class FakeTranscriber:
    """Answers each transcription with the next of its answers (or 'words'), once its gate is open."""

    def __init__(self, command: tuple[str, ...]) -> None:
        self.command = command
        self.answers: list[str | VoiceError] = []
        self.heard: list[Path] = []
        self.gate = threading.Event()
        self.gate.set()

    def transcribe(self, recording: Path) -> str:
        assert self.gate.wait(10)
        self.heard.append(recording)
        answer = self.answers.pop(0) if self.answers else "words"
        if isinstance(answer, VoiceError):
            raise answer
        return answer


class FakeVoice:
    """A fake microphone and the fake transcribers made for each config read."""

    def __init__(self) -> None:
        self.recordings: list[FakeRecording] = []
        self.refusal: VoiceError | None = None
        self.transcribers: list[FakeTranscriber] = []

    def start(self, path: Path) -> FakeRecording:
        if self.refusal is not None:
            raise self.refusal
        recording = FakeRecording(path)
        self.recordings.append(recording)
        return recording

    def make_transcriber(self, command: tuple[str, ...]) -> Transcriber:
        transcriber = FakeTranscriber(command)
        self.transcribers.append(transcriber)
        return transcriber

    @property
    def transcriber(self) -> FakeTranscriber:
        return self.transcribers[-1]

    @property
    def devices(self) -> VoiceDevices:
        return VoiceDevices(recorder=self, make_transcriber=self.make_transcriber)


def write_voice_config(root: Path, voice_table: str) -> None:
    (root / "atelier.toml").write_text(f"{REVIEW_TABLE}\n[review.voice]\n{voice_table}")


@pytest.fixture(autouse=True)
def any_take_is_long_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tests press and let go at once; the one about slips puts the real threshold back.
    monkeypatch.setattr(voice_module, "_SHORTEST_TAKE_S", 0.0)


@pytest.fixture
def backend(tmp_path: Path) -> FakeBackend:
    backend = FakeBackend(tmp_path)
    for reference in backend.references:
        make_image(tmp_path / reference.path, 64, 64)
    backend.review_queue = make_focus_queue(tmp_path)
    backend.histories[CARBINE] = carbine_history(tmp_path)
    write_voice_config(tmp_path, 'transcribe = ["fake-whisper", "--fast"]\n')
    return backend


@pytest.fixture
def fake_voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def studio(backend: FakeBackend, fake_voice: FakeVoice, tmp_path: Path, wait_until: WaitUntil) -> Iterator[Studio]:
    settings = QSettings(str(tmp_path / "Atelier.conf"), QSettings.Format.IniFormat)
    studio = Studio(backend, TaskRunner(), settings, voice_devices=fake_voice.devices)
    studio.connectDaemon()
    studio.openProject("Fake")
    wait_until(lambda: studio.review.property("hasRefreshed"))
    yield studio
    studio.close()


@pytest.fixture
def voice(studio: Studio) -> VoiceNotes:
    """Voice notes with focus mode open on WAY-60 and on screen."""
    studio.review.openTicket(0)
    studio.voice.setProperty("isFocusShown", True)
    return studio.voice


def take_states(note: DecisionNote) -> list[tuple[str, str]]:
    return [(take["state"], take["text"]) for take in note.property("takes")]


def message(studio: Studio) -> str:
    return str(studio.property("message"))


def speak(voice: VoiceNotes, place: str = "focus") -> None:
    voice.startTake(place)
    voice.finishTake()


class TestTakes:
    def test_a_take_records_into_the_draft_folder_with_a_level_and_becomes_words(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        note = studio.focus.current_note
        fake_voice.transcriber.answers = ["Grip too thin."]
        voice.startTake("focus")

        assert take_states(note) == [("recording", "")]
        assert (voice.property("isRecording"), voice.property("recordingPlace")) == (True, "focus")
        recording = fake_voice.recordings[0]
        assert recording.path.parent == studio.focus.draft_folder("WAY-60")
        wait_until(lambda: voice.property("level") == pytest.approx(0.6))

        voice.finishTake()
        assert not voice.property("isRecording")
        wait_until(lambda: take_states(note) == [("words", "Grip too thin.")])
        assert recording.outcome == "stopped"
        assert recording.path.is_file()
        assert fake_voice.transcriber.heard == [recording.path]
        assert fake_voice.transcriber.command == ("fake-whisper", "--fast")
        assert note.all_takes() == ("Grip too thin.",)

    def test_takes_keep_the_order_they_were_spoken_in_and_stay_with_their_ticket(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        transcriber = fake_voice.transcriber
        transcriber.gate.clear()
        transcriber.answers = ["first", "second", "for the wall"]
        speak(voice)
        speak(voice)
        carbine_note = studio.focus.current_note
        assert take_states(carbine_note) == [("transcribing", ""), ("transcribing", "")]

        # Moving on while they transcribe: the next take is the wall's, and the carbine's land on the carbine.
        studio.focus.nextTicket()
        assert studio.focus.shown_ticket == "WAY-61"
        speak(voice)
        transcriber.gate.set()
        wait_until(lambda: take_states(carbine_note) == [("words", "first"), ("words", "second")])
        wait_until(lambda: studio.focus.note_for("WAY-61").all_takes() == ("for the wall",))
        assert fake_voice.recordings[2].path.parent == studio.focus.draft_folder("WAY-61")

    def test_retake_drops_the_newest_take_and_its_recording_but_never_one_before_the_checkpoint(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        note = studio.focus.current_note
        fake_voice.transcriber.answers = ["one", "two", "three"]
        speak(voice)
        speak(voice)
        voice.checkpoint("focus")
        assert note.property("lockedCount") == 2
        assert "2 takes locked" in message(studio)
        speak(voice)
        wait_until(lambda: note.all_takes() == ("one", "two", "three"))
        assert [take["isLocked"] for take in note.property("takes")] == [True, True, False]

        voice.retake("focus")
        assert note.all_takes() == ("one", "two")
        assert not fake_voice.recordings[2].path.exists()
        assert fake_voice.recordings[1].path.is_file()

        voice.retake("focus")
        assert note.all_takes() == ("one", "two")
        assert "before the checkpoint are kept" in message(studio)

    def test_retake_while_recording_throws_the_recording_away(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        voice.startTake("focus")
        voice.retake("focus")
        assert not voice.property("isRecording")
        assert take_states(studio.focus.current_note) == []
        wait_until(lambda: fake_voice.recordings[0].outcome == "cancelled")
        assert fake_voice.transcriber.heard == []

    def test_retake_while_transcribing_drops_the_take_for_good(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        note = studio.focus.current_note
        fake_voice.transcriber.gate.clear()
        speak(voice)
        voice.retake("focus")
        assert take_states(note) == []
        fake_voice.transcriber.gate.set()
        wait_until(lambda: fake_voice.transcriber.heard == [fake_voice.recordings[0].path])
        wait_until(lambda: not fake_voice.recordings[0].path.exists())
        assert take_states(note) == []
        assert message(studio) == "WAY-60: take discarded"

    def test_a_take_that_fails_keeps_its_recording_holds_the_decision_and_can_be_retried(
        self, studio: Studio, backend: FakeBackend, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        note = studio.focus.current_note
        fake_voice.transcriber.answers = [VoiceError("the GPU is busy"), "Warmer metal."]
        speak(voice)
        wait_until(lambda: take_states(note) == [("failed", "")])
        assert note.property("takes")[0]["error"] == "the GPU is busy"
        assert "kept to retry" in message(studio)
        recording = fake_voice.recordings[0].path
        assert recording.is_file()

        studio.focus.finishWithChanges()
        assert backend.decisions == []
        assert "failed to transcribe" in message(studio)
        assert studio.focus.shown_ticket == "WAY-60"

        voice.retryTake("focus", 0)
        wait_until(lambda: note.all_takes() == ("Warmer metal.",))
        assert fake_voice.transcriber.heard == [recording, recording]
        studio.focus.finishWithChanges()
        wait_until(lambda: len(backend.decisions) == 1)
        assert backend.decisions[0].takes == ("Warmer metal.",)
        # Once the job is queued the draft folder goes, recordings and all.
        wait_until(lambda: not recording.exists())

    def test_a_take_still_transcribing_holds_the_decision(
        self, studio: Studio, backend: FakeBackend, voice: VoiceNotes, fake_voice: FakeVoice
    ) -> None:
        fake_voice.transcriber.gate.clear()
        speak(voice)
        studio.focus.approve()
        assert backend.decisions == []
        assert "still being recorded or transcribed" in message(studio)
        fake_voice.transcriber.gate.set()

    def test_discarding_a_failed_take_deletes_its_recording(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice, wait_until: WaitUntil
    ) -> None:
        fake_voice.transcriber.answers = [VoiceError("no model")]
        speak(voice)
        note = studio.focus.current_note
        wait_until(lambda: take_states(note) == [("failed", "")])
        voice.discardTake("focus", 0)
        assert take_states(note) == []
        assert not fake_voice.recordings[0].path.exists()

    def test_a_take_with_no_words_and_a_slip_of_the_key_say_so_and_leave_no_take(
        self,
        studio: Studio,
        voice: VoiceNotes,
        fake_voice: FakeVoice,
        monkeypatch: pytest.MonkeyPatch,
        wait_until: WaitUntil,
    ) -> None:
        note = studio.focus.current_note
        fake_voice.transcriber.answers = [""]
        speak(voice)
        wait_until(lambda: "no words were heard" in message(studio))
        assert take_states(note) == []
        assert not fake_voice.recordings[0].path.exists()

        monkeypatch.setattr(voice_module, "_SHORTEST_TAKE_S", 10.0)
        speak(voice)
        assert "too short" in message(studio)
        assert take_states(note) == []
        wait_until(lambda: fake_voice.recordings[1].outcome == "cancelled")

    def test_a_recorder_that_cannot_start_says_why(
        self, studio: Studio, voice: VoiceNotes, fake_voice: FakeVoice
    ) -> None:
        fake_voice.refusal = VoiceError("pw-record (PipeWire) is not installed")
        voice.startTake("focus")
        assert not voice.property("isRecording")
        assert message(studio) == "Could not record: pw-record (PipeWire) is not installed"
        assert take_states(studio.focus.current_note) == []

    def test_the_bulk_note_takes_its_own_takes_and_its_recordings_go_once_decided(
        self, studio: Studio, backend: FakeBackend, fake_voice: FakeVoice, tmp_path: Path, wait_until: WaitUntil
    ) -> None:
        voice = studio.voice
        board = studio.review
        board.selectAll()
        voice.setProperty("isBulkNoteOpen", True)
        fake_voice.transcriber.answers = ["Every grip is too thin."]
        voice.startTake("bulk")
        assert voice.property("recordingPlace") == "bulk"
        voice.finishTake()
        bulk_folder = tmp_path / "state" / REVIEW_DIR_NAME / BULK_NOTE_DIR_NAME
        assert fake_voice.recordings[0].path.parent == bulk_folder
        wait_until(lambda: board.note.all_takes() == ("Every grip is too thin.",))

        assert board.submitDecision("changes")
        wait_until(lambda: len(backend.decisions) == 1)
        assert backend.decisions[0].takes == ("Every grip is too thin.",)
        assert len(backend.decisions[0].tickets) == 4
        wait_until(lambda: not bulk_folder.exists())


class TestConfig:
    def test_without_a_command_voxtype_is_the_transcriber_when_it_is_on_path(
        self,
        studio: Studio,
        backend: FakeBackend,
        fake_voice: FakeVoice,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        write_voice_config(backend.root, "")
        tools = tmp_path / "bin"
        write_script(tools / "voxtype", "")
        monkeypatch.setenv("PATH", str(tools))
        studio.voice.reload_config()
        assert studio.voice.property("isAvailable")
        assert fake_voice.transcriber.command == ("voxtype", "transcribe")
        assert (studio.voice.property("talkKeyLabel"), studio.voice.property("retakeKeyLabel")) == ("`", "R")
        assert studio.voice.property("checkpointKeyLabel") == "K"

        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        studio.voice.reload_config()
        assert not studio.voice.property("isAvailable")
        assert "voxtype is not on PATH" in studio.voice.property("unavailableReason")
        studio.review.openTicket(0)
        studio.voice.startTake("bulk")
        assert message(studio).startswith("Voice notes are off: voxtype is not on PATH")
        assert take_states(studio.review.note) == []

    def test_keys_mode_and_command_come_from_the_config(
        self, studio: Studio, backend: FakeBackend, fake_voice: FakeVoice
    ) -> None:
        write_voice_config(
            backend.root,
            'transcribe = ["my-whisper"]\ntalk_key = "F8"\ntalk_mode = "toggle"\n'
            'retake_key = "X"\ncheckpoint_key = "L"\n',
        )
        studio.voice.reload_config()
        assert fake_voice.transcriber.command == ("my-whisper",)
        assert studio.voice.property("isToggle")
        labels = [studio.voice.property(name) for name in ("talkKeyLabel", "retakeKeyLabel", "checkpointKeyLabel")]
        assert labels == ["F8", "X", "L"]

    def test_a_key_that_is_not_one_plain_key_turns_voice_notes_off(self, studio: Studio, backend: FakeBackend) -> None:
        write_voice_config(backend.root, 'transcribe = ["my-whisper"]\ntalk_key = "Ctrl+T"\n')
        studio.voice.reload_config()
        assert not studio.voice.property("isAvailable")
        assert "talk_key 'Ctrl+T' is not one key" in studio.voice.property("unavailableReason")

    def test_an_unreadable_config_turns_voice_notes_off_and_says_why(
        self, studio: Studio, backend: FakeBackend
    ) -> None:
        (backend.root / "atelier.toml").write_text("[review.voice\n")
        studio.voice.reload_config()
        assert not studio.voice.property("isAvailable")
        assert "atelier.toml could not be read" in studio.voice.property("unavailableReason")

    def test_the_three_keys_must_differ(self) -> None:
        with pytest.raises(ValidationError, match="three different keys"):
            VoiceConfig(talk_key="r", retake_key="R")


class TestDevices:
    def test_voxtype_output_reads_as_its_words_alone(self) -> None:
        assert transcript_text(VOXTYPE_STDOUT) == "Grip too thin."
        assert transcript_text("First line\nsecond line\n") == "First line second line"

    def test_the_command_transcriber_reads_the_words_and_reports_a_failure(self, tmp_path: Path) -> None:
        recording = tmp_path / "take.wav"
        write_wav(recording, 0.5)
        speaking = write_script(
            tmp_path / "speaking",
            "import sys\n"
            "print('Processing 8000 samples (0.50s)...')\n"
            "print('2026-11-03T02:52:51.219412Z  INFO Model loaded')\n"
            "print('Heard', sys.argv[-1].rsplit('/', 1)[-1])\n",
        )
        assert CommandTranscriber((str(speaking),)).transcribe(recording) == "Heard take.wav"

        failing = write_script(
            tmp_path / "failing", "import sys\nsys.stderr.write('loading\\nno GPU found\\n')\nsys.exit(3)\n"
        )
        with pytest.raises(VoiceError, match=r"failing failed \(exit 3\): no GPU found"):
            CommandTranscriber((str(failing),)).transcribe(recording)
        with pytest.raises(VoiceError, match="is not installed"):
            CommandTranscriber((str(tmp_path / "missing"),)).transcribe(recording)

    def test_the_pipewire_recorder_writes_a_16_khz_mono_wav_and_measures_the_level(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_until: WaitUntil
    ) -> None:
        # A stand-in pw-record streaming a loud tone as raw s16 until it is stopped, so no microphone is touched.
        tools = tmp_path / "bin"
        write_script(
            tools / "pw-record",
            "import array, math, sys, time\n"
            "assert sys.argv[1:] == ['--rate', '16000', '--channels', '1', '--format', 's16', '--raw', '-']\n"
            "tone = array.array('h', (round(16000 * math.sin(index / 8)) for index in range(800))).tobytes()\n"
            "while True:\n"
            "    sys.stdout.buffer.write(tone)\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(0.05)\n",
        )
        monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
        path = tmp_path / "draft" / "take.wav"
        recording = PipeWireRecorder().start(path)
        wait_until(lambda: recording.level() > 0.5)
        recording.stop()
        with wave.open(str(path), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16_000, 1, 2)
            assert wav.getnframes() >= 800

    def test_a_recorder_with_no_pw_record_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(VoiceError, match="pw-record"):
            PipeWireRecorder().start(tmp_path / "take.wav")


# ========================================================================================
# The keys, on the real window
# ========================================================================================


@pytest.fixture
def window(
    studio: Studio,
    qt_application: QGuiApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: WaitUntil,
) -> Iterator[tuple[QQuickWindow, list[QQmlError]]]:
    """The whole window in Review mode, shown on `offscreen` (nothing appears on screen), the grid holding the keys."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt_application.setOrganizationName("AtelierTests")
    qt_application.setApplicationName("AtelierTests")
    engine = QQmlApplicationEngine()
    warnings: list[QQmlError] = []
    engine.warnings.connect(warnings.extend)
    engine.rootContext().setContextProperty("studio", studio)
    engine.rootContext().setContextProperty("launchOptions", {"windowWidth": 1400, "windowHeight": 860})
    engine.load(QUrl.fromLocalFile(str(_MAIN_QML)))
    root = engine.rootObjects()[0]
    assert isinstance(root, QQuickWindow), [warning.toString() for warning in warnings]
    root.setProperty("mode", "review")
    root.show()
    root.requestActivate()
    assert QTest.qWaitForWindowActive(root)
    grid = root.findChild(QQuickItem, "reviewGrid")
    assert grid is not None
    wait_until(lambda: grid.hasActiveFocus() and grid.property("count") == 4)
    yield root, warnings
    root.close()


TALK = Qt.Key.Key_QuoteLeft


def repeat_talk_key(window: QQuickWindow) -> None:
    """What holding the key sends: auto-repeated presses."""
    event = QKeyEvent(QEvent.Type.KeyPress, TALK, Qt.KeyboardModifier.NoModifier, "`", True)
    QGuiApplication.sendEvent(window, event)


class TestKeys:
    def test_holding_the_talk_key_in_focus_mode_records_a_take_even_while_typing(
        self,
        studio: Studio,
        fake_voice: FakeVoice,
        window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        root, warnings = window
        QTest.keyClick(root, Qt.Key.Key_Return)
        wait_until(lambda: studio.focus.shown_ticket == "WAY-60" and studio.voice.property("isFocusShown"))
        note = studio.focus.current_note
        fake_voice.transcriber.answers = ["Grip too thin.", "and the stock", "Warmer metal."]

        QTest.keyPress(root, TALK)
        repeat_talk_key(root)
        repeat_talk_key(root)
        assert len(fake_voice.recordings) == 1
        assert studio.voice.property("recordingPlace") == "focus"
        QTest.keyRelease(root, TALK)
        wait_until(lambda: note.all_takes() == ("Grip too thin.",))

        # While typing in the note: the talk key is never typed, R types its letter, and Ctrl+K checkpoints.
        QTest.keyClick(root, Qt.Key.Key_N)
        QTest.keyClick(root, Qt.Key.Key_R)
        QTest.keyPress(root, TALK)
        QTest.keyRelease(root, TALK)
        wait_until(lambda: note.all_takes() == ("Grip too thin.", "and the stock", "r"))
        QTest.keyClick(root, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        assert note.property("lockedCount") == 2
        QTest.keyClick(root, Qt.Key.Key_Escape)

        # Back on the page: R retakes, but not past the checkpoint.
        QTest.keyPress(root, TALK)
        QTest.keyRelease(root, TALK)
        wait_until(lambda: len(note.all_takes()) == 4)
        QTest.keyClick(root, Qt.Key.Key_R)
        assert note.all_takes() == ("Grip too thin.", "and the stock", "r")
        QTest.keyClick(root, Qt.Key.Key_R)
        assert note.all_takes() == ("Grip too thin.", "and the stock", "r")
        assert studio.focus.shown_ticket == "WAY-60"
        assert [warning.toString() for warning in warnings] == []

    def test_toggle_mode_starts_and_ends_a_take_with_two_presses(
        self,
        studio: Studio,
        backend: FakeBackend,
        fake_voice: FakeVoice,
        window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        root, _ = window
        write_voice_config(backend.root, 'transcribe = ["fake"]\ntalk_mode = "toggle"\n')
        studio.voice.reload_config()
        QTest.keyClick(root, Qt.Key.Key_Return)
        wait_until(lambda: studio.voice.property("isFocusShown"))
        QTest.keyClick(root, TALK)
        assert studio.voice.property("isRecording")
        QTest.keyClick(root, TALK)
        assert not studio.voice.property("isRecording")
        wait_until(lambda: studio.focus.current_note.all_takes() == ("words",))

    def test_the_bulk_note_popup_records_and_retakes_with_ctrl(
        self,
        studio: Studio,
        fake_voice: FakeVoice,
        window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        root, warnings = window
        note = studio.review.note
        QTest.keyClick(root, Qt.Key.Key_C)
        wait_until(lambda: studio.voice.property("isBulkNoteOpen"))
        QTest.keyPress(root, TALK)
        assert studio.voice.property("recordingPlace") == "bulk"
        QTest.keyRelease(root, TALK)
        wait_until(lambda: note.all_takes() == ("words",))
        assert note.property("draft") == ""

        QTest.keyClick(root, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
        assert note.all_takes() == ()
        QTest.keyClick(root, Qt.Key.Key_Escape)
        wait_until(lambda: not studio.voice.property("isBulkNoteOpen"))
        # With no note on screen, the talk key is the grid's to ignore.
        QTest.keyClick(root, TALK)
        assert len(fake_voice.recordings) == 1
        assert [warning.toString() for warning in warnings] == []
