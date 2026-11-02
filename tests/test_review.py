"""The review queue: its config, linking tickets to sources, refreshing from a tracker, and writing decisions back.

The tracker is `FakeTracker`; no test reaches a real one. `test_youtrack.py` covers YouTrack's REST shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_tracker import (
    ART_QUERY,
    REVIEW_CONFIG,
    FakeTracker,
    add_review_scenario,
    make_attachment,
    make_comment,
)

from atelierd.catalog import Catalog
from atelierd.config import ConfigError, ReviewVerdict, load_config
from atelierd.project import Project, open_project
from atelierd.review import DecisionError, ReviewEvent, ReviewEventKind, ReviewService
from atelierd.review.links import find_ticket_links
from atelierd.trackers import TrackerError

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"

# ####################### #
#        FIXTURES         #
# ####################### #


@pytest.fixture
def project(project_root: Path, tmp_path: Path) -> Project:
    """The fixture project with a review queue configured, its state outside it."""
    config_path = project_root / "atelier.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + REVIEW_CONFIG, encoding="utf-8")
    return open_project(project_root, state_dir=tmp_path / "state")


@pytest.fixture
def catalog(project: Project) -> Catalog:
    catalog = Catalog(project)
    catalog.scan()
    return catalog


@pytest.fixture
def tracker(fake_tracker: FakeTracker) -> FakeTracker:
    add_review_scenario(fake_tracker)
    return fake_tracker


class Service:
    """A review service with every event it reported."""

    def __init__(self, project: Project, catalog: Catalog, tracker: FakeTracker) -> None:
        self.events: list[ReviewEvent] = []
        self.review = ReviewService(project, catalog, tracker.opener, self.events.append)


@pytest.fixture
def service(project: Project, catalog: Catalog, tracker: FakeTracker) -> Service:
    return Service(project, catalog, tracker)


# ####################### #
#         CONFIG          #
# ####################### #


class TestConfig:
    def test_the_review_table_loads(self, project: Project) -> None:
        review = load_config(project.config_path).review
        assert review is not None
        assert list(review.queues) == ["Art", "UI"]
        assert review.stages[ReviewVerdict.APPROVE] == "Done"
        assert review.on_approve[0].linked_summary == "\\[UI\\] Adopt"
        assert review.tracker.stage_field == "Stage"
        assert review.ticket_files == ("SourceArt/**/reference.json", "SourceArt/**/spec.md")

    def test_no_review_table_means_no_queue(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "atelier.toml").review is None

    @pytest.mark.parametrize(
        ("broken", "fixed", "complaint"),
        [
            ('changes = "Develop"', "", "changes"),
            ('reviewer = "tracker-reviewer"', 'reviewer = "tracker-reviewer"\ntoken = "x"', "token"),
            ("'\\[UI\\] Adopt'", "'[UI'", "regular expression"),
            ('url = "https://tracker.example"', 'url = "tracker.example"', "http"),
            ('kind = "youtrack"', 'kind = "jira"', "kind"),
        ],
    )
    def test_a_broken_review_table_fails_loudly(self, tmp_path: Path, broken: str, fixed: str, complaint: str) -> None:
        config_path = tmp_path / "atelier.toml"
        assert broken in REVIEW_CONFIG
        config_path.write_text(REVIEW_CONFIG.replace(broken, fixed), encoding="utf-8")
        with pytest.raises(ConfigError, match=complaint):
            load_config(config_path)


# ####################### #
#          LINKS          #
# ####################### #


class TestLinks:
    def test_ticket_files_link_tickets_to_the_source_above_them(self, project: Project, catalog: Catalog) -> None:
        carbine_folder = project.root / "SourceArt/Weapons/Carbine/Source/Carbine"
        carbine_folder.mkdir(parents=True)
        (carbine_folder / "reference.json").write_text(
            json.dumps({"ticket": "WAY-60", "subjects": [{"name": "Carbine", "meshes": ["SK_Carbine"]}]})
        )
        (carbine_folder / "spec.md").write_text("Ticket: WAY-60\n\n# Carbine\n")
        bed_folder = project.root / "SourceArt/Rooms/Source/Bed"
        bed_folder.mkdir(parents=True)
        (bed_folder / "spec.md").write_text("\nTicket: WAY-62\n")
        wall_folder = project.root / "SourceArt/Ship/Source/Wall"
        wall_folder.mkdir(parents=True)
        (wall_folder / "reference.json").write_text(json.dumps({"ticket": "WAY-61", "subjects": []}))
        (project.root / "SourceArt/Weapons/Carbine/Source/notes").mkdir()
        (project.root / "SourceArt/Weapons/Carbine/Source/notes/spec.md").write_text("# No ticket here\n")

        links = find_ticket_links(
            project.root, ("SourceArt/**/reference.json", "SourceArt/**/spec.md"), catalog.list_sources()
        )

        assert links["WAY-60"].source == CARBINE
        assert links["WAY-60"].exports == ("SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx",)
        # Folders of several kits, with no meshes named to choose between them, link nothing.
        assert set(links) == {"WAY-60"}

    def test_a_kit_is_picked_by_the_meshes_its_ticket_file_names(self, project: Project, catalog: Catalog) -> None:
        bed_folder = project.root / "SourceArt/Rooms/Source/Bed"
        bed_folder.mkdir(parents=True)
        (bed_folder / "reference.json").write_text(
            json.dumps({"ticket": "WAY-62", "subjects": [{"meshes": ["SM_Bed"]}]})
        )
        links = find_ticket_links(project.root, ("SourceArt/**/reference.json",), catalog.list_sources())
        assert links["WAY-62"].source == "SourceArt/Rooms/Rooms.blend"
        assert links["WAY-62"].exports == ("SourceArt/Rooms/Cabin/Export/SM_Bed.fbx",)


# ####################### #
#         REFRESH         #
# ####################### #


class TestRefresh:
    def test_a_refresh_lists_every_queue_with_its_latest_delivery(
        self,
        service: Service,
        project: Project,
        tracker: FakeTracker,
    ) -> None:
        queue = service.review.refresh()

        assert queue.is_configured
        assert [(tab.name, tab.tickets) for tab in queue.tabs] == [
            ("Art", ("WAY-61", "WAY-60", "WAY-70")),
            ("UI", ("WAY-70",)),
        ]
        assert [item.ticket for item in queue.items] == ["WAY-61", "WAY-60", "WAY-70"]
        carbine = service.review.item("WAY-60")
        assert carbine.queues == ("Art",)
        assert service.review.item("WAY-70").queues == ("Art", "UI")
        assert [comment.is_reviewer for comment in carbine.comments] == [False, True, False, False]

        # The newest comment with files that the reviewer did not write; the reviewer's drawings are not a delivery.
        delivery = carbine.delivery
        assert delivery is not None
        assert (delivery.comment_id, delivery.text, delivery.error) == ("4-3", "Round 2", "")
        names = [attachment.name for attachment in delivery.attachments]
        assert names == ["hero.png", "image.png", "image-8-3.png", "_.._escape.png"]
        folder = project.state_dir / "review" / "WAY-60" / "delivery" / "4-3"
        assert all(attachment.path.parent == folder for attachment in delivery.attachments)
        assert (folder / "hero.png").read_bytes() == b"hero-round-2"
        assert (folder / "image-8-3.png").read_bytes() == b"side-again"
        assert service.review.item("WAY-61").delivery is None
        assert service.events == [
            ReviewEvent(kind=ReviewEventKind.REFRESHED, added=("WAY-61", "WAY-60", "WAY-70")),
        ]

    def test_a_refresh_links_items_to_their_sources(self, service: Service, project: Project) -> None:
        carbine_folder = project.root / "SourceArt/Weapons/Carbine/Source/Carbine"
        carbine_folder.mkdir(parents=True)
        (carbine_folder / "spec.md").write_text("Ticket: WAY-60\n")
        service.review.refresh()
        link = service.review.item("WAY-60").link
        assert link is not None
        assert link.source == CARBINE
        assert service.review.item("WAY-61").link is None

    def test_files_already_downloaded_are_kept_and_superseded_rounds_removed(
        self,
        service: Service,
        project: Project,
        tracker: FakeTracker,
    ) -> None:
        service.review.refresh()
        assert len(tracker.downloads) == 4
        service.review.refresh()
        assert len(tracker.downloads) == 4
        assert len(service.events) == 1

        round_three = make_attachment("8-5", "hero.png", b"hero-round-3", tracker)
        carbine = tracker.tickets["WAY-60"]
        tracker.tickets["WAY-60"] = carbine.model_copy(
            update={"comments": (*carbine.comments, make_comment("4-5", "2026-10-04T00:00:00+00:00", (round_three,)))}
        )
        service.review.refresh()

        delivery_folder = project.state_dir / "review" / "WAY-60" / "delivery"
        assert [folder.name for folder in delivery_folder.iterdir()] == ["4-5"]
        assert service.events[-1] == ReviewEvent(kind=ReviewEventKind.REFRESHED, changed=("WAY-60",))

    def test_a_ticket_that_leaves_the_queue_takes_its_downloads_with_it(
        self,
        service: Service,
        project: Project,
        tracker: FakeTracker,
    ) -> None:
        service.review.refresh()
        tracker.results[ART_QUERY] = ["WAY-61", "WAY-70"]
        service.review.refresh()

        assert not any((project.state_dir / "review" / "WAY-60" / "delivery").iterdir())
        assert service.events[-1] == ReviewEvent(kind=ReviewEventKind.REFRESHED, removed=("WAY-60",))
        with pytest.raises(KeyError, match="not in the review queue"):
            service.review.item("WAY-60")

    def test_a_failed_download_is_reported_on_its_delivery(self, service: Service, tracker: FakeTracker) -> None:
        tracker.failing_attachments.add("8-2")
        service.review.refresh()
        delivery = service.review.item("WAY-60").delivery
        assert delivery is not None
        assert "image.png: attachment 8-2 is gone" in delivery.error
        assert "image.png" not in [attachment.name for attachment in delivery.attachments]

    def test_a_failed_refresh_keeps_the_last_queue_and_says_why(self, service: Service, tracker: FakeTracker) -> None:
        service.review.refresh()
        tracker.search_error = "YouTrack answered 401"
        with pytest.raises(TrackerError):
            service.review.refresh()

        queue = service.review.queue()
        assert len(queue.items) == 3
        assert queue.error == "YouTrack answered 401"
        assert service.events[-1] == ReviewEvent(kind=ReviewEventKind.FAILED, error="YouTrack answered 401")

        tracker.search_error = None
        assert service.review.refresh().error == ""
        assert service.events[-1].kind is ReviewEventKind.REFRESHED

    def test_a_project_without_a_review_table_has_an_empty_queue(
        self,
        project_root: Path,
        tmp_path: Path,
        tracker: FakeTracker,
    ) -> None:
        project = open_project(project_root, state_dir=tmp_path / "plain-state")
        service = Service(project, Catalog(project), tracker)
        queue = service.review.refresh()
        assert not queue.is_configured
        assert queue.items == ()
        assert tracker.opened_with == []
        assert service.events == []


# ####################### #
#        DECISIONS        #
# ####################### #


class TestDecisions:
    def test_approving_posts_attaches_moves_and_leaves_the_queue(
        self,
        service: Service,
        tracker: FakeTracker,
        tmp_path: Path,
    ) -> None:
        service.review.refresh()
        drawing = tmp_path / "WAY-60-markup-1.png"
        drawing.write_bytes(b"png")

        decision = service.review.apply_decision("WAY-60", ReviewVerdict.APPROVE, "Approved.", [drawing])

        assert tracker.writes == [
            ("comment", "WAY-60", "4-101", "Approved."),
            ("attach", "WAY-60", "4-101", "WAY-60-markup-1.png"),
            ("stage", "WAY-60", "Done"),
        ]
        assert (decision.comment_id, decision.stage, decision.attachments) == (
            "4-101",
            "Done",
            ("WAY-60-markup-1.png",),
        )
        assert decision.moved_linked == ()
        assert "WAY-60" not in [item.ticket for item in service.review.queue().items]
        assert "WAY-60" not in service.review.queue().tabs[0].tickets
        assert service.events[-1] == ReviewEvent(kind=ReviewEventKind.DECIDED, removed=("WAY-60",))

    def test_approving_a_ui_design_unblocks_only_its_matching_adoption_ticket(
        self,
        service: Service,
        tracker: FakeTracker,
    ) -> None:
        service.review.refresh()
        decision = service.review.apply_decision("WAY-70", ReviewVerdict.APPROVE, "Approved.")
        assert decision.moved_linked == ("WAY-71",)
        assert tracker.tickets["WAY-71"].stage == "Backlog"
        # Wrong stage, and wrong summary: both left alone.
        assert tracker.tickets["WAY-72"].stage == "Develop"
        assert tracker.tickets["WAY-73"].stage == "Blocked"

    def test_requesting_changes_moves_back_and_unblocks_nothing(self, service: Service, tracker: FakeTracker) -> None:
        service.review.refresh()
        decision = service.review.apply_decision("WAY-70", ReviewVerdict.CHANGES, "Bigger buttons.")
        assert decision.stage == "Develop"
        assert decision.moved_linked == ()
        assert tracker.tickets["WAY-71"].stage == "Blocked"

    def test_a_failure_after_posting_hands_back_the_comment_so_a_retry_never_posts_twice(
        self,
        service: Service,
        tracker: FakeTracker,
        tmp_path: Path,
    ) -> None:
        drawing = tmp_path / "markup.png"
        drawing.write_bytes(b"png")
        tracker.attach_error = "YouTrack answered 403"
        with pytest.raises(DecisionError, match="after posting its comment") as failure:
            service.review.apply_decision("WAY-60", ReviewVerdict.CHANGES, "Longer barrel.", [drawing])
        posted = failure.value.posted
        assert posted is not None

        tracker.attach_error = None
        service.review.apply_decision("WAY-60", ReviewVerdict.CHANGES, "Longer barrel.", [drawing], posted=posted)
        assert [write[0] for write in tracker.writes] == ["comment", "attach", "stage"]

    def test_a_ticket_decided_during_a_refresh_stays_out_of_it(self, service: Service, tracker: FakeTracker) -> None:
        service.review.refresh()

        def decide_while_reading() -> None:
            service.review.apply_decision("WAY-61", ReviewVerdict.APPROVE, "Approved.")

        # The tracker still lists WAY-61 in Review: it was read before the stage move landed.
        tracker.during_search = decide_while_reading
        queue = service.review.refresh()
        assert "WAY-61" not in [item.ticket for item in queue.items]

        assert "WAY-61" not in [item.ticket for item in service.review.refresh().items]
        tracker.tickets["WAY-61"] = tracker.tickets["WAY-61"].model_copy(update={"stage": "Review"})
        assert "WAY-61" in [item.ticket for item in service.review.refresh().items]

    def test_deciding_needs_a_review_table(self, project_root: Path, tmp_path: Path, tracker: FakeTracker) -> None:
        project = open_project(project_root, state_dir=tmp_path / "plain-state")
        service = Service(project, Catalog(project), tracker)
        with pytest.raises(ValueError, match="no \\[review\\] table"):
            service.review.apply_decision("WAY-60", ReviewVerdict.APPROVE, "Approved.")
