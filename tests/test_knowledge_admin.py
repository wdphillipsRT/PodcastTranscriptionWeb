"""The administrator sees and corrects hosts and guests; admin values win."""

from extensions import db
from integrations.knowledge.people import guest_input_sha256
from models import ContentItem, ContentSource, GuestExtraction
from tests.test_web_sources import workspace  # noqa: F401


def rows(workspace):  # noqa: F811
    return ContentSource.query.one(), ContentItem.query.one()


def test_source_without_host_is_flagged_until_the_admin_names_one(workspace):  # noqa: F811
    with workspace.app.app_context():
        source, _ = rows(workspace)
        source_id = source.id
    page = workspace.client.get(f"/sources/{source_id}").get_data(as_text=True)
    assert "No host is set" in page and "not exported" in page
    response = workspace.client.post(f"/sources/{source_id}/hosts", data={"hosts": "Ada Host\nGrace Cohost\n"})
    assert response.status_code == 303
    page = workspace.client.get(f"/sources/{source_id}").get_data(as_text=True)
    assert "No host is set" not in page
    assert "Ada Host, Grace Cohost" in page and "Set by the administrator" in page
    with workspace.app.app_context():
        source, _ = rows(workspace)
        assert source.admin_hosts == ["Ada Host", "Grace Cohost"] and source.hosts_updated_at is not None


def test_admin_hosts_win_and_an_empty_list_restores_the_feed(workspace):  # noqa: F811
    with workspace.app.app_context():
        source, _ = rows(workspace)
        source.feed_hosts = ["Channel Owner"]
        db.session.commit()
        source_id = source.id
    assert "Channel Owner" in workspace.client.get(f"/sources/{source_id}").get_data(as_text=True)
    response = workspace.client.post(f"/api/sources/{source_id}/hosts", json={"hosts": "Real Host"})
    assert response.status_code == 200 and response.json == {"id": source_id, "hosts": ["Real Host"],
                                                             "basis": "admin_set"}
    response = workspace.client.post(f"/api/sources/{source_id}/hosts", json={"hosts": ""})
    assert response.json == {"id": source_id, "hosts": ["Channel Owner"], "basis": "feed_declared"}
    with workspace.app.app_context():
        assert rows(workspace)[0].admin_hosts is None


def test_invalid_host_input_is_rejected_without_changes(workspace):  # noqa: F811
    with workspace.app.app_context():
        source_id = rows(workspace)[0].id
    for payload in ({"hosts": "!!!"}, {"hosts": ["Ada"]}, {"hosts": "Ada", "extra": "x"}):
        assert workspace.client.post(f"/api/sources/{source_id}/hosts", json=payload).status_code == 400
    assert workspace.client.post("/api/sources/999/hosts", json={"hosts": "Ada"}).status_code == 404
    with workspace.app.app_context():
        assert rows(workspace)[0].admin_hosts is None


def test_item_page_shows_guests_with_basis_and_accepts_corrections(workspace):  # noqa: F811
    with workspace.app.app_context():
        source, item = rows(workspace)
        item.description = "With Grace Hopper"
        db.session.add(GuestExtraction(item_id=item.id, input_sha256=guest_input_sha256(item), state="completed",
            guests=[{"name": "Grace Hopper", "confidence": 0.81}], model="claude-sonnet-5-5"))
        db.session.commit()
        item_id = item.id
    page = workspace.client.get(f"/items/{item_id}").get_data(as_text=True)
    assert "Grace Hopper · Inferred from the title and description · confidence 0.81" in page
    assert "Hosts: none" in page
    # The correction form is not prefilled with the model's guess.
    assert '<textarea id="guests" name="guests" rows="3" maxlength="10000"></textarea>' in page
    response = workspace.client.post(f"/items/{item_id}/guests", data={"guests": "Grace Hopper\nAlan Turing"})
    assert response.status_code == 303
    page = workspace.client.get(f"/items/{item_id}").get_data(as_text=True)
    assert "Alan Turing · Set by the administrator" in page and "Use feed and inferred guests" in page
    response = workspace.client.post(f"/api/items/{item_id}/guests", json={"guests": ""})
    assert response.json == {"id": item_id, "guests": []}
    with workspace.app.app_context():
        assert rows(workspace)[1].admin_guests == []
    response = workspace.client.post(f"/api/items/{item_id}/guests/reset", json={})
    assert response.json == {"id": item_id, "guests": ["Grace Hopper"]}
    with workspace.app.app_context():
        item = rows(workspace)[1]
        assert item.admin_guests is None and item.guests_updated_at is not None


