"""ESP32 Power Relay - Android (Kivy) version, black & green theme.

Protocol (same as the original Tkinter/Windows app):
  command -> {"device_id": ID, "relay": N | [N,..], "state": "on"|"off"}
  status  -> {"device_id": ID, "query": "status"}
"""
import json
import os
import queue
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
from kivy.metrics import dp
from kivy.storage.jsonstore import JsonStore
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
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

# ------------------------------------------------------------- theme
BLACK = (0, 0, 0, 1)
PANEL = (0.03, 0.07, 0.04, 1)
GREEN = (0.10, 0.90, 0.40, 1)
GREEN_DIM = (0.05, 0.35, 0.17, 1)
GREEN_DARK = (0.04, 0.16, 0.08, 1)
GREY_TXT = (0.45, 0.58, 0.49, 1)
WHITE_TXT = (0.88, 0.97, 0.90, 1)
RED = (1.0, 0.32, 0.32, 1)

Window.clearcolor = BLACK
Window.softinput_mode = "below_target"


# ------------------------------------------------------------ helpers
def relay_numbers(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
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


def describe_tx(payload):
    if "query" in payload:
        return "Status requested"
    nums = relay_numbers(payload.get("relay"))
    state = str(payload.get("state", "")).upper()
    if len(nums) >= RELAY_COUNT:
        return f"All relays -> {state}"
    if len(nums) == 1:
        return f"Relay {nums[0]} -> {state}"
    return f"Relays {', '.join(map(str, nums))} -> {state}"


class GButton(Button):
    """Flat green-outlined button."""

    def __init__(self, solid=False, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", GREEN if solid else GREEN_DARK)
        kw.setdefault("color", BLACK if solid else GREEN)
        kw.setdefault("bold", True)
        super().__init__(**kw)


class Panel(BoxLayout):
    """Dark rounded panel with a green border."""

    def __init__(self, **kw):
        kw.setdefault("orientation", "vertical")
        kw.setdefault("padding", dp(12))
        kw.setdefault("spacing", dp(8))
        super().__init__(**kw)
        with self.canvas.before:
            Color(*PANEL)
            self._bg = RoundedRectangle(radius=[dp(10)])
            Color(*GREEN_DIM)
            self._ln = Line(rounded_rectangle=(0, 0, 1, 1, dp(10)), width=1.1)
        self.bind(pos=self._upd, size=self._upd)

    def _upd(self, *_):
        self._bg.pos, self._bg.size = self.pos, self.size
        self._ln.rounded_rectangle = (self.x, self.y, self.width, self.height, dp(10))


class DotsButton(ButtonBehavior, Widget):
    """Three vertical dots (menu button)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.bind(pos=self._draw, size=self._draw, state=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        r = dp(2.6)
        cx = self.center_x
        with self.canvas:
            Color(*(WHITE_TXT if self.state == "down" else GREEN))
            for dy in (-dp(9), 0, dp(9)):
                Ellipse(pos=(cx - r, self.center_y + dy - r), size=(2 * r, 2 * r))


class Field(BoxLayout):
    def __init__(self, label, password=False, numeric=False, **kw):
        super().__init__(orientation="vertical", size_hint_y=None, height=dp(66), **kw)
        self.add_widget(Label(text=label, size_hint_y=None, height=dp(20),
                              halign="left", font_size="12sp", color=GREY_TXT,
                              text_size=(dp(280), dp(20))))
        self.input = TextInput(
            multiline=False, password=password,
            input_filter="int" if numeric else None, write_tab=False,
            size_hint_y=None, height=dp(42),
            background_normal="", background_active="",
            background_color=GREEN_DARK, foreground_color=WHITE_TXT,
            cursor_color=GREEN, selection_color=(0.1, 0.9, 0.4, 0.35),
            padding=(dp(10), dp(11)))
        self.add_widget(self.input)

    @property
    def text(self):
        return self.input.text.strip()

    @text.setter
    def text(self, v):
        self.input.text = str(v)


# ---------------------------------------------------------------- app
class RelayMqttApp(App):
    title = "ESP32 Power Relay"

    # ------------------------------------------------------------ UI
    def build(self):
        self.client = None
        self.connected = False
        self.events = queue.Queue()
        self.relay_states = [False] * RELAY_COUNT
        self.log_lines = []
        self.store = JsonStore(os.path.join(self.user_data_dir, "settings.json"))
        self.settings = dict(DEFAULTS)
        try:
            if self.store.exists("mqtt"):
                self.settings.update({k: str(v) for k, v in self.store.get("mqtt").items()
                                      if k in DEFAULTS})
        except Exception:
            pass
        self.active_device_id = self.settings["device_id"]
        self.active_response_topic = self.settings["subscribe_topic"]

        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))

        # ---- header
        head = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        head.add_widget(Label(text="ESP32 Power Relay", bold=True, font_size="19sp",
                              color=GREEN, halign="left", valign="middle",
                              text_size=(dp(220), dp(48))))
        self.status_label = Label(text="Disconnected", color=GREY_TXT, bold=True,
                                  font_size="13sp", halign="right", valign="middle")
        self.status_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        head.add_widget(self.status_label)
        dots = DotsButton(size_hint_x=None, width=dp(40))
        dots.bind(on_release=lambda *_: self.open_mqtt_popup())
        head.add_widget(dots)
        root.add_widget(head)

        # ---- relay control
        relay_panel = Panel(size_hint_y=None)
        relay_panel.add_widget(Label(text="Relay Control", bold=True, color=GREEN,
                                     size_hint_y=None, height=dp(24), halign="left",
                                     text_size=(dp(300), dp(24))))
        self.relay_labels = []
        for n in range(1, RELAY_COUNT + 1):
            row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
            lbl = Label(text=f"Relay {n}: OFF", color=GREY_TXT, bold=True,
                        halign="left", valign="middle")
            lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
            on = GButton(text="ON", solid=True, size_hint_x=None, width=dp(78))
            off = GButton(text="OFF", size_hint_x=None, width=dp(78))
            on.bind(on_release=lambda _b, r=n: self.send_relay_command(r, "on"))
            off.bind(on_release=lambda _b, r=n: self.send_relay_command(r, "off"))
            row.add_widget(lbl)
            row.add_widget(on)
            row.add_widget(off)
            relay_panel.add_widget(row)
            self.relay_labels.append(lbl)

        allrow = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        b_on = GButton(text="All ON", solid=True)
        b_off = GButton(text="All OFF")
        b_st = GButton(text="Request Status")
        allrows = list(range(1, RELAY_COUNT + 1))
        b_on.bind(on_release=lambda *_: self.send_relay_command(allrows, "on"))
        b_off.bind(on_release=lambda *_: self.send_relay_command(allrows, "off"))
        b_st.bind(on_release=lambda *_: self.request_status())
        for b in (b_on, b_off, b_st):
            allrow.add_widget(b)
        relay_panel.add_widget(allrow)
        relay_panel.height = dp(24 + 48 * (RELAY_COUNT + 1) + 8 * (RELAY_COUNT + 1) + 24)
        root.add_widget(relay_panel)

        # ---- MQTT activity
        act = Panel()
        act.add_widget(Label(text="MQTT Activity", bold=True, color=GREEN,
                             size_hint_y=None, height=dp(24), halign="left",
                             text_size=(dp(300), dp(24))))
        self.last_label = Label(text="No messages received", size_hint_y=None,
                                height=dp(34), font_size="12sp", color=GREY_TXT,
                                halign="left", valign="middle", shorten=True,
                                shorten_from="right")
        self.last_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        act.add_widget(self.last_label)
        self.log_label = Label(text="", size_hint_y=None, halign="left", valign="top",
                               font_size="12sp", markup=True, color=WHITE_TXT)
        self.log_label.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        self.log_label.bind(texture_size=lambda w, s: setattr(w, "height", s[1] + dp(6)))
        self.log_scroll = ScrollView(bar_color=GREEN, bar_inactive_color=GREEN_DIM,
                                     bar_width=dp(4))
        self.log_scroll.add_widget(self.log_label)
        act.add_widget(self.log_scroll)
        root.add_widget(act)

        self._build_popup()
        Clock.schedule_interval(self._process_events, 0.1)
        return root

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

        form = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        for f in fields:
            form.add_widget(f)
        form.height = dp(70) * len(fields)
        sv = ScrollView(bar_color=GREEN, bar_width=dp(4))
        sv.add_widget(form)

        self.popup_status = Label(text="Disconnected", color=GREY_TXT, bold=True,
                                  size_hint_y=None, height=dp(24))

        self.connect_button = GButton(text="Connect", solid=True)
        self.connect_button.bind(on_release=lambda *_: self.toggle_connection())
        b_status = GButton(text="Request Status")
        b_status.bind(on_release=lambda *_: self.request_status())
        r1 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        r1.add_widget(self.connect_button)
        r1.add_widget(b_status)

        b_save = GButton(text="Save", solid=True)
        b_save.bind(on_release=lambda *_: self.save_settings())
        b_reset = GButton(text="Reset Default")
        b_reset.bind(on_release=lambda *_: self.reset_defaults())
        r2 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        r2.add_widget(b_save)
        r2.add_widget(b_reset)

        body = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        body.add_widget(sv)
        body.add_widget(self.popup_status)
        body.add_widget(r1)
        body.add_widget(r2)

        self.popup = Popup(
            title="MQTT Connection", content=body, size_hint=(0.94, 0.92),
            background="", background_color=(0.02, 0.05, 0.03, 1),
            title_color=GREEN, separator_color=GREEN, title_size="16sp",
            overlay_color=(0, 0, 0, 0.8))

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
        self.popup_status.color = GREY_TXT

    def alert(self, title, msg):
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(12))
        lbl = Label(text=msg, color=WHITE_TXT, halign="center", valign="middle")
        lbl.bind(size=lambda w, s: setattr(w, "text_size", s))
        box.add_widget(lbl)
        close = GButton(text="OK", solid=True, size_hint_y=None, height=dp(44))
        box.add_widget(close)
        pop = Popup(title=title, content=box, size_hint=(0.85, 0.35),
                    background="", background_color=(0.02, 0.05, 0.03, 1),
                    title_color=GREEN, separator_color=GREEN)
        close.bind(on_release=pop.dismiss)
        pop.open()

    # ------------------------------------------------------- log view
    def log(self, text, kind="info", raw=None):
        colors = {"tx": "e0f7e6", "rx": "39ff88", "info": "6fae85", "err": "ff5252"}
        tags = {"tx": "SENT", "rx": "GOT ", "info": "INFO", "err": "ERR "}
        t = datetime.now().strftime("%H:%M:%S")
        line = (f"[color=4f7a5f]{t}[/color] [b][color={colors[kind]}]{tags[kind]}[/color][/b] "
                f"[color={colors[kind]}]{escape_markup(text)}[/color]")
        if raw:
            line += f"\n          [color=4f7a5f]{escape_markup(raw)}[/color]"
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
            self.log(describe_tx(payload), "tx", raw=f"{topic}: {text}")

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

    def _handle_device_message(self, topic, payload):
        text = json.dumps(payload, separators=(",", ":"))
        self.last_label.text = f"Last response: {text}"
        summary = "Message received from device"

        if isinstance(payload, dict):
            mtype = str(payload.get("type") or payload.get("message") or "").lower()
            nums = []
            for key in ("relay", "relay_number", "relay_numbers", "relays"):
                v = payload.get(key)
                if isinstance(v, (int, str, list, tuple)):
                    nums = relay_numbers(v)
                    if nums:
                        break

            if mtype in ("relay_on", "relay_off"):
                for n in nums:
                    self._set_relay(n, mtype == "relay_on")
                word = "ON" if mtype == "relay_on" else "OFF"
                summary = f"Relay {', '.join(map(str, nums))} turned {word}" if nums \
                    else f"Relay turned {word}"
            elif mtype == "ack":
                ok = payload.get("success", True)
                state = str(payload.get("state", "")).lower()
                if ok and state in ("on", "off"):
                    for n in nums:
                        self._set_relay(n, state == "on")
                summary = ("Device confirmed command" if ok else "Device reported FAILURE") + \
                    (f": relay {', '.join(map(str, nums))} {state.upper()}" if nums and state else "")

            relays = payload.get("relays")
            if isinstance(relays, list) and relays and not isinstance(relays[0], dict):
                if mtype not in ("relay_on", "relay_off", "ack") or len(relays) == RELAY_COUNT:
                    for i, v in enumerate(relays[:RELAY_COUNT], start=1):
                        self._set_relay(i, is_on(v))
                    summary = "Device status: " + ", ".join(
                        f"R{i} {'ON' if s else 'OFF'}"
                        for i, s in enumerate(self.relay_states, start=1))
            elif isinstance(relays, dict):
                for k, v in relays.items():
                    self._set_relay(k, is_on(v))
                summary = "Device status: " + ", ".join(
                    f"R{i} {'ON' if s else 'OFF'}"
                    for i, s in enumerate(self.relay_states, start=1))

        self.log(summary, "rx", raw=f"{topic}: {text}")
        self._refresh_relay_labels()

    def _set_relay(self, number, on):
        try:
            number = int(number)
        except (TypeError, ValueError):
            return
        if 1 <= number <= RELAY_COUNT:
            self.relay_states[number - 1] = on

    def _refresh_relay_labels(self):
        for i, (lbl, on) in enumerate(zip(self.relay_labels, self.relay_states), start=1):
            lbl.text = f"Relay {i}: {'ON' if on else 'OFF'}"
            lbl.color = GREEN if on else GREY_TXT

    def _update_connection_ui(self, text):
        ok = text == "Connected"
        color = GREEN if ok else (RED if "failed" in text else GREY_TXT)
        for lbl in (self.status_label, self.popup_status):
            lbl.text = text
            lbl.color = color
        self.connect_button.text = "Disconnect" if ok else "Connect"

    def on_stop(self):
        self.disconnect()


if __name__ == "__main__":
    RelayMqttApp().run()
