import io
from zipfile import ZipFile

import pytest

from app.files import add_file, add_zip, relative_name, within


@pytest.mark.parametrize(
    'name', ['../escape', '/root/private', 'a/../../escape', 'C:\\Windows\\file', 'CON.txt', 'a/NUL', 'a/file.']
)
def test_invalid_and_windows_incompatible_paths(name):
    with pytest.raises(ValueError):
        relative_name(name)


def test_native_windows_relative_paths_and_symlink_escape(tmp_path):
    assert relative_name('team\\nested\\report.xlsx').as_posix() == 'team/nested/report.xlsx'
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.mkdir()
    (tmp_path / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        within(tmp_path, 'link/file.txt')


def test_duplicate_content_kept_but_case_collisions_rejected(tmp_path):
    manifest = []
    add_file(tmp_path, 'inputs', 'Team/first.txt', b'same', manifest)
    add_file(tmp_path, 'inputs', 'Team/second.txt', b'same', manifest)
    assert manifest[0]['sha256'] == manifest[1]['sha256']
    with pytest.raises(ValueError):
        add_file(tmp_path, 'inputs', 'team/FIRST.txt', b'new', manifest)
    assert (tmp_path / 'inputs/Team/first.txt').read_bytes() == b'same'


def test_zip_nested_extract_and_expanded_size_limit(tmp_path):
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('department/HR/report.txt', 'synthetic report')
    manifest = []
    add_zip(tmp_path, 'packet.zip', data.getvalue(), manifest, 10, 100)
    assert (tmp_path / 'inputs/packet/department/HR/report.txt').read_text() == 'synthetic report'
    with pytest.raises(ValueError, match='expanded'):
        add_zip(tmp_path, 'other.zip', data.getvalue(), [], 10, 1)


def test_zip_slip_does_not_write_outside_workspace(tmp_path):
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('../../escaped.txt', 'unsafe')
    with pytest.raises(ValueError):
        add_zip(tmp_path, 'packet.zip', data.getvalue(), [], 10, 100)
    assert not (tmp_path / 'escaped.txt').exists()