def test_invalid_guest_input_is_rejected(workspace):  # noqa: F811
    with workspace.app.app_context():
        item_id = rows(workspace)[1].id
    for payload in ({"guests": "???"}, {"guests": None}, {"guests": "Ada", "hosts": "x"}):
        assert workspace.client.post(f"/api/items/{item_id}/guests", json=payload).status_code == 400
    assert workspace.client.post("/api/items/999/guests", json={"guests": "Ada"}).status_code == 404
    with workspace.app.app_context():
        assert rows(workspace)[1].admin_guests is None


def test_resaving_unchanged_hosts_keeps_the_timestamp(workspace):  # noqa: F811
    with workspace.app.app_context():
        source_id = rows(workspace)[0].id
    assert workspace.client.post(f"/api/sources/{source_id}/hosts", json={"hosts": ""}).status_code == 200
    with workspace.app.app_context():
        assert rows(workspace)[0].hosts_updated_at is None
    workspace.client.post(f"/api/sources/{source_id}/hosts", json={"hosts": "Ada Host\nGrace Cohost"})
    with workspace.app.app_context():
        first = rows(workspace)[0].hosts_updated_at
        assert first is not None
    response = workspace.client.post(f"/sources/{source_id}/hosts", data={"hosts": " Ada  Host \n\nGrace Cohost\n"})
    assert response.status_code == 303
    with workspace.app.app_context():
        assert rows(workspace)[0].hosts_updated_at == first
    response = workspace.client.post(f"/api/sources/{source_id}/hosts", json={"hosts": "Grace Cohost\nAda Host"})
    assert response.json["hosts"] == ["Grace Cohost", "Ada Host"]
    with workspace.app.app_context():
        assert rows(workspace)[0].hosts_updated_at > first


def test_resaving_unchanged_guests_or_resetting_twice_keeps_the_timestamp(workspace):  # noqa: F811
    with workspace.app.app_context():
        item_id = rows(workspace)[1].id
    response = workspace.client.post(f"/api/items/{item_id}/guests/reset", json={})
    assert response.json == {"id": item_id, "guests": []}
    with workspace.app.app_context():
        assert rows(workspace)[1].guests_updated_at is None
    workspace.client.post(f"/api/items/{item_id}/guests", json={"guests": "Grace Hopper"})
    with workspace.app.app_context():
        first = rows(workspace)[1].guests_updated_at
        assert first is not None
    response = workspace.client.post(f"/items/{item_id}/guests", data={"guests": "Grace  Hopper\n"})
    assert response.status_code == 303
    with workspace.app.app_context():
        assert rows(workspace)[1].guests_updated_at == first
    response = workspace.client.post(f"/api/items/{item_id}/guests", json={"guests": ""})
    assert response.json == {"id": item_id, "guests": []}
    with workspace.app.app_context():
        second = rows(workspace)[1].guests_updated_at
        assert rows(workspace)[1].admin_guests == [] and second > first
    workspace.client.post(f"/api/items/{item_id}/guests", json={"guests": ""})
    with workspace.app.app_context():
        assert rows(workspace)[1].guests_updated_at == second
    workspace.client.post(f"/api/items/{item_id}/guests/reset", json={})
    with workspace.app.app_context():
        third = rows(workspace)[1].guests_updated_at
        assert rows(workspace)[1].admin_guests is None and third > second
    workspace.client.post(f"/items/{item_id}/guests/reset", data={})
    with workspace.app.app_context():
        assert rows(workspace)[1].guests_updated_at == third


def test_guest_form_warns_that_saving_replaces_feed_guests(workspace):  # noqa: F811
    with workspace.app.app_context():
        item = rows(workspace)[1]
        item.feed_guests = ["Feed Guest"]
        db.session.commit()
        item_id = item.id
    page = workspace.client.get(f"/items/{item_id}").get_data(as_text=True)
    assert ("Saving replaces feed and inferred guests with this list, even unchanged: "
            "feed guests become set by the administrator and the item is exported again.") in page
