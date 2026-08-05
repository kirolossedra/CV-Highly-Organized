# Detects one or two physical nRF boards, handles duplicate J-Link CDC interfaces,
# creates timestamped run folders, logs RSSI/beacon lines, and plots RSSI in real time.

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import serial
import serial.tools.list_ports

try:
    from PySide6.QtCore import Qt, QThread, Signal, QTimer
    from PySide6.QtGui import QColor, QFont, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print("Missing GUI dependency: PySide6")
    print("Install it with:")
    print("  python3 -m pip install PySide6 pyqtgraph pyserial")
    sys.exit(1)

try:
    import pyqtgraph as pg
except ImportError:
    print("Missing plotting dependency: pyqtgraph")
    print("Install it with:")
    print("  python3 -m pip install PySide6 pyqtgraph pyserial")
    sys.exit(1)


BAUDRATE = 115200
TIMEOUT_SECONDS = 1
MAX_BOARDS = 2
PROBE_SECONDS_PER_PORT = 2.0
MAX_POINTS_PER_BOARD = 5000
CUTOFF_MIN_GAP_DB = 10

RSSI_PATTERN = re.compile(r"\brssi\s*=\s*(-?\d+)", re.IGNORECASE)


@dataclass
class SelectedBoard:
    board_number: int
    board_key: str
    port: str
    serial_number: str
    location: str
    description: str


@dataclass
class CutoffEvent:
    event_number: int
    pc_timestamp: str
    elapsed_seconds: float
    previous_leader: str
    new_leader: str
    board_1_rssi: int
    board_2_rssi: int
    gap_before_cutoff_db: float
    rssi_gap_at_cutoff_db: float


def timestamp_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def timestamp_for_folder() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def safe_filename(text: str) -> str:
    safe = text.replace("/", "_").replace("\\", "_").replace(":", "_")
    safe = safe.replace(" ", "_")
    return safe


def create_run_folder() -> Path:
    runs_root = Path.cwd() / "runs"
    runs_root.mkdir(exist_ok=True)

    base_name = timestamp_for_folder()
    run_folder = runs_root / base_name

    counter = 1
    while run_folder.exists():
        run_folder = runs_root / f"{base_name}_{counter}"
        counter += 1

    run_folder.mkdir(exist_ok=False)
    return run_folder


def get_available_ports():
    return list(serial.tools.list_ports.comports())


def port_text(port_info) -> str:
    return " ".join(
        [
            str(port_info.device or ""),
            str(port_info.name or ""),
            str(port_info.description or ""),
            str(port_info.manufacturer or ""),
            str(port_info.product or ""),
            str(port_info.serial_number or ""),
            str(port_info.location or ""),
            str(port_info.interface or ""),
            str(port_info.hwid or ""),
        ]
    ).lower()


def is_likely_nrf_or_jlink_port(port_info) -> bool:
    text = port_text(port_info)

    likely_keywords = [
        "j-link",
        "jlink",
        "segger",
        "nrf",
        "nordic",
        "cmsis-dap",
        "ttyacm",
        "usb serial",
    ]

    return any(keyword in text for keyword in likely_keywords)


def physical_board_key(port_info) -> str:
    if port_info.serial_number:
        return f"serial:{port_info.serial_number}"

    if port_info.location:
        location_base = str(port_info.location).split(":")[0]
        return f"location:{location_base}"

    if port_info.hwid:
        hwid = str(port_info.hwid)

        if "SER=" in hwid:
            serial_part = hwid.split("SER=", 1)[1].split(" ", 1)[0]
            return f"serial:{serial_part}"

        if "LOCATION=" in hwid:
            location_part = hwid.split("LOCATION=", 1)[1].split(" ", 1)[0]
            location_base = location_part.split(":")[0]
            return f"location:{location_base}"

    return f"device:{port_info.device}"


def print_port_details(index, port) -> None:
    print(f"  [{index}] {port.device}")
    print(f"      Name        : {port.name}")
    print(f"      Description : {port.description}")
    print(f"      Manufacturer: {port.manufacturer}")
    print(f"      Product     : {port.product}")
    print(f"      Serial No.  : {port.serial_number}")
    print(f"      Location    : {port.location}")
    print(f"      Interface   : {port.interface}")
    print(f"      HWID        : {port.hwid}")


