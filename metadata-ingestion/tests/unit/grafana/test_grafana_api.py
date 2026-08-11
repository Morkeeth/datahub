from unittest.mock import MagicMock, patch

import pytest
import requests
from pydantic import SecretStr

from datahub.ingestion.source.grafana.grafana_api import GrafanaAPIClient
from datahub.ingestion.source.grafana.models import Dashboard, Folder
from datahub.ingestion.source.grafana.report import GrafanaSourceReport


@pytest.fixture
def mock_session():
    with patch("requests.Session") as session:
        yield session.return_value


@pytest.fixture
def api_client(mock_session):
    report = GrafanaSourceReport()
    return GrafanaAPIClient(
        base_url="http://grafana.test",
        token=SecretStr("test-token"),
        verify_ssl=True,
        page_size=100,
        report=report,
    )


def test_create_session(mock_session):
    report = GrafanaSourceReport()
    GrafanaAPIClient(
        base_url="http://grafana.test",
        token=SecretStr("test-token"),
        verify_ssl=True,
        page_size=100,
        report=report,
    )

    # Verify headers were properly set
    mock_session.headers.update.assert_called_once_with(
        {
            "Authorization": "Bearer test-token",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    # Verify SSL verification was set
    assert mock_session.verify is True


def test_get_folders_success(api_client, mock_session):
    # First call returns folders
    first_response = MagicMock()
    first_response.json.return_value = [
        {"id": "1", "title": "Folder 1", "description": ""},
        {"id": "2", "title": "Folder 2", "description": ""},
    ]
    first_response.raise_for_status.return_value = None

    # Second call returns empty list to end pagination
    second_response = MagicMock()
    second_response.json.return_value = []
    second_response.raise_for_status.return_value = None

    mock_session.get.side_effect = [first_response, second_response]

    folders = api_client.get_folders()

    assert len(folders) == 2
    assert all(isinstance(f, Folder) for f in folders)
    assert folders[0].id == "1"
    assert folders[0].title == "Folder 1"


def _folder_page(*folders):
    response = MagicMock()
    response.json.return_value = list(folders)
    response.raise_for_status.return_value = None
    return response


def test_get_folders_includes_nested_subfolders(api_client, mock_session):
    # /api/folders returns one level at a time: the root without parentUid,
    # then a folder's direct children with it.
    mock_session.get.side_effect = [
        _folder_page({"id": "1", "uid": "root-uid", "title": "Root"}),
        _folder_page(),
        _folder_page({"id": "2", "uid": "child-uid", "title": "Child"}),
        _folder_page(),
        _folder_page({"id": "3", "uid": "grandchild-uid", "title": "Grandchild"}),
        _folder_page(),
        _folder_page(),
    ]

    folders = api_client.get_folders()

    assert {f.title for f in folders} == {"Root", "Child", "Grandchild"}
    assert all(isinstance(f, Folder) for f in folders)

    parent_uids = [
        call.kwargs["params"].get("parentUid") for call in mock_session.get.call_args_list
    ]
    assert parent_uids[0] is None
    assert "child-uid" in parent_uids


def test_get_folders_does_not_revisit_a_folder(api_client, mock_session):
    # A folder returned as its own descendant would otherwise recurse forever.
    mock_session.get.side_effect = [
        _folder_page({"id": "1", "uid": "a", "title": "A"}),
        _folder_page(),
        _folder_page({"id": "1", "uid": "a", "title": "A"}),
        _folder_page(),
    ]

    folders = api_client.get_folders()

    assert len(folders) == 1


def test_get_folders_keeps_parents_when_a_child_level_fails(api_client, mock_session):
    # A folder the token cannot read should cost that subtree, not the whole walk.
    root_page = _folder_page({"id": "1", "uid": "root-uid", "title": "Root"})
    mock_session.get.side_effect = [
        root_page,
        _folder_page(),
        requests.exceptions.RequestException("403"),
    ]

    folders = api_client.get_folders()

    assert [f.title for f in folders] == ["Root"]
    assert len(api_client.report.failures) == 1


def test_folder_model_carries_the_parent_uid(api_client, mock_session):
    # `parentUid` is what identifies the hierarchy; dropping it at validation
    # is how the walk found subfolders without being able to place them.
    mock_session.get.side_effect = [
        _folder_page(
            {"id": "1", "uid": "root-uid", "title": "Root"},
            {"id": "2", "uid": "child-uid", "parentUid": "root-uid", "title": "Child"},
        ),
        _folder_page(),
        _folder_page(),
        _folder_page(),
    ]

    folders = api_client.get_folders()

    by_title = {f.title: f for f in folders}
    assert by_title["Root"].parent_uid is None
    assert by_title["Child"].parent_uid == "root-uid"
    assert by_title["Child"].uid == "child-uid"


def test_get_folders_error(api_client, mock_session):
    mock_session.get.side_effect = requests.exceptions.RequestException("API Error")

    folders = api_client.get_folders()

    assert len(folders) == 0
    assert len(api_client.report.failures) == 1


def test_get_dashboard_success(api_client, mock_session):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "dashboard": {
            "uid": "test-uid",
            "title": "Test Dashboard",
            "description": "",
            "version": "1",
            "panels": [],
            "tags": [],
            "schemaVersion": "1.0",
            "timezone": "utc",
            "refresh": None,
            "meta": {"folderId": "123"},
        }
    }
    mock_session.get.return_value = mock_response

    dashboard = api_client.get_dashboard("test-uid")

    assert isinstance(dashboard, Dashboard)
    assert dashboard.uid == "test-uid"
    assert dashboard.title == "Test Dashboard"


def test_get_dashboard_error(api_client, mock_session):
    mock_session.get.side_effect = requests.exceptions.RequestException("API Error")

    dashboard = api_client.get_dashboard("test-uid")

    assert dashboard is None
    assert len(api_client.report.warnings) == 1


def test_get_dashboards_success(api_client, mock_session):
    # Mock search response
    search_response = MagicMock()
    search_response.raise_for_status.return_value = None
    search_response.json.return_value = [{"uid": "dash1"}, {"uid": "dash2"}]

    # Mock individual dashboard responses
    dash1_response = MagicMock()
    dash1_response.raise_for_status.return_value = None
    dash1_response.json.return_value = {
        "dashboard": {
            "uid": "dash1",
            "title": "Dashboard 1",
            "description": "",
            "version": "1",
            "panels": [],
            "tags": [],
            "timezone": "utc",
            "schemaVersion": "1.0",
            "meta": {"folderId": None},
        }
    }

    # Mock dashboard2 response
    dash2_response = MagicMock()
    dash2_response.raise_for_status.return_value = None
    dash2_response.json.return_value = {
        "dashboard": {
            "uid": "dash2",
            "title": "Dashboard 2",
            "description": "",
            "version": "1",
            "panels": [],
            "tags": [],
            "timezone": "utc",
            "schemaVersion": "1.0",
            "meta": {"folderId": None},
        }
    }

    # Empty response to end pagination
    empty_response = MagicMock()
    empty_response.json.return_value = []
    empty_response.raise_for_status.return_value = None

    mock_session.get.side_effect = [
        search_response,
        dash1_response,
        dash2_response,
        empty_response,
    ]

    dashboards = api_client.get_dashboards()

    assert len(dashboards) == 2
    assert dashboards[0].uid == "dash1"
    assert dashboards[0].title == "Dashboard 1"


def test_get_dashboards_error(api_client, mock_session):
    mock_session.get.side_effect = requests.exceptions.RequestException("API Error")

    dashboards = api_client.get_dashboards()

    assert len(dashboards) == 0
    assert len(api_client.report.failures) == 1
