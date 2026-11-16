from __future__ import annotations

import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .bo_controller import BOController
from .config_manager import ConfigManager
from .counter_manager import apply_custom_ips, generate_counters
from .logger import create_logger


class App:
    def __init__(self, root: tk.Tk, base_dir: Path):
        self.root = root
        self.base_dir = base_dir

        self.config_manager = ConfigManager(
            base_dir / "config" / "config.json"
        )

        self.config = self.config_manager.load()
        self.logger = create_logger(base_dir / "logs")

        ui = self.config["ui"]

        self.root.title(
            "POS File Distributor — PC → BO → Counters"
        )

        self.root.geometry(
            f'{ui.get("window_width", 1280)}x'
            f'{ui.get("window_height", 850)}'
        )

        self.root.minsize(1100, 720)

        self.busy = False
        self.cancel_event = threading.Event()
        self.counters = []

        # -------------------------------------------------
        # BO CONNECTION
        # -------------------------------------------------

        self.bo_ip = tk.StringVar()
        self.bo_user = tk.StringVar()
        self.bo_password = tk.StringVar()
        self.bo_port = tk.StringVar(value="22")

        self.bo_connect_timeout = tk.StringVar(
            value="5"
        )

        self.bo_operation_timeout = tk.StringVar(
            value="60"
        )

        # -------------------------------------------------
        # COUNTER CONNECTION
        # -------------------------------------------------

        self.counter_user = tk.StringVar()
        self.counter_password = tk.StringVar()
        self.counter_port = tk.StringVar(value="22")

        self.counter_connect_timeout = tk.StringVar(
            value="5"
        )

        self.counter_operation_timeout = tk.StringVar(
            value="60"
        )

        # -------------------------------------------------
        # COUNTER SELECTION
        # -------------------------------------------------

        # Number of counters is still user-configurable.
        #
        # Counter IP generation ALWAYS starts at .112.
        #
        # Example:
        # BO = 10.25.36.12
        # Count = 4
        #
        # 1 -> 10.25.36.112
        # 2 -> 10.25.36.113
        # 3 -> 10.25.36.114
        # 4 -> 10.25.36.115

        self.counter_count = tk.StringVar(
            value="10"
        )

        # -------------------------------------------------
        # FILE SETTINGS
        # -------------------------------------------------

        self.transfer_mode = tk.StringVar(value="File")
        self.local_file = tk.StringVar()
        self.remote_path = tk.StringVar()
        self.local_path_label = tk.StringVar(value="Local replacement file")
        self.target_path_label = tk.StringVar(value="Target file path on counters")

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        self.status = tk.StringVar(
            value="Ready"
        )

        self.progress = tk.DoubleVar(
            value=0
        )

        self._build()
        self._load()

    # =====================================================
    # BUILD GUI
    # =====================================================

    def _build(self):
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            container,
            orient="vertical",
            command=canvas.yview
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        outer = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window(
            (0, 0),
            window=outer,
            anchor="nw"
        )

        outer.bind(
            "<Configure>",
            lambda _event: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                window_id,
                width=event.width
            )
        )

        # Scroll with the mouse wheel while the pointer is over the form.
        canvas.bind(
            "<Enter>",
            lambda _event: canvas.bind_all(
                "<MouseWheel>",
                lambda event: canvas.yview_scroll(
                    int(-event.delta / 120),
                    "units"
                )
            )
        )
        canvas.bind(
            "<Leave>",
            lambda _event: canvas.unbind_all("<MouseWheel>")
        )

        # -------------------------------------------------
        # TITLE
        # -------------------------------------------------

        ttk.Label(
            outer,
            text="POS File Distributor",
            font=("Segoe UI", 20, "bold"),
        ).pack(
            anchor="w"
        )

        ttk.Label(
            outer,
            text=(
                "PC → BO → Ubuntu POS Counters | "
                "Separate BO and Counter credentials"
            ),
        ).pack(
            anchor="w",
            pady=(0, 10)
        )

        # -------------------------------------------------
        # BO SECTION
        # -------------------------------------------------

        bo = ttk.LabelFrame(
            outer,
            text="1. BO Server Connection",
            padding=10
        )

        bo.pack(
            fill="x",
            pady=(0, 8)
        )

        self._entry(
            bo,
            "BO IP",
            self.bo_ip,
            0,
            0
        )

        self._entry(
            bo,
            "BO Username",
            self.bo_user,
            0,
            2
        )

        self._entry(
            bo,
            "BO Password",
            self.bo_password,
            0,
            4,
            "*"
        )

        self._entry(
            bo,
            "BO SSH Port",
            self.bo_port,
            0,
            6
        )

        self._entry(
            bo,
            "BO Connect Timeout (sec)",
            self.bo_connect_timeout,
            1,
            0
        )

        self._entry(
            bo,
            "BO Operation Timeout (sec)",
            self.bo_operation_timeout,
            1,
            2
        )

        # -------------------------------------------------
        # COUNTER SSH SECTION
        # -------------------------------------------------

        counter = ttk.LabelFrame(
            outer,
            text=(
                "2. Counter SSH Connection — "
                "different credentials from BO"
            ),
            padding=10
        )

        counter.pack(
            fill="x",
            pady=(0, 8)
        )

        self._entry(
            counter,
            "Counter Username",
            self.counter_user,
            0,
            0
        )

        self._entry(
            counter,
            "Counter Password",
            self.counter_password,
            0,
            2,
            "*"
        )

        self._entry(
            counter,
            "Counter SSH Port",
            self.counter_port,
            0,
            4
        )

        self._entry(
            counter,
            "Counter Connect Timeout (sec)",
            self.counter_connect_timeout,
            1,
            0
        )

        self._entry(
            counter,
            "Counter Operation Timeout (sec)",
            self.counter_operation_timeout,
            1,
            2
        )

        # -------------------------------------------------
        # COUNTER SELECTION
        # -------------------------------------------------

        selection = ttk.Frame(
            outer
        )

        selection.pack(
            fill="x",
            pady=(0, 8)
        )

        # -------------------------------------------------
        # AUTOMATIC GENERATION
        # -------------------------------------------------

        automatic = ttk.LabelFrame(
            selection,
            text="3A. Automatic Counter IP Generation",
            padding=10
        )

        automatic.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 5)
        )

        self._entry(
            automatic,
            "Number of Counters",
            self.counter_count,
            0,
            0
        )

        ttk.Label(
            automatic,
            text=(
                "Counter IPs are generated automatically "
                "starting from .112.\n"
                "Example: BO 10.25.36.12 + 4 counters\n"
                "→ 10.25.36.112, .113, .114, .115"
            ),
        ).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0)
        )

        # -------------------------------------------------
        # CUSTOM IP
        # -------------------------------------------------

        custom = ttk.LabelFrame(
            selection,
            text="3B. Custom-Only / Custom Overrides",
            padding=10
        )

        custom.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(5, 0)
        )

        ttk.Label(
            custom,
            text=(
                "Custom-only mode: leave Number of Counters empty.\n"
                "Otherwise these override generated counters.\n"
                "Format: counter_number=IP"
            ),
        ).pack(
            anchor="w"
        )

        self.custom_text = tk.Text(
            custom,
            height=5,
            width=48
        )

        self.custom_text.pack(
            fill="both",
            expand=True,
            pady=(5, 0)
        )

        # -------------------------------------------------
        # DISTRIBUTION INPUTS
        # -------------------------------------------------

        files = ttk.LabelFrame(
            outer,
            text="4. Distribution",
            padding=10
        )

        files.pack(
            fill="x",
            pady=(0, 8)
        )

        ttk.Label(
            files,
            text="Update type"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            padx=5,
            pady=5
        )

        self.mode_combo = ttk.Combobox(
            files,
            textvariable=self.transfer_mode,
            values=("File", "Folder Update"),
            state="readonly",
            width=20
        )
        self.mode_combo.grid(
            row=0,
            column=1,
            sticky="w",
            padx=5,
            pady=5
        )
        self.mode_combo.bind(
            "<<ComboboxSelected>>",
            self._on_mode_change
        )

        ttk.Label(
            files,
            textvariable=self.local_path_label
        ).grid(
            row=1,
            column=0,
            sticky="w",
            padx=5,
            pady=5
        )

        ttk.Entry(
            files,
            textvariable=self.local_file
        ).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=5,
            pady=5
        )

        ttk.Button(
            files,
            text="Browse",
            command=self.browse
        ).grid(
            row=1,
            column=2,
            padx=5
        )

        ttk.Label(
            files,
            textvariable=self.target_path_label
        ).grid(
            row=2,
            column=0,
            sticky="w",
            padx=5,
            pady=5
        )

        ttk.Entry(
            files,
            textvariable=self.remote_path
        ).grid(
            row=2,
            column=1,
            sticky="ew",
            padx=5,
            pady=5
        )

        ttk.Label(
            files,
            text=(
                "Folder Update merges only the selected local folder contents. "
                "Existing files are backed up before replacement; other target "
                "files and folders are preserved."
            ),
            wraplength=900,
            justify="left"
        ).grid(
            row=3,
            column=0,
            columnspan=3,
            sticky="w",
            padx=5,
            pady=(3, 0)
        )

        files.columnconfigure(
            1,
            weight=1
        )

        # -------------------------------------------------
        # ACTION BUTTONS
        # -------------------------------------------------

        actions = ttk.Frame(
            outer
        )

        actions.pack(
            fill="x",
            pady=(0, 8)
        )

        self.generate_btn = ttk.Button(
            actions,
            text="Generate Counter List",
            command=self.generate
        )

        self.generate_btn.pack(
            side="left",
            padx=(0, 5)
        )

        self.test_btn = ttk.Button(
            actions,
            text="Test BO Connection",
            command=self.test_bo
        )

        self.test_btn.pack(
            side="left",
            padx=5
        )

        self.transfer_btn = ttk.Button(
            actions,
            text="UPLOAD TO BO + DISTRIBUTE",
            command=self.start_transfer
        )

        self.transfer_btn.pack(
            side="left",
            padx=5
        )

        self.cancel_btn = ttk.Button(
            actions,
            text="Cancel",
            command=self.cancel,
            state="disabled"
        )

        self.cancel_btn.pack(
            side="left",
            padx=5
        )

        ttk.Label(
            actions,
            textvariable=self.status,
            font=("Segoe UI", 10, "bold")
        ).pack(
            side="right"
        )

        # -------------------------------------------------
        # PROGRESS
        # -------------------------------------------------

        ttk.Progressbar(
            outer,
            variable=self.progress,
            maximum=100
        ).pack(
            fill="x",
            pady=(0, 8)
        )

        # -------------------------------------------------
        # RESULTS TABLE
        # -------------------------------------------------

        table_frame = ttk.LabelFrame(
            outer,
            text="Transfer Results",
            padding=5
        )

        table_frame.pack(
            fill="both",
            expand=True
        )

        cols = (
            "counter",
            "ip",
            "status",
            "message",
            "backup"
        )

        self.tree = ttk.Treeview(
            table_frame,
            columns=cols,
            show="headings"
        )

        headers = {
            "counter": "Counter",
            "ip": "IP",
            "status": "Status",
            "message": "Details",
            "backup": "Backup",
        }

        widths = {
            "counter": 70,
            "ip": 160,
            "status": 130,
            "message": 800,
            "backup": 330,
        }

        for c in cols:
            self.tree.heading(
                c,
                text=headers[c]
            )

            self.tree.column(
                c,
                width=widths[c],
                anchor="w"
            )

        sy = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview
        )

        sx = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.tree.xview
        )

        self.tree.configure(
            yscrollcommand=sy.set,
            xscrollcommand=sx.set
        )

        self.tree.pack(
            fill="both",
            expand=True
        )

        sy.pack(
            side="right",
            fill="y"
        )

        sx.pack(
            side="bottom",
            fill="x"
        )

        self.tree.tag_configure(
            "success",
            foreground="dark green"
        )

        self.tree.tag_configure(
            "off",
            foreground="red"
        )

        self.tree.tag_configure(
            "failed",
            foreground="red"
        )

        self.tree.tag_configure(
            "working",
            foreground="dark orange"
        )

    # =====================================================
    # ENTRY HELPER
    # =====================================================

    @staticmethod
    def _entry(
        parent,
        label,
        variable,
        row,
        col,
        show=None
    ):
        ttk.Label(
            parent,
            text=label
        ).grid(
            row=row,
            column=col,
            sticky="w",
            padx=4,
            pady=4
        )

        ttk.Entry(
            parent,
            textvariable=variable,
            show=show or "",
            width=22
        ).grid(
            row=row,
            column=col + 1,
            sticky="ew",
            padx=4,
            pady=4
        )

        parent.columnconfigure(
            col + 1,
            weight=1
        )

    # =====================================================
    # LOAD CONFIG
    # =====================================================

    def _load(self):
        bo = self.config["bo"]
        counter = self.config["counter"]
        ui = self.config.get("ui", {})

        self.bo_user.set(
            bo.get("username", "")
        )

        self.bo_password.set(
            bo.get("password", "")
        )

        self.bo_port.set(
            str(bo.get("port", 22))
        )

        self.bo_connect_timeout.set(
            str(bo.get("connect_timeout", 5))
        )

        self.bo_operation_timeout.set(
            str(bo.get("operation_timeout", 60))
        )

        self.counter_user.set(
            counter.get("username", "")
        )

        self.counter_password.set(
            counter.get("password", "")
        )

        self.counter_port.set(
            str(counter.get("port", 22))
        )

        self.counter_connect_timeout.set(
            str(counter.get("connect_timeout", 5))
        )

        self.counter_operation_timeout.set(
            str(counter.get("operation_timeout", 60))
        )

        mode = ui.get("transfer_mode", "File")
        if mode not in ("File", "Folder Update"):
            mode = "File"
        self.transfer_mode.set(mode)
        self._on_mode_change()

    # =====================================================
    # SAVE CONFIG
    # =====================================================

    def _save(self):
        self.config["bo"] = {
            "username": self.bo_user.get(),
            "password": self.bo_password.get(),
            "port": int(self.bo_port.get()),
            "connect_timeout": float(
                self.bo_connect_timeout.get()
            ),
            "operation_timeout": float(
                self.bo_operation_timeout.get()
            ),
        }

        self.config["counter"] = {
            "username": self.counter_user.get(),
            "password": self.counter_password.get(),
            "port": int(self.counter_port.get()),
            "connect_timeout": float(
                self.counter_connect_timeout.get()
            ),
            "operation_timeout": float(
                self.counter_operation_timeout.get()
            ),
        }

        self.config.setdefault("ui", {})["transfer_mode"] = self.transfer_mode.get()

        self.config_manager.save(
            self.config
        )

    # =====================================================
    # FILE BROWSER
    # =====================================================

    def _on_mode_change(self, _event=None):
        if self.transfer_mode.get() == "Folder Update":
            self.local_path_label.set("Local update folder")
            self.target_path_label.set("Target folder on counters")
        else:
            self.local_path_label.set("Local replacement file")
            self.target_path_label.set("Target file path on counters")

    def browse(self):
        if self.transfer_mode.get() == "Folder Update":
            selected = filedialog.askdirectory(
                title="Select folder containing the update"
            )
        else:
            selected = filedialog.askopenfilename(
                title="Select replacement file"
            )

        if selected:
            self.local_file.set(selected)

    # =====================================================
    # GENERATE COUNTER LIST
    # =====================================================

    def generate(self):
        try:
            custom = self.custom_text.get(
                "1.0",
                "end"
            ).strip()

            count_text = self.counter_count.get().strip()

            # -------------------------------------------------
            # CUSTOM-ONLY MODE
            # -------------------------------------------------

            if not count_text and custom:

                counters = self._custom_only(
                    custom
                )

                if not counters:
                    raise ValueError(
                        "No counters were provided."
                    )

                mode = "custom-only"

            # -------------------------------------------------
            # AUTOMATIC + CUSTOM OVERRIDE MODE
            # -------------------------------------------------

            else:

                if not count_text:
                    raise ValueError(
                        "Enter Number of Counters, "
                        "or leave it empty and provide "
                        "Custom Counter IPs."
                    )

                try:
                    count = int(
                        count_text
                    )
                except ValueError:
                    raise ValueError(
                        "Number of Counters must be "
                        "a valid number."
                    )

                if count <= 0:
                    raise ValueError(
                        "Number of Counters must be "
                        "greater than 0."
                    )

                bo_ip = self.bo_ip.get().strip()

                if not bo_ip:
                    raise ValueError(
                        "Enter the BO IP before "
                        "generating counters."
                    )

                # -------------------------------------------------
                # FIXED START OCTET
                #
                # The first counter is ALWAYS .112.
                #
                # Counter 1 -> .112
                # Counter 2 -> .113
                # Counter 3 -> .114
                # ...
                # -------------------------------------------------

                START_OCTET = 112

                counters = generate_counters(
                    bo_ip,
                    count,
                    START_OCTET
                )

                # -------------------------------------------------
                # CUSTOM IPs OVERRIDE GENERATED IPs
                # -------------------------------------------------

                if custom:
                    counters = apply_custom_ips(
                        counters,
                        custom
                    )

                mode = "automatic/override"

            # -------------------------------------------------
            # VALIDATE
            # -------------------------------------------------

            if not counters:
                raise ValueError(
                    "No counters were generated."
                )

            self.counters = counters

            # -------------------------------------------------
            # CLEAR TABLE
            # -------------------------------------------------

            for item in self.tree.get_children():
                self.tree.delete(item)

            # -------------------------------------------------
            # ADD COUNTERS TO TABLE
            # -------------------------------------------------

            for c in self.counters:

                self.tree.insert(
                    "",
                    "end",
                    iid=str(c.number),
                    values=(
                        c.number,
                        c.ip,
                        "Not Tested",
                        "",
                        "",
                    )
                )

            self.status.set(
                f"{len(counters)} counters generated "
                f"({mode})"
            )

        except Exception as exc:

            messagebox.showerror(
                "Counter Configuration",
                str(exc)
            )

    # =====================================================
    # CUSTOM IP PARSER
    # =====================================================

    @staticmethod
    def _custom_only(text):
        from .counter_manager import Counter
        import ipaddress

        counters = []

        for raw in text.splitlines():

            line = raw.strip()

            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                raise ValueError(
                    f"Invalid custom line: {line}\n"
                    "Use: 1=10.25.36.112"
                )

            number_text, ip_text = line.split(
                "=",
                1
            )

            try:
                number = int(
                    number_text.strip()
                )

            except ValueError:

                raise ValueError(
                    f"Invalid counter number: "
                    f"{number_text.strip()}"
                )

            if number <= 0:
                raise ValueError(
                    "Counter number must be "
                    "greater than 0."
                )

            ip_text = ip_text.strip()

            try:
                ip = ipaddress.ip_address(
                    ip_text
                )

            except ValueError:

                raise ValueError(
                    f"Invalid IP: {ip_text}"
                )

            if ip.version != 4:
                raise ValueError(
                    "Only IPv4 is supported."
                )

            counters.append(
                Counter(
                    number,
                    ip_text,
                    "custom"
                )
            )

        # -------------------------------------------------
        # DUPLICATE COUNTER CHECK
        # -------------------------------------------------

        numbers = [
            c.number
            for c in counters
        ]

        if len(numbers) != len(set(numbers)):

            raise ValueError(
                "Duplicate counter number "
                "in custom IP list."
            )

        return sorted(
            counters,
            key=lambda c: c.number
        )

    # =====================================================
    # BO CONTROLLER
    # =====================================================

    def _controller(self):
        return BOController(
            bo_ip=self.bo_ip.get().strip(),

            bo_username=self.bo_user.get(),

            bo_password=self.bo_password.get(),

            bo_port=int(
                self.bo_port.get()
            ),

            connect_timeout=float(
                self.bo_connect_timeout.get()
            ),

            operation_timeout=float(
                self.bo_operation_timeout.get()
            ),

            logger=self.logger,
        )

    # =====================================================
    # TEST BO
    # =====================================================

    def test_bo(self):
        try:

            if not self.bo_ip.get().strip():
                raise ValueError(
                    "Enter the BO IP."
                )

            if not self.bo_user.get():
                raise ValueError(
                    "Enter the BO username."
                )

            if not self.bo_password.get():
                raise ValueError(
                    "Enter the BO password."
                )

            int(
                self.bo_port.get()
            )

            float(
                self.bo_connect_timeout.get()
            )

            float(
                self.bo_operation_timeout.get()
            )

            ok, message = (
                self._controller().test_bo()
            )

            if ok:

                self.status.set(
                    "BO Connected"
                )

                messagebox.showinfo(
                    "BO Connection",
                    message
                )

            else:

                self.status.set(
                    "BO Connection Failed"
                )

                messagebox.showerror(
                    "BO Connection",
                    message
                )

        except Exception as exc:

            messagebox.showerror(
                "BO Connection",
                str(exc)
            )

    # =====================================================
    # VALIDATION
    # =====================================================

    def _validate(self):

        if not self.counters:
            self.generate()

        if not self.counters:
            raise ValueError(
                "Generate the counter list first."
            )

        if not self.bo_ip.get().strip():
            raise ValueError(
                "BO IP is required."
            )

        if not self.bo_user.get():
            raise ValueError(
                "BO username is required."
            )

        if not self.bo_password.get():
            raise ValueError(
                "BO password is required."
            )

        if not self.counter_user.get():
            raise ValueError(
                "Counter username is required."
            )

        if not self.counter_password.get():
            raise ValueError(
                "Counter password is required."
            )

        local = Path(
            self.local_file.get().strip()
        )

        if self.transfer_mode.get() == "Folder Update":
            if not local.is_dir():
                raise ValueError(
                    "Select a valid update folder."
                )
        else:
            if not local.is_file():
                raise ValueError(
                    "Select a valid replacement file."
                )

        target = self.remote_path.get().strip()

        if not target.startswith("/"):
            raise ValueError(
                "Counter target path must be "
                "an absolute Ubuntu/Linux path."
            )

        # -------------------------------------------------
        # PORT VALIDATION
        # -------------------------------------------------

        for value, name in (
            (
                self.bo_port.get(),
                "BO SSH port"
            ),
            (
                self.counter_port.get(),
                "Counter SSH port"
            ),
        ):

            port = int(value)

            if not 1 <= port <= 65535:
                raise ValueError(
                    f"{name} must be 1-65535."
                )

        # -------------------------------------------------
        # TIMEOUT VALIDATION
        # -------------------------------------------------

        for value, name in (
            (
                self.bo_connect_timeout.get(),
                "BO connect timeout"
            ),
            (
                self.bo_operation_timeout.get(),
                "BO operation timeout"
            ),
            (
                self.counter_connect_timeout.get(),
                "Counter connect timeout"
            ),
            (
                self.counter_operation_timeout.get(),
                "Counter operation timeout"
            ),
        ):

            if float(value) <= 0:
                raise ValueError(
                    f"{name} must be greater than 0."
                )

    # =====================================================
    # START TRANSFER
    # =====================================================

    def start_transfer(self):

        if self.busy:
            return

        try:

            self._validate()
            self._save()

        except Exception as exc:

            messagebox.showerror(
                "Validation",
                str(exc)
            )

            return

        answer = messagebox.askyesno(
            "Confirm Distribution",

            "The application will:\n\n"

            "1. Connect to the BO using BO credentials.\n"

            "2. Upload the selected update file/folder "
            "to a temporary BO folder.\n"

            "3. Ask the BO to connect to counters "
            "using COUNTER credentials.\n"

            "4. Distribute to counters in parallel "
            "over the store LAN.\n\n"

            f"Target: {self.remote_path.get()}\n"

            f"Counters: {len(self.counters)}\n\n"

            "Continue?",
        )

        if not answer:
            return

        self.busy = True

        self.cancel_event.clear()

        self.progress.set(
            0
        )

        self.status.set(
            "Connecting to BO..."
        )

        self.cancel_btn.configure(
            state="normal"
        )

        self.generate_btn.configure(
            state="disabled"
        )

        self.test_btn.configure(
            state="disabled"
        )

        self.transfer_btn.configure(
            state="disabled"
        )

        # -------------------------------------------------
        # MARK COUNTERS AS WAITING
        # -------------------------------------------------

        for c in self.counters:

            self._row(
                c.number,
                c.ip,
                "Waiting",
                "Waiting for BO distribution...",
                ""
            )

        threading.Thread(
            target=self._worker,
            daemon=True
        ).start()

    # =====================================================
    # WORKER
    # =====================================================

    def _worker(self):

        try:

            result = (
                self._controller().run_distribution(
                    local_file=self.local_file.get().strip(),

                    target_path=self.remote_path.get().strip(),

                    transfer_mode=(
                        "folder"
                        if self.transfer_mode.get() == "Folder Update"
                        else "file"
                    ),

                    counters=self.counters,

                    counter_username=self.counter_user.get(),

                    counter_password=self.counter_password.get(),

                    counter_port=int(
                        self.counter_port.get()
                    ),

                    counter_connect_timeout=float(
                        self.counter_connect_timeout.get()
                    ),

                    counter_operation_timeout=float(
                        self.counter_operation_timeout.get()
                    ),

                    cancel_event=self.cancel_event,
                )
            )

            self.root.after(
                0,
                self._finished,
                result
            )

        except Exception as exc:

            self.root.after(
                0,
                self._fatal,
                str(exc)
            )

    # =====================================================
    # FINISHED
    # =====================================================

    def _finished(self, result):

        success = 0
        off = 0
        failed = 0

        by_number = {
            str(item["counter"]): item
            for item in result.results
        }

        for counter in self.counters:

            item = by_number.get(
                str(counter.number)
            )

            if not item:

                self._row(
                    counter.number,
                    counter.ip,
                    "Failed",
                    "No result returned by BO",
                    ""
                )

                failed += 1

                continue

            status = item["status"]

            self._row(
                counter.number,
                item["ip"],
                status,
                item["message"],
                item["backup"]
            )

            if status == "SUCCESS":

                success += 1

            elif status == "Device Off":

                off += 1

            else:

                failed += 1

        self.progress.set(
            100
        )

        self.status.set(
            f"Complete | SUCCESS: {success} | "
            f"Device Off: {off} | "
            f"Failed: {failed}"
        )

        self._set_idle()

        if off or failed:

            messagebox.showwarning(
                "Distribution Completed with Issues",

                f"SUCCESS: {success}\n"
                f"Device Off: {off}\n"
                f"Failed: {failed}\n\n"
                "See the table and logs for details."
            )

        else:

            update_label = (
                "Folder update"
                if self.transfer_mode.get() == "Folder Update"
                else "File"
            )

            messagebox.showinfo(
                "Distribution Complete",

                f"{update_label} distributed successfully "
                f"to {success} counters."
            )

    # =====================================================
    # FATAL ERROR
    # =====================================================

    def _fatal(self, message):

        self.status.set(
            "Transfer Failed"
        )

        self._set_idle()

        messagebox.showerror(
            "Distribution Error",
            message
        )

    # =====================================================
    # UPDATE RESULT ROW
    # =====================================================

    def _row(
        self,
        number,
        ip,
        status,
        message,
        backup
    ):

        if not self.tree.exists(
            str(number)
        ):

            self.tree.insert(
                "",
                "end",
                iid=str(number),
                values=(
                    number,
                    ip,
                    status,
                    message,
                    backup
                )
            )

        else:

            self.tree.item(
                str(number),
                values=(
                    number,
                    ip,
                    status,
                    message,
                    backup
                )
            )

        tag = ""

        if status == "SUCCESS":

            tag = "success"

        elif status == "Device Off":

            tag = "off"

        elif status in (
            "Failed",
            "Connection Failed",
            "Permission Failed"
        ):

            tag = "failed"

        elif status in (
            "Waiting",
            "Uploading",
            "Transferring"
        ):

            tag = "working"

        self.tree.item(
            str(number),
            tags=(tag,) if tag else ()
        )

    # =====================================================
    # CANCEL
    # =====================================================

    def cancel(self):

        self.cancel_event.set()

        self.status.set(
            "Cancellation requested..."
        )

    # =====================================================
    # SET GUI IDLE
    # =====================================================

    def _set_idle(self):

        self.busy = False

        self.cancel_btn.configure(
            state="disabled"
        )

        self.generate_btn.configure(
            state="normal"
        )

        self.test_btn.configure(
            state="normal"
        )

        self.transfer_btn.configure(
            state="normal"
        )
