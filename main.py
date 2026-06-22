import json
import math
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import cv2
import mss
import numpy as np
import pyperclip
import pytesseract
from PIL import Image, ImageDraw, ImageFont, ImageTk

from audio_recorder import AudioRecorder, ffmpeg_available, mux_video_audio
from upload_service import load_config, upload_file
#.\venv\Scripts\python.exe main.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GALLERY_DIR = os.path.join(BASE_DIR, "gallery")
os.makedirs(GALLERY_DIR, exist_ok=True)

# --- Cấu hình Tesseract (Windows) ---
_cfg = load_config()
_tesseract_cmd = _cfg.get("tesseract_cmd")
if _tesseract_cmd and os.path.exists(_tesseract_cmd):
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd

# --- Biến toàn cục ---
image_thumbnails = []
video_thumbnails = []
update_preview = True

video_writer = None
recording_start_time = 0
recording_stopped = False
current_video_path = None
current_video_temp_path = None
audio_recorder = None
record_system_audio = None
record_mic_audio = None
sct = mss.MSS()
primary_monitor = sct.monitors[1]

root = None
preview_label = None
record_button = None
stop_button = None
status_var = None


def timestamp():
    return time.strftime("%Y%m%d%H%M%S")


def grab_screen(monitor=None):
    monitor = monitor or primary_monitor
    shot = sct.grab(monitor)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def grab_region(bbox):
    region = {
        "left": int(bbox[0]),
        "top": int(bbox[1]),
        "width": int(bbox[2] - bbox[0]),
        "height": int(bbox[3] - bbox[1]),
    }
    shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def run_ocr(image_path):
    try:
        text = pytesseract.image_to_string(
            Image.open(image_path), lang="vie+eng"
        ).strip()
        if text:
            pyperclip.copy(text)
        return text
    except pytesseract.TesseractNotFoundError:
        messagebox.showwarning(
            "OCR",
            "Không tìm thấy Tesseract OCR.\n"
            "Cài từ: https://github.com/UB-Mannheim/tesseract/wiki\n"
            "Sau đó cập nhật tesseract_cmd trong config.json",
        )
        return ""
    except Exception as exc:
        messagebox.showerror("OCR", f"Lỗi OCR: {exc}")
        return ""


def restore_main_window():
    global update_preview
    update_preview = True
    if root:
        root.deiconify()
        root.lift()
        root.focus_force()


def hide_main_window():
    global update_preview
    update_preview = False
    if root:
        root.withdraw()
        root.update()


def copy_to_clipboard(text):
    root.clipboard_clear()
    root.clipboard_append(text)
    pyperclip.copy(text)


def upload_and_share(file_path):
    def on_success(link):
        copy_to_clipboard(link)
        messagebox.showinfo(
            "Upload thành công",
            f"Link đã copy vào clipboard:\n{link}",
        )
        status_var.set("Upload xong — link đã copy")

    def on_error(message):
        messagebox.showerror("Upload thất bại", message)
        status_var.set("Upload thất bại")

    def worker():
        try:
            link = upload_file(file_path)
            root.after(0, lambda l=link: on_success(l))
        except Exception as exc:
            root.after(0, lambda msg=str(exc): on_error(msg))

    status_var.set("Đang upload...")
    threading.Thread(target=worker, daemon=True).start()


class RegionSelector(tk.Toplevel):
    """Chọn vùng màn hình kiểu Lightshot."""

    def __init__(self, parent, screenshot, callback):
        super().__init__(parent)
        self.callback = callback
        self.start_x = self.start_y = 0
        self.rect_id = None
        self.result = None

        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.configure(cursor="cross")

        self.photo = ImageTk.PhotoImage(screenshot)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", lambda e: self._cancel())

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=2
        )

    def _on_drag(self, event):
        self.canvas.coords(
            self.rect_id, self.start_x, self.start_y, event.x, event.y
        )

    def _on_release(self, event):
        x1, y1 = min(self.start_x, event.x), min(self.start_y, event.y)
        x2, y2 = max(self.start_x, event.x), max(self.start_y, event.y)
        if x2 - x1 < 5 or y2 - y1 < 5:
            self._cancel()
            return
        self.result = (x1, y1, x2, y2)
        self.destroy()
        self.callback(self.result)

    def _cancel(self):
        self.result = None
        self.destroy()
        self.callback(None)


