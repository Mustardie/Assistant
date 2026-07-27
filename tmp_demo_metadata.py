from pathlib import Path
from tempfile import TemporaryDirectory
from file_manager.indexer import FileIndexManager
import os
import time

with TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    manager = FileIndexManager(db_path=tmp_path / 'index.sqlite3', config_path=tmp_path / 'index.json')
    manager.config['roots'] = [str(tmp_path)]

    for name, content, mtime in [
        ('old.pdf', 'old', time.time() - 200),
        ('new.pdf', 'new', time.time() - 20),
        ('movie.mp4', 'x' * 100, time.time() - 10),
        ('movie2.mp4', 'x' * 400, time.time() - 5),
    ]:
        path = tmp_path / name
        path.write_text(content, encoding='utf-8')
        os.utime(path, (mtime, mtime))
        manager._index_file(path)

    print('build_index', manager.rebuild_index())
    print('latest_pdf', manager.parse_query('open my latest pdf'))
    print('latest_pdf_results', [item['path'] for item in manager.search('open my latest pdf', limit=5)])
    print('newest_doc', manager.parse_query('open my newest document'))
    print('biggest_video', manager.parse_query('find my biggest video'))
    print('biggest_video_results', [item['path'] for item in manager.search('find my biggest video', limit=5)])
