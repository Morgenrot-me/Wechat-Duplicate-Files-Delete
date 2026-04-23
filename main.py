"""本文件用于提供微信重复文件删除工具的核心逻辑与桌面界面入口。"""

from __future__ import annotations

from collections.abc import Callable
from collections import defaultdict
from pathlib import Path
import hashlib
import os
import stat
import sys
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

DUPLICATE_STEM_PATTERN = re.compile(r"^(?P<base>.+)\((?P<index>\d+)\)$")
HASH_CHUNK_SIZE = 1024 * 1024
MAX_WORKERS = 4
SCAN_PROGRESS_INTERVAL = 200


def build_original_file_path(candidate_path: Path) -> Path | None:
    match = DUPLICATE_STEM_PATTERN.fullmatch(candidate_path.stem)
    if match is None:
        return None

    original_name = f"{match.group('base')}{candidate_path.suffix}"
    return candidate_path.with_name(original_name)


def calculate_file_hash(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file:
        while True:
            chunk = file.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def get_common_prefix(paths: list[Path]) -> Path:
    if not paths:
        return Path()

    common = paths[0].parts
    for path in paths[1:]:
        parts = path.parts
        common = tuple(c for i, c in enumerate(common) if i < len(parts) and parts[i] == c)
        if not common:
            break

    return Path(*common) if common else Path()


def is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def files_have_same_size(original_path: Path, duplicate_path: Path) -> bool:
    if not is_regular_file(original_path) or not is_regular_file(duplicate_path):
        return False

    return original_path.stat().st_size == duplicate_path.stat().st_size


def files_have_same_content(original_path: Path, duplicate_path: Path) -> bool:
    if not is_regular_file(original_path) or not is_regular_file(duplicate_path):
        return False

    if original_path.stat().st_size != duplicate_path.stat().st_size:
        return False

    return calculate_file_hash(original_path) == calculate_file_hash(duplicate_path)


def get_duplicate_candidate_pairs(
    directory: str | os.PathLike[str] | Path,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """同文件夹扫描：只在同一目录内查找重复文件"""
    directory_path = Path(directory)
    if not directory_path.is_dir():
        return [], []

    matched_pairs: list[tuple[Path, Path]] = []
    errors: list[str] = []
    for candidate_path in sorted(directory_path.iterdir(), key=lambda item: item.name):
        try:
            if not is_regular_file(candidate_path):
                continue

            original_path = build_original_file_path(candidate_path)
            if original_path is None:
                continue

            if not is_regular_file(original_path):
                continue

            if not files_have_same_size(original_path, candidate_path):
                continue

            matched_pairs.append((original_path, candidate_path))
        except OSError as exc:
            errors.append(f"扫描失败：{candidate_path}，错误：{exc}")

    return matched_pairs, errors


def get_duplicate_candidate_pairs_cross_directory(
    root_directory: str | os.PathLike[str] | Path,
    progress_callback: Callable[[str], None] | None = None,
    progress_interval: int = SCAN_PROGRESS_INTERVAL,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """全目录树扫描：在整个目录树中查找重复文件

    优化策略：
    1. 只存储候选文件和对应的原文件，不存储所有文件
    2. 边遍历边匹配，避免二次遍历
    3. 用字典按 (base_name, suffix, size) 索引原文件，O(1) 查找

    时间复杂度：O(n)，n 为文件数，只遍历一次
    空间复杂度：O(m)，m 为候选文件数 + 唯一原文件数（通常远小于 n）
    """
    root_path = Path(root_directory)
    if not root_path.is_dir():
        return [], []

    original_files: dict[tuple[str, str, int], Path] = {}
    pending_candidates: list[tuple[Path, str, str, int]] = []
    matched_pairs: list[tuple[Path, Path]] = []
    errors: list[str] = []

    scanned_directories = 0
    scanned_files = 0
    normalized_interval = max(progress_interval, 1)

    def emit_progress(message: str, force: bool = False) -> None:
        if progress_callback is None:
            return
        if force or scanned_files % normalized_interval == 0:
            progress_callback(message)

    for current_dir, _, filenames in os.walk(root_path):
        scanned_directories += 1
        for filename in filenames:
            file_path = Path(current_dir) / filename
            scanned_files += 1
            try:
                if not is_regular_file(file_path):
                    continue

                size = file_path.stat().st_size
                stem = file_path.stem
                suffix = file_path.suffix

                match = DUPLICATE_STEM_PATTERN.match(stem)
                if match:
                    base_name = match.group("base")
                    pending_candidates.append((file_path, base_name, suffix, size))
                else:
                    key = (stem, suffix, size)
                    if key not in original_files:
                        original_files[key] = file_path
            except OSError as exc:
                errors.append(f"扫描失败：{file_path}，错误：{exc}")

            emit_progress(
                f"正在扫描目录树... 已检查 {scanned_directories} 个目录，已处理 {scanned_files} 个文件，发现 {len(pending_candidates)} 个候选文件"
            )

    emit_progress(
        f"正在匹配候选项... 共有 {len(pending_candidates)} 个候选文件待匹配",
        force=True,
    )

    for index, (candidate_path, base_name, suffix, size) in enumerate(pending_candidates, start=1):
        key = (base_name, suffix, size)
        if key in original_files:
            matched_pairs.append((original_files[key], candidate_path))

        if progress_callback is not None and (
            index % normalized_interval == 0 or index == len(pending_candidates)
        ):
            progress_callback(
                f"正在匹配候选项... 已处理 {index}/{len(pending_candidates)} 个候选文件，已匹配 {len(matched_pairs)} 组"
            )

    return matched_pairs, errors


def iter_target_directories(directory: Path, recursive: bool) -> list[Path]:
    if not recursive:
        return [directory]

    return [Path(root) for root, _dirs, _files in os.walk(directory)]


def get_wechat_accounts(directory: str | os.PathLike[str] | Path) -> list[tuple[str, Path]]:
    """获取所有可用的微信账号及其对应的文件目录

    返回: [(账号名称, msg/file路径), ...]
    """
    selected_path = Path(directory)
    if not selected_path.is_dir():
        return []

    accounts: list[tuple[str, Path]] = []

    direct_msg_file = selected_path / "msg" / "file"
    if direct_msg_file.is_dir():
        account_name = selected_path.name if selected_path.name != "Weixin Text" else "默认账号"
        return [(account_name, direct_msg_file)]

    if selected_path.name == "file" and selected_path.parent.name == "msg":
        account_name = selected_path.parent.parent.name
        return [(account_name, selected_path)]

    xwechat_root = selected_path / "xwechat_files" if selected_path.name == "Weixin Text" else selected_path
    if xwechat_root.name != "xwechat_files" or not xwechat_root.is_dir():
        return []

    for child in sorted(xwechat_root.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue

        msg_file_directory = child / "msg" / "file"
        if msg_file_directory.is_dir():
            accounts.append((child.name, msg_file_directory))

    return accounts


def resolve_wechat_scan_directories(directory: str | os.PathLike[str] | Path, selected_accounts: list[str] | None = None) -> list[Path]:
    """根据选中的账号列表解析扫描目录

    Args:
        directory: 用户选择的根目录
        selected_accounts: 选中的账号名称列表，None表示全选
    """
    accounts = get_wechat_accounts(directory)
    if not accounts:
        return []

    if selected_accounts is None:
        return [path for _, path in accounts]

    return [path for name, path in accounts if name in selected_accounts]


def scan_wechat_directories(
    selected_directory: str | os.PathLike[str] | Path,
    recursive: bool,
    cross_directory_mode: bool,
    progress_callback: Callable[[str], None] | None = None,
    progress_interval: int = SCAN_PROGRESS_INTERVAL,
) -> tuple[list[tuple[Path, Path]], list[str], list[Path]]:
    target_directories = resolve_wechat_scan_directories(selected_directory)
    if not target_directories:
        return [], ["未找到可扫描的微信文件目录。请选择 Weixin Text、xwechat_files、账号目录或 msg/file 目录。"], []

    all_pairs: list[tuple[Path, Path]] = []
    all_errors: list[str] = []
    all_scanned_directories: list[Path] = []

    if cross_directory_mode:
        for index, target_directory in enumerate(target_directories, start=1):
            def report_progress(message: str) -> None:
                if progress_callback is None:
                    return
                progress_callback(f"[{index}/{len(target_directories)}] {target_directory}\n{message}")

            pairs, errors = get_duplicate_candidate_pairs_cross_directory(
                target_directory,
                progress_callback=report_progress,
                progress_interval=progress_interval,
            )
            all_pairs.extend(pairs)
            all_errors.extend(errors)
        all_scanned_directories = target_directories
    else:
        scanned_directory_count = 0
        for target_directory in target_directories:
            for current_directory in iter_target_directories(target_directory, recursive):
                pairs, errors = get_duplicate_candidate_pairs(current_directory)
                all_pairs.extend(pairs)
                all_errors.extend(errors)
                all_scanned_directories.append(current_directory)
                scanned_directory_count += 1
                if progress_callback is not None:
                    progress_callback(
                        f"正在扫描微信文件目录... 已检查 {scanned_directory_count} 个目录，发现 {len(all_pairs)} 个候选文件\n当前目录：{current_directory}"
                    )

    return all_pairs, all_errors, all_scanned_directories


def default_send_to_trash(path: str) -> None:
    import send2trash

    send2trash.send2trash(path)


def delete_duplicate_files(
    directory: str | os.PathLike[str] | Path,
    recursive: bool = False,
    send_to_trash: Callable[[str], None] | None = None,
) -> dict[str, int | list[str]]:
    directory_path = Path(directory)
    result: dict[str, int | list[str]] = {
        "checked_directories": 0,
        "matched": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": 0,
        "messages": [],
    }

    if not directory_path.is_dir():
        return result

    target_directories = resolve_wechat_scan_directories(directory_path)
    if not target_directories:
        messages = result["messages"]
        assert isinstance(messages, list)
        messages.append("未找到可删除的微信文件目录。请选择 Weixin Text、xwechat_files、账号目录或 msg/file 目录。")
        result["errors"] = 1
        return result

    trash_handler = send_to_trash or default_send_to_trash
    messages = result["messages"]
    assert isinstance(messages, list)

    for target_directory in target_directories:
        for current_directory in iter_target_directories(target_directory, recursive):
            result["checked_directories"] += 1
            pairs, scan_errors = get_duplicate_candidate_pairs(current_directory)
            result["matched"] += len(pairs)
            result["errors"] += len(scan_errors)
            messages.extend(scan_errors)

            for original_path, duplicate_path in pairs:
                try:
                    if not files_have_same_content(original_path, duplicate_path):
                        result["skipped"] += 1
                        messages.append(f"跳过（哈希不一致）：{duplicate_path}")
                        continue

                    duplicate_path.chmod(duplicate_path.stat().st_mode | stat.S_IWRITE)
                    trash_handler(str(duplicate_path))
                    result["deleted"] += 1
                    messages.append(f"已移入回收站：{duplicate_path}")
                except Exception as exc:
                    result["errors"] += 1
                    messages.append(f"删除失败：{duplicate_path}，错误：{exc}")

    return result


def get_startup_notice_text() -> str:
    return (
        "本工具用于识别并整理微信类重复文件，帮助你在删除前先看清候选项来自哪里。\n\n"
        "工作方式：\n"
        "1. 只把扩展名前以 (数字) 结尾的普通文件视为候选，例如 a(1).jpg、a(12).txt。\n"
        "2. 必须能找到同名原文件，例如 a.jpg。\n"
        "3. 扫描阶段只做规则匹配和大小筛选，不会直接删除任何文件。\n"
        "4. 删除前会再次执行 SHA-256 校验，只有内容完全一致的文件才会被处理。\n"
        "5. 删除动作为移入回收站，不会直接永久删除。\n\n"
        "使用建议：\n"
        "- 先扫描，再查看列表中的原文件与候选文件关系。\n"
        "- 选择 Weixin Text、xwechat_files、账号目录或 msg/file 目录后，程序会自动定位真正需要扫描的 msg/file。\n"
        "- 默认勾选的是规则命中的候选项，你仍然可以逐项取消。\n"
        "- 如果你对某一项没有把握，就先不要删。"
    )


def format_result_text(
    directory: str | os.PathLike[str] | Path,
    recursive: bool,
    result: dict[str, int | list[str]],
) -> str:
    lines = [
        f"目录：{Path(directory)}",
        f"模式：{'递归删除' if recursive else '当前目录删除'}",
        f"检查目录数：{result['checked_directories']}",
        f"匹配文件数：{result['matched']}",
        f"成功删除数：{result['deleted']}",
        f"失败数：{result['errors']}",
    ]

    messages = result.get("messages", [])
    if isinstance(messages, list) and messages:
        lines.append("")
        lines.extend(messages)

    return "\n".join(lines)


def run_app() -> int:
    from PyQt5 import QtWidgets
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QListWidgetItem
    from PyQt5.QtGui import QIcon

    def resource_path(relative_path: str) -> str:
        """获取资源文件的绝对路径(支持打包后的exe)"""
        if hasattr(sys, '_MEIPASS'):
            return os.path.join(sys._MEIPASS, relative_path)
        return os.path.join(os.path.abspath("."), relative_path)

    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)

    class AccountSelectionDialog(QtWidgets.QDialog):
        def __init__(self, accounts: list[tuple[str, Path]], parent=None) -> None:
            super().__init__(parent)
            self.setWindowTitle("选择微信账号")
            self.setWindowIcon(QIcon(resource_path('logo.png')))
            self.resize(500, 400)
            self.accounts = accounts
            self.selected_accounts: list[str] = []

            layout = QtWidgets.QVBoxLayout(self)

            info_label = QtWidgets.QLabel(f"检测到 {len(accounts)} 个微信账号，请选择要扫描的账号：")
            layout.addWidget(info_label)

            self.list_widget = QtWidgets.QListWidget(self)
            for account_name, account_path in accounts:
                try:
                    file_count = sum(1 for _ in account_path.rglob("*") if _.is_file())
                    item_text = f"{account_name} ({account_path}) - 约 {file_count} 个文件"
                except Exception:
                    item_text = f"{account_name} ({account_path})"

                item = QListWidgetItem(item_text)
                item.setCheckState(Qt.CheckState.Checked)
                item.setData(Qt.ItemDataRole.UserRole, account_name)
                self.list_widget.addItem(item)

            layout.addWidget(self.list_widget)

            button_layout = QtWidgets.QHBoxLayout()
            select_all_btn = QtWidgets.QPushButton("全选")
            deselect_all_btn = QtWidgets.QPushButton("取消全选")
            select_all_btn.clicked.connect(self.select_all)
            deselect_all_btn.clicked.connect(self.deselect_all)
            button_layout.addWidget(select_all_btn)
            button_layout.addWidget(deselect_all_btn)
            layout.addLayout(button_layout)

            confirm_layout = QtWidgets.QHBoxLayout()
            ok_btn = QtWidgets.QPushButton("确定")
            cancel_btn = QtWidgets.QPushButton("取消")
            ok_btn.clicked.connect(self.accept)
            cancel_btn.clicked.connect(self.reject)
            confirm_layout.addWidget(ok_btn)
            confirm_layout.addWidget(cancel_btn)
            layout.addLayout(confirm_layout)

        def select_all(self) -> None:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item:
                    item.setCheckState(Qt.CheckState.Checked)

        def deselect_all(self) -> None:
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item:
                    item.setCheckState(Qt.CheckState.Unchecked)

        def get_selected_accounts(self) -> list[str]:
            selected = []
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item and item.checkState() == Qt.CheckState.Checked:
                    selected.append(item.data(Qt.ItemDataRole.UserRole))
            return selected

    class DeleteToolWidget(QtWidgets.QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("文件删除工具")
            self.setWindowIcon(QIcon(resource_path('logo.png')))
            self.resize(700, 550)
            self.candidate_pairs: list[tuple[Path, Path]] = []
            self.scan_errors: list[str] = []
            self.current_directory = ""
            self.current_recursive = False
            self.cross_directory_mode = False
            self.selected_accounts: list[str] = []

            layout = QtWidgets.QVBoxLayout(self)

            info_label = QtWidgets.QLabel(
                "只有尾部编号且与原文件哈希一致的普通文件才会进入回收站。\n"
                "点击扫描按钮后，勾选要删除的文件，再点击删除按钮。"
            )
            layout.addWidget(info_label)

            mode_layout = QtWidgets.QHBoxLayout()
            mode_label = QtWidgets.QLabel("扫描模式：")
            self.mode_same_folder = QtWidgets.QRadioButton("同文件夹（快速）")
            self.mode_cross_directory = QtWidgets.QRadioButton("全目录树（跨文件夹）")
            self.mode_same_folder.setChecked(True)
            mode_layout.addWidget(mode_label)
            mode_layout.addWidget(self.mode_same_folder)
            mode_layout.addWidget(self.mode_cross_directory)
            mode_layout.addStretch()
            layout.addLayout(mode_layout)

            button_layout = QtWidgets.QHBoxLayout()
            self.scan_recursive_button = QtWidgets.QPushButton("扫描（递归）", self)
            self.scan_normal_button = QtWidgets.QPushButton("扫描（当前目录）", self)
            self.delete_button = QtWidgets.QPushButton("删除选中项", self)
            self.delete_button.setEnabled(False)
            button_layout.addWidget(self.scan_recursive_button)
            button_layout.addWidget(self.scan_normal_button)
            button_layout.addWidget(self.delete_button)
            layout.addLayout(button_layout)

            filter_layout = QtWidgets.QHBoxLayout()
            self.select_all_button = QtWidgets.QPushButton("全选", self)
            self.deselect_all_button = QtWidgets.QPushButton("取消全选", self)
            self.invert_selection_button = QtWidgets.QPushButton("反选", self)
            self.filter_large_button = QtWidgets.QPushButton("只选 >1MB", self)
            self.select_all_button.setEnabled(False)
            self.deselect_all_button.setEnabled(False)
            self.invert_selection_button.setEnabled(False)
            self.filter_large_button.setEnabled(False)
            filter_layout.addWidget(self.select_all_button)
            filter_layout.addWidget(self.deselect_all_button)
            filter_layout.addWidget(self.invert_selection_button)
            filter_layout.addWidget(self.filter_large_button)
            layout.addLayout(filter_layout)

            self.list_widget = QtWidgets.QTreeWidget(self)
            self.list_widget.setHeaderLabels(["文件", "大小"])
            self.list_widget.setColumnWidth(0, 500)
            layout.addWidget(self.list_widget)

            self.progress_bar = QtWidgets.QProgressBar(self)
            self.progress_bar.setVisible(False)
            layout.addWidget(self.progress_bar)

            self.result_text = QtWidgets.QTextBrowser(self)
            self.result_text.setMaximumHeight(100)
            layout.addWidget(self.result_text)

            self.scan_recursive_button.clicked.connect(lambda: self.handle_scan(True))
            self.scan_normal_button.clicked.connect(lambda: self.handle_scan(False))
            self.delete_button.clicked.connect(self.handle_delete_selected)
            self.select_all_button.clicked.connect(self.select_all)
            self.deselect_all_button.clicked.connect(self.deselect_all)
            self.invert_selection_button.clicked.connect(self.invert_selection)
            self.filter_large_button.clicked.connect(self.filter_large_files)

        def select_all(self) -> None:
            root = self.list_widget.invisibleRootItem()
            for i in range(root.childCount()):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    child.setCheckState(0, Qt.Checked)

        def deselect_all(self) -> None:
            root = self.list_widget.invisibleRootItem()
            for i in range(root.childCount()):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    child.setCheckState(0, Qt.Unchecked)

        def invert_selection(self) -> None:
            root = self.list_widget.invisibleRootItem()
            for i in range(root.childCount()):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    current = child.checkState(0)
                    child.setCheckState(0, Qt.Unchecked if current == Qt.Checked else Qt.Checked)

        def filter_large_files(self) -> None:
            root = self.list_widget.invisibleRootItem()
            for i in range(root.childCount()):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    pair = child.data(0, Qt.UserRole)
                    if pair:
                        _, duplicate_path = pair
                        try:
                            file_size = duplicate_path.stat().st_size
                            child.setCheckState(0, Qt.Checked if file_size > 1024 * 1024 else Qt.Unchecked)
                        except OSError:
                            child.setCheckState(0, Qt.Unchecked)

        def handle_scan(self, recursive: bool) -> None:
            directory = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "选择目录",
                ".",
                QtWidgets.QFileDialog.ShowDirsOnly | QtWidgets.QFileDialog.DontResolveSymlinks,
            )
            if not directory:
                QtWidgets.QMessageBox.information(self, "已取消", "未选择目录，未执行扫描。")
                return

            accounts = get_wechat_accounts(directory)
            if not accounts:
                QtWidgets.QMessageBox.warning(
                    self, "错误",
                    "未找到可扫描的微信文件目录。\n请选择 Weixin Text、xwechat_files、账号目录或 msg/file 目录。"
                )
                return

            if len(accounts) > 1:
                dialog = AccountSelectionDialog(accounts, self)
                if dialog.exec_() != QtWidgets.QDialog.Accepted:
                    QtWidgets.QMessageBox.information(self, "已取消", "未选择账号，未执行扫描。")
                    return

                self.selected_accounts = dialog.get_selected_accounts()
                if not self.selected_accounts:
                    QtWidgets.QMessageBox.warning(self, "错误", "未选择任何账号。")
                    return
            else:
                self.selected_accounts = [accounts[0][0]]

            self.current_directory = directory
            self.current_recursive = recursive
            self.cross_directory_mode = self.mode_cross_directory.isChecked()
            self.list_widget.clear()
            self.candidate_pairs = []
            self.scan_errors = []

            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(0)
            self.progress_bar.setValue(0)
            self.result_text.setPlainText("正在扫描...")

            directory_path = Path(directory)

            def update_scan_progress(message: str) -> None:
                self.result_text.setPlainText(message)
                QtWidgets.QApplication.processEvents()

            target_directories = resolve_wechat_scan_directories(directory_path, self.selected_accounts)
            if not target_directories:
                QtWidgets.QMessageBox.warning(self, "错误", "所选账号无有效文件目录。")
                self.progress_bar.setVisible(False)
                return

            all_pairs: list[tuple[Path, Path]] = []
            all_errors: list[str] = []

            if self.cross_directory_mode:
                for index, target_directory in enumerate(target_directories, start=1):
                    def report_progress(message: str) -> None:
                        update_scan_progress(f"[{index}/{len(target_directories)}] {target_directory}\n{message}")

                    pairs, errors = get_duplicate_candidate_pairs_cross_directory(
                        target_directory,
                        progress_callback=report_progress,
                        progress_interval=SCAN_PROGRESS_INTERVAL,
                    )
                    all_pairs.extend(pairs)
                    all_errors.extend(errors)
            else:
                scanned_directory_count = 0
                for target_directory in target_directories:
                    for current_directory in iter_target_directories(target_directory, recursive):
                        pairs, errors = get_duplicate_candidate_pairs(current_directory)
                        all_pairs.extend(pairs)
                        all_errors.extend(errors)
                        scanned_directory_count += 1
                        update_scan_progress(
                            f"正在扫描微信文件目录... 已检查 {scanned_directory_count} 个目录，发现 {len(all_pairs)} 个候选文件\n当前目录：{current_directory}"
                        )

            pairs = all_pairs
            errors = all_errors
            scanned_directories = target_directories
            self.candidate_pairs = pairs
            self.scan_errors = errors
            scanned_directory_count = len(scanned_directories)

            account_info = f"已选账号: {', '.join(self.selected_accounts)}\n" if len(self.selected_accounts) > 1 else ""

            self.progress_bar.setVisible(False)

            if self.scan_errors:
                self.result_text.setPlainText("\n".join(self.scan_errors))

            if not self.candidate_pairs:
                QtWidgets.QMessageBox.information(
                    self, "扫描完成",
                    f"{account_info}未发现重复文件。\n已检查微信文件目录：{scanned_directory_count} 个"
                )
                self.delete_button.setEnabled(False)
                return

            all_paths = [dup for _, dup in self.candidate_pairs]
            common_prefix = get_common_prefix(all_paths)
            total_size = 0

            # 按原文件分组
            grouped: dict[Path, list[Path]] = defaultdict(list)
            for original_path, duplicate_path in self.candidate_pairs:
                grouped[original_path].append(duplicate_path)

            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(len(grouped))
            self.result_text.setPlainText("正在加载列表...")

            for idx, (original_path, duplicates) in enumerate(grouped.items()):
                try:
                    relative_orig = original_path.relative_to(common_prefix) if common_prefix != Path() else original_path

                    # 创建父节点（原文件）
                    parent_item = QtWidgets.QTreeWidgetItem(self.list_widget)
                    parent_item.setText(0, f"原文件: {relative_orig}")
                    parent_item.setText(1, f"{len(duplicates)} 个重复")
                    parent_item.setFlags(parent_item.flags() & ~Qt.ItemIsUserCheckable)

                    # 添加子节点（重复文件）
                    for duplicate_path in duplicates:
                        file_size = duplicate_path.stat().st_size
                        total_size += file_size

                        relative_dup = duplicate_path.relative_to(common_prefix) if common_prefix != Path() else duplicate_path

                        child_item = QtWidgets.QTreeWidgetItem(parent_item)
                        child_item.setText(0, str(relative_dup))
                        child_item.setText(1, format_size(file_size))
                        child_item.setCheckState(0, Qt.Checked)
                        child_item.setData(0, Qt.UserRole, (original_path, duplicate_path))

                    parent_item.setExpanded(True)

                except OSError:
                    parent_item = QtWidgets.QTreeWidgetItem(self.list_widget)
                    parent_item.setText(0, f"原文件: {original_path}")
                    parent_item.setText(1, "读取失败")

                if idx % 10 == 0:
                    self.progress_bar.setValue(idx)
                    QtWidgets.QApplication.processEvents()

            self.progress_bar.setVisible(False)
            self.result_text.clear()

            self.delete_button.setEnabled(True)
            self.select_all_button.setEnabled(True)
            self.deselect_all_button.setEnabled(True)
            self.invert_selection_button.setEnabled(True)
            self.filter_large_button.setEnabled(True)

            prefix_info = f"公共路径: {common_prefix}\n" if common_prefix != Path() else ""
            QtWidgets.QMessageBox.information(
                self,
                "扫描完成",
                f"{account_info}"
                f"已检查微信文件目录：{scanned_directory_count} 个\n"
                f"发现 {len(self.candidate_pairs)} 个重复文件。\n"
                f"预计可节省空间: {format_size(total_size)}\n"
                f"请勾选要删除的项，然后点击删除按钮。",
            )

        def handle_delete_selected(self) -> None:
            selected_pairs: list[tuple[Path, Path]] = []
            root = self.list_widget.invisibleRootItem()
            for i in range(root.childCount()):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    if child.checkState(0) == Qt.Checked:
                        pair = child.data(0, Qt.UserRole)
                        if pair:
                            selected_pairs.append(pair)

            if not selected_pairs:
                QtWidgets.QMessageBox.information(self, "未选择", "未勾选任何文件，未执行删除。")
                return

            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(len(selected_pairs))
            self.progress_bar.setValue(0)

            deleted = 0
            errors = 0
            skipped = 0
            saved_size = 0
            messages: list[str] = []
            completed = 0

            def process_file(original_path: Path, duplicate_path: Path) -> tuple[str, int, str]:
                try:
                    if not files_have_same_content(original_path, duplicate_path):
                        return ("skipped", 0, f"跳过（哈希不一致）：{duplicate_path}")

                    file_size = duplicate_path.stat().st_size
                    duplicate_path.chmod(duplicate_path.stat().st_mode | stat.S_IWRITE)
                    default_send_to_trash(str(duplicate_path))
                    return ("deleted", file_size, f"已移入回收站：{duplicate_path}")
                except Exception as exc:
                    return ("error", 0, f"删除失败：{duplicate_path}，错误：{exc}")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(process_file, orig, dup): (orig, dup)
                          for orig, dup in selected_pairs}

                for future in as_completed(futures):
                    status, size, message = future.result()
                    messages.append(message)
                    if status == "deleted":
                        deleted += 1
                        saved_size += size
                    elif status == "skipped":
                        skipped += 1
                    elif status == "error":
                        errors += 1

                    completed += 1
                    self.progress_bar.setValue(completed)
                    QtWidgets.QApplication.processEvents()

            self.progress_bar.setVisible(False)

            result_text = (
                f"目录：{self.current_directory}\n"
                f"模式：{'递归删除' if self.current_recursive else '当前目录删除'}\n"
                f"选中文件数：{len(selected_pairs)}\n"
                f"成功删除数：{deleted}\n"
                f"节省空间：{format_size(saved_size)}\n"
                f"跳过数：{skipped}\n"
                f"失败数：{errors}\n\n"
                + "\n".join(messages)
            )
            self.result_text.setPlainText(result_text)
            QtWidgets.QMessageBox.information(
                self, "删除完成",
                f"成功删除 {deleted} 个，节省 {format_size(saved_size)}\n跳过 {skipped} 个，失败 {errors} 个。"
            )

            self.list_widget.clear()
            self.candidate_pairs = []
            self.delete_button.setEnabled(False)
            self.select_all_button.setEnabled(False)
            self.deselect_all_button.setEnabled(False)
            self.invert_selection_button.setEnabled(False)
            self.filter_large_button.setEnabled(False)

    app = QtWidgets.QApplication(sys.argv)
    window = DeleteToolWidget()
    QtWidgets.QMessageBox.information(window, "使用说明", get_startup_notice_text())
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(run_app())
