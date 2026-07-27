from pathlib import Path
from file_manager.indexer import FileIndexManager


def test_intent_detection_prefers_folder_requests(tmp_path):
    indexer = FileIndexManager(db_path=tmp_path / '.tmp_index.sqlite3', config_path=tmp_path / '.tmp_index.json')
    intent = indexer._build_intent('open my Assistant folder')
    assert intent['type'] == 'open_folder'
    assert intent['target'] == 'folder'


def test_hindi_query_penalizes_zip_files(tmp_path):
    indexer = FileIndexManager(db_path=tmp_path / '.tmp_index.sqlite3', config_path=tmp_path / '.tmp_index.json')
    record = {'filename': 'notes.zip', 'path': 'C:/temp/notes.zip', 'extension': '.zip', 'summary': '', 'caption': '', 'keywords': []}
    score, _ = indexer._score_record(record, {'filters': {'extension': ['.pdf', '.doc', '.docx', '.txt', '.md']}, 'sort': None}, ['hindi', 'notes'])
    assert score < 0


def test_build_intent_extracts_drive_filter(tmp_path):
    indexer = FileIndexManager(db_path=tmp_path / '.tmp_index.sqlite3', config_path=tmp_path / '.tmp_index.json')
    intent = indexer._build_intent('Assistant folder in F drive')
    assert intent['drive'] == 'f'


def test_install_lookup_prefers_program_folders_over_appdata(tmp_path):
    indexer = FileIndexManager(db_path=tmp_path / '.tmp_index.sqlite3', config_path=tmp_path / '.tmp_index.json')
    intent = indexer._build_intent('Cyberpunk installed folder')
    install_folder = {'filename': 'Cyberpunk 2077', 'path': 'D:/SteamLibrary/steamapps/common/Cyberpunk 2077', 'extension': '', 'summary': '', 'caption': '', 'keywords': [], 'is_dir': True, 'name': 'Cyberpunk 2077', 'parent_path': 'D:/SteamLibrary/steamapps/common'}
    appdata_folder = {'filename': 'SaveData', 'path': 'C:/Users/Test/AppData/Roaming/Cyberpunk 2077', 'extension': '', 'summary': '', 'caption': '', 'keywords': [], 'is_dir': True, 'name': 'SaveData', 'parent_path': 'C:/Users/Test/AppData/Roaming'}

    install_score, _ = indexer._score_record(install_folder, intent, ['cyberpunk'])
    appdata_score, _ = indexer._score_record(appdata_folder, intent, ['cyberpunk'])

    assert install_score > appdata_score