def print_available_ports(ports) -> None:
    if not ports:
        print("No serial ports found.")
        return

    print("Available serial ports:")

    for index, port in enumerate(ports, start=1):
        print_port_details(index, port)


def group_ports_by_physical_board(ports) -> Dict[str, List]:
    grouped: Dict[str, List] = {}

    for port in ports:
        key = physical_board_key(port)

        if key not in grouped:
            grouped[key] = []

        grouped[key].append(port)

    return grouped


def probe_port_for_serial_output(port_device: str) -> Dict:
    readable_lines = []

    try:
        with serial.Serial(
            port=port_device,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
        ) as ser:
            start_time = time.time()

            while time.time() - start_time < PROBE_SECONDS_PER_PORT:
                data = ser.readline()

                if data:
                    text = data.decode("utf-8", errors="replace").strip()

                    if text:
                        readable_lines.append(text)

                time.sleep(0.01)

    except serial.SerialException as e:
        return {
            "port": port_device,
            "opened": False,
            "has_output": False,
            "sample": [],
            "error": str(e),
        }

    return {
        "port": port_device,
        "opened": True,
        "has_output": len(readable_lines) > 0,
        "sample": readable_lines[:3],
        "error": None,
    }


def choose_best_port_from_board(board_number: int, board_ports: List) -> str:
    print()
    print(f"Physical board {board_number} exposes these serial interfaces:")

    for index, port in enumerate(board_ports, start=1):
        print(f"  [{index}] {port.device} - {port.description}")
        print(f"      Interface : {port.interface}")
        print(f"      HWID      : {port.hwid}")

    print()
    print(f"Probing physical board {board_number} ports for actual UART output...")

    probe_results = []

    for port in board_ports:
        result = probe_port_for_serial_output(port.device)
        probe_results.append(result)

        if result["opened"] and result["has_output"]:
            print(f"  {port.device}: output detected")

            for sample_line in result["sample"]:
                print(f"      sample: {sample_line}")

        elif result["opened"]:
            print(f"  {port.device}: opened, but no output during probe window")
        else:
            print(f"  {port.device}: failed to open: {result['error']}")

    ports_with_output = [result for result in probe_results if result["opened"] and result["has_output"]]

    if len(ports_with_output) == 1:
        selected_port = ports_with_output[0]["port"]
        print(f"Selected {selected_port} for physical board {board_number} because it produced UART output.")
        return selected_port

    if len(ports_with_output) > 1:
        print()
        print(f"More than one interface produced output for physical board {board_number}.")
        print("Select the real UART/logging interface manually.")
    else:
        print()
        print(f"No interface produced output for physical board {board_number} during probing.")
        print("This can happen if the firmware is silent right now.")
        print("Select the UART/logging interface manually.")

    print()
    print("Available interfaces for this physical board:")

    for index, port in enumerate(board_ports, start=1):
        print(f"  [{index}] {port.device} - {port.description}")

    selection = input("Select interface number: ").strip()

    if not selection.isdigit():
        print("Invalid selection.")
        sys.exit(1)

    selected_index = int(selection)

    if selected_index < 1 or selected_index > len(board_ports):
        print("Invalid selection.")
        sys.exit(1)

    return board_ports[selected_index - 1].device


