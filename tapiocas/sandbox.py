import io
import PySimpleGUI as sg
from PIL import Image, ImageDraw
import cv2
import numpy as np
import imutils
import logging
import collections
import threading
import time
import random
from enum import Enum, unique, auto

from adb_connector import AdbConnector
import config_manager
import log_manager


@unique
class Keys(Enum):
    BUTTON_RECORD = auto(),
    MULTILINE_RECORD = auto(),
    BUTTON_SCREENSHOT = auto(),
    BUTTON_SCREENSHOT_LIVE = auto(),
    IMAGE_MAIN = auto(),
    RADIO_GROUP_IMAGE_MAIN_CLICK = auto(),
    RADIO_BTN_IMAGE_MAIN_ZOOM = auto(),
    RADIO_BTN_IMAGE_MAIN_TAP = auto(),
    IMAGE_ZOOM = auto(),
    SLIDER_ZOOM = auto(),
    BUTTON_ZOOM_CLOSE = auto(),
    COLUMN_ZOOM = auto(),
    LABEL_STATUS = auto(),
    LABEL_COORD = auto(),


DISPLAY_MAX_SIZE = (300, 600)
SCREENSHOT_RAW = False
SCREENSHOT_PULL = False

ZOOM_LEVELS = [250, 112, 50, 22, 10]
RECORDING_COLOR = "tan1"
NO_RECORDING_COLOR = "lightsteelblue2"


class Model:
    window: sg.Window

    def __init__(self, device_screen_size):
        self.device_screen_size = device_screen_size
        self.__zc = (0, 0)
        self.__zr = 50
        self.zoom_mode = False
        self.recording = False
        self.recording_stopped = False
        self.live_screenshot = False
        start_color = (random.randint(25, 100), random.randint(25, 100), random.randint(25, 100))
        start_image = np.zeros((device_screen_size[1], device_screen_size[0], 3), np.uint8)
        start_image[:] = start_color
        self.screenshot_raw = start_image
        self.screenshot_thumbnail = get_image_thumbnail(self.screenshot_raw)
        self._screenshot_lock = threading.Lock()
        self._screenshot_raw_new = None

    # called from background thread
    def enqueue_new_screenshot(self, img):
        self._screenshot_lock.acquire()
        self._screenshot_raw_new = img
        self._screenshot_lock.release()

    # called from main thread
    def check_for_new_screenshot(self):
        refresh = False
        self._screenshot_lock.acquire()
        if self._screenshot_raw_new is not None:
            self.screenshot_raw, self._screenshot_raw_new = self._screenshot_raw_new, None
            self.screenshot_thumbnail = get_image_thumbnail(self.screenshot_raw)
            refresh = True
        self._screenshot_lock.release()
        return refresh

    @property
    def zoom_center(self):
        return self.__zc

    @zoom_center.setter
    def zoom_center(self, zc):
        self.__zc = zc
        self._zoom_center_check()

    @property
    def zoom_radius(self):
        return self.__zr

    @zoom_radius.setter
    def zoom_radius(self, zr):
        self.__zr = zr
        self._zoom_center_check()

    def _zoom_center_check(self):
        zx, zy = self.zoom_center
        zr = self.zoom_radius
        zx = min(max(zx, zr), self.device_screen_size[0]-zr-1)
        zy = min(max(zy, zr), self.device_screen_size[1]-zr-1)
        self.__zc = (zx, zy)