class AnnotationEditor(tk.Toplevel):
    """Công cụ ghi chú: khung đỏ, mũi tên, chữ."""

    def __init__(self, parent, image, on_done, on_cancel=None):
        super().__init__(parent)
        self.title("Chỉnh sửa ảnh chụp")
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.attributes("-topmost", True)
        self.focus_force()
        self.base_image = image.copy()
        self.tool = tk.StringVar(value="rect")
        self.annotations = []
        self.start_x = self.start_y = 0
        self.temp_item = None

        toolbar = tk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=8)

        for text, value in (
            ("Khung đỏ", "rect"),
            ("Mũi tên", "arrow"),
            ("Chữ", "text"),
        ):
            tk.Radiobutton(
                toolbar, text=text, variable=self.tool, value=value
            ).pack(side=tk.LEFT, padx=4)

        tk.Button(toolbar, text="Hoàn tất", command=self._finish).pack(
            side=tk.RIGHT, padx=4
        )
        tk.Button(toolbar, text="Hủy", command=self._cancel).pack(
            side=tk.RIGHT, padx=4
        )
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        max_w, max_h = 1200, 700
        w, h = self.base_image.size
        scale = min(max_w / w, max_h / h, 1.0)
        self.scale = scale
        self.display_size = (int(w * scale), int(h * scale))

        self.canvas = tk.Canvas(
            self, width=self.display_size[0], height=self.display_size[1]
        )
        self.canvas.pack(padx=8, pady=8)

        self._refresh_canvas()
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _refresh_canvas(self):
        preview = self.base_image.copy()
        draw = ImageDraw.Draw(preview)
        self._render_annotations(draw, self.annotations)

        preview = preview.resize(self.display_size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        for ann in self.annotations:
            self._draw_annotation_on_canvas(ann)

    def _to_image_coords(self, x, y):
        return x / self.scale, y / self.scale

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.tool.get() == "text":
            text = simpledialog.askstring("Nhập chữ", "Nội dung:", parent=self)
            if text:
                ix, iy = self._to_image_coords(event.x, event.y)
                self.annotations.append(
                    {"type": "text", "pos": (ix, iy), "text": text}
                )
                self._refresh_canvas()
            return

        color = "red"
        if self.tool.get() == "rect":
            self.temp_item = self.canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline=color, width=2
            )
        else:
            self.temp_item = self.canvas.create_line(
                event.x, event.y, event.x, event.y, fill=color, width=2, arrow=tk.LAST
            )

    def _on_drag(self, event):
        if self.temp_item is None:
            return
        if self.tool.get() == "rect":
            self.canvas.coords(
                self.temp_item, self.start_x, self.start_y, event.x, event.y
            )
        else:
            self.canvas.coords(
                self.temp_item, self.start_x, self.start_y, event.x, event.y
            )

    def _on_release(self, event):
        if self.temp_item is None or self.tool.get() == "text":
            return

        x1, y1 = self._to_image_coords(self.start_x, self.start_y)
        x2, y2 = self._to_image_coords(event.x, event.y)

        if abs(x2 - x1) > 3 and abs(y2 - y1) > 3:
            self.annotations.append(
                {
                    "type": self.tool.get(),
                    "coords": (x1, y1, x2, y2),
                }
            )

        self.temp_item = None
        self._refresh_canvas()

    def _draw_annotation_on_canvas(self, ann):
        s = self.scale
        if ann["type"] == "rect":
            x1, y1, x2, y2 = ann["coords"]
            self.canvas.create_rectangle(
                x1 * s, y1 * s, x2 * s, y2 * s, outline="red", width=2
            )
        elif ann["type"] == "arrow":
            x1, y1, x2, y2 = ann["coords"]
            self.canvas.create_line(
                x1 * s, y1 * s, x2 * s, y2 * s, fill="red", width=2, arrow=tk.LAST
            )
        elif ann["type"] == "text":
            x, y = ann["pos"]
            self.canvas.create_text(
                x * s, y * s, text=ann["text"], fill="red", anchor=tk.NW, font=("Arial", 14)
            )

    def _render_annotations(self, draw, annotations):
        for ann in annotations:
            if ann["type"] == "rect":
                draw.rectangle(ann["coords"], outline="red", width=3)
            elif ann["type"] == "arrow":
                self._draw_arrow(draw, ann["coords"], "red", 3)
            elif ann["type"] == "text":
                try:
                    font = ImageFont.truetype("arial.ttf", 24)
                except OSError:
                    font = ImageFont.load_default()
                draw.text(ann["pos"], ann["text"], fill="red", font=font)

    @staticmethod
    def _draw_arrow(draw, coords, color, width):
        x1, y1, x2, y2 = coords
        draw.line((x1, y1, x2, y2), fill=color, width=width)
        angle = math.atan2(y2 - y1, x2 - x1)
        head = 16
        left = (
            x2 - head * math.cos(angle - math.pi / 6),
            y2 - head * math.sin(angle - math.pi / 6),
        )
        right = (
            x2 - head * math.cos(angle + math.pi / 6),
            y2 - head * math.sin(angle + math.pi / 6),
        )
        draw.polygon([(x2, y2), left, right], fill=color)

    def _render_final(self):
        result = self.base_image.copy()
        draw = ImageDraw.Draw(result)
        self._render_annotations(draw, self.annotations)
        return result

    def _cancel(self):
        if self.on_cancel:
            self.on_cancel()
        self.destroy()

    def _finish(self):
        final_image = self._render_final()
        self.destroy()
        self.on_done(final_image)