def choose_physical_boards(grouped_boards: Dict[str, List]) -> List[Tuple[str, List]]:
    board_items = list(grouped_boards.items())

    print()
    print("Detected physical board groups:")

    for board_index, (board_key, board_ports) in enumerate(board_items, start=1):
        print(f"  Physical board group [{board_index}] {board_key}")

        for port in board_ports:
            print(f"      {port.device} - {port.description} - interface: {port.interface}")

    print()

    if len(board_items) == 1:
        print("Only one physical board group detected. Using it.")
        return board_items

    if len(board_items) == 2:
        print("Exactly two physical board groups detected. Using both.")
        return board_items

    print(f"More than {MAX_BOARDS} physical board groups detected.")
    print("Select one or two physical board groups.")
    print("Example for one board: 1")
    print("Example for two boards: 1,2")
    print()

    selection = input("Select physical board group number(s): ").strip()

    if not selection:
        print("No board selected.")
        sys.exit(1)

    try:
        selected_indexes = [int(item.strip()) for item in selection.split(",")]
    except ValueError:
        print("Invalid selection. Use numbers only, for example: 1 or 1,2")
        sys.exit(1)

    if len(selected_indexes) > MAX_BOARDS:
        print(f"Select at most {MAX_BOARDS} physical boards.")
        sys.exit(1)

    selected_board_items = []

    for selected_index in selected_indexes:
        if selected_index < 1 or selected_index > len(board_items):
            print(f"Invalid board group number: {selected_index}")
            sys.exit(1)

        selected_board_items.append(board_items[selected_index - 1])

    return selected_board_items


def choose_boards() -> List[SelectedBoard]:
    all_ports = get_available_ports()

    print("Detected serial ports:")
    print_available_ports(all_ports)
    print()

    if not all_ports:
        print("No serial ports detected.")
        sys.exit(1)

    candidate_ports = [port for port in all_ports if is_likely_nrf_or_jlink_port(port)]

    if not candidate_ports:
        print("No obvious nRF/J-Link ports found.")
        print("Falling back to all detected serial ports.")
        candidate_ports = all_ports

    grouped_boards = group_ports_by_physical_board(candidate_ports)
    selected_board_items = choose_physical_boards(grouped_boards)

    selected_boards: List[SelectedBoard] = []

    for board_number, (board_key, board_ports) in enumerate(selected_board_items, start=1):
        selected_port = choose_best_port_from_board(board_number, board_ports)
        selected_port_info = next((port for port in board_ports if port.device == selected_port), board_ports[0])

        selected_boards.append(
            SelectedBoard(
                board_number=board_number,
                board_key=board_key,
                port=selected_port,
                serial_number=str(selected_port_info.serial_number or ""),
                location=str(selected_port_info.location or ""),
                description=str(selected_port_info.description or ""),
            )
        )

    return selected_boards


def create_board_log_file(run_folder: Path, board: SelectedBoard) -> Path:
    safe_port_name = safe_filename(board.port)
    safe_serial = safe_filename(board.serial_number if board.serial_number else "no_serial")
    log_filename = f"board_{board.board_number}_{safe_serial}_{safe_port_name}_rssi_beacons.txt"
    return run_folder / log_filename


def parse_rssi(line: str) -> Optional[int]:
    match = RSSI_PATTERN.search(line)

    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None


