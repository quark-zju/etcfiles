import contextlib
import difflib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "merge-pacnew.py"
SPEC = importlib.util.spec_from_file_location("merge_pacnew", SCRIPT)
assert SPEC and SPEC.loader
merge_pacnew = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_pacnew)


def make_diff(original: bytes, modified: bytes) -> bytes:
    return b"".join(
        difflib.diff_bytes(
            difflib.unified_diff,
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=b"package/etc/example.conf",
            tofile=b"current/etc/example.conf",
            n=65536,
        )
    )


class MergePacnewTest(unittest.TestCase):
    def test_files_from_unified_diff(self):
        original = b"first\nsecond\nthird\n"
        modified = b"first changed\nsecond\nextra\nthird\n"

        restored_original, restored_modified = merge_pacnew.files_from_unified_diff(
            make_diff(original, modified)
        )

        self.assertEqual(restored_original, original)
        self.assertEqual(restored_modified, modified)

    def test_incomplete_context_does_not_match_tracked_file(self):
        original = b"before\nsetting=old\nafter\n"
        modified = b"before\nsetting=local\nafter\n"
        short_diff = b"".join(
            difflib.diff_bytes(
                difflib.unified_diff,
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                n=0,
            )
        )

        _, reconstructed_modified = merge_pacnew.files_from_unified_diff(short_diff)

        self.assertNotEqual(reconstructed_modified, modified)

    def test_dry_run_prints_change_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_etc, system_etc, paths = self.make_files(Path(temp_dir))
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                changed = merge_pacnew.process_diff(
                    paths["diff"], repo_etc, system_etc, dry_run=True
                )

            self.assertTrue(changed)
            self.assertEqual(paths["system"].read_bytes(), paths["modified"])
            self.assertTrue(paths["pacnew"].exists())
            self.assertIn("Would delete", output.getvalue())

    def test_real_run_merges_and_deletes_pacnew(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_etc, system_etc, paths = self.make_files(Path(temp_dir))

            changed = merge_pacnew.process_diff(
                paths["diff"], repo_etc, system_etc, dry_run=False
            )

            self.assertTrue(changed)
            self.assertEqual(paths["system"].read_bytes(), paths["merged"])
            self.assertFalse(paths["pacnew"].exists())

    def test_changed_system_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_etc, system_etc, paths = self.make_files(Path(temp_dir))
            paths["system"].write_bytes(b"changed after backup\n")

            changed = merge_pacnew.process_diff(
                paths["diff"], repo_etc, system_etc, dry_run=False
            )

            self.assertFalse(changed)
            self.assertEqual(paths["system"].read_bytes(), b"changed after backup\n")
            self.assertTrue(paths["pacnew"].exists())

    @staticmethod
    def make_files(temp: Path):
        repo_etc = temp / "repo" / "etc"
        system_etc = temp / "system" / "etc"
        repo_etc.mkdir(parents=True)
        system_etc.mkdir(parents=True)

        original = b"setting=old\n# unchanged\nupstream=old\n"
        modified = b"setting=local\n# unchanged\nupstream=old\n"
        pacnew = b"setting=old\n# unchanged\nupstream=new\n"
        merged = b"setting=local\n# unchanged\nupstream=new\n"
        tracked_path = repo_etc / "example.conf"
        diff_path = repo_etc / "example.conf.diff"
        system_path = system_etc / "example.conf"
        pacnew_path = system_etc / "example.conf.pacnew"
        tracked_path.write_bytes(modified)
        diff_path.write_bytes(make_diff(original, modified))
        system_path.write_bytes(modified)
        pacnew_path.write_bytes(pacnew)
        return (
            repo_etc,
            system_etc,
            {
                "diff": diff_path,
                "system": system_path,
                "pacnew": pacnew_path,
                "modified": modified,
                "merged": merged,
            },
        )


if __name__ == "__main__":
    unittest.main()