def save_captured_image(image):
    path = os.path.join(GALLERY_DIR, f"screenshot_{timestamp()}.jpg")
    image.save(path, "JPEG", quality=95)
    return path


def after_capture(image):
    def on_edited(final_image):
        restore_main_window()
        path = save_captured_image(final_image)
        show_preview_image(final_image)
        text = run_ocr(path)
        if text:
            messagebox.showinfo(
                "OCR",
                f"Đã copy văn bản vào clipboard ({len(text)} ký tự):\n\n{text[:500]}"
                + ("..." if len(text) > 500 else ""),
            )
        status_var.set(f"Đã lưu: {os.path.basename(path)}")

        if messagebox.askyesno("Upload", "Bạn có muốn upload ảnh lên đám mây?"):
            upload_and_share(path)

    def on_cancel():
        restore_main_window()
        status_var.set("Đã hủy chỉnh sửa ảnh")

    hide_main_window()
    AnnotationEditor(root, image, on_edited, on_cancel=on_cancel)


def capture_fullscreen():
    hide_main_window()
    time.sleep(0.25)
    image = grab_screen()
    after_capture(image)


def capture_region():
    hide_main_window()
    time.sleep(0.25)
    screenshot = grab_screen()

    def on_region_selected(bbox):
        if bbox is None:
            restore_main_window()
            status_var.set("Đã hủy chọn vùng")
            return

        time.sleep(0.1)
        image = grab_region(bbox)
        after_capture(image)

    RegionSelector(root, screenshot, on_region_selected)


def show_preview_image(image):
    preview = image.copy()
    preview.thumbnail((960, 540), Image.Resampling.LANCZOS)
    photo = ImageTk.PhotoImage(preview)
    preview_label.config(image=photo)
    preview_label.image = photo


def start_recording():
    global video_writer, recording_start_time, recording_stopped, update_preview
    global current_video_path, current_video_temp_path, audio_recorder

    if video_writer:
        return

    stamp = timestamp()
    current_video_path = os.path.join(GALLERY_DIR, f"screen_record_{stamp}.mp4")
    current_video_temp_path = os.path.join(
        GALLERY_DIR, f"screen_record_{stamp}_video.mp4"
    )

    width = primary_monitor["width"]
    height = primary_monitor["height"]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    video_writer = cv2.VideoWriter(
        current_video_temp_path, fourcc, 10.0, (width, height)
    )

    recording_start_time = time.time()
    recording_stopped = False
    update_preview = False
    record_button.config(state=tk.DISABLED)
    stop_button.config(state=tk.NORMAL)

    want_system = record_system_audio.get()
    want_mic = record_mic_audio.get()
    audio_recorder = None

    if want_system or want_mic:
        try:
            audio_recorder = AudioRecorder()
            audio_recorder.start(
                record_system=want_system, record_mic=want_mic
            )
            parts = []
            if want_system:
                parts.append("âm thanh HT")
            if want_mic:
                parts.append("micro")
            status_var.set(f"Đang ghi màn hình + {' + '.join(parts)}...")
        except Exception as exc:
            audio_recorder = None
            messagebox.showwarning(
                "Ghi âm",
                f"Không bật được ghi âm: {exc}\nChỉ ghi hình, không có tiếng.",
            )
            status_var.set("Đang ghi màn hình (không có âm thanh)...")
    else:
        status_var.set("Đang ghi màn hình (không có âm thanh)...")

    threading.Thread(target=record_screen_loop, daemon=True).start()