class SerialReaderThread(QThread):
    line_received = Signal(int, str, float, str, int)
    status_message = Signal(str)
    serial_error = Signal(int, str)

    def __init__(self, board: SelectedBoard, log_path: Path, run_start_time: float):
        super().__init__()
        self.board = board
        self.log_path = log_path
        self.run_start_time = run_start_time
        self.keep_running = True
        self.serial_connection = None
        self.log_file = None

    def stop(self) -> None:
        self.keep_running = False

    def run(self) -> None:
        try:
            self.serial_connection = serial.Serial(
                port=self.board.port,
                baudrate=BAUDRATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=TIMEOUT_SECONDS,
            )

            self.log_file = open(self.log_path, "w", encoding="utf-8")

            start_line = (
                f"[{timestamp_now()}] Board {self.board.board_number} connected to "
                f"{self.serial_connection.port} at {BAUDRATE} baud. Waiting for RSSI/beacon data..."
            )

            self.status_message.emit(start_line)
            self.log_file.write(start_line + "\n")
            self.log_file.write(f"[{timestamp_now()}] Board key: {self.board.board_key}\n")
            self.log_file.write(f"[{timestamp_now()}] Serial number: {self.board.serial_number}\n")
            self.log_file.write(f"[{timestamp_now()}] Location: {self.board.location}\n")
            self.log_file.write(f"[{timestamp_now()}] Port: {self.board.port}\n")
            self.log_file.write(f"[{timestamp_now()}] Format: [PC timestamp] [Board number] [Port] firmware log line\n\n")
            self.log_file.flush()

            while self.keep_running:
                try:
                    data = self.serial_connection.readline()

                    if data:
                        text = data.decode("utf-8", errors="replace").rstrip()
                        pc_timestamp = timestamp_now()
                        elapsed_seconds = time.time() - self.run_start_time
                        rssi = parse_rssi(text)

                        output_line = (
                            f"[{pc_timestamp}] [Board {self.board.board_number}] "
                            f"[{self.serial_connection.port}] {text}"
                        )

                        print(output_line)

                        self.log_file.write(output_line + "\n")
                        self.log_file.flush()

                        if rssi is not None:
                            self.line_received.emit(
                                self.board.board_number,
                                pc_timestamp,
                                elapsed_seconds,
                                text,
                                rssi,
                            )

                    time.sleep(0.005)

                except serial.SerialException as e:
                    error_line = (
                        f"[{timestamp_now()}] [Board {self.board.board_number}] "
                        f"[{self.board.port}] Serial error: {e}"
                    )
                    self.serial_error.emit(self.board.board_number, error_line)

                    try:
                        self.log_file.write(error_line + "\n")
                        self.log_file.flush()
                    except Exception:
                        pass

                    break

        except serial.SerialException as e:
            self.serial_error.emit(self.board.board_number, f"Failed to open {self.board.port}: {e}")

        finally:
            stop_line = f"[{timestamp_now()}] Board {self.board.board_number} serial listener stopped."
            self.status_message.emit(stop_line)

            try:
                if self.log_file is not None:
                    self.log_file.write("\n" + stop_line + "\n")
                    self.log_file.flush()
                    self.log_file.close()
            except Exception:
                pass

            try:
                if self.serial_connection is not None:
                    self.serial_connection.close()
            except Exception:
                pass


class AntennaWidget(QWidget):
    def __init__(self, board_number: int, color: QColor):
        super().__init__()
        self.board_number = board_number
        self.color = color
        self.rssi: Optional[int] = None
        self.setMinimumSize(150, 130)

    def set_rssi(self, rssi: Optional[int]) -> None:
        self.rssi = rssi
        self.update()

    def signal_level(self) -> int:
        if self.rssi is None:
            return 0

        if self.rssi >= -45:
            return 4
        if self.rssi >= -60:
            return 3
        if self.rssi >= -75:
            return 2
        if self.rssi >= -90:
            return 1

        return 0

    def paintEvent(self, event) -> None:
        del event

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = self.width()
        height = self.height()
        center_x = width // 2
        base_y = height - 28

        muted = QColor("#6b7280")
        active = self.color

        painter.setPen(QPen(active, 4))
        painter.drawLine(center_x, base_y, center_x, 48)
        painter.drawLine(center_x - 22, base_y, center_x + 22, base_y)
        painter.drawLine(center_x, 48, center_x - 14, 26)
        painter.drawLine(center_x, 48, center_x + 14, 26)

        level = self.signal_level()

        for arc_index in range(1, 5):
            pen_color = active if arc_index <= level else muted
            pen_color.setAlpha(230 if arc_index <= level else 80)
            painter.setPen(QPen(pen_color, 3))

            rect_width = 38 + arc_index * 24
            rect_height = 38 + arc_index * 24
            x = center_x - rect_width // 2
            y = 18 - arc_index * 2

            painter.drawArc(x, y, rect_width, rect_height, 35 * 16, 110 * 16)

        painter.setPen(QPen(QColor("#e5e7eb"), 1))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(0, height - 8, width, 20, Qt.AlignCenter, f"Board {self.board_number}")


