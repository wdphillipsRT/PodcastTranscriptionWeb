"""Knowledge-export people, guest extraction, and export-state tables."""

import pytest
import sqlalchemy as sa

from extensions import db
import models as m


@pytest.fixture
def owner(app):
    with app.app_context():
        db.create_all()
        admin = m.Admin(email="owner@example.com", password_hash="hash")
        db.session.add(admin)
        db.session.flush()
        source = m.ContentSource(admin_id=admin.id, source_type="podcast_rss", external_id="feed",
            canonical_url="https://example.com/feed", name="Show", feed_hosts=["Feed Host"],
            admin_hosts=["Admin Host"])
        item = m.ContentItem(admin_id=admin.id, platform="podcast", item_type="podcast_episode",
            external_id="episode", canonical_url="https://example.com/e", title="Episode",
            feed_guests=["Feed Guest"], admin_guests=[])
        db.session.add_all([source, item])
        db.session.commit()
        yield source, item
        db.session.remove()
        db.drop_all()


def test_people_columns_round_trip(owner):
    source, item = owner
    db.session.expire_all()
    assert (source.feed_hosts, source.admin_hosts) == (["Feed Host"], ["Admin Host"])
    assert (item.feed_guests, item.admin_guests) == (["Feed Guest"], [])


@pytest.mark.parametrize("values", [
    dict(state="completed", guests=None),
    dict(state="completed", guests=[], error_category="claude_invalid_output"),
    dict(state="failed", guests=None),
    dict(state="skipped", guests=[]),
    dict(state="completed", guests=[], cost_microusd=-1),
])
def test_guest_extraction_outcomes_are_constrained(owner, values):
    _, item = owner
    db.session.add(m.GuestExtraction(item_id=item.id, input_sha256="a" * 64, model="claude-sonnet-5-5", **values))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_guest_extraction_is_unique_per_item_input(owner):
    _, item = owner
    for _ in range(2):
        db.session.add(m.GuestExtraction(item_id=item.id, input_sha256="b" * 64, state="completed",
                                         guests=[], model="claude-sonnet-5-5"))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def batch(**values):
    defaults = dict(batch_id="podcast-20261022-001", run_date="20261022", sequence=1, record_count=1, blob_count=0)
    return m.KnowledgeExportBatch(**{**defaults, **values})


@pytest.mark.parametrize("values", [
    dict(sequence=0), dict(sequence=1000), dict(record_count=0), dict(record_count=10001),
    dict(blob_count=-1), dict(state="delivered"),
])
def test_export_batch_constraints(owner, values):
    db.session.add(batch(**values))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_export_batch_ids_and_sequences_are_unique(owner):
    db.session.add_all([batch(), batch(batch_id="podcast-20261022-002")])
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_item_exports_are_unique_per_generation_and_record(owner):
    _, item = owner
    row = batch()
    db.session.add(row)
    db.session.flush()
    for record_id in ("podcast:podcast.episode:item-1/rev-1/e1", "podcast:podcast.episode:item-1/rev-1/e1-copy"):
        db.session.add(m.KnowledgeItemExport(export_batch_id=row.id, item_id=item.id, classification_revision_id=1,
            generation=1, episode_record_id=record_id, payload_sha256="c" * 64, passage_count=0))
    with pytest.raises(sa.exc.IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_deleting_a_batch_cascades_to_its_item_exports(owner):
    _, item = owner
    row = batch()
    db.session.add(row)
    db.session.flush()
    db.session.add(m.KnowledgeItemExport(export_batch_id=row.id, item_id=item.id, classification_revision_id=1,
        generation=1, episode_record_id="podcast:podcast.episode:item-1/rev-1/e1",
        payload_sha256="c" * 64, passage_count=2))
    db.session.commit()
    db.session.delete(row)
    db.session.commit()
    assert db.session.scalar(sa.select(sa.func.count()).select_from(m.KnowledgeItemExport)) == 0