def stop_recording():
    global video_writer, recording_stopped, update_preview, audio_recorder

    if not video_writer:
        return

    recording_stopped = True
    video_writer.release()
    video_writer = None
    record_button.config(state=tk.NORMAL)
    stop_button.config(state=tk.DISABLED)
    update_preview = True

    temp_video = current_video_temp_path
    final_video = current_video_path
    recorder = audio_recorder
    audio_recorder = None

    status_var.set("Đang xử lý video...")

    def finalize():
        saved_path = final_video
        audio_note = ""

        try:
            if recorder and (record_system_audio.get() or record_mic_audio.get()):
                audio_path = final_video.replace(".mp4", "_audio.wav")
                wav = recorder.stop(audio_path)

                if wav and os.path.exists(wav) and ffmpeg_available():
                    mux_video_audio(temp_video, wav, final_video)
                    os.remove(temp_video)
                    os.remove(wav)
                    audio_note = " (có âm thanh)"
                elif wav and os.path.exists(wav):
                    os.rename(temp_video, final_video)
                    audio_note = " — có file .wav riêng (thiếu ffmpeg để ghép)"
                else:
                    os.rename(temp_video, final_video)
                    audio_note = " (không ghi được âm thanh)"
            else:
                os.rename(temp_video, final_video)
        except Exception as exc:
            if os.path.exists(temp_video):
                os.rename(temp_video, final_video)
            audio_note = f" — lỗi ghép âm thanh: {exc}"

        def done():
            status_var.set(
                f"Đã lưu video: {os.path.basename(saved_path)}{audio_note}"
            )
            if messagebox.askyesno(
                "Upload", "Bạn có muốn upload video lên đám mây?"
            ):
                upload_and_share(saved_path)

        root.after(0, done)

    threading.Thread(target=finalize, daemon=True).start()