class BoardCard(QFrame):
    def __init__(self, board: SelectedBoard, color: QColor):
        super().__init__()
        self.board = board
        self.color = color

        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            """
            QFrame {
                background-color: #111827;
                border: 1px solid #374151;
                border-radius: 16px;
            }
            QLabel {
                color: #f9fafb;
                background: transparent;
                border: none;
            }
            """
        )

        self.title_label = QLabel(f"Board {board.board_number}")
        self.title_label.setFont(QFont("Arial", 14, QFont.Bold))

        self.port_label = QLabel(f"Port: {board.port}")
        self.serial_label = QLabel(f"Serial: {board.serial_number if board.serial_number else 'unknown'}")
        self.rssi_label = QLabel("RSSI: waiting")
        self.rssi_label.setFont(QFont("Arial", 18, QFont.Bold))

        self.antenna = AntennaWidget(board.board_number, color)

        layout = QVBoxLayout()
        layout.addWidget(self.title_label)
        layout.addWidget(self.antenna)
        layout.addWidget(self.rssi_label)
        layout.addWidget(self.port_label)
        layout.addWidget(self.serial_label)
        self.setLayout(layout)

    def update_rssi(self, rssi: int) -> None:
        self.rssi_label.setText(f"RSSI: {rssi} dBm")
        self.antenna.set_rssi(rssi)


