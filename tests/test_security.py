"""Host, origin, token and endpoint-URL checks."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from helpers import LOCAL


def test_unknown_host_is_rejected_without_token(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url='http://attacker.example') as client:
        assert client.get('/api/jobs').status_code == 400
        assert client.get('/').status_code == 400
    for host in ('http://localhost:8003', 'http://[::1]:8003'):
        with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url=host) as client:
            assert client.get('/api/jobs').status_code == 200, host


def test_allowed_hosts_and_token_open_other_host_names(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key='', allowed_hosts=('payroll.internal',))
    with TestClient(create_app(settings), base_url='http://payroll.internal') as client:
        assert client.get('/api/jobs').status_code == 200
    settings = Settings(data_dir=tmp_path, api_key='', host='0.0.0.0', access_token='s3cret')
    with TestClient(create_app(settings), base_url='http://10.0.0.5') as client:
        assert client.get('/api/jobs').status_code == 401
        assert client.get('/api/jobs', headers={'Authorization': 'Bearer s3cret'}).status_code == 200


def test_non_ascii_token_is_refused_not_a_server_error(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key='', access_token='s3cret')
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        response = client.get('/api/jobs', headers={'Authorization': 'Bearer pässwörd'.encode()})
        assert response.status_code == 401


def test_extra_origin_is_allowed_only_when_configured(tmp_path):
    origin = {'Origin': 'https://payroll.example.com'}
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key='')), base_url=LOCAL) as client:
        assert client.get('/api/jobs', headers=origin).status_code == 403
    settings = Settings(data_dir=tmp_path, api_key='', allowed_origins=('https://payroll.example.com/',))
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        assert client.get('/api/jobs', headers=origin).status_code == 200


@pytest.mark.parametrize(
    'url,ok',
    [
        ('http://localhost.evil.com/v1', False),
        ('http://127.0.0.1.nip.io/v1', False),
        ('http://localhost:11434/v1', True),
        ('https://gateway.example.com/v1', True),
    ],
)
def test_base_url_must_be_https_or_real_loopback(url, ok):
    settings = Settings(base_url=url)
    if ok:
        settings.validate()
    else:
        with pytest.raises(ValueError):
            settings.validate()
