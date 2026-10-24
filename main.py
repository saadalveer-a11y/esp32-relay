"""ESP32 Power Relay - Android (Kivy) version, Spotify-style dark/green theme.

Protocol (same as the original Tkinter/Windows app):
  command -> {"device_id": ID, "relay": N | [N,..], "state": "on"|"off"}
  status  -> {"device_id": ID, "query": "status"}
Device replies:
  {"type":"ack","success":true,"state":"on|off","relays":[affected relay numbers]}
  {"type":"status","relay_on":[..]|"none","relay_off":[..]|"none", ...}
"""
import json
import os
import queue
import re
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, RoundedRectangle
from kivy.metrics import dp
from kivy.storage.jsonstore import JsonStore
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import escape_markup

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install paho-mqtt") from exc

# ------------------------------------------------------------ defaults
DEFAULTS = {
    "host": "31.220.61.9",
    "port": "1883",
    "username": "upstart",
    "password": "upstartbd2024",
    "publish_topic": "esp_power/web",
    "subscribe_topic": "esp_power/device",
    "device_id": "260001",
}
RELAY_COUNT = 4

# ------------------------------------------------- theme (Spotify-like)
BG = (0.071, 0.071, 0.071, 1)          # #121212
CARD = (0.110, 0.110, 0.110, 1)        # #1C1C1C
SURFACE = (0.165, 0.165, 0.165, 1)     # #2A2A2A
GREEN = (0.114, 0.725, 0.329, 1)       # #1DB954
WHITE = (1, 1, 1, 1)
BLACK = (0, 0, 0, 1)
GREY = (0.702, 0.702, 0.702, 1)        # #B3B3B3
MUTED = (0.416, 0.416, 0.416, 1)       # #6A6A6A
RED = (1.0, 0.30, 0.37, 1)

HEX_GREEN, HEX_GREY, HEX_MUTED = "1DB954", "B3B3B3", "6A6A6A"
HEX_WHITE, HEX_RED = "FFFFFF", "FF4D5E"

Window.clearcolor = BG
Window.softinput_mode = "below_target"


# ------------------------------------------------------------ helpers
def parse_relays(value):
    """1 / '1,2' / [1,2] / 'all' / 'none' -> list of ints."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "none", "null"):
            return []
        if s == "all":
            return list(range(1, RELAY_COUNT + 1))
        value = [p for p in re.split(r"[,\s]+", s) if p]
    elif not isinstance(value, (list, tuple)):
        value = [value]
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def is_on(value):
    if isinstance(value, str):
        return value.strip().lower() in ("on", "1", "true")
    return bool(value)


class PillButton(ButtonBehavior, Label):
    """Rounded 'pill' button (Spotify style)."""

    def __init__(self, bg=SURFACE, fg=WHITE, **kw):
        kw.setdefault("bold", True)
        kw.setdefault("halign", "center")
        kw.setdefault("valign", "middle")
        super().__init__(**kw)
        self._bg = bg
        self.color = fg
        with self.canvas.before:
            self._col = Color(*bg)
            self._rect = RoundedRectangle(radius=[dp(24)])
        self.bind(pos=self._upd, size=self._upd, state=self._upd)
        self.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(8), s[1])))

    def set_style(self, bg, fg):
        self._bg = bg
        self.color = fg
        self._upd()

    def _upd(self, *_):
        self._rect.pos, self._rect.size = self.pos, self.size
        self._rect.radius = [self.height / 2]
        k = 0.8 if self.state == "down" else 1.0
        self._col.rgba = (self._bg[0] * k, self._bg[1] * k, self._bg[2] * k, 1)


class Panel(BoxLayout):
    """Dark rounded card."""

    def __init__(self, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("padding", dp(14))
        kw.setdefault("spacing", dp(8))
        super().__init__(**kw)
        with self.canvas.before:
            Color(*CARD)
            self._bg = RoundedRectangle(radius=[dp(14)])
        self.bind(pos=self._upd, size=self._upd)

    def _upd(self, *_):
        self._bg.pos, self._bg.size = self.pos, self.size


class DotsButton(ButtonBehavior, Widget):
    """Three vertical dots (menu button)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self._draw, size=self._draw, state=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        r = dp(2.6)
        with self.canvas:
            Color(*(GREEN if self.state == "down" else WHITE))
            for dy in (-dp(9), 0, dp(9)):
                Ellipse(pos=(self.center_x - r, self.center_y + dy - r), size=(2 * r, 2 * r))


