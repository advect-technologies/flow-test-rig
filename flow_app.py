# flow_app.py
import asyncio
import sys
from enum import StrEnum

# Windows asyncio fix — must be very early
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


from textual.app import App, ComposeResult, on
from textual.containers import Horizontal, Vertical, Center, Middle
from textual.widgets import (
    Header, Footer, Button, Input, Label, Static, DataTable, Select, RichLog,
    TabbedContent, TabPane, ProgressBar, TextArea
)
from textual.reactive import reactive
from textual.message import Message

from config_loader import load_test_rig_config
import models
from models import States as RigStates
import machine
from loguru import logger
from pathlib import Path

def format_duration(seconds: float) -> str:
    """Return human-friendly duration (s / min / h)"""
    if seconds < 300:           # < 5 minutes
        return f"{int(seconds)}s"
    elif seconds < 3600:        # < 1 hour
        minutes = int(seconds / 60)
        return f"{minutes}m"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"

# Shared globals
test_rig = None
test_rig_event_q: asyncio.Queue = asyncio.Queue()

class MetricsUpdated(Message):
    def __init__(self):
        super().__init__()

class FlowTestApp(App):

    CSS_PATH = "flow_app.tcss"
    metrics_table_data = reactive({}, layout=True, repaint=True)
    test_status_data = reactive({}, layout=True, repaint=True)
    current_state = reactive(RigStates.IDLE)   

    def _scan_protocols(self) -> list[tuple[str, Path]]:
        """Return (display_name, Path) pairs for Select"""
        protocols_dir = Path("protocols")
        if not protocols_dir.exists():
            protocols_dir.mkdir()

        files = sorted(protocols_dir.glob("*.json"), key=lambda p: p.name.lower())

        return [(f.name, f) for f in files]

    def compose(self) -> ComposeResult:
        yield Header()

        with TabbedContent(initial="live"):
            # LIVE TAB
            with TabPane("🔴 Live", id="live"):
                with Horizontal():
                    with Vertical(classes="panel"):
                        with Vertical(id='metrics-box'):
                            yield Static("Live Metrics", classes="h2")
                            yield DataTable(id="metrics-table")
                        with Vertical():
                            yield Static("📊 Test Progress", classes="h2")
                            self.progress_static = Static("🟡 No active test", 
                                    id="progress-display", 
                                    markup=True)
                            yield self.progress_static
                            with Center():
                                self.progress_bar = ProgressBar(id="test-progress-bar",total=100,show_eta=False)
                                yield self.progress_bar

                    with Vertical(classes="panel"):
                        yield Static("Recent Log", classes="h2")
                        yield RichLog(id="log", markup=True, wrap=True)

            # CONTROL TAB - Much flatter & explicit
            with TabPane("🎛️ Control", id="control"):
                with Vertical():    
                    
                    # === STATUS BANNER (reactive) ===
                    self.status_banner = Static("", id="status-banner", classes="banner")
                    yield self.status_banner                    
                    
                    with Horizontal(id="button-bar", classes="panel"):
                        self.start_btn = Button("▶️ START TEST", id="start_test", variant="success")
                        self.stop_btn = Button("🛑 STOP TEST", id="stop_test", variant="error", disabled=True)
                        yield self.start_btn
                        yield self.stop_btn
                        yield Button("⚠️ EXIT", id="quit",variant='warning')
                    
                    with Horizontal():
                        with Vertical(classes="panel", id="metadata-panel"):
                            yield Static("📖 Test Metadata", classes="h1")

                        # Metadata column
                            yield Label("Cylinder Height (cm):")
                            self.height = Input(placeholder="height", type='number',id='height')
                            yield self.height

                            yield Label("Cylinder Diameter (cm):")
                            self.diameter = Input(placeholder="diameter", type='number',id='diameter')
                            yield self.diameter

                            yield Label("Gas:")
                            self.gas = Select.from_values(["Air", "N2", "CO2", "Other"], value="Air", id="gas")
                            yield self.gas

                            yield Label("Notes:")
                            # self.notes = Input(placeholder="Flow characterization...", id="notes")
                            self.notes = TextArea(placeholder="Flow characterization...", id="notes")
                            yield self.notes

                            # Protocol Selector
                            yield Label("📈📉 Test Protocol:")
                            self.protocol_select = Select(
                                options=self._scan_protocols(),
                                value=self._scan_protocols()[0][1] if self._scan_protocols() else Select.NULL,
                                allow_blank=True,
                                id="protocol-select"
                            )
                            with Horizontal():
                                yield self.protocol_select
                            
                                yield Button("♻️ Refresh List", id="refresh_protocols", variant="primary")

                        # Control column
                        with Vertical(classes="panel", id="control-panel"):
                            with Horizontal(classes='box'):
                                yield Static("🎮 Live Control", classes="h2")
                                yield Label("Setpoint (SLPM):")
                                self.setpoint_input = Input(value="0.0", id="setpoint")
                                yield self.setpoint_input
                                yield Button("Send Setpoint", id="send_setpoint", variant="primary")

                            yield Button("Tare Scale", id="tare_scale", variant="primary")

        yield Footer()

    def on_mount(self) -> None:
        self.title = "Advect Flow Test Rig"
        self.sub_title = "Alicat + Scale DAQ"

        global test_rig
        cfg = load_test_rig_config()
        test_rig = machine.TestRig(cfg)
        
        self._init_metadata()

        # Table
        table = self.query_one("#metrics-table", DataTable)
        table.add_columns("Parameter", "Value", "Unit")
        table.zebra_stripes = True

        # Log
        log = self.query_one("#log", RichLog)
        logger.remove()
        logger.add(log.write, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
        
        self.update_status_banner()
        self.stop_flag = asyncio.Event()
        self.run_worker(self.run_flow_app, exclusive=True)
        self.run_worker(self._state_watcher())

    async def run_flow_app(self):
        await machine.flow_tasks(
            test_rig,
            self.stop_flag,
            test_rig_event_q,
            on_metrics_update=lambda: self.post_message(MetricsUpdated())
        )

    async def _state_watcher(self):
        """keep ui state in sync with machine state"""
        while not self.stop_flag.is_set():
            self.current_state = test_rig.state
            await asyncio.sleep(0.3)   # 300ms is responsive enough

    def watch_metrics_table_data(self, new_data: dict):
        table = self.query_one("#metrics-table", DataTable)
        table.clear(columns=False)
        if not new_data:
            table.add_row("Waiting...", "", "")
            return
        for param, info in sorted(new_data.items()):
            if isinstance(info, dict):
                value = info.get("value", info)
                unit = info.get("unit", "")
            else:
                value = info
                unit = ""
            val_str = f"{value:.4g}" if isinstance(value, (int, float)) else str(value)
            table.add_row(param, val_str, unit)

    def watch_test_status_data(self, status:dict):
        progress_msg = self.query_one('#progress-display')
        progress_bar = self.query_one(ProgressBar)

        if not status:
            progress_msg.update("🟡 No active test")
            # progress_bar.progress = 0
            return
    
        stage_info = f"Stage {status.get('current_stage', '?')}/{status.get('stage_total', '?')}"
        elapsed = status.get('run_time', 0)
        total = status.get('total_duration', 0) or 1
        percent = int((elapsed / total) * 100) if total > 0 else 0
        remaining = max(0.0, total - elapsed)

        lines=(f'🟢 [bold]{status.get('name', 'Test')}[/bold] — {stage_info}',   
                f'⏱️  {format_duration(elapsed)} / {format_duration(total)}   •   ⏳ {format_duration(remaining)} remaining')
        text = '\n'.join(lines)

        progress_msg.update(text)
        progress_bar.update(progress=percent)


    def watch_current_state(self):
        """Reactive watcher for test_rig.state"""
        self.update_status_banner()
        self._lock_metadata(test_rig.state != RigStates.IDLE)
        
        if self.current_state == RigStates.ACTIVE:
            self.start_btn.disabled = True
            self.stop_btn.disabled = False
        elif self.current_state == RigStates.IDLE:
            self.start_btn.disabled = False
            self.stop_btn.disabled = True

    def _get_status_text_and_class(self) -> tuple[str,str]:

        state = test_rig.state
        if state == RigStates.IDLE:
            return "🟢 IDLE — Ready to start test", "status-idle"
        elif state == RigStates.ACTIVE:
            return "🟡 ACTIVE — Test in progress", "status-running"
        elif state == RigStates.FAULT:
            return "🔴 FAULT — Check hardware / logs", "status-fault"
        else:
            return f"⚪ {state}", "status-idle"

    def update_status_banner(self):
        """Update banner text + styling"""
        text, css_class = self._get_status_text_and_class()
        
        try:
            banner = self.query_one("#status-banner", Static)
            banner.update(text)
            
            # Clear all state classes first, then apply the correct one
            for cls in ("status-idle", "status-running", "status-fault"):
                banner.set_class(False, cls)
            
            banner.set_class(True, css_class)
        except Exception as e:
            logger.warning(f"Banner update failed: {e}")

    def _send_metadata_update(self):
        """Send current UI values to backend"""
        meta = models.UserMetadata(
            height = float(self.height.value) if self.height.value != "" else None,
            diameter = float(self.diameter.value) if self.diameter.value != "" else None,
            gas=self.gas.value,
            notes=self.notes.text.strip(),
            )

        event = models.MetadataUpdateEvent(
            meta = meta
        )
        test_rig_event_q.put_nowait(event)
        logger.debug(f"Metadata update sent →")

    def _lock_metadata(self, locked: bool):
        setpoint_button = self.query_one('#send_setpoint')
        quit_button = self.query_one('#quit',Button)
        for w in (self.height, self.diameter, self.gas, self.notes, setpoint_button, quit_button):
            w.disabled = locked

    @on(Input.Changed, "#height")
    @on(Input.Changed, "#diameter")
    @on(TextArea.Changed, "#notes")
    @on(Select.Changed, "#gas")
    def on_metadata_field_changed(self, event):
        """Any metadata field change triggers update"""
        self._send_metadata_update()

    def _init_metadata(self):
        data = models.UserMetadata.load_user_data()
        self.query_one('#height').value = str(data.height)
        self.query_one('#diameter').value = str(data.diameter)
        self.query_one('#notes').text = data.notes
        self.query_one('#gas').value = data.gas        

    @on(MetricsUpdated)
    def handle_metrics_update(self):
        self.metrics_table_data = test_rig.fetch_flat_metrics()
        self.test_status_data = test_rig.fetch_flat_test_status()

    # Button handlers (same as before)
    @on(Button.Pressed, "#start_test")
    def on_start(self):
        if self.current_state == RigStates.IDLE:
            test_rig_event_q.put_nowait(models.StartButtonEvent())           
            logger.info("✅ Test start request sent")

    @on(Button.Pressed, "#stop_test")
    def on_stop(self):
        if self.current_state == RigStates.ACTIVE:
            test_rig_event_q.put_nowait(models.StopButtonEvent())
            logger.info("⏹️ Test stop request sent")

    @on(Button.Pressed, "#send_setpoint")
    def on_send_setpoint(self):
        try:
            sp = float(self.setpoint_input.value)
            test_rig_event_q.put_nowait(models.SetpointEvent(value=sp))
            logger.info(f"Setpoint → {sp} SLPM")
        except ValueError:
            logger.warning("Invalid setpoint")

    @on(Button.Pressed, "#tare_scale")
    def on_tare(self):
        test_rig_event_q.put_nowait(models.TareScaleEvent())
        logger.info("Tare sent")

    @on(Button.Pressed, "#quit")
    def on_quit(self):
        self.stop_flag.set()
        self.exit()

    @on(Select.Changed, "#protocol-select")
    def on_protocol_selected(self, event: Select.Changed):
        """User picked a protocol (or cleared it)"""
        
        if event.value is None or event.select.is_blank():
            logger.info("No protocol selected")
            return        
        
        if isinstance(event.value, Path):
            test_rig_event_q.put_nowait(
                models.ProtocolChangedEvent(file=event.value)
            )
            logger.success(f"Protocol selected: {event.value.name}")

    @on(Button.Pressed, "#refresh_protocols")
    def on_refresh_protocols(self):
        new_options = self._scan_protocols()
        self.protocol_select.set_options(new_options)
        
        # Re-select first one
        if new_options:
            self.protocol_select.value = new_options[0][1]
        else:
            self.protocol_select.clear()
        logger.info("Protocol list refreshed")

if __name__ == "__main__":
    FlowTestApp().run()