class BackgroundWorker:
    thread = None
    queue = collections.deque()
    exit = False

    def __init__(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.exit:
            if self.queue:
                (action, args) = self.queue.popleft()
                try:
                    action(*args)
                except Exception as e:
                    logging.error(e)

            else:
                time.sleep(.05)

    def enqueue(self, action, *args):
        self.queue.append((action, args))


def get_image_thumbnail(img):
    thumbnail = cv2.resize(img, DISPLAY_MAX_SIZE, interpolation=cv2.INTER_AREA)
    return thumbnail


def get_image_bytes(img):
    return cv2.imencode('.png', img)[1].tobytes()


def get_image_size(image_element: sg.Image):
    w = image_element.Widget
    return w.image.width(), w.image.height()


def get_pointer_position_on_image(image_element: sg.Image):
    w = image_element.Widget
    img_pos_x, img_pos_y = image_element.Position
    mouse_x, mouse_y = w.winfo_pointerxy()
    widget_width, widget_height = w.winfo_width(), w.winfo_height()
    image_width, image_height = w.image.width(), w.image.height()
    extra_width, extra_height = widget_width - image_width, widget_height - image_height
    padx, pady = extra_width - img_pos_x, extra_height - img_pos_y # not sure that's the right way but it works with current layout
    x, y = mouse_x - w.winfo_rootx() - padx, mouse_y - w.winfo_rooty() - pady
    return (x, y) if 0 <= x < image_width and 0 <= y < image_height else None


def get_coordinates_on_image(position_on_image, image_size, device_size):
    x, y = position_on_image
    x = x * device_size[0] // image_size[0]
    y = y * device_size[1] // image_size[1]
    return x, y


def capture_screenshot_action(model: Model, worker: BackgroundWorker, connector: AdbConnector):
    def action():
        try:
            model.window[Keys.LABEL_STATUS].update("Screenshot in progress...")
            new_screenshot = connector.get_screenshot_opencv(raw=SCREENSHOT_RAW, pull=SCREENSHOT_PULL)
            model.enqueue_new_screenshot(new_screenshot)
            model.window[Keys.LABEL_STATUS].update("")
            if model.live_screenshot:
                capture_screenshot_action(model, worker, connector)
        except Exception as e:
            model.window[Keys.LABEL_STATUS].update("Error")
            logging.error(e)
    worker.enqueue(action)


def send_tap_action(coords, model: Model, worker: BackgroundWorker, connector: AdbConnector):
    def action():
        try:
            model.window[Keys.LABEL_STATUS].update(f"Sending tap to {coords}")
            connector.tap(*coords, wait_ms=0)
            model.window[Keys.LABEL_STATUS].update("")
        except Exception as e:
            model.window[Keys.LABEL_STATUS].update("Error")
            logging.error(e)
    worker.enqueue(action)


def record_events_action(model: Model, worker: BackgroundWorker, connector: AdbConnector):
    def action():
        try:
            model.recording = True
            model.recording_stopped = False
            model.window[Keys.BUTTON_RECORD].update(text="Stop recording")
            model.window[Keys.MULTILINE_RECORD].update(background_color=RECORDING_COLOR)
            model.window[Keys.LABEL_STATUS].update(f"Recording events...")
            for t in connector.listen():
                model.window[Keys.MULTILINE_RECORD].print(t)
            model.window[Keys.LABEL_STATUS].update("")
        except Exception as e:
            # exception is expected when we normally abort the recording, because we close the connection
            if model.recording_stopped:
                model.window[Keys.LABEL_STATUS].update("")
            else:
                model.window[Keys.LABEL_STATUS].update("Error")
                logging.error(e)
        finally:
            model.recording = False
            model.window[Keys.BUTTON_RECORD].update(text="Record events")
            model.window[Keys.MULTILINE_RECORD].update(background_color=NO_RECORDING_COLOR)
    worker.enqueue(action)


def get_pointer_pos_in_device_coordinates(model: Model):
    pos = get_pointer_position_on_image(model.window[Keys.IMAGE_MAIN])
    if pos:
        return get_coordinates_on_image(pos, get_image_size(model.window[Keys.IMAGE_MAIN]), model.device_screen_size)
    else:
        pos = get_pointer_position_on_image(model.window[Keys.IMAGE_ZOOM])
        if pos:
            zoom_diameter = int(model.zoom_radius * 2)
            x, y = get_coordinates_on_image(pos, get_image_size(model.window[Keys.IMAGE_ZOOM]), (zoom_diameter, zoom_diameter))
            return x + model.zoom_center[0] - model.zoom_radius, y + model.zoom_center[1] - model.zoom_radius


def display_pointer_pos_in_device_coordinates(model: Model):
    pos = get_pointer_pos_in_device_coordinates(model)
    if pos:
        x, y = pos
        model.window[Keys.LABEL_COORD].update(value=f"x={int(x)}  y={int(y)}")
    else:
        model.window[Keys.LABEL_COORD].update(value="")


def update_main_image(model: Model):
    img = model.screenshot_thumbnail.copy()
    if model.zoom_mode:
        x, y = model.zoom_center
        zr = model.zoom_radius
        h, w, _ = img.shape
        ul = get_coordinates_on_image((x - zr, y - zr), model.device_screen_size, (w, h))
        br = get_coordinates_on_image((x + zr, y + zr), model.device_screen_size, (w, h))
        cv2.rectangle(img, ul, br, (50, 50, 240, 240))
    model.window[Keys.IMAGE_MAIN].update(data=get_image_bytes(img))


def update_zoom_image(model: Model):
    x, y = model.zoom_center
    zr = model.zoom_radius
    zoom = model.screenshot_raw[y - zr:y + zr, x - zr:x + zr]
    zoom = cv2.resize(zoom, (500, 500), interpolation=cv2.INTER_NEAREST)
    cv2.rectangle(zoom, (0, 0), (499, 499), (50, 50, 240, 240))
    model.window[Keys.IMAGE_ZOOM].update(data=get_image_bytes(zoom))


def layout_col_action_panel():
    b = sg.B("Record events", key=Keys.BUTTON_RECORD, size=(40, 1))
    t = sg.Multiline(size=(75, 30), key=Keys.MULTILINE_RECORD, autoscroll=True, background_color=NO_RECORDING_COLOR)
    col = sg.Column(layout=[[b], [t]])
    return col


def layout_col_main_image_menu():
    screenshot_btn = sg.B("Get Screenshot", key=Keys.BUTTON_SCREENSHOT, size=(30, 1))
    live_btn = sg.Checkbox("Live", key=Keys.BUTTON_SCREENSHOT_LIVE, enable_events=True, size=(10, 1))
    status_label_element = sg.T("", size=(40, 1), key=Keys.LABEL_STATUS)
    r1 = sg.Radio("Zoom on click", key=Keys.RADIO_BTN_IMAGE_MAIN_ZOOM, group_id=Keys.RADIO_GROUP_IMAGE_MAIN_CLICK, default=True)
    r2 = sg.Radio("Tap on click", key=Keys.RADIO_BTN_IMAGE_MAIN_TAP, group_id=Keys.RADIO_GROUP_IMAGE_MAIN_CLICK)
    col = sg.Column(layout=[[status_label_element], [r1, r2], [screenshot_btn, live_btn]])
    return col


def layout_col_main_image(model: Model):
    coord_label_element = sg.Text(size=(30, 1), justification='center', key=Keys.LABEL_COORD)
    main_image_element = sg.Image(data=get_image_bytes(model.screenshot_thumbnail),
                                        enable_events=True,
                                        key=Keys.IMAGE_MAIN)
    col = sg.Column(layout=[[layout_col_main_image_menu()], [main_image_element], [coord_label_element]],
                    element_justification='center')
    return col


def layout_col_zoom_image():
    zoom_image = np.zeros((500, 500, 3), np.uint8)
    zoom_image_element = sg.Image(data=get_image_bytes(zoom_image),
                                        enable_events=True,
                                        key=Keys.IMAGE_ZOOM)
    close_button = sg.B("Close", key=Keys.BUTTON_ZOOM_CLOSE)
    zoom_slider = sg.Slider(default_value=2, range=(0, len(ZOOM_LEVELS) - 1), disable_number_display=True,
                            orientation='h', resolution=1, enable_events=True, size=(50, 20), key=Keys.SLIDER_ZOOM)
    col = sg.Column(layout=[[sg.T(f"x{250 // ZOOM_LEVELS[0]}"), zoom_slider, sg.T(f"x{250 // ZOOM_LEVELS[-1]}")],
                            [zoom_image_element],
                            [close_button]],
                           key=Keys.COLUMN_ZOOM,
                           visible=False,
                           element_justification='center')
    return col


def main():
    worker = BackgroundWorker()

    logging.info(f"Connecting to device {config.phone_ip}")
    connector = AdbConnector(ip=config.phone_ip, adbkey_path=config.adbkey_path, output_dir=config.output_dir)
    width, height = connector.screen_width(), connector.screen_height()
    logging.info(f"Device screen size is {width} x {height}")
    model = Model((width, height))

    layout = [
        [layout_col_action_panel(), layout_col_main_image(model), layout_col_zoom_image()]
    ]
    window = sg.Window("Tapiocas' Sandbox", layout)
    model.window = window

    while True:
        event, values = window.read(timeout=100)
        new_screenshot = model.check_for_new_screenshot()
        if new_screenshot:
            update_main_image(model)
            if model.zoom_mode:
                update_zoom_image(model)

        if event == sg.TIMEOUT_KEY:
            display_pointer_pos_in_device_coordinates(model)
        elif event in (None, 'Exit'):
            break
        elif event in (Keys.IMAGE_MAIN, Keys.IMAGE_ZOOM):
            pos = get_pointer_pos_in_device_coordinates(model)
            if pos:
                if values[Keys.RADIO_BTN_IMAGE_MAIN_ZOOM]:
                    model.zoom_mode = True
                    model.zoom_center = pos
                    update_main_image(model)
                    update_zoom_image(model)
                    model.window[Keys.COLUMN_ZOOM].update(visible=True)
                elif values[Keys.RADIO_BTN_IMAGE_MAIN_TAP]:
                    send_tap_action(pos, model, worker, connector)
        elif event == Keys.BUTTON_SCREENSHOT:
            capture_screenshot_action(model, worker, connector)
        elif event == Keys.BUTTON_SCREENSHOT_LIVE:
            model.live_screenshot = values[Keys.BUTTON_SCREENSHOT_LIVE]
        elif event == Keys.BUTTON_ZOOM_CLOSE:
            model.zoom_mode = False
            model.window[Keys.COLUMN_ZOOM].update(visible=False)
            update_main_image(model)
        elif event == Keys.SLIDER_ZOOM:
            model.zoom_radius = ZOOM_LEVELS[int(values[Keys.SLIDER_ZOOM])]
            update_main_image(model)
            update_zoom_image(model)
        elif event == Keys.BUTTON_RECORD:
            if model.recording:
                # not sent to background worker because we want to abort the current action
                model.recording_stopped = True
                connector.abort_listening()
            else:
                record_events_action(model, worker, connector)

    window.close()


if __name__ == "__main__":
    config = config_manager.get_configuration()
    log_manager.initialize_log(config.log_dir, log_level=config.log_level)
    main()
