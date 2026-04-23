
"""本文件用于验证重复文件判定与删除前筛选逻辑。"""

from pathlib import Path
from typing import Any, cast
import sys
import tempfile
import types


class DummyQApplication:
    @staticmethod
    def setAttribute(*_args: Any, **_kwargs: Any) -> None:
        return None


class DummyQWidget:
    pass


class DummyFileDialog:
    ShowDirsOnly = 0
    DontResolveSymlinks = 0

    @staticmethod
    def getExistingDirectory(*_args: Any, **_kwargs: Any) -> str:
        return ""


class DummyQt:
    AA_EnableHighDpiScaling = 0


class DummySend2TrashModule:
    @staticmethod
    def send2trash(*_args: Any, **_kwargs: Any) -> None:
        return None


def install_test_stubs() -> None:
    pyqt5_module = types.ModuleType("PyQt5")
    qtwidgets_module = types.ModuleType("PyQt5.QtWidgets")
    qtgui_module = types.ModuleType("PyQt5.QtGui")
    qtcore_module = types.ModuleType("PyQt5.QtCore")

    cast(Any, qtwidgets_module).QApplication = DummyQApplication
    cast(Any, qtwidgets_module).QWidget = DummyQWidget
    cast(Any, qtwidgets_module).QFileDialog = DummyFileDialog
    cast(Any, qtcore_module).Qt = DummyQt

    cast(Any, pyqt5_module).QtCore = qtcore_module
    cast(Any, pyqt5_module).QtWidgets = qtwidgets_module
    cast(Any, pyqt5_module).QtGui = qtgui_module

    sys.modules.setdefault("PyQt5", pyqt5_module)
    sys.modules.setdefault("PyQt5.QtWidgets", qtwidgets_module)
    sys.modules.setdefault("PyQt5.QtGui", qtgui_module)
    sys.modules.setdefault("PyQt5.QtCore", qtcore_module)

    send2trash_module = cast(Any, DummySend2TrashModule())
    sys.modules.setdefault("send2trash", send2trash_module)


install_test_stubs()

import main
from main import (
    delete_duplicate_files,
    get_duplicate_candidate_pairs,
    get_duplicate_candidate_pairs_cross_directory,
    get_startup_notice_text,
    resolve_wechat_scan_directories,
    scan_wechat_directories,
)


def make_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_detects_tail_number_candidate() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(1).txt", b"same")
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert len(pairs) == 1
        assert pairs[0][0].name == "a.txt"
        assert pairs[0][1].name == "a(1).txt"


def test_detects_multi_digit_tail_number_candidate() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(12).txt", b"same")
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert len(pairs) == 1
        assert pairs[0][1].name == "a(12).txt"


def test_rejects_non_tail_number_name() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(1)b.txt", b"same")
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert pairs == []


def test_requires_original_file_to_exist() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a(1).txt", b"same")
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert pairs == []


def test_rejects_directory_named_like_duplicate() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "foo").mkdir()
        (root / "foo(1)").mkdir()
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert pairs == []


def test_rejects_different_file_size() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(1).txt", b"same plus")
        pairs, errors = get_duplicate_candidate_pairs(root)
        assert errors == []
        assert pairs == []


def test_rejects_different_hash_with_same_size() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "Weixin Text"
        msg_file = root / "xwechat_files" / "user_a" / "msg" / "file"
        make_file(msg_file / "a.txt", b"abcd")
        make_file(msg_file / "a(1).txt", b"wxyz")
        pairs, errors = get_duplicate_candidate_pairs(msg_file)
        assert errors == []
        assert len(pairs) == 1

        trashed: list[str] = []
        result = delete_duplicate_files(root, send_to_trash=lambda path: trashed.append(Path(path).name))
        assert result["deleted"] == 0
        assert trashed == []


def test_deletes_only_verified_duplicate_files() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "Weixin Text"
        msg_file = root / "xwechat_files" / "user_a" / "msg" / "file"
        trashed: list[str] = []
        make_file(msg_file / "a.txt", b"same")
        make_file(msg_file / "a(1).txt", b"same")
        make_file(msg_file / "b.txt", b"left")
        make_file(msg_file / "b(1).txt", b"diff")

        pairs, errors = get_duplicate_candidate_pairs(msg_file)
        assert len(pairs) == 2

        result = delete_duplicate_files(root, send_to_trash=lambda path: trashed.append(Path(path).name))

        assert result["deleted"] == 1
        assert result["errors"] == 0
        assert trashed == ["a(1).txt"]


def test_handles_scan_errors_without_crashing() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(1).txt", b"same")

        original_files_have_same_size = main.files_have_same_size

        def failing_files_have_same_size(original_path: Path, duplicate_path: Path) -> bool:
            if duplicate_path.name == "a(1).txt":
                raise OSError("simulated scan failure")
            return original_files_have_same_size(original_path, duplicate_path)

        main.files_have_same_size = failing_files_have_same_size
        try:
            pairs, errors = get_duplicate_candidate_pairs(root)
        finally:
            main.files_have_same_size = original_files_have_same_size

        assert len(pairs) == 0
        assert len(errors) == 1
        assert any("扫描失败" in message for message in errors)


