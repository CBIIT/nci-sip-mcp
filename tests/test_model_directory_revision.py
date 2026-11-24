"""A model supplied as a directory is identified by a hash of its files."""

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ncit_index.embeddings import SentenceTransformersProvider
from ncit_index.errors import IndexCompatibilityError

FILES = {
    "config.json": b'{"model_type": "bert"}',
    "1_Pooling/config.json": b'{"pooling": "mean"}',
    "tokenizer/vocab.txt": b"[PAD]\nneoplasm\n",
}


def expected_revision(files):
    """The documented value, computed independently: sorted `<path>\\0<sha256>\\n` lines."""
    lines = "".join(
        f"{path}\0{hashlib.sha256(content).hexdigest()}\n"
        for path, content in sorted(files.items())
    )
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


class Model:
    def __init__(self, name, *, local_files_only=False):
        if not (local_files_only or Path(name).is_dir()):
            raise OSError("Network loading is forbidden")


def write_tree(root, files):
    for path, content in files.items():
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class ModelDirectoryRevisionTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.model = self.root / "model"
        write_tree(self.model, FILES)
        patcher = patch.dict(
            sys.modules, {"sentence_transformers": types.SimpleNamespace(SentenceTransformer=Model)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def revision(self, path=None, **options):
        provider = SentenceTransformersProvider(str(path or self.model), **options)
        return getattr(provider, "revision", "absent")

    def test_the_revision_is_the_documented_hash_in_both_loading_modes(self):
        for options in ({"local_files_only": True}, {}):
            with self.subTest(**options):
                self.assertEqual(self.revision(**options), expected_revision(FILES))

    def test_location_and_modification_time_do_not_count(self):
        copy = self.root / "elsewhere" / "copy"
        shutil.copytree(self.model, copy)
        os.utime(copy / "config.json", (1, 1))
        self.assertEqual(self.revision(copy), expected_revision(FILES))

    def test_a_changed_byte_a_rename_and_an_added_file_each_change_it(self):
        changed = {**FILES, "config.json": b'{"model_type": "Bert"}'}
        renamed = {("config2.json" if k == "config.json" else k): v for k, v in FILES.items()}
        added = {**FILES, "extra.bin": b"x"}
        for label, files in (("byte", changed), ("rename", renamed), ("added", added)):
            with self.subTest(label):
                other = self.root / label
                write_tree(other, files)
                self.assertEqual(self.revision(other), expected_revision(files))
                self.assertNotEqual(expected_revision(files), expected_revision(FILES))

    def test_a_file_link_is_hashed_as_its_target_under_the_links_own_path(self):
        # A Hub snapshot folder holds links into blobs/; it must hash like a copy of the files.
        hub = self.root / "hub"
        blobs = hub / "blobs"
        blobs.mkdir(parents=True)
        for index, (path, content) in enumerate(FILES.items()):
            (blobs / str(index)).write_bytes(content)
            link = hub / "snapshot" / path
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(blobs / str(index), link.parent))
        self.assertEqual(self.revision(hub / "snapshot"), expected_revision(FILES))

    def test_unhashable_entries_are_refused_naming_the_path(self):
        cases = {
            "tokenizer/linked": lambda p: p.symlink_to(self.model / "tokenizer"),
            "broken.bin": lambda p: p.symlink_to(self.root / "absent"),
            "empty": lambda p: p.mkdir(),
        }
        for relative, make in cases.items():
            with self.subTest(relative):
                make(self.model / relative)
                with self.assertRaisesRegex(IndexCompatibilityError, relative):
                    self.revision()
                path = self.model / relative
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()

    @unittest.skipIf(os.geteuid() == 0, "root can list any directory")
    def test_an_unlistable_subdirectory_is_refused_naming_the_path(self):
        locked = self.model / "tokenizer"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaisesRegex(IndexCompatibilityError, "tokenizer"):
            self.revision()


if __name__ == "__main__":
    unittest.main()