class RelayName(ButtonBehavior, Label):
    """Tappable relay name + state (tap to rename)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(size=lambda w, s: setattr(w, "text_size", s))


def styled_input(**kw):
    return TextInput(
        multiline=False, write_tab=False,
        background_normal="", background_active="",
        background_color=SURFACE, foreground_color=WHITE,
        cursor_color=GREEN, selection_color=(0.114, 0.725, 0.329, 0.35),
        padding=(dp(12), dp(12)), **kw)


class Field(BoxLayout):
    def __init__(self, label, password=False, numeric=False, **kw):
        super().__init__(orientation="vertical", size_hint_y=None, height=dp(68), **kw)
        lab = Label(text=label, size_hint_y=None, height=dp(20), halign="left",
                    font_size="12sp", color=GREY)
        lab.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(lab)
        self.input = styled_input(password=password,
                                  input_filter="int" if numeric else None,
                                  size_hint_y=None, height=dp(44))
        self.add_widget(self.input)

    @property
    def text(self):
        return self.input.text.strip()

    @text.setter
    def text(self, v):
        self.input.text = str(v)


def dark_popup(title, content, size_hint):
    return Popup(title=title, content=content, size_hint=size_hint,
                 background="", background_color=CARD, title_color=WHITE,
                 title_size="16sp", separator_color=SURFACE,
                 overlay_color=(0, 0, 0, 0.75))


# ---------------------------------------------------------------- app
class RelayMqttApp(App):
    title = "ESP32 Power Relay"

    # ------------------------------------------------------------ UI
    def build(self):
        self.client = None
        self.connected = False
        self.events = queue.Queue()
        self.relay_states = [False] * RELAY_COUNT
        self.relay_names = [f"Relay {i}" for i in range(1, RELAY_COUNT + 1)]
        self.log_lines = []
        self.show_activity = True
        self.store = JsonStore(os.path.join(self.user_data_dir, "settings.json"))
        self.settings = dict(DEFAULTS)
        try:
            if self.store.exists("mqtt"):
                self.settings.update({k: str(v) for k, v in self.store.get("mqtt").items()
                                      if k in DEFAULTS})
            if self.store.exists("names"):
                saved = self.store.get("names").get("items", [])
                for i, n in enumerate(saved[:RELAY_COUNT]):
                    if str(n).strip():
                        self.relay_names[i] = str(n).strip()
            if self.store.exists("ui"):
                self.show_activity = bool(self.store.get("ui").get("show_activity", True))
        except Exception:
            pass
        self.active_device_id = self.settings["device_id"]
        self.active_response_topic = self.settings["subscribe_topic"]

        root = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(12))

        # ---- header
        head = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        title = Label(text="ESP32 Power Relay", bold=True, font_size="20sp", color=WHITE,
                      halign="left", valign="middle", shorten=True, shorten_from="right")
        title.bind(size=lambda w, s: setattr(w, "text_size", s))
        head.add_widget(title)
        self.status_label = Label(text="Disconnected", color=GREY, bold=True,
                                  font_size="13sp", halign="right", valign="middle",
                                  size_hint_x=None, width=dp(105))
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        head.add_widget(self.status_label)
        dots = DotsButton(size_hint_x=None, width=dp(36))
        dots.bind(on_release=lambda *_: self.open_mqtt_popup())
        head.add_widget(dots)
        root.add_widget(head)

        # ---- relay control
        relay_panel = Panel(size_hint_y=None, height=dp(372))
        rh = BoxLayout(size_hint_y=None, height=dp(26))
        t = Label(text="Relay Control", bold=True, color=WHITE, font_size="16sp",
                  halign="left", valign="middle")
        t.bind(size=lambda w, s: setattr(w, "text_size", s))
        hint = Label(text="tap name to rename", color=MUTED, font_size="11sp",
                     halign="right", valign="middle")
        hint.bind(size=lambda w, s: setattr(w, "text_size", s))
        rh.add_widget(t)
        rh.add_widget(hint)
        relay_panel.add_widget(rh)

        self.name_labels, self.on_buttons, self.off_buttons = [], [], []
        for n in range(1, RELAY_COUNT + 1):
            row = BoxLayout(size_hint_y=None, height=dp(54), spacing=dp(8))
            lbl = RelayName(markup=True, halign="left", valign="middle", color=WHITE)
            lbl.bind(on_release=lambda _w, i=n - 1: self.rename_relay(i))
            on = PillButton(text="ON", size_hint_x=None, width=dp(78))
            off = PillButton(text="OFF", size_hint_x=None, width=dp(78))
            on.bind(on_release=lambda _b, r=n: self.send_relay_command(r, "on"))
            off.bind(on_release=lambda _b, r=n: self.send_relay_command(r, "off"))
            row.add_widget(lbl)
            row.add_widget(on)
            row.add_widget(off)
            relay_panel.add_widget(row)
            self.name_labels.append(lbl)
            self.on_buttons.append(on)
            self.off_buttons.append(off)

        allrow = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self.b_all_on = PillButton(text="All ON")
        self.b_all_off = PillButton(text="All OFF")
        b_st = PillButton(text="Request Status", font_size="13sp")
        everyone = list(range(1, RELAY_COUNT + 1))
        self.b_all_on.bind(on_release=lambda *_: self.send_relay_command(everyone, "on"))
        self.b_all_off.bind(on_release=lambda *_: self.send_relay_command(everyone, "off"))
        b_st.bind(on_release=lambda *_: self.request_status())
        for b in (self.b_all_on, self.b_all_off, b_st):
            allrow.add_widget(b)
        relay_panel.add_widget(allrow)
        root.add_widget(relay_panel)

        # ---- MQTT activity (collapsible)
        self.act = Panel()
        ah = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(8))
        at = Label(text="MQTT Activity", bold=True, color=WHITE, font_size="16sp",
                   halign="left", valign="middle")
        at.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.toggle_btn = PillButton(text="Hide", size_hint_x=None, width=dp(74),
                                     font_size="13sp")
        self.toggle_btn.bind(on_release=lambda *_: self.toggle_activity())
        ah.add_widget(at)
        ah.add_widget(self.toggle_btn)
        self.act.add_widget(ah)

        self.last_label = Label(text="No messages received", size_hint_y=None,
                                height=dp(26), font_size="12sp", color=GREY,
                                halign="left", valign="middle", shorten=True,
                                shorten_from="right")
        self.last_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.act.add_widget(self.last_label)

        self.log_label = Label(text="", size_hint_y=None, halign="left", valign="top",
                               font_size="12sp", markup=True, color=WHITE)
        self.log_label.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        self.log_label.bind(texture_size=lambda w, s: setattr(w, "height", s[1] + dp(6)))
        self.log_scroll = ScrollView(bar_color=GREEN, bar_inactive_color=SURFACE,
                                     bar_width=dp(4))
        self.log_scroll.add_widget(self.log_label)
        self.act.add_widget(self.log_scroll)
        root.add_widget(self.act)

        self._build_popup()
        self._apply_activity_visibility()
        self._refresh_ui()
        Clock.schedule_interval(self._process_events, 0.1)
        return root

    # ------------------------------------------ activity show / hide
    def toggle_activity(self):
        self.show_activity = not self.show_activity
        self._apply_activity_visibility()
        try:
            self.store.put("ui", show_activity=self.show_activity)
        except Exception:
            pass

    def _apply_activity_visibility(self):
        if self.show_activity:
            self.act.size_hint_y = 1
            self.last_label.height = dp(26)
            self.last_label.opacity = 1
            self.log_scroll.size_hint_y = 1
            self.log_scroll.opacity = 1
            self.log_scroll.disabled = False
            self.toggle_btn.text = "Hide"
        else:
            self.act.size_hint_y = None
            self.act.height = dp(34 + 28)
            self.last_label.height = 0
            self.last_label.opacity = 0
            self.log_scroll.size_hint_y = None
            self.log_scroll.height = 0
            self.log_scroll.opacity = 0
            self.log_scroll.disabled = True
            self.toggle_btn.text = "Show"

    # ----------------------------------------------- relay renaming
    def rename_relay(self, idx):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(12))
        ti = styled_input(text=self.relay_names[idx], size_hint_y=None, height=dp(46))
        box.add_widget(ti)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        cancel = PillButton(text="Cancel")
        save = PillButton(text="Save", bg=GREEN, fg=BLACK)
        row.add_widget(cancel)
        row.add_widget(save)
        box.add_widget(row)
        box.add_widget(Widget())
        pop = dark_popup(f"Rename relay {idx + 1}", box, (0.88, 0.4))

        def do_save(*_):
            name = ti.text.strip() or f"Relay {idx + 1}"
            self.relay_names[idx] = name[:24]
            try:
                self.store.put("names", items=self.relay_names)
            except Exception:
                pass
            self._refresh_ui()
            pop.dismiss()

        save.bind(on_release=do_save)
        ti.bind(on_text_validate=do_save)
        cancel.bind(on_release=pop.dismiss)
        pop.open()
        Clock.schedule_once(lambda *_: setattr(ti, "focus", True), 0.2)

    # ------------------------------------------------- settings popup
    def _build_popup(self):
        self.f_host = Field("Broker")
        self.f_port = Field("Port", numeric=True)
        self.f_user = Field("Username")
        self.f_pass = Field("Password", password=True)
        self.f_pub = Field("Command topic")
        self.f_sub = Field("Response topic")
        self.f_dev = Field("Device ID")
        fields = (self.f_host, self.f_port, self.f_user, self.f_pass,
                  self.f_pub, self.f_sub, self.f_dev)
        self._load_fields(self.settings)

        form = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        for f in fields:
            form.add_widget(f)
        form.height = dp(74) * len(fields)
        sv = ScrollView(bar_color=GREEN, bar_width=dp(4))
        sv.add_widget(form)

        self.popup_status = Label(text="Disconnected", color=GREY, bold=True,
                                  size_hint_y=None, height=dp(26))

        self.connect_button = PillButton(text="Connect", bg=GREEN, fg=BLACK)
        self.connect_button.bind(on_release=lambda *_: self.toggle_connection())
        b_status = PillButton(text="Request Status", font_size="14sp")
        b_status.bind(on_release=lambda *_: self.request_status())
        r1 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        r1.add_widget(self.connect_button)
        r1.add_widget(b_status)

        b_save = PillButton(text="Save", bg=GREEN, fg=BLACK)
        b_save.bind(on_release=lambda *_: self.save_settings())
        b_reset = PillButton(text="Reset Default", font_size="14sp")
        b_reset.bind(on_release=lambda *_: self.reset_defaults())
        r2 = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        r2.add_widget(b_save)
        r2.add_widget(b_reset)

        body = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        body.add_widget(sv)
        body.add_widget(self.popup_status)
        body.add_widget(r1)
        body.add_widget(r2)
        self.popup = dark_popup("MQTT Connection", body, (0.94, 0.92))

    def open_mqtt_popup(self):
        self.popup.open()

    def _load_fields(self, d):
        self.f_host.text = d["host"]
        self.f_port.text = d["port"]
        self.f_user.text = d["username"]
        self.f_pass.text = d["password"]
        self.f_pub.text = d["publish_topic"]
        self.f_sub.text = d["subscribe_topic"]
        self.f_dev.text = d["device_id"]

    def _current_fields(self):
        return {
            "host": self.f_host.text, "port": self.f_port.text,
            "username": self.f_user.text, "password": self.f_pass.input.text,
            "publish_topic": self.f_pub.text, "subscribe_topic": self.f_sub.text,
            "device_id": self.f_dev.text,
        }

    def save_settings(self):
        self.settings = self._current_fields()
        try:
            self.store.put("mqtt", **self.settings)
            self.popup_status.text = "Settings saved"
            self.popup_status.color = GREEN
            self.log("Settings saved", "info")
        except Exception as exc:
            self.popup_status.text = f"Save failed: {exc}"
            self.popup_status.color = RED

    def reset_defaults(self):
        self._load_fields(DEFAULTS)
        self.popup_status.text = "Defaults loaded - press Save to keep them"
        self.popup_status.color = GREY

    def alert(self, title, msg):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(12))
        lbl = Label(text=msg, color=WHITE, halign="center", valign="middle")
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        box.add_widget(lbl)
        close = PillButton(text="OK", bg=GREEN, fg=BLACK, size_hint_y=None, height=dp(46))
        box.add_widget(close)
        pop = dark_popup(title, box, (0.86, 0.36))
        close.bind(on_release=pop.dismiss)
        pop.open()

    # ------------------------------------------------------- log view
    def log(self, text, kind="info", raw=None):
        colors = {"tx": HEX_WHITE, "rx": HEX_GREEN, "info": HEX_GREY, "err": HEX_RED}
        tags = {"tx": "SENT", "rx": "GOT ", "info": "INFO", "err": "ERR "}
        t = datetime.now().strftime("%H:%M:%S")
        line = (f"[color={HEX_MUTED}]{t}[/color]  [b][color={colors[kind]}]{tags[kind]}[/color][/b]  "
                f"[color={colors[kind]}]{escape_markup(text)}[/color]")
        if raw:
            line += f"\n[size=10sp][color={HEX_MUTED}]{escape_markup(raw)}[/color][/size]"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-150:]
        self.log_label.text = "\n".join(self.log_lines)
        Clock.schedule_once(lambda *_: setattr(self.log_scroll, "scroll_y", 0), 0.05)

    # ------------------------------------------------------ connection
    def toggle_connection(self):
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        cfg = self._current_fields()
        try:
            port = int(cfg["port"])
        except ValueError:
            self.alert("Invalid port", "MQTT port must be a number.")
            return
        if not cfg["host"] or not cfg["device_id"]:
            self.alert("Missing settings", "Broker and device ID are required.")
            return

        self.cfg = cfg
        self.active_device_id = cfg["device_id"]
        self.active_response_topic = cfg["subscribe_topic"]
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"PowerRelayGUI-{cfg['device_id']}",
                protocol=mqtt.MQTTv311,
            )
            if cfg["username"]:
                self.client.username_pw_set(cfg["username"], cfg["password"])
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.connect_async(cfg["host"], port, keepalive=60)
            self.client.loop_start()
            self._update_connection_ui("Connecting...")
            self.log(f"Connecting to {cfg['host']}:{port}", "info")
        except Exception as exc:
            self.alert("MQTT error", str(exc))

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass
        self.client = None
        self.connected = False
        self._update_connection_ui("Disconnected")

    # paho callbacks (paho thread -> only push to queue)
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = getattr(reason_code, "is_failure", None)
        if failed is None:
            failed = reason_code != 0
        if failed:
            self.events.put(("error", f"MQTT connection rejected: {reason_code}"))
            return
        client.subscribe(self.active_response_topic)
        self.events.put(("connected", None))

    def _on_disconnect(self, client, userdata, disconnect_flags=None,
                       reason_code=None, properties=None):
        self.events.put(("disconnected", str(reason_code)))

    def _on_message(self, client, userdata, message):
        try:
            data = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.events.put(("log", f"Invalid message on {message.topic}"))
            return
        incoming_id = ""
        if isinstance(data, dict):
            incoming_id = str(data.get("device_id", "")).strip()
        if incoming_id and incoming_id != self.active_device_id:
            return
        self.events.put(("message", (message.topic, data)))

    # --------------------------------------------------------- publish
    def names_of(self, nums):
        nums = [n for n in nums if 1 <= n <= RELAY_COUNT]
        if len(nums) >= RELAY_COUNT:
            return "All relays"
        return ", ".join(self.relay_names[n - 1] for n in nums) or "?"

    def describe_tx(self, payload):
        if "query" in payload:
            return "Status requested"
        return f"{self.names_of(parse_relays(payload.get('relay')))} -> " \
               f"{str(payload.get('state', '')).upper()}"

    def send_relay_command(self, relay, state):
        self._publish({"device_id": self.f_dev.text, "relay": relay, "state": state})

    def request_status(self):
        self._publish({"device_id": self.f_dev.text, "query": "status"})

    def _publish(self, payload):
        if not self.connected or self.client is None:
            self.alert("Not connected",
                       "Connect to MQTT first.\nTap the 3 dots (top right) > Connect.")
            return
        topic = (getattr(self, "cfg", None) or self._current_fields())["publish_topic"]
        text = json.dumps(payload, separators=(",", ":"))
        result = self.client.publish(topic, text)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            self.log(f"Publish failed with code {result.rc}", "err")
        else:
            self.log(self.describe_tx(payload), "tx", raw=f"{topic}: {text}")

    # ---------------------------------------------------- UI-thread side
    def _process_events(self, _dt):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "connected":
                    self.connected = True
                    self._update_connection_ui("Connected")
                    self.log(f"Connected; subscribed to {self.active_response_topic}", "info")
                    self.popup.dismiss()
                    self.request_status()
                elif kind == "disconnected":
                    self.connected = False
                    self._update_connection_ui("Disconnected")
                    self.log(f"Disconnected: {value}", "info")
                elif kind == "error":
                    self.connected = False
                    self._update_connection_ui("Connection failed")
                    self.log(value, "err")
                elif kind == "log":
                    self.log(value, "err")
                elif kind == "message":
                    self._handle_device_message(*value)
        except queue.Empty:
            pass

    def status_text(self):
        return ", ".join(f"{self.relay_names[i]} {'ON' if s else 'OFF'}"
                         for i, s in enumerate(self.relay_states))

    def _handle_device_message(self, topic, payload):
        text = json.dumps(payload, separators=(",", ":"))
        kind = "rx"
        summary = "Message received from device"

        if isinstance(payload, dict):
            mtype = str(payload.get("type") or payload.get("message") or "").lower()

            if mtype == "ack":
                ok = payload.get("success", True)
                state = str(payload.get("state", "")).lower()
                nums = parse_relays(payload.get("relays", payload.get("relay")))
                if ok and state in ("on", "off"):
                    for n in nums:
                        self._set_relay(n, state == "on")
                    summary = f"Confirmed: {self.names_of(nums)} -> {state.upper()}"
                elif not ok:
                    summary, kind = "Device reported FAILURE", "err"
                else:
                    summary = "Device confirmed command"

            elif mtype == "status" or "relay_on" in payload or "relay_off" in payload:
                on_list = parse_relays(payload.get("relay_on"))
                off_list = parse_relays(payload.get("relay_off"))
                for n in on_list:
                    self._set_relay(n, True)
                for n in off_list:
                    self._set_relay(n, False)
                relays = payload.get("relays")
                if isinstance(relays, list) and relays and all(isinstance(x, bool) for x in relays):
                    for i, v in enumerate(relays[:RELAY_COUNT], start=1):
                        self._set_relay(i, v)
                elif isinstance(relays, dict):
                    for k, v in relays.items():
                        self._set_relay(k, is_on(v))
                summary = "Status: " + self.status_text()

            elif mtype in ("relay_on", "relay_off"):
                nums = parse_relays(payload.get("relay", payload.get("relays")))
                for n in nums:
                    self._set_relay(n, mtype == "relay_on")
                summary = f"{self.names_of(nums)} turned {'ON' if mtype == 'relay_on' else 'OFF'}"

        self.last_label.text = f"Last: {summary}"
        self.log(summary, kind, raw=f"{topic}: {text}")
        self._refresh_ui()

    def _set_relay(self, number, on):
        try:
            number = int(number)
        except (TypeError, ValueError):
            return
        if 1 <= number <= RELAY_COUNT:
            self.relay_states[number - 1] = bool(on)

    def _refresh_ui(self):
        for i, on in enumerate(self.relay_states):
            name = escape_markup(self.relay_names[i])
            col, word = (HEX_GREEN, "ON") if on else (HEX_GREY, "OFF")
            self.name_labels[i].text = (f"[b]{name}[/b]\n"
                                        f"[size=12sp][color={col}]{word}[/color][/size]")
            # ON active -> green ; OFF active -> white ; inactive -> dark grey
            if on:
                self.on_buttons[i].set_style(GREEN, BLACK)
                self.off_buttons[i].set_style(SURFACE, WHITE)
            else:
                self.on_buttons[i].set_style(SURFACE, WHITE)
                self.off_buttons[i].set_style(WHITE, BLACK)
        all_on = all(self.relay_states)
        all_off = not any(self.relay_states)
        self.b_all_on.set_style(GREEN, BLACK) if all_on else self.b_all_on.set_style(SURFACE, WHITE)
        self.b_all_off.set_style(WHITE, BLACK) if all_off else self.b_all_off.set_style(SURFACE, WHITE)

    def _update_connection_ui(self, text):
        ok = text == "Connected"
        color = GREEN if ok else (RED if "failed" in text else GREY)
        for lbl in (self.status_label, self.popup_status):
            lbl.text = text
            lbl.color = color
        self.connect_button.text = "Disconnect" if ok else "Connect"
        if ok:
            self.connect_button.set_style(WHITE, BLACK)
        else:
            self.connect_button.set_style(GREEN, BLACK)

    def on_stop(self):
        self.disconnect()


if __name__ == "__main__":
    RelayMqttApp().run()