def test_rejects_symbolic_link_candidates() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        make_file(root / "a.txt", b"same")
        make_file(root / "a(1).txt", b"same")

        original_is_symlink = Path.is_symlink

        def fake_is_symlink(self: Path) -> bool:
            if self.name == "a(1).txt":
                return True
            return original_is_symlink(self)

        Path.is_symlink = fake_is_symlink
        try:
            pairs, errors = get_duplicate_candidate_pairs(root)
        finally:
            Path.is_symlink = original_is_symlink

        assert errors == []
        assert pairs == []


def test_recursive_delete_checks_subdirectories() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "Weixin Text"
        child = root / "xwechat_files" / "user_a" / "msg" / "file" / "child"
        trashed: list[str] = []
        make_file(root / "xwechat_files" / "user_a" / "msg" / "file" / "a.txt", b"same")
        make_file(root / "xwechat_files" / "user_a" / "msg" / "file" / "a(1).txt", b"same")
        make_file(child / "c.txt", b"same")
        make_file(child / "c(1).txt", b"same")

        result = delete_duplicate_files(root, recursive=True, send_to_trash=lambda path: trashed.append(Path(path).name))

        assert result["deleted"] == 2
        assert result["errors"] == 0
        assert sorted(trashed) == ["a(1).txt", "c(1).txt"]


def test_recursive_scan_checks_subdirectories_in_msg_file() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "Weixin Text"
        child = root / "xwechat_files" / "user_a" / "msg" / "file" / "child"
        progress_messages: list[str] = []
        make_file(root / "xwechat_files" / "user_a" / "msg" / "file" / "a.txt", b"same")
        make_file(root / "xwechat_files" / "user_a" / "msg" / "file" / "a(1).txt", b"same")
        make_file(child / "c.txt", b"same")
        make_file(child / "c(1).txt", b"same")

        pairs, errors, scanned_directories = scan_wechat_directories(
            root,
            recursive=True,
            cross_directory_mode=False,
            progress_callback=progress_messages.append,
        )

        assert errors == []
        assert len(pairs) == 2
        assert child in scanned_directories
        assert any("当前目录" in message and "child" in message for message in progress_messages)


def test_cross_directory_scan_reports_progress() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        child = root / "child"
        progress_messages: list[str] = []
        make_file(root / "a.txt", b"same")
        make_file(child / "a(1).txt", b"same")

        pairs, errors = get_duplicate_candidate_pairs_cross_directory(
            root,
            progress_callback=progress_messages.append,
            progress_interval=1,
        )

        assert errors == []
        assert len(pairs) == 1
        assert any("正在扫描目录树" in message for message in progress_messages)
        assert any("正在匹配候选项" in message for message in progress_messages)


def test_resolves_msg_file_directories_from_weixin_text_root() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "Weixin Text"
        make_file(root / "xwechat_files" / "user_a" / "msg" / "file" / "a.txt", b"same")
        make_file(root / "xwechat_files" / "user_b" / "msg" / "file" / "b.txt", b"same")
        make_file(root / "xwechat_files" / "user_a" / "msg" / "encrypted" / "c.txt", b"same")

        resolved = resolve_wechat_scan_directories(root)

        assert resolved == [
            root / "xwechat_files" / "user_a" / "msg" / "file",
            root / "xwechat_files" / "user_b" / "msg" / "file",
        ]


def test_resolves_msg_file_directory_from_account_root() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        account_root = Path(temp_dir) / "xwechat_files" / "user_a"
        make_file(account_root / "msg" / "file" / "a.txt", b"same")
        make_file(account_root / "msg" / "encrypted" / "b.txt", b"same")

        resolved = resolve_wechat_scan_directories(account_root)

        assert resolved == [account_root / "msg" / "file"]


def test_startup_notice_explains_safety_rules() -> None:
    notice = get_startup_notice_text()

    assert "识别并整理微信类重复文件" in notice
    assert "扩展名前以 (数字) 结尾" in notice
    assert "SHA-256" in notice
    assert "回收站" in notice
    assert "先扫描" in notice


def run_tests() -> None:
    tests = [
        test_detects_tail_number_candidate,
        test_detects_multi_digit_tail_number_candidate,
        test_rejects_non_tail_number_name,
        test_requires_original_file_to_exist,
        test_rejects_directory_named_like_duplicate,
        test_rejects_different_file_size,
        test_rejects_different_hash_with_same_size,
        test_deletes_only_verified_duplicate_files,
        test_handles_scan_errors_without_crashing,
        test_rejects_symbolic_link_candidates,
        test_recursive_delete_checks_subdirectories,
        test_recursive_scan_checks_subdirectories_in_msg_file,
        test_cross_directory_scan_reports_progress,
        test_resolves_msg_file_directories_from_weixin_text_root,
        test_resolves_msg_file_directory_from_account_root,
        test_startup_notice_explains_safety_rules,
    ]

    failed = []
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")

    if failed:
        raise SystemExit(1)

    print("All verification tests passed.")


if __name__ == "__main__":
    run_tests()