def record_screen_loop():
    global recording_stopped

    while video_writer is not None and not recording_stopped:
        shot = sct.grab(primary_monitor)
        frame = np.array(shot)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        elapsed = int(time.time() - recording_start_time)
        cv2.putText(
            frame,
            f"Recording: {elapsed}s",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (0, 0, 255),
            2,
        )
        video_writer.write(frame)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((960, 540), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        root.after(0, lambda p=photo: _set_preview(p))

        time.sleep(0.1)

    root.after(10, update_screen_preview)


def _set_preview(photo):
    preview_label.config(image=photo)
    preview_label.image = photo


def update_screen_preview():
    if update_preview and video_writer is None:
        try:
            image = grab_screen()
            show_preview_image(image)
        except Exception:
            pass

    root.after(500, update_screen_preview)


def play_video(video_path):
    def close_player():
        nonlocal playing
        playing = False
        video_cap.release()
        video_player.destroy()
        global update_preview
        update_preview = True

    global update_preview
    update_preview = False
    playing = True

    video_player = tk.Toplevel(root)
    video_player.title("Phát video")
    video_cap = cv2.VideoCapture(video_path)
    video_label = tk.Label(video_player)
    video_label.pack()

    def next_frame():
        if not playing:
            return
        ret, frame = video_cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            photo = ImageTk.PhotoImage(img)
            video_label.config(image=photo)
            video_label.image = photo
            fps = video_cap.get(cv2.CAP_PROP_FPS) or 10
            video_player.after(int(1000 / fps), next_frame)
        else:
            close_player()

    video_player.protocol("WM_DELETE_WINDOW", close_player)
    next_frame()


def create_video_thumbnail(video_path):
    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None, None
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    thumb = Image.fromarray(frame).resize((100, 100))
    return ImageTk.PhotoImage(thumb), os.path.basename(video_path)


def open_gallery():
    global update_preview
    update_preview = False

    win = tk.Toplevel(root)
    win.title("Thư viện")
    win.geometry("400x600")

    def back():
        win.destroy()
        global update_preview
        update_preview = True

    tk.Button(win, text="← Quay lại", command=back).pack(pady=8)

    scroll_frame = tk.Frame(win)
    scroll_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    canvas = tk.Canvas(scroll_frame)
    scrollbar = ttk.Scrollbar(scroll_frame, orient=tk.VERTICAL, command=canvas.yview)
    inner = tk.Frame(canvas)
    inner.bind(
        "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    canvas.create_window((0, 0), window=inner, anchor=tk.NW)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    del image_thumbnails[:]
    del video_thumbnails[:]

    images = sorted(
        f for f in os.listdir(GALLERY_DIR) if f.lower().endswith((".jpg", ".png"))
    )
    videos = sorted(
        f for f in os.listdir(GALLERY_DIR) if f.lower().endswith(".mp4")
    )

    for name in images:
        path = os.path.join(GALLERY_DIR, name)
        thumb = Image.open(path).resize((100, 100))
        photo = ImageTk.PhotoImage(thumb)
        image_thumbnails.append(photo)

        frame = tk.Frame(inner, relief=tk.GROOVE, bd=1, padx=4, pady=4)
        frame.pack(fill=tk.X, pady=4)

        tk.Label(frame, image=photo).pack(side=tk.LEFT)

        btn_frame = tk.Frame(frame)
        btn_frame.pack(side=tk.LEFT, padx=8)

        tk.Label(btn_frame, text=name, wraplength=180, justify=tk.LEFT).pack(
            anchor=tk.W
        )

        tk.Button(
            btn_frame,
            text="Xem",
            command=lambda p=path: show_preview_image(Image.open(p)),
        ).pack(anchor=tk.W, pady=2)
        tk.Button(
            btn_frame,
            text="OCR",
            command=lambda p=path: messagebox.showinfo("OCR", run_ocr(p) or "(Không có văn bản)"),
        ).pack(anchor=tk.W, pady=2)
        tk.Button(
            btn_frame,
            text="Upload",
            command=lambda p=path: upload_and_share(p),
        ).pack(anchor=tk.W, pady=2)

    for name in videos:
        path = os.path.join(GALLERY_DIR, name)
        photo, vname = create_video_thumbnail(path)
        if not photo:
            continue
        video_thumbnails.append(photo)

        frame = tk.Frame(inner, relief=tk.GROOVE, bd=1, padx=4, pady=4)
        frame.pack(fill=tk.X, pady=4)

        tk.Label(frame, image=photo).pack(side=tk.LEFT)
        btn_frame = tk.Frame(frame)
        btn_frame.pack(side=tk.LEFT, padx=8)
        tk.Label(btn_frame, text=vname).pack(anchor=tk.W)
        tk.Button(
            btn_frame, text="Phát", command=lambda p=path: play_video(p)
        ).pack(anchor=tk.W, pady=2)
        tk.Button(
            btn_frame, text="Upload", command=lambda p=path: upload_and_share(p)
        ).pack(anchor=tk.W, pady=2)


def on_quit():
    global video_writer, recording_stopped, audio_recorder
    recording_stopped = True
    if audio_recorder:
        audio_recorder.recording = False
        audio_recorder = None
    if video_writer:
        video_writer.release()
        video_writer = None
    sct.close()
    root.destroy()


def main():
    global root, preview_label, record_button, stop_button, status_var
    global record_system_audio, record_mic_audio

    if not os.path.exists(os.path.join(BASE_DIR, "config.json")):
        example = os.path.join(BASE_DIR, "config.example.json")
        if os.path.exists(example):
            import shutil
            shutil.copy(example, os.path.join(BASE_DIR, "config.json"))

    root = tk.Tk()
    root.title("Chụp & Ghi màn hình")
    root.geometry("1000x700")

    toolbar = tk.Frame(root)
    toolbar.pack(fill=tk.X, padx=10, pady=10)

    tk.Button(
        toolbar, text="Chụp toàn màn hình", command=capture_fullscreen, width=18
    ).pack(side=tk.LEFT, padx=4)
    tk.Button(
        toolbar, text="Chụp vùng chọn", command=capture_region, width=14
    ).pack(side=tk.LEFT, padx=4)
    record_button = tk.Button(
        toolbar, text="Ghi màn hình", command=start_recording, width=12
    )
    record_button.pack(side=tk.LEFT, padx=4)
    stop_button = tk.Button(
        toolbar, text="Dừng ghi", command=stop_recording, width=10, state=tk.DISABLED
    )
    stop_button.pack(side=tk.LEFT, padx=4)
    tk.Button(toolbar, text="Thư viện", command=open_gallery, width=10).pack(
        side=tk.LEFT, padx=4
    )
    tk.Button(toolbar, text="Thoát", command=on_quit, width=8).pack(side=tk.LEFT, padx=4)

    audio_cfg = _cfg.get("audio", {})
    record_system_audio = tk.BooleanVar(
        value=audio_cfg.get("record_system", True)
    )
    record_mic_audio = tk.BooleanVar(value=audio_cfg.get("record_mic", True))

    audio_frame = tk.Frame(root)
    audio_frame.pack(fill=tk.X, padx=10)
    tk.Checkbutton(
        audio_frame,
        text="Ghi âm thanh hệ thống",
        variable=record_system_audio,
    ).pack(side=tk.LEFT, padx=4)
    tk.Checkbutton(
        audio_frame,
        text="Ghi micro",
        variable=record_mic_audio,
    ).pack(side=tk.LEFT, padx=4)

    preview_label = tk.Label(root, bg="#222")
    preview_label.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    status_var = tk.StringVar(value="Sẵn sàng — xem trước màn hình bên dưới")
    tk.Label(root, textvariable=status_var, anchor=tk.W).pack(
        fill=tk.X, padx=10, pady=(0, 10)
    )

    update_screen_preview()
    root.mainloop()


if __name__ == "__main__":
    main()