class RssiRealtimeWindow(QMainWindow):
    def __init__(self, boards: List[SelectedBoard], run_folder: Path):
        super().__init__()

        self.boards = boards
        self.run_folder = run_folder
        self.run_start_time = time.time()

        self.board_colors = {
            1: QColor("#ef4444"),
            2: QColor("#3b82f6"),
        }

        self.board_data: Dict[int, Dict[str, List[float]]] = {
            board.board_number: {"time": [], "rssi": []}
            for board in boards
        }

        self.latest_rssi: Dict[int, Optional[int]] = {
            board.board_number: None
            for board in boards
        }

        self.current_leader: Optional[int] = None
        self.current_leader_max_gap_db: float = 0.0
        self.cutoff_events: List[CutoffEvent] = []
        self.reader_threads: List[SerialReaderThread] = []

        self.cutoff_log_path = self.run_folder / "cutoff_events.txt"
        self.summary_path = self.run_folder / "run_summary.txt"

        self.setWindowTitle("nRF RSSI Real-Time Run Monitor")
        self.resize(1300, 850)

        self._configure_pyqtgraph()
        self._build_ui()
        self._write_run_summary_start()
        self._start_serial_threads()

        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.refresh_plot)
        self.plot_timer.start(100)

    def _configure_pyqtgraph(self) -> None:
        pg.setConfigOption("background", "#030712")
        pg.setConfigOption("foreground", "#e5e7eb")
        pg.setConfigOptions(antialias=True)

    def _build_ui(self) -> None:
        root = QWidget()
        main_layout = QVBoxLayout()

        header = QHBoxLayout()

        title_box = QVBoxLayout()
        title = QLabel("nRF RSSI Real-Time Run Monitor")
        title.setFont(QFont("Arial", 22, QFont.Bold))
        title.setStyleSheet("color: #f9fafb;")

        subtitle = QLabel(f"Run folder: {self.run_folder}")
        subtitle.setStyleSheet("color: #9ca3af;")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        self.stop_button = QPushButton("Stop Run")
        self.stop_button.setMinimumHeight(42)
        self.stop_button.setStyleSheet(
            """
            QPushButton {
                background-color: #991b1b;
                color: white;
                border-radius: 12px;
                padding: 10px 18px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #dc2626;
            }
            """
        )
        self.stop_button.clicked.connect(self.stop_run)

        header.addLayout(title_box)
        header.addStretch()
        header.addWidget(self.stop_button)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setLabel("left", "RSSI", units="dBm")
        self.plot_widget.setLabel("bottom", "Elapsed Time", units="s")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.addLegend(offset=(20, 20))
        self.plot_widget.setYRange(-100, -10)
        self.plot_widget.setMinimumHeight(430)

        self.curves = {}

        for board in self.boards:
            color = self.board_colors.get(board.board_number, QColor("#22c55e"))
            pen = pg.mkPen(color=color.name(), width=3)
            self.curves[board.board_number] = self.plot_widget.plot(
                [],
                [],
                pen=pen,
                name=f"Board {board.board_number} RSSI",
            )

        self.board_cards: Dict[int, BoardCard] = {}
        cards_layout = QHBoxLayout()

        for board in self.boards:
            color = self.board_colors.get(board.board_number, QColor("#22c55e"))
            card = BoardCard(board, color)
            self.board_cards[board.board_number] = card
            cards_layout.addWidget(card)

        if len(self.boards) == 1:
            note = QLabel("Cutoff detection needs two boards. Current run is single-board RSSI monitoring.")
        else:
            note = QLabel(
                f"Cutoff detection active: recording leader flips only after the previous leader held "
                f"at least a {CUTOFF_MIN_GAP_DB} dB RSSI gap."
            )

        note.setStyleSheet(
            """
            QLabel {
                color: #facc15;
                background-color: #1f2937;
                border: 1px solid #374151;
                border-radius: 12px;
                padding: 10px;
            }
            """
        )

        self.status_label = QLabel("Starting serial readers...")
        self.status_label.setStyleSheet(
            """
            QLabel {
                color: #d1d5db;
                background-color: #111827;
                border: 1px solid #374151;
                border-radius: 12px;
                padding: 10px;
            }
            """
        )

        cutoff_title = QLabel("Cutoff / Leader Flip Events")
        cutoff_title.setFont(QFont("Arial", 14, QFont.Bold))
        cutoff_title.setStyleSheet("color: #f9fafb;")

        self.cutoff_table = QTableWidget(0, 9)
        self.cutoff_table.setHorizontalHeaderLabels(
            [
                "#",
                "PC timestamp",
                "Elapsed s",
                "Previous leader",
                "New leader",
                "Board 1 RSSI",
                "Board 2 RSSI",
                "Prior gap dB",
                "Gap now dB",
            ]
        )
        self.cutoff_table.setMinimumHeight(180)
        self.cutoff_table.setStyleSheet(
            """
            QTableWidget {
                background-color: #111827;
                color: #f9fafb;
                gridline-color: #374151;
                border: 1px solid #374151;
                border-radius: 12px;
            }
            QHeaderView::section {
                background-color: #1f2937;
                color: #f9fafb;
                padding: 6px;
                border: 1px solid #374151;
                font-weight: bold;
            }
            """
        )

        main_layout.addLayout(header)
        main_layout.addWidget(self.plot_widget)
        main_layout.addLayout(cards_layout)
        main_layout.addWidget(note)
        main_layout.addWidget(self.status_label)
        main_layout.addWidget(cutoff_title)
        main_layout.addWidget(self.cutoff_table)

        root.setLayout(main_layout)
        root.setStyleSheet("background-color: #030712;")
        self.setCentralWidget(root)

    def _write_run_summary_start(self) -> None:
        with open(self.summary_path, "w", encoding="utf-8") as summary_file:
            summary_file.write(f"Run started: {timestamp_now()}\n")
            summary_file.write(f"Run folder: {self.run_folder}\n")
            summary_file.write(f"Baudrate: {BAUDRATE}\n")
            summary_file.write(f"Detected selected boards: {len(self.boards)}\n\n")

            for board in self.boards:
                summary_file.write(f"Board {board.board_number}\n")
                summary_file.write(f"  Board key: {board.board_key}\n")
                summary_file.write(f"  Port: {board.port}\n")
                summary_file.write(f"  Serial number: {board.serial_number}\n")
                summary_file.write(f"  Location: {board.location}\n")
                summary_file.write(f"  Description: {board.description}\n\n")

        with open(self.cutoff_log_path, "w", encoding="utf-8") as cutoff_file:
            cutoff_file.write("Cutoff / leader flip events\n")
            cutoff_file.write(f"Run started: {timestamp_now()}\n")
            cutoff_file.write(
                f"Definition: a cutoff event is recorded only when the board with stronger RSSI changes "
                f"after the previous leader previously held at least a {CUTOFF_MIN_GAP_DB} dB RSSI gap.\n"
            )
            cutoff_file.write(
                "Format: event_number, pc_timestamp, elapsed_seconds, previous_leader, new_leader, "
                "board_1_rssi, board_2_rssi, gap_before_cutoff_db, rssi_gap_at_cutoff_db\n\n"
            )

            if len(self.boards) < 2:
                cutoff_file.write("Single-board run: cutoff detection not active.\n")

    def _start_serial_threads(self) -> None:
        for board in self.boards:
            log_path = create_board_log_file(self.run_folder, board)
            thread = SerialReaderThread(board, log_path, self.run_start_time)
            thread.line_received.connect(self.handle_rssi_line)
            thread.status_message.connect(self.handle_status_message)
            thread.serial_error.connect(self.handle_serial_error)
            self.reader_threads.append(thread)
            thread.start()

    def handle_status_message(self, message: str) -> None:
        self.status_label.setText(message)
        print(message)

    def handle_serial_error(self, board_number: int, message: str) -> None:
        self.status_label.setText(message)
        print(message)

        QMessageBox.warning(
            self,
            f"Board {board_number} serial error",
            message,
        )

    def handle_rssi_line(
        self,
        board_number: int,
        pc_timestamp: str,
        elapsed_seconds: float,
        text: str,
        rssi: int,
    ) -> None:
        del text

        data = self.board_data[board_number]
        data["time"].append(elapsed_seconds)
        data["rssi"].append(rssi)

        if len(data["time"]) > MAX_POINTS_PER_BOARD:
            data["time"] = data["time"][-MAX_POINTS_PER_BOARD:]
            data["rssi"] = data["rssi"][-MAX_POINTS_PER_BOARD:]

        self.latest_rssi[board_number] = rssi

        if board_number in self.board_cards:
            self.board_cards[board_number].update_rssi(rssi)

        self.evaluate_cutoff(pc_timestamp, elapsed_seconds)

    def evaluate_cutoff(self, pc_timestamp: str, elapsed_seconds: float) -> None:
        if len(self.boards) < 2:
            return

        board_1_rssi = self.latest_rssi.get(1)
        board_2_rssi = self.latest_rssi.get(2)

        if board_1_rssi is None or board_2_rssi is None:
            return

        if board_1_rssi == board_2_rssi:
            return

        rssi_gap_db = abs(board_1_rssi - board_2_rssi)
        new_leader = 1 if board_1_rssi > board_2_rssi else 2

        if self.current_leader is None:
            self.current_leader = new_leader
            self.current_leader_max_gap_db = rssi_gap_db
            self.status_label.setText(
                f"Initial stronger RSSI: Board {new_leader} "
                f"(B1={board_1_rssi} dBm, B2={board_2_rssi} dBm, gap={rssi_gap_db:.1f} dB)"
            )
            return

        if new_leader == self.current_leader:
            self.current_leader_max_gap_db = max(self.current_leader_max_gap_db, rssi_gap_db)
            return

        gap_before_cutoff_db = self.current_leader_max_gap_db
        previous_leader = self.current_leader

        self.current_leader = new_leader
        self.current_leader_max_gap_db = rssi_gap_db

        if gap_before_cutoff_db < CUTOFF_MIN_GAP_DB:
            self.status_label.setText(
                f"Ignored weak RSSI crossing: Board {previous_leader} -> Board {new_leader} "
                f"at {elapsed_seconds:.3f}s because prior gap was only {gap_before_cutoff_db:.1f} dB "
                f"(< {CUTOFF_MIN_GAP_DB} dB)."
            )
            return

        event = CutoffEvent(
            event_number=len(self.cutoff_events) + 1,
            pc_timestamp=pc_timestamp,
            elapsed_seconds=elapsed_seconds,
            previous_leader=f"Board {previous_leader}",
            new_leader=f"Board {new_leader}",
            board_1_rssi=board_1_rssi,
            board_2_rssi=board_2_rssi,
            gap_before_cutoff_db=gap_before_cutoff_db,
            rssi_gap_at_cutoff_db=rssi_gap_db,
        )

        self.cutoff_events.append(event)

        self.append_cutoff_event(event)
        self.add_cutoff_marker(event)
        self.add_cutoff_event_to_table(event)

        self.status_label.setText(
            f"Cutoff event #{event.event_number}: {event.previous_leader} -> {event.new_leader} "
            f"at {event.elapsed_seconds:.3f}s after prior gap {event.gap_before_cutoff_db:.1f} dB, "
            f"B1={event.board_1_rssi} dBm, B2={event.board_2_rssi} dBm"
        )

    def append_cutoff_event(self, event: CutoffEvent) -> None:
        with open(self.cutoff_log_path, "a", encoding="utf-8") as cutoff_file:
            cutoff_file.write(
                f"{event.event_number}, "
                f"{event.pc_timestamp}, "
                f"{event.elapsed_seconds:.3f}, "
                f"{event.previous_leader}, "
                f"{event.new_leader}, "
                f"{event.board_1_rssi}, "
                f"{event.board_2_rssi}, "
                f"{event.gap_before_cutoff_db:.1f}, "
                f"{event.rssi_gap_at_cutoff_db:.1f}\n"
            )

    def add_cutoff_marker(self, event: CutoffEvent) -> None:
        marker = pg.InfiniteLine(
            pos=event.elapsed_seconds,
            angle=90,
            movable=False,
            pen=pg.mkPen("#facc15", width=2, style=Qt.DashLine),
        )

        label = pg.TextItem(
            text=f"Cutoff #{event.event_number}",
            color="#facc15",
            anchor=(0, 1),
        )
        label.setPos(event.elapsed_seconds, max(event.board_1_rssi, event.board_2_rssi))

        self.plot_widget.addItem(marker)
        self.plot_widget.addItem(label)

    def add_cutoff_event_to_table(self, event: CutoffEvent) -> None:
        row = self.cutoff_table.rowCount()
        self.cutoff_table.insertRow(row)

        values = [
            str(event.event_number),
            event.pc_timestamp,
            f"{event.elapsed_seconds:.3f}",
            event.previous_leader,
            event.new_leader,
            str(event.board_1_rssi),
            str(event.board_2_rssi),
            f"{event.gap_before_cutoff_db:.1f}",
            f"{event.rssi_gap_at_cutoff_db:.1f}",
        ]

        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setTextAlignment(Qt.AlignCenter)
            self.cutoff_table.setItem(row, column, item)

        self.cutoff_table.resizeColumnsToContents()

    def refresh_plot(self) -> None:
        for board in self.boards:
            board_number = board.board_number
            data = self.board_data[board_number]
            self.curves[board_number].setData(data["time"], data["rssi"])

        max_time = 0.0

        for data in self.board_data.values():
            if data["time"]:
                max_time = max(max_time, data["time"][-1])

        if max_time > 30:
            self.plot_widget.setXRange(max(0, max_time - 60), max_time + 2, padding=0)

    def stop_run(self) -> None:
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")

        for thread in self.reader_threads:
            thread.stop()

        for thread in self.reader_threads:
            thread.wait(3000)

        self.write_run_summary_stop()
        self.status_label.setText(f"Run stopped. Saved in: {self.run_folder}")
        self.stop_button.setText("Stopped")

    def write_run_summary_stop(self) -> None:
        with open(self.summary_path, "a", encoding="utf-8") as summary_file:
            summary_file.write(f"\nRun stopped: {timestamp_now()}\n")
            summary_file.write(f"Cutoff threshold: {CUTOFF_MIN_GAP_DB} dB prior leader gap\n")
            summary_file.write(f"Cutoff events recorded: {len(self.cutoff_events)}\n")

            for board in self.boards:
                data = self.board_data[board.board_number]
                summary_file.write(f"\nBoard {board.board_number} samples: {len(data['rssi'])}\n")

                if data["rssi"]:
                    summary_file.write(f"  First RSSI: {data['rssi'][0]} dBm\n")
                    summary_file.write(f"  Last RSSI: {data['rssi'][-1]} dBm\n")
                    summary_file.write(f"  Min RSSI: {min(data['rssi'])} dBm\n")
                    summary_file.write(f"  Max RSSI: {max(data['rssi'])} dBm\n")

    def closeEvent(self, event) -> None:
        self.stop_run()
        event.accept()


def main() -> None:
    run_folder = create_run_folder()

    print()
    print(f"Created run folder: {run_folder}")
    print()

    selected_boards = choose_boards()

    print()
    print("Selected UART/logging port(s):")

    for board in selected_boards:
        print(f"  Board {board.board_number}: {board.port}")

    print()

    app = QApplication(sys.argv)
    window = RssiRealtimeWindow(selected_boards, run_folder)
    window.show()

    exit_code = app.exec()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